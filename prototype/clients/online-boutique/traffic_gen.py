#!/usr/bin/env python3
"""
External Online Boutique traffic generator (runs on CLIENT_HOST).

Profiles live as JSON files in profiles/. Each profile defines:

  * A "mode" — "closed-loop" (default) or "open-loop"
  * A weighted mix of request types (single requests or multi-step workflows)
  * A SKU pool — `{sku}` placeholders in paths/bodies are substituted per pick
  * A retry policy (max retries, backoff, retry-on-status list)

Two load-generation modes:

  closed-loop (default):
    `count` workers, each with its own persistent HTTP/1.1 connection.
    Each worker loops: pick request → execute (with retries) → sleep to
    hit `rate_rps_per_client` → repeat. Offered RPS is bounded by
    `count / per-request-latency`. Self-throttles when the backend slows.
    Schema fields: count, rate_rps_per_client.

  open-loop:
    One firer coroutine fires requests on a wall-clock schedule, regardless
    of whether previous requests have completed. In-flight count grows
    unbounded if the backend slows (capped by `max_inflight` for safety).
    A bounded `pool_size` of HTTP connections is shared by all in-flight
    tasks via async lease/return. Offered RPS is exactly `rate_rps`,
    matching real production traffic semantics.
    Schema fields: rate_rps, pool_size, max_inflight (optional).

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
# workers a profile requests. Bump it so high pool_size or count values
# actually produce parallel HTTP calls.
DEFAULT_HTTP_THREAD_POOL_SIZE = 1024


# ---------------------------------------------------------------------------
# Persistent HTTP connection (one per worker in closed-loop, pooled in open-loop)
# ---------------------------------------------------------------------------
#
# urllib.request.urlopen does not reuse connections — every call does a fresh
# TCP handshake (+ optional TLS). Under load that adds hundreds of ms per
# attempt and saturates the kernel conntrack/tw_reuse tables. We fix this by
# holding `http.client.HTTPConnection` instances with HTTP keep-alive.
#
# This class is NOT thread-safe. In closed-loop, each worker owns its own
# instance. In open-loop, the ConnectionPool below ensures only one coroutine
# holds a session at a time (via asyncio.Queue), so within asyncio.to_thread
# the connection is also accessed by only one thread.
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
# ConnectionPool — bounded async pool of KeepAliveSessions for open-loop mode
# ---------------------------------------------------------------------------
#
# In open-loop mode, the firer dispatches requests at a fixed wall-clock rate
# regardless of how many are already in flight. We don't want each request to
# open a fresh TCP connection (that would saturate the gateway's connection
# pool and the kernel's ephemeral-port range), so requests share a bounded
# pool of persistent HTTP/1.1 connections.
#
# acquire() blocks if all sessions are checked out, which is exactly the
# back-pressure behavior we want — under cluster overload, the in-flight
# count grows because requests pile up waiting for a connection slot.
class ConnectionPool:
    """Bounded async pool of KeepAliveSessions, leased per request."""

    def __init__(self, host: str, port: int, size: int, timeout_s: float) -> None:
        self.host = host
        self.port = port
        self.size = size
        self.timeout_s = timeout_s
        self._pool: asyncio.Queue[Optional[KeepAliveSession]] = asyncio.Queue(maxsize=size)
        # Pre-fill with placeholders. The actual KeepAliveSession is created
        # lazily on first acquire so we don't open all `size` TCP connections
        # at startup before any traffic is fired.
        for _ in range(size):
            self._pool.put_nowait(None)

    async def acquire(self) -> KeepAliveSession:
        """Block until a session is available. Lazily create if first time."""
        slot = await self._pool.get()
        if slot is None:
            slot = KeepAliveSession(self.host, self.port, self.timeout_s)
        return slot

    def release(self, session: KeepAliveSession) -> None:
        """Return a session to the pool. Always succeeds (queue is bounded but pre-allocated)."""
        try:
            self._pool.put_nowait(session)
        except asyncio.QueueFull:
            # Should never happen — we put exactly `size` items in __init__
            # and never add more. If it does, something is leaking. Drop
            # the session safely.
            session.close()

    async def close_all(self) -> None:
        """Drain the pool and close all open sessions."""
        for _ in range(self.size):
            try:
                slot = self._pool.get_nowait()
            except asyncio.QueueEmpty:
                break
            if slot is not None:
                slot.close()


# ---------------------------------------------------------------------------
# Profile loading + normalization
# ---------------------------------------------------------------------------

def normalize_profile(p: dict[str, Any]) -> dict[str, Any]:
    """Backfill defaults and convert legacy `paths:` profiles to the new schema."""
    p.setdefault("mode", "closed-loop")
    # Closed-loop fields:
    p.setdefault("count", 1)
    p.setdefault("rate_rps_per_client", 1.0)
    # Open-loop fields:
    p.setdefault("rate_rps", 0)            # 0 → not configured
    p.setdefault("pool_size", 0)            # 0 → not configured
    p.setdefault("max_inflight", 0)         # 0 → unbounded (with safety warning)
    # Shared fields:
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
# Core execution: one HTTP step with retries → CSV rows
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

    The session is provided by the caller — closed-loop workers pass their
    long-lived session, open-loop firer passes a session leased from the pool.
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
        timeout_s = float(profile.get("timeout_s", 0))
        try:
            coro = asyncio.to_thread(
                send_once,
                session=session,
                method=method,
                path=path,
                headers=headers,
                body=body_bytes,
            )
            if timeout_s > 0:
                ok, status, err = await asyncio.wait_for(coro, timeout=timeout_s)
            else:
                ok, status, err = await coro
        except asyncio.TimeoutError:
            ok, status, err = False, 0, "client_timeout"
            # The underlying thread is still blocked in the socket call;
            # close the connection so it gets a BrokenPipeError and exits.
            session._close()
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


# ---------------------------------------------------------------------------
# execute_logical_request — pick from mix, run step or workflow
#
# This is the unit of work shared by both modes. Closed-loop workers call it
# in a loop with their own session; the open-loop firer calls it as a
# fire-and-forget task with a leased pool session.
# ---------------------------------------------------------------------------

async def execute_logical_request(
    *,
    profile: dict[str, Any],
    worker_idx: int,
    session: KeepAliveSession,
    host_header: str,
    headers_base: dict[str, str],
    requests_mix: list,
    weights: list,
    sku_pool: list,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> None:
    # Weighted pick from the request mix.
    picked = random.choices(requests_mix, weights=weights, k=1)[0]

    # Fresh shop session per top-level pick — workflows share it across steps,
    # single requests just use it once. Frontend's `shop_session-id` cookie
    # identifies the cart.
    session_id = str(uuid.uuid4())
    sku = random.choice(sku_pool) if sku_pool else ""

    if "workflow" in picked:
        # Sequential steps sharing session+sku. Abort the workflow if any
        # step fails (can't checkout without successful add-to-cart).
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


# ---------------------------------------------------------------------------
# Closed-loop worker — owns one session, loops forever
# ---------------------------------------------------------------------------

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

            await execute_logical_request(
                profile=profile,
                worker_idx=worker_idx,
                session=session,
                host_header=host_header,
                headers_base=headers_base,
                requests_mix=requests_mix,
                weights=weights,
                sku_pool=sku_pool,
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
# Open-loop firer — fixed-schedule fire-and-forget, shared connection pool
# ---------------------------------------------------------------------------

async def _open_loop_one(
    *,
    profile: dict[str, Any],
    fire_idx: int,
    pool: ConnectionPool,
    host_header: str,
    headers_base: dict[str, str],
    requests_mix: list,
    weights: list,
    sku_pool: list,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
    inflight_sem: Optional[asyncio.Semaphore],
) -> None:
    """One fire-and-forget logical request: lease a session, run, return it."""
    if stop_event.is_set():
        return
    # Hold the in-flight semaphore (if any) for the entire duration of the
    # request, so it actually bounds concurrency rather than just gating dispatch.
    if inflight_sem is not None:
        if inflight_sem.locked():
            # Client itself is overloaded — drop this request and log it.
            await append_csv(
                f"{time.time():.6f},{profile.get('name','client')},{fire_idx},"
                f"client-overload,client-overload,DROP,/,1,0,-1,0,0.000000\n",
                out_csv, csv_lock,
            )
            return
        await inflight_sem.acquire()
    try:
        session = await pool.acquire()
        try:
            await execute_logical_request(
                profile=profile,
                worker_idx=fire_idx,
                session=session,
                host_header=host_header,
                headers_base=headers_base,
                requests_mix=requests_mix,
                weights=weights,
                sku_pool=sku_pool,
                out_csv=out_csv,
                csv_lock=csv_lock,
                stop_event=stop_event,
            )
        finally:
            pool.release(session)
    finally:
        if inflight_sem is not None:
            inflight_sem.release()


async def open_loop_firer(
    profile: dict[str, Any],
    target_host: str,
    target_port: int,
    host_header: str,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> None:
    """
    Fire requests on a wall-clock schedule at exactly `rate_rps`, regardless
    of whether prior requests have completed. Each fire is a fire-and-forget
    asyncio task that leases a session from a shared pool, runs, and returns.
    """
    name = str(profile.get("name", "client"))
    rate_rps = float(profile.get("rate_rps", 0) or 0)
    pool_size = int(profile.get("pool_size", 0) or 0)
    max_inflight = int(profile.get("max_inflight", 0) or 0)
    timeout_s = float(profile.get("timeout_s", 2.0))
    requests_mix = list(profile.get("requests", []))
    weights = [float(r.get("weight", 1)) for r in requests_mix]
    sku_pool = list(profile.get("sku_pool", []))

    if rate_rps <= 0:
        print(f"[err] profile {name}: open-loop mode requires rate_rps > 0",
              file=sys.stderr)
        return
    if pool_size <= 0:
        print(f"[err] profile {name}: open-loop mode requires pool_size > 0",
              file=sys.stderr)
        return
    if not requests_mix:
        print(f"[warn] profile {name}: no requests, firer idle", file=sys.stderr)
        return

    headers_base = {
        "User-Agent": f"retry-study-client/{name}",
        "X-Retry-Client-Type": name,
        "X-Retry-Client-Worker": "open-loop",
    }

    pool = ConnectionPool(target_host, target_port, pool_size, timeout_s)
    inflight_sem: Optional[asyncio.Semaphore] = (
        asyncio.Semaphore(max_inflight) if max_inflight > 0 else None
    )

    interval = 1.0 / rate_rps
    next_fire = time.monotonic()
    fire_idx = 0
    fired_tasks: list[asyncio.Task] = []

    try:
        while not stop_event.is_set():
            now = time.monotonic()
            sleep_for = next_fire - now
            if sleep_for > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=sleep_for)
                    break  # stop requested
                except asyncio.TimeoutError:
                    pass
            # Schedule the next fire BEFORE dispatching, so the schedule
            # doesn't drift if dispatch takes nontrivial time.
            next_fire += interval
            fire_idx += 1

            task = asyncio.create_task(_open_loop_one(
                profile=profile,
                fire_idx=fire_idx,
                pool=pool,
                host_header=host_header,
                headers_base=headers_base,
                requests_mix=requests_mix,
                weights=weights,
                sku_pool=sku_pool,
                out_csv=out_csv,
                csv_lock=csv_lock,
                stop_event=stop_event,
                inflight_sem=inflight_sem,
            ))
            fired_tasks.append(task)

            # Periodically reap completed tasks so the list doesn't grow forever.
            if fire_idx % 1000 == 0:
                fired_tasks = [t for t in fired_tasks if not t.done()]
    finally:
        # Wait for in-flight requests to complete (with a hard cap so shutdown
        # doesn't hang forever).
        if fired_tasks:
            try:
                await asyncio.wait_for(
                    asyncio.gather(*fired_tasks, return_exceptions=True),
                    timeout=max(5.0, timeout_s * 2),
                )
            except asyncio.TimeoutError:
                for t in fired_tasks:
                    if not t.done():
                        t.cancel()
        await pool.close_all()


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
    # `count` (closed-loop) or `pool_size` (open-loop) values actually produce
    # parallel HTTP calls.
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
        mode = str(p.get("mode", "closed-loop")).lower()
        if mode == "open-loop":
            tasks.append(asyncio.create_task(open_loop_firer(
                profile=p,
                target_host=target_host,
                target_port=target_port,
                host_header=args.host_header or "",
                out_csv=out_csv,
                csv_lock=csv_lock,
                stop_event=stop_event,
            )))
        elif mode == "closed-loop":
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
        else:
            print(f"[err] profile {p.get('name')}: unknown mode {mode!r} "
                  f"(expected 'closed-loop' or 'open-loop')", file=sys.stderr)
            return 3

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
