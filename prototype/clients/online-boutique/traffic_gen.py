#!/usr/bin/env python3
"""
External Online Boutique traffic generator (runs on CLIENT_HOST).

Profiles live as JSON files in profiles/. Each profile defines:

  * A fleet of `count` worker instances sending requests at `rate_rps_per_client`.
  * A weighted mix of request types (single requests or multi-step workflows).
  * A SKU pool — `{sku}` placeholders in paths/bodies are substituted per pick.
  * A retry policy (max retries, backoff, retry-on-status list).

Each worker loops forever:
  1. Weighted-random pick one entry from `requests`.
  2. Generate a fresh session UUID (shared across a workflow's steps) and a
     fresh SKU from `sku_pool`.
  3. Execute the request (or workflow) with the profile's retry policy.
     Each attempt sends `X-Attempt-Number` (counter starts at 1, increments
     per retry), `X-Request-ID`, `X-Retry-Client-Type`, `X-Retry-Client-Worker`,
     and `Cookie: shop_session-id=<uuid>`.
  4. Sleep to hit the target RPS and repeat.

One CSV row is written per HTTP attempt. Schema:

  timestamp, profile, worker, request_id, request_type, method, path,
  attempt, is_retry, status, ok, latency_s

Legacy profiles that use the old `paths: [...]` + top-level `method` are
transparently adapted to the new schema on load.
"""

from __future__ import annotations

import argparse
import asyncio
import concurrent.futures
import http.client
import json
import random
import signal
import socket
import sys
import time
import urllib.parse
import uuid
from pathlib import Path
from typing import Any, Optional

# Concurrent HTTP calls are issued through asyncio.to_thread(), which uses the
# loop's default ThreadPoolExecutor. That defaults to min(32, cpu_count+4) —
# typically ~12 on a d430 — which caps real parallelism regardless of how many
# workers a profile requests. Bump it so `count: 50` actually means 50 parallel
# requests and not "queue 50 through 12 threads".
DEFAULT_HTTP_THREAD_POOL_SIZE = 256


# ---------------------------------------------------------------------------
# Persistent HTTP connection per worker
# ---------------------------------------------------------------------------
#
# urllib.request.urlopen does not reuse connections — every call does a fresh
# TCP handshake (+ optional TLS). Under load that adds hundreds of ms per
# attempt and saturates the kernel conntrack/tw_reuse tables. We fix this by
# holding one `http.client.HTTPConnection` per worker with HTTP keep-alive.
#
# This class is NOT thread-safe. Each worker owns its own instance; we rely on
# the fact that a single asyncio worker only ever has one outstanding
# to_thread() at a time, so the connection is never touched by two threads
# concurrently.
class KeepAliveSession:
    """One persistent HTTP/1.1 connection. Reconnects on error."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        self.host = host
        self.port = port
        self.timeout_s = timeout_s
        self._conn: Optional[http.client.HTTPConnection] = None

    def _open(self) -> None:
        self._conn = http.client.HTTPConnection(
            self.host, self.port, timeout=self.timeout_s
        )
        # Force TCP connect now (vs lazy on first request()) so latency
        # measurements don't include the connect cost on the hot path.
        self._conn.connect()

    def _close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def request(
        self,
        method: str,
        path: str,
        headers: dict[str, str],
        body: Optional[bytes] = None,
    ) -> tuple[bool, int, str]:
        """Returns (ok, status, err_label). Reconnects automatically on failure."""
        # Make sure Connection: keep-alive is sent (HTTP/1.1 default, but be explicit).
        headers = dict(headers)
        headers.setdefault("Connection", "keep-alive")

        # Try twice: once on the existing connection, once more after forced reconnect.
        last_err = ""
        for attempt_conn in range(2):
            if self._conn is None:
                try:
                    self._open()
                except (OSError, socket.timeout) as exc:
                    self._close()
                    last_err = exc.__class__.__name__
                    continue

            try:
                self._conn.request(method, path, body=body, headers=headers)
                resp = self._conn.getresponse()
                status = int(resp.status)
                # Drain the body so the connection can be reused.
                resp.read()
                ok = 200 <= status < 400
                return ok, status, ""
            except http.client.HTTPException as exc:
                last_err = exc.__class__.__name__
                self._close()
            except (OSError, socket.timeout) as exc:
                last_err = exc.__class__.__name__
                self._close()

        return False, 0, last_err or "connection_failed"

    def close(self) -> None:
        self._close()


# ---------------------------------------------------------------------------
# Profile loading + normalization
# ---------------------------------------------------------------------------

def normalize_profile(p: dict[str, Any]) -> dict[str, Any]:
    """Backfill defaults and convert legacy `paths:` profiles to the new schema."""
    p.setdefault("count", 1)
    p.setdefault("rate_rps_per_client", 1.0)
    p.setdefault("timeout_s", 2.0)
    p.setdefault("retries", 0)
    p.setdefault("backoff", {})
    p.setdefault("retry_on_status", [])
    p.setdefault("sku_pool", [])

    # Legacy compatibility: `paths: [...]` + top-level `method`.
    if "requests" not in p:
        method = str(p.get("method", "GET")).upper()
        paths = list(p.get("paths", ["/"])) or ["/"]
        p["requests"] = [
            {
                "name": (path.strip("/").replace("/", "-") or "root"),
                "method": method,
                "path": path,
                "weight": 1,
            }
            for path in paths
        ]

    # Normalize every request entry (top-level and nested workflow steps).
    for entry in p["requests"]:
        _normalize_request_entry(entry)

    return p


def _normalize_request_entry(r: dict[str, Any]) -> None:
    r.setdefault("weight", 1)
    r.setdefault("name", r.get("path", "unnamed") or "unnamed")
    if "workflow" in r:
        for step in r["workflow"]:
            _normalize_request_step(step)
    else:
        _normalize_request_step(r)


def _normalize_request_step(step: dict[str, Any]) -> None:
    step.setdefault("method", "GET")
    step["method"] = str(step["method"]).upper()
    step.setdefault("path", "/")
    step.setdefault("name", step.get("path", "unnamed"))
    if step["method"] in {"POST", "PUT", "PATCH"}:
        step.setdefault("body", "")
        step.setdefault("content_type", "application/x-www-form-urlencoded")


def load_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for path in sorted(profile_dir.glob("*.json")):
        data = json.loads(path.read_text())
        data["_path"] = str(path)
        profiles.append(normalize_profile(data))
    return profiles


# ---------------------------------------------------------------------------
# Retry policy
# ---------------------------------------------------------------------------

def backoff_sleep_seconds(profile: dict[str, Any], retry_index: int) -> float:
    cfg = profile.get("backoff") or {}
    mode = str(cfg.get("mode", "none")).lower()
    base = float(cfg.get("base_s", 0.0) or 0.0)
    max_s = float(cfg.get("max_s", base) or base)
    jitter = str(cfg.get("jitter", "none")).lower()

    if mode in {"none", ""}:
        return 0.0
    if mode == "fixed":
        delay = base
    elif mode == "exponential":
        delay = min(max_s, base * (2 ** max(0, retry_index - 1)))
    else:
        delay = base

    if jitter == "full" and delay > 0:
        delay = random.uniform(0.0, delay)
    return max(0.0, delay)


def should_retry(profile: dict[str, Any], status: int) -> bool:
    # Profile config is the single source of truth: only retry on statuses
    # explicitly listed in retry_on_status. Use status=0 in the list to retry
    # network-level errors (socket timeouts, connection refused, TLS failures),
    # which traffic_gen.py reports as status=0.
    retry_on = set(int(x) for x in profile.get("retry_on_status", []))
    return status in retry_on


# ---------------------------------------------------------------------------
# Template substitution + HTTP send
# ---------------------------------------------------------------------------

def substitute(template: str, subs: dict[str, str]) -> str:
    if not template:
        return template
    out = template
    for key, val in subs.items():
        out = out.replace("{" + key + "}", val)
    return out


def send_once(
    *,
    session: KeepAliveSession,
    method: str,
    path: str,
    headers: dict[str, str],
    body: Optional[bytes] = None,
) -> tuple[bool, int, str]:
    """Run one HTTP request on a persistent connection."""
    return session.request(method=method, path=path, headers=headers, body=body)


# ---------------------------------------------------------------------------
# CSV writer
# ---------------------------------------------------------------------------

async def append_csv(line: str, out_csv: Path, lock: asyncio.Lock) -> None:
    async with lock:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("a") as f:
            f.write(line)


# ---------------------------------------------------------------------------
# Worker: one request attempt (with retries) → CSV rows
# ---------------------------------------------------------------------------

async def execute_step(
    *,
    step: dict[str, Any],
    profile: dict[str, Any],
    worker_idx: int,
    session: KeepAliveSession,
    host_header: str,
    headers_base: dict[str, str],
    session_id: str,
    sku: str,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> bool:
    """
    Run a single request step with the profile's retry policy.
    Returns True if the final attempt was ok (so a workflow can advance).
    """
    name = str(profile.get("name", "client"))
    max_retries = int(profile.get("retries", 0))
    method = step["method"]
    subs = {"sku": sku}
    path = substitute(step["path"], subs)

    body_bytes: Optional[bytes] = None
    if method in {"POST", "PUT", "PATCH"} and step.get("body"):
        body_bytes = substitute(step["body"], subs).encode("utf-8")

    req_id = str(uuid.uuid4())
    step_name = str(step.get("name", path))

    final_ok = False
    for retry_index in range(0, max_retries + 1):
        attempt_number = retry_index + 1
        is_retry = retry_index > 0

        headers = dict(headers_base)
        headers["X-Request-ID"] = req_id
        headers["X-Attempt-Number"] = str(attempt_number)
        headers["Cookie"] = f"shop_session-id={session_id}"
        if host_header:
            headers["Host"] = host_header
        if body_bytes is not None:
            headers["Content-Type"] = step.get("content_type", "application/x-www-form-urlencoded")
            headers["Content-Length"] = str(len(body_bytes))

        t0 = time.time()
        ok, status, err = await asyncio.to_thread(
            send_once,
            session=session,
            method=method,
            path=path,
            headers=headers,
            body=body_bytes,
        )
        latency = time.time() - t0
        final_ok = ok

        # One CSV row per attempt.
        csv_line = (
            f"{time.time():.6f},{name},{worker_idx},{req_id},{step_name},"
            f"{method},{path},{attempt_number},{int(is_retry)},{status},"
            f"{int(ok)},{latency:.6f}\n"
        )
        await append_csv(csv_line, out_csv, csv_lock)

        # Structured log line (consumed by tail -f or jq during debugging).
        print(json.dumps({
            "event": "attempt",
            "ts": time.time(),
            "profile": name,
            "worker": worker_idx,
            "request_id": req_id,
            "request_type": step_name,
            "method": method,
            "path": path,
            "attempt": attempt_number,
            "is_retry": is_retry,
            "status": status,
            "ok": ok,
            "latency_ms": round(latency * 1000, 2),
            "error": err or None,
        }), flush=True)

        if ok or not should_retry(profile, status) or retry_index >= max_retries:
            break

        delay = backoff_sleep_seconds(profile, retry_index + 1)
        if delay > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return final_ok  # stop requested mid-backoff
            except asyncio.TimeoutError:
                pass

    return final_ok


async def client_worker(
    profile: dict[str, Any],
    worker_idx: int,
    target_host: str,
    target_port: int,
    host_header: str,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> None:
    name = str(profile.get("name", "client"))
    rate = float(profile.get("rate_rps_per_client", 1.0))
    interval = max(0.001, 1.0 / max(0.001, rate))
    timeout_s = float(profile.get("timeout_s", 2.0))
    requests_mix = list(profile.get("requests", []))
    weights = [float(r.get("weight", 1)) for r in requests_mix]
    sku_pool = list(profile.get("sku_pool", []))

    if not requests_mix:
        print(f"[warn] profile {name}: no requests, worker idle", file=sys.stderr)
        return

    headers_base = {
        "User-Agent": f"retry-study-client/{name}",
        "X-Retry-Client-Type": name,
        "X-Retry-Client-Worker": str(worker_idx),
    }

    # One long-lived HTTP connection per worker. Reconnects on error.
    session = KeepAliveSession(target_host, target_port, timeout_s=timeout_s)

    try:
        while not stop_event.is_set():
            loop_started = time.time()

            # Weighted pick from the request mix.
            picked = random.choices(requests_mix, weights=weights, k=1)[0]

            # Fresh shop session per top-level pick — workflows share it, single
            # requests just use it once. Frontend's `shop_session-id` cookie
            # identifies the cart.
            session_id = str(uuid.uuid4())
            sku = random.choice(sku_pool) if sku_pool else ""

            if "workflow" in picked:
                # Sequential steps sharing session+sku. Abort the workflow if
                # any step fails (can't checkout without successful add-to-cart).
                for step in picked["workflow"]:
                    step_ok = await execute_step(
                        step=step,
                        profile=profile,
                        worker_idx=worker_idx,
                        session=session,
                        host_header=host_header,
                        headers_base=headers_base,
                        session_id=session_id,
                        sku=sku,
                        out_csv=out_csv,
                        csv_lock=csv_lock,
                        stop_event=stop_event,
                    )
                    if not step_ok or stop_event.is_set():
                        break
            else:
                await execute_step(
                    step=picked,
                    profile=profile,
                    worker_idx=worker_idx,
                    session=session,
                    host_header=host_header,
                    headers_base=headers_base,
                    session_id=session_id,
                    sku=sku,
                    out_csv=out_csv,
                    csv_lock=csv_lock,
                    stop_event=stop_event,
                )

            elapsed = time.time() - loop_started
            sleep_s = interval - elapsed
            if sleep_s > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
                except asyncio.TimeoutError:
                    pass
    finally:
        session.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CSV_HEADER = (
    "timestamp,profile,worker,request_id,request_type,method,path,"
    "attempt,is_retry,status,ok,latency_s\n"
)


async def main_async(args) -> int:
    profile_dir = Path(args.profile_dir)
    profiles = load_profiles(profile_dir)
    if args.profiles:
        allow = {x.strip() for x in args.profiles.split(",") if x.strip()}
        profiles = [p for p in profiles if p.get("name") in allow]
    if not profiles:
        print("No profiles selected.", file=sys.stderr)
        return 1

    out_csv = Path(args.output_dir) / "client_attempts.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.exists():
        out_csv.write_text(CSV_HEADER)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    # Replace the default ThreadPoolExecutor with a much larger one so high
    # `count` values in profiles actually produce parallel HTTP calls.
    loop.set_default_executor(
        concurrent.futures.ThreadPoolExecutor(
            max_workers=DEFAULT_HTTP_THREAD_POOL_SIZE,
            thread_name_prefix="http-worker",
        )
    )

    def _handle_stop(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop_event.set())

    # Parse the base URL once — http.client wants host + port, not a full URL.
    # `target_base_url` looks like "http://pc835.emulab.net:31234".
    parsed = urllib.parse.urlparse(args.target_base_url)
    if parsed.scheme != "http":
        print(f"ERROR: only http:// is supported (got {parsed.scheme}://)", file=sys.stderr)
        return 2
    target_host = parsed.hostname or ""
    target_port = parsed.port or 80
    if not target_host:
        print(f"ERROR: could not parse host from {args.target_base_url}", file=sys.stderr)
        return 2

    csv_lock = asyncio.Lock()
    tasks = []
    for p in profiles:
        count = int(p.get("count", 1))
        for i in range(count):
            tasks.append(asyncio.create_task(client_worker(
                profile=p,
                worker_idx=i,
                target_host=target_host,
                target_port=target_port,
                host_header=args.host_header or "",
                out_csv=out_csv,
                csv_lock=csv_lock,
                stop_event=stop_event,
            )))

    print(json.dumps({
        "event": "startup",
        "ts": time.time(),
        "target_base_url": args.target_base_url,
        "host_header": args.host_header,
        "profiles": [
            {k: v for k, v in p.items() if not k.startswith("_")}
            for p in profiles
        ],
        "output_csv": str(out_csv),
    }), flush=True)

    await asyncio.gather(*tasks)
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="External online-boutique traffic generator")
    p.add_argument("--target-base-url", required=True, help="e.g. http://MASTER:NodePort")
    p.add_argument("--host-header", default="", help="Host header for Gateway routing")
    p.add_argument("--profile-dir", default=str(Path(__file__).parent / "profiles"))
    p.add_argument("--profiles", default="", help="Comma-separated profile names to run (default: all)")
    p.add_argument("--output-dir", default="client-metrics")
    return p.parse_args()


def main():
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
