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
import collections
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
# Per-process module state
# ---------------------------------------------------------------------------
#
# These hold the long-lived CSV file handle and the verbosity flag for the
# current process. They're set once at startup in main_async() and read by
# the hot path (write_csv_row, execute_step). Module globals are fine because
# each shard runs as its own OS process, so there's no cross-shard sharing.
#
# Why module-level instead of plumbing through every signature: every
# coroutine and helper would otherwise need a `csv_file` parameter, and the
# only thing it would do with it is call `.write()`. The plumbing cost
# (and the thread of csv_lock that came with it) was the original bottleneck
# we're removing — keeping the new write path equally noisy in every
# function signature defeats the purpose.

_CSV_FILE: "Optional[Any]" = None
_LOG_ATTEMPTS: bool = False

# ---------------------------------------------------------------------------
# RL controller observation window (in-memory ring buffer + HTTP)
# ---------------------------------------------------------------------------
#
# When --rl-window-port-base is non-zero, every CSV row is also appended to a
# bounded deque, and a tiny aiohttp server on `port_base + shard_id` exposes
# `/window?since=<unix_ts>` returning the rows newer than `since`. This
# replaces the controller's SSH-cat loop: bytes per tick drop from
# O(file_size) to O(rps × decision_interval), and the loader pays at most
# one extra deque-append per HTTP attempt.
#
# `_WINDOW_KEEP_SEC` bounds memory: rows older than that are dropped on
# every append (lazy GC). A 60 s ceiling fits the longest configured
# observation window plus margin without growing unbounded. The
# `_WINDOW_RING` is per-process (one shard), so the controller fans out
# across ports per tick.

_WINDOW_RING: "Optional[collections.deque[dict[str, Any]]]" = None
_WINDOW_KEEP_SEC: float = 60.0
_WINDOW_SHARD_ID: int = 0


def write_csv_row(line: str) -> None:
    """Append one row to the run's CSV. Single-threaded by construction:
    every caller is a coroutine on the same asyncio event loop, and `write`
    holds the GIL until it returns, so two writes can never interleave."""
    if _CSV_FILE is not None:
        _CSV_FILE.write(line)


def enqueue_window_row(row: dict[str, Any]) -> None:
    """Push one structured attempt row onto the in-memory ring (if enabled).

    The row schema mirrors the CSV columns so `/window` consumers see
    exactly the same fields the on-disk file has. Pruning happens on every
    enqueue: cheaper than a separate housekeeping task, and matches the
    deque's natural growth rate."""
    if _WINDOW_RING is None:
        return
    _WINDOW_RING.append(row)
    cutoff = row["timestamp"] - _WINDOW_KEEP_SEC
    # The deque is naturally ordered by enqueue time → drop from the left
    # while it's older than the cutoff. Two-pop bound = the worst lag a
    # single tick can introduce; in practice we drop 0-2 rows per call.
    while _WINDOW_RING and _WINDOW_RING[0]["timestamp"] < cutoff:
        _WINDOW_RING.popleft()


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
    for path in sorted(profile_dir.rglob("*.json")):
        data = json.loads(path.read_text())
        data["_path"] = str(path)
        # Relative stem for matching via --profiles (e.g. "fairness/post-cart-stress-open-1").
        data["_relkey"] = str(path.relative_to(profile_dir).with_suffix(""))
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
# Core execution: one HTTP step with retries → CSV rows
#
# CSV writes go through the module-level `write_csv_row` (no per-write
# open/close, no asyncio.Lock). The legacy async `append_csv` helper —
# which did `out_csv.open("a")` per row inside a lock — was the dominant
# loader-side bottleneck and has been removed.
# ---------------------------------------------------------------------------

async def execute_step(
    *,
    step: dict[str, Any],
    profile: dict[str, Any],
    worker_idx: int,
    session: "Optional[KeepAliveSession]" = None,
    pool: "Optional[ConnectionPool]" = None,
    host_header: str,
    headers_base: dict[str, str],
    session_id: str,
    sku: str,
    stop_event: asyncio.Event,
) -> bool:
    """
    Run a single request step with the profile's retry policy.
    Returns True if the final attempt was ok (so a workflow can advance).

    Session management has two modes:
      - closed-loop: caller passes a long-lived `session`, no `pool`. The
        session is reused across all retries (same TCP connection).
      - open-loop:   caller passes `pool` (no `session`). Each attempt
        acquires a fresh session from the pool and releases it immediately
        after the attempt completes. This keeps the per-attempt hold time
        to `timeout_s` instead of `retries × timeout_s`, preventing the
        connection pool from saturating during fault-window backlog drain.
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

        # ---- Per-attempt session management (open-loop only) ----
        # In open-loop mode (pool provided), acquire a fresh connection for
        # each attempt and release it immediately after. Hold time per slot
        # = timeout_s (one attempt), NOT retries × timeout_s (all attempts).
        # In closed-loop mode (session provided), reuse the caller's
        # persistent connection — no pool interaction.
        if pool is not None:
            attempt_session = await pool.acquire()
        else:
            attempt_session = session

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
                session=attempt_session,
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
            attempt_session._close()
            # The session is now poisoned — the worker thread may still
            # hold a reference to the old self._conn. Don't return it to
            # the pool; release a None placeholder instead so the pool
            # slot is freed and the next acquire creates a fresh session.
            if pool is not None:
                pool.release(None)
                attempt_session = None  # prevent the finally from double-releasing
        finally:
            if pool is not None and attempt_session is not None:
                pool.release(attempt_session)

        latency = time.time() - t0
        final_ok = ok

        # One CSV row per attempt. Synchronous write to a long-held file
        # handle — see write_csv_row for why this is safe.
        attempt_ts = time.time()
        write_csv_row(
            f"{attempt_ts:.6f},{name},{worker_idx},{req_id},{step_name},"
            f"{method},{path},{attempt_number},{int(is_retry)},{status},"
            f"{int(ok)},{latency:.6f}\n"
        )

        # Also enqueue the structured row for /window consumers.
        # No-op when --rl-window-port-base is 0. Schema matches the CSV.
        enqueue_window_row({
            "timestamp": attempt_ts,
            "profile": name,
            "worker": worker_idx,
            "request_id": req_id,
            "request_type": step_name,
            "method": method,
            "path": path,
            "attempt": attempt_number,
            "is_retry": int(is_retry),
            "status": status,
            "ok": int(ok),
            "latency_s": latency,
        })

        # Per-attempt structured log line. Off by default — at high RPS the
        # json.dumps + flushed print is one of the dominant per-fire costs.
        # Re-enable with --log-attempts for debugging.
        if _LOG_ATTEMPTS:
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
    session: "Optional[KeepAliveSession]" = None,
    pool: "Optional[ConnectionPool]" = None,
    host_header: str,
    headers_base: dict[str, str],
    requests_mix: list,
    weights: list,
    sku_pool: list,
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
                pool=pool,
                host_header=host_header,
                headers_base=headers_base,
                session_id=session_id,
                sku=sku,
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
            pool=pool,
            host_header=host_header,
            headers_base=headers_base,
            session_id=session_id,
            sku=sku,
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
            drop_ts = time.time()
            drop_profile = profile.get("name", "client")
            write_csv_row(
                f"{drop_ts:.6f},{drop_profile},{fire_idx},"
                f"client-overload,client-overload,DROP,/,1,0,-1,0,0.000000\n"
            )
            enqueue_window_row({
                "timestamp": drop_ts,
                "profile": drop_profile,
                "worker": fire_idx,
                "request_id": "client-overload",
                "request_type": "client-overload",
                "method": "DROP",
                "path": "/",
                "attempt": 1,
                "is_retry": 0,
                "status": -1,
                "ok": 0,
                "latency_s": 0.0,
            })
            return
        await inflight_sem.acquire()
    try:
        # Pass the pool through — execute_step acquires/releases a session
        # per attempt, so each attempt holds a pool slot for only timeout_s
        # instead of retries × timeout_s. This prevents the pool from
        # saturating during fault-window backlog drain.
        await execute_logical_request(
            profile=profile,
            worker_idx=fire_idx,
            pool=pool,
            host_header=host_header,
            headers_base=headers_base,
            requests_mix=requests_mix,
            weights=weights,
            sku_pool=sku_pool,
            stop_event=stop_event,
        )
    finally:
        if inflight_sem is not None:
            inflight_sem.release()


async def open_loop_firer(
    profile: dict[str, Any],
    target_host: str,
    target_port: int,
    host_header: str,
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
# RL controller observation server (aiohttp /window endpoint)
# ---------------------------------------------------------------------------
#
# Tiny HTTP server that exposes the in-memory ring buffer to the in-cluster
# RL controller. One server per shard, on `port_base + shard_id`, so the
# controller can fan-out across all 4 shard ports per tick.
#
# aiohttp is imported lazily because:
#   * the loader machine doesn't always have it installed (opt-in per
#     cluster bootstrap; legacy SSH-cat path still works without it),
#   * even on machines with it, this module is sometimes imported by the
#     test harness which doesn't need the server.
# If the import fails the server is silently disabled and a warning is
# printed; the controller's --legacy-ssh-obs flag is the fallback.


async def _handle_window(request: "Any") -> "Any":
    """`GET /window?since=<unix_ts>` → JSON of rows with timestamp ≥ since.

    Lazily imports aiohttp inside this handler is wrong (handlers are
    already running on aiohttp); this body only runs when aiohttp is in
    scope, so the bare `web.json_response(...)` reference is safe."""
    from aiohttp import web  # noqa: PLC0415

    since_raw = request.query.get("since", "0")
    try:
        since = float(since_raw)
    except (TypeError, ValueError):
        return web.json_response({"error": f"bad since={since_raw!r}"}, status=400)

    # Snapshot the deque first so we don't iterate concurrently with new
    # appends. `list()` on a deque is O(n) and safe under the GIL — we
    # don't need a lock as long as the snapshot is one statement.
    if _WINDOW_RING is None:
        rows: list[dict[str, Any]] = []
    else:
        rows = [r for r in list(_WINDOW_RING) if r["timestamp"] >= since]
    return web.json_response({
        "rows": rows,
        "now": time.time(),
        "shard": _WINDOW_SHARD_ID,
    })


async def _start_window_server(port: int) -> "Any":
    """Start the aiohttp server. Returns the AppRunner so main_async can
    cleanly stop it on shutdown. Returns None on import failure."""
    try:
        from aiohttp import web  # noqa: PLC0415
    except ImportError:
        print(
            "[traffic_gen] aiohttp is not installed; /window server "
            "disabled (controller falls back to --legacy-ssh-obs).",
            file=sys.stderr,
        )
        return None

    app = web.Application()
    app.router.add_get("/window", _handle_window)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, host="0.0.0.0", port=port)
    try:
        await site.start()
    except OSError as exc:
        print(
            f"[traffic_gen] /window server bind failed on :{port}: {exc}; "
            "the controller will fall back to --legacy-ssh-obs.",
            file=sys.stderr,
        )
        await runner.cleanup()
        return None

    print(json.dumps({
        "event": "window_server_started",
        "ts": time.time(),
        "port": port,
        "shard_id": _WINDOW_SHARD_ID,
    }), flush=True)
    return runner


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

CSV_HEADER = (
    "timestamp,profile,worker,request_id,request_type,method,path,"
    "attempt,is_retry,status,ok,latency_s\n"
)


async def main_async(args) -> int:
    global _CSV_FILE, _LOG_ATTEMPTS, _WINDOW_RING, _WINDOW_KEEP_SEC, _WINDOW_SHARD_ID

    profile_dir = Path(args.profile_dir)
    profiles = load_profiles(profile_dir)
    if args.profiles:
        allow = {x.strip() for x in args.profiles.split(",") if x.strip()}
        profiles = [p for p in profiles
                    if p.get("name") in allow or p.get("_relkey") in allow]
    if not profiles:
        print("No profiles selected.", file=sys.stderr)
        return 1

    # ---- Apply per-shard division ----
    # Each loader process owns 1/num_shards of the offered load. Dividing
    # rate / count / pool / inflight per-process keeps the *aggregate*
    # behavior identical regardless of how many shards are launched: a
    # single profile with rate_rps=4000 produces ~4000 rps total whether
    # run as 1 shard, 4 shards × 1000 rps, or 8 shards × 500 rps.
    num_shards = max(1, int(args.num_shards))
    shard_id = max(0, int(args.shard_id))
    if num_shards > 1:
        for p in profiles:
            for field in ("rate_rps", "max_inflight", "pool_size", "count"):
                v = p.get(field, 0) or 0
                if v > 0:
                    # Floor at 1 so divisions like rate_rps=3 / num_shards=4
                    # still produce a runnable shard. Aggregate over-shoots
                    # the configured rate by at most num_shards-1 fires/sec
                    # in pathological cases.
                    p[field] = max(1, v / num_shards) if isinstance(v, float) \
                        else max(1, v // num_shards)

    # ---- Output CSV: one file per shard, distinguishable by suffix ----
    # Sharded runs write client_attempts.shard{N}.csv; single-shard runs
    # keep the legacy filename so existing analysis pipelines (and any
    # archived run directories) keep working unchanged.
    if num_shards > 1:
        out_csv = Path(args.output_dir) / f"client_attempts.shard{shard_id}.csv"
    else:
        out_csv = Path(args.output_dir) / "client_attempts.csv"
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    if not out_csv.exists():
        out_csv.write_text(CSV_HEADER)

    # Open the CSV once for the lifetime of the process. `buffering=1` is
    # line buffering — Python flushes after every '\n', so each `f.write`
    # of one row results in one write() syscall, no large in-memory buffer
    # to lose on shutdown. The old code did open()+write()+close() per row;
    # this halves the syscalls and removes the asyncio.Lock entirely.
    _CSV_FILE = open(out_csv, "a", buffering=1)
    _LOG_ATTEMPTS = bool(args.log_attempts)

    # Enable the in-memory ring + /window server when a port base is
    # configured. The ring is enabled even if aiohttp is missing — it's
    # cheap and lets us see in the log what the server *would* have
    # exposed. The server start is best-effort: failure prints a warning
    # and the controller transparently falls back to --legacy-ssh-obs.
    window_runner = None
    if args.rl_window_port_base > 0:
        _WINDOW_SHARD_ID = shard_id
        _WINDOW_KEEP_SEC = float(args.rl_window_keep_sec)
        _WINDOW_RING = collections.deque()
        port = args.rl_window_port_base + shard_id
        window_runner = await _start_window_server(port)

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

    tasks = []
    for p in profiles:
        mode = str(p.get("mode", "closed-loop")).lower()
        if mode == "open-loop":
            tasks.append(asyncio.create_task(open_loop_firer(
                profile=p,
                target_host=target_host,
                target_port=target_port,
                host_header=args.host_header or "",
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
        "shard_id": shard_id,
        "num_shards": num_shards,
        "log_attempts": _LOG_ATTEMPTS,
        "profiles": [
            {k: v for k, v in p.items() if not k.startswith("_")}
            for p in profiles
        ],
        "output_csv": str(out_csv),
    }), flush=True)

    try:
        await asyncio.gather(*tasks)
    finally:
        # Flush + close the CSV so no rows are lost on shutdown.
        if _CSV_FILE is not None:
            try:
                _CSV_FILE.flush()
                _CSV_FILE.close()
            except Exception:
                pass
            _CSV_FILE = None
        if window_runner is not None:
            try:
                await window_runner.cleanup()
            except Exception:
                pass
    return 0


def parse_args():
    p = argparse.ArgumentParser(description="External online-boutique traffic generator")
    p.add_argument("--target-base-url", required=True, help="e.g. http://MASTER:NodePort")
    p.add_argument("--host-header", default="", help="Host header for Gateway routing")
    p.add_argument("--profile-dir", default=str(Path(__file__).parent / "profiles"))
    p.add_argument("--profiles", default="", help="Comma-separated profile names to run (default: all)")
    p.add_argument("--output-dir", default="client-metrics")
    # Sharding: launch N processes externally, each with the same num-shards
    # but a unique shard-id. Each process owns rate_rps / N of the offered
    # load and writes to client_attempts.shard{N}.csv. Defaults preserve
    # the legacy single-process behavior.
    p.add_argument("--shard-id", type=int, default=0,
                   help="This process's shard index (0-based)")
    p.add_argument("--num-shards", type=int, default=1,
                   help="Total number of loader processes (default 1 = no sharding)")
    # The per-attempt JSON log line is the dominant CPU cost on the firer
    # at high RPS (single asyncio loop is GIL-pinned to one core). Off by
    # default; turn on for debugging.
    p.add_argument("--log-attempts", action="store_true",
                   help="Log a JSON line per HTTP attempt to stdout (slow)")
    # In-memory ring + aiohttp /window endpoint on
    # `port_base + shard_id`. 0 = disabled (legacy SSH-cat path).
    p.add_argument(
        "--rl-window-port-base", type=int, default=0,
        help=(
            "If > 0, expose a /window?since=<ts> aiohttp endpoint on "
            "port_base + shard_id and keep an in-memory ring of recent "
            "attempt rows. Used by the in-cluster RL controller."
        ),
    )
    p.add_argument(
        "--rl-window-keep-sec", type=float, default=60.0,
        help=(
            "How many seconds of attempt rows to keep in the ring. Default "
            "60 s (longest configured observation_window_sec + margin)."
        ),
    )
    return p.parse_args()


def main():
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    sys.exit(main())
