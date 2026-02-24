#!/usr/bin/env python3
"""
External Online Boutique traffic generator (runs on CLIENT_HOST).

Uses split profile JSON files to model heterogeneous client retry behaviors.
Profiles are AWS-SDK-style (inspired by aws_outage/client/traffic_gen.py), but
implemented for generic HTTP traffic against the Online Boutique gateway.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import signal
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any


def load_profiles(profile_dir: Path) -> list[dict[str, Any]]:
    profiles: list[dict[str, Any]] = []
    for path in sorted(profile_dir.glob("*.json")):
        data = json.loads(path.read_text())
        data["_path"] = str(path)
        profiles.append(data)
    return profiles


def backoff_sleep_seconds(profile: dict[str, Any], retry_index: int) -> float:
    cfg = profile.get("backoff", {}) or {}
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
    retry_on = set(int(x) for x in profile.get("retry_on_status", []))
    return status == 0 or status in retry_on


async def append_csv(line: str, out_csv: Path, lock: asyncio.Lock) -> None:
    async with lock:
        out_csv.parent.mkdir(parents=True, exist_ok=True)
        with out_csv.open("a") as f:
            f.write(line)


def send_once(
    *,
    method: str,
    url: str,
    headers: dict[str, str],
    timeout_s: float,
) -> tuple[bool, int, str]:
    req = urllib.request.Request(url=url, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            status = int(resp.getcode())
            _ = resp.read(64)
            ok = 200 <= status < 400
            return ok, status, ""
    except urllib.error.HTTPError as exc:
        return False, int(exc.code), f"http_error:{exc.code}"
    except Exception as exc:
        return False, 0, exc.__class__.__name__


async def client_worker(
    profile: dict[str, Any],
    worker_idx: int,
    target_base_url: str,
    host_header: str,
    out_csv: Path,
    csv_lock: asyncio.Lock,
    stop_event: asyncio.Event,
) -> None:
    name = str(profile.get("name", "client"))
    rate = float(profile.get("rate_rps_per_client", 1.0))
    timeout_s = float(profile.get("timeout_s", 2.0))
    retries = int(profile.get("retries", 0))
    method = str(profile.get("method", "GET")).upper()
    paths = list(profile.get("paths", ["/"]))
    if not paths:
        paths = ["/"]
    interval = max(0.001, 1.0 / max(0.001, rate))

    headers_base = {
        "User-Agent": f"retry-study-client/{name}",
        "X-Retry-Client-Type": name,
        "X-Retry-Client-Worker": str(worker_idx),
    }
    if host_header:
        headers_base["Host"] = host_header

    while not stop_event.is_set():
        started = time.time()
        req_id = str(uuid.uuid4())
        path = random.choice(paths)
        url = target_base_url.rstrip("/") + path

        final_status = 0
        final_ok = False
        total_attempts = 0

        for retry_index in range(0, retries + 1):
            total_attempts += 1
            headers = dict(headers_base)
            headers["X-Request-ID"] = req_id
            headers["X-Attempt-Number"] = str(total_attempts)
            is_retry = retry_index > 0
            attempt_start = time.time()
            status = 0
            err = ""
            ok, status, err = await asyncio.to_thread(
                send_once,
                method=method,
                url=url,
                headers=headers,
                timeout_s=timeout_s,
            )

            latency = time.time() - attempt_start
            final_status = status
            final_ok = ok

            print(json.dumps({
                "event": "attempt",
                "ts": time.time(),
                "profile": name,
                "worker": worker_idx,
                "request_id": req_id,
                "path": path,
                "attempt": total_attempts,
                "is_retry": is_retry,
                "status": status,
                "ok": ok,
                "latency_ms": round(latency * 1000, 2),
                "error": err or None,
            }), flush=True)

            await append_csv(
                f"{time.time()},{name},{worker_idx},{req_id},{path},{total_attempts},{int(is_retry)},{status},{int(ok)},{latency:.6f}\n",
                out_csv,
                csv_lock,
            )

            if ok or not should_retry(profile, status) or retry_index >= retries:
                break

            delay = backoff_sleep_seconds(profile, retry_index + 1)
            if delay > 0:
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=delay)
                    break
                except asyncio.TimeoutError:
                    pass

        print(json.dumps({
            "event": "request_done",
            "ts": time.time(),
            "profile": name,
            "worker": worker_idx,
            "request_id": req_id,
            "attempts": total_attempts,
            "success": final_ok,
            "final_status": final_status,
        }), flush=True)

        elapsed = time.time() - started
        sleep_s = interval - elapsed
        if sleep_s > 0:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=sleep_s)
            except asyncio.TimeoutError:
                pass


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
        out_csv.write_text("timestamp,profile,worker,request_id,path,attempt,is_retry,status,ok,latency_s\n")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()

    def _handle_stop(*_):
        stop_event.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _handle_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_args: stop_event.set())

    csv_lock = asyncio.Lock()
    tasks = []
    for p in profiles:
        count = int(p.get("count", 1))
        for i in range(count):
            tasks.append(asyncio.create_task(client_worker(
                profile=p,
                worker_idx=i,
                target_base_url=args.target_base_url,
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
        "profiles": [{k: v for k, v in p.items() if not k.startswith("_")} for p in profiles],
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
