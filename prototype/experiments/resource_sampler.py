#!/usr/bin/env python3
"""Sample Kubernetes CPU/memory usage during prototype experiments.

Optionally monitors a local process (e.g. the RL controller) via --pid,
requiring the ``psutil`` package.  When psutil is unavailable the --pid flag
is silently ignored so the K8s sampling still works without it.
"""

from __future__ import annotations

import argparse
import csv
import re
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import psutil as _psutil
    _PSUTIL_OK = True
except ImportError:
    _psutil = None  # type: ignore[assignment]
    _PSUTIL_OK = False

STOP = False


def handle_signal(signum: int, frame: Any) -> None:
    del signum, frame
    global STOP
    STOP = True


signal.signal(signal.SIGTERM, handle_signal)
signal.signal(signal.SIGINT, handle_signal)


@dataclass
class Target:
    scope: str
    namespace: str
    pod: str
    node: str


def run_cmd(args: list[str], timeout: float = 5.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)


def kubectl(args: list[str], timeout: float = 5.0) -> str:
    try:
        proc = run_cmd(["kubectl", *args], timeout=timeout)
    except subprocess.TimeoutExpired:
        return ""
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


def parse_cpu_mcores(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    if value.endswith("m"):
        return float(value[:-1])
    return float(value) * 1000.0


def parse_memory_mib(value: str) -> float | None:
    value = value.strip()
    if not value:
        return None
    match = re.match(r"^([0-9.]+)([KMGTE]i?|[kmgte])?$", value)
    if not match:
        return None
    number = float(match.group(1))
    unit = (match.group(2) or "").lower()
    if unit in ("ki", "k"):
        return number / 1024.0
    if unit in ("mi", "m", ""):
        return number
    if unit in ("gi", "g"):
        return number * 1024.0
    if unit in ("ti", "t"):
        return number * 1024.0 * 1024.0
    return None


def first_pod(namespace: str, selector: str) -> tuple[str, str]:
    output = kubectl([
        "-n",
        namespace,
        "get",
        "pod",
        "-l",
        selector,
        "-o",
        "jsonpath={.items[0].metadata.name}{'\\t'}{.items[0].spec.nodeName}",
    ])
    if "\t" not in output:
        return "", ""
    pod, node = output.split("\t", 1)
    return pod.strip(), node.strip()


def discover_targets(app_namespace: str) -> list[Target]:
    targets: list[Target] = []
    for scope, namespace, selector in [
        ("cartservice_pod", app_namespace, "app=cartservice"),
        ("istio_gateway_pod", app_namespace, "gateway.networking.k8s.io/gateway-name=boutique-gateway"),
        ("istiod_pod", "istio-system", "app=istiod"),
    ]:
        pod, node = first_pod(namespace, selector)
        if pod:
            targets.append(Target(scope=scope, namespace=namespace, pod=pod, node=node))
    return targets


def discover_rl_pod(namespace: str, job_name: str) -> Target | None:
    """Discover the in-cluster RL controller pod for *job_name*.

    Returns a Target with scope ``rl_controller_pod`` so its CPU/memory
    flow through the same ``top_pod`` path as the other targets. Returns
    None if the Job hasn't spawned a pod yet (e.g. brief race at startup)
    or if the pod has terminated, so the sampler can recover gracefully
    across pod restarts without raising.
    """
    if not job_name:
        return None
    pod, node = first_pod(namespace, f"job-name={job_name}")
    if not pod:
        return None
    return Target(scope="rl_controller_pod", namespace=namespace, pod=pod, node=node)


def top_pod(namespace: str, pod: str) -> list[tuple[str, float | None, float | None, str]]:
    output = kubectl(["-n", namespace, "top", "pod", pod, "--containers", "--no-headers"], timeout=6)
    rows = []
    for line in output.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        container = parts[1]
        cpu = None
        mem = None
        try:
            cpu = parse_cpu_mcores(parts[2])
            mem = parse_memory_mib(parts[3])
        except ValueError:
            pass
        rows.append((container, cpu, mem, line))
    return rows


def top_node(node: str) -> tuple[float | None, float | None, str]:
    output = kubectl(["top", "node", node, "--no-headers"], timeout=6)
    parts = output.split()
    if len(parts) < 5:
        return None, None, output
    try:
        return parse_cpu_mcores(parts[1]), parse_memory_mib(parts[3]), output
    except ValueError:
        return None, None, output


def write_header(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "timestamp",
            "scope",
            "namespace",
            "pod",
            "container",
            "node",
            "cpu_mcores",
            "memory_mib",
            "raw",
        ])


def sample_local_pid(
    pid: int,
    writer: "csv.writer",
    ts: str,
    proc_cache: dict[int, Any],
) -> None:
    """Append one row for a local process to *writer*.

    CPU is expressed in mcores (1000 m = 1 core).
    Memory is expressed in MiB.
    Uses psutil; silently no-ops if psutil is unavailable or the process died.
    """
    if not _PSUTIL_OK:
        return
    try:
        proc = proc_cache.get(pid)
        if proc is None:
            proc = _psutil.Process(pid)
            # First call to cpu_percent always returns 0.0; prime it.
            proc.cpu_percent(interval=None)
            proc_cache[pid] = proc
            return  # skip first sample — cpu_percent would be 0
        cpu_pct = proc.cpu_percent(interval=None)   # % of one CPU, 0–100*n_cpus
        mem_bytes = proc.memory_info().rss
        cpu_mcores = cpu_pct * 10.0                 # 100 % of 1 core = 1000 m
        mem_mib = mem_bytes / (1024.0 * 1024.0)
        writer.writerow([
            ts,
            "rl_controller_local",
            "",
            f"pid:{pid}",
            proc.name(),
            "",
            f"{cpu_mcores:.3f}",
            f"{mem_mib:.3f}",
            f"pid={pid} cpu={cpu_pct:.1f}% mem_rss={mem_bytes}",
        ])
    except (_psutil.NoSuchProcess, _psutil.AccessDenied, ProcessLookupError):
        proc_cache.pop(pid, None)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", required=True)
    parser.add_argument("--namespace", default="online-boutique")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument(
        "--pid",
        type=int,
        default=None,
        help="Local process PID to sample alongside K8s pods (requires psutil).",
    )
    parser.add_argument(
        "--rl-job-name",
        default="",
        help=(
            "Name of the in-cluster RL controller Job; when set, its pod is "
            "sampled each interval via `kubectl top pod -l job-name=...` and "
            "emitted with scope=rl_controller_pod."
        ),
    )
    args = parser.parse_args()

    if args.pid is not None and not _PSUTIL_OK:
        import sys
        print(
            "[resource_sampler] WARNING: --pid given but psutil is not installed; "
            "local process sampling disabled. Install with: pip install psutil",
            file=sys.stderr,
        )

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_header(out_path)

    proc_cache: dict[int, Any] = {}

    with out_path.open("a", newline="") as f:
        writer = csv.writer(f)
        while not STOP:
            ts = f"{time.time():.6f}"
            targets = discover_targets(args.namespace)
            rl_target = discover_rl_pod(args.namespace, args.rl_job_name)
            if rl_target is not None:
                targets.append(rl_target)
            seen_nodes: set[tuple[str, str]] = set()
            if not targets:
                writer.writerow([ts, "cluster_probe", "", "", "", "", "", "", "target discovery unavailable"])
                f.flush()

            for target in targets:
                pod_rows = top_pod(target.namespace, target.pod)
                if not pod_rows:
                    writer.writerow([ts, target.scope, target.namespace, target.pod, "", target.node, "", "", "top pod unavailable"])
                for container, cpu, mem, raw in pod_rows:
                    writer.writerow([
                        ts,
                        target.scope,
                        target.namespace,
                        target.pod,
                        container,
                        target.node,
                        "" if cpu is None else f"{cpu:.3f}",
                        "" if mem is None else f"{mem:.3f}",
                        raw,
                    ])

                if target.node and (target.scope, target.node) not in seen_nodes:
                    seen_nodes.add((target.scope, target.node))
                    cpu, mem, raw = top_node(target.node)
                    writer.writerow([
                        ts,
                        target.scope.replace("_pod", "_node"),
                        "",
                        "",
                        "",
                        target.node,
                        "" if cpu is None else f"{cpu:.3f}",
                        "" if mem is None else f"{mem:.3f}",
                        raw,
                    ])

            if args.pid is not None:
                sample_local_pid(args.pid, writer, ts, proc_cache)

            f.flush()

            deadline = time.time() + max(args.interval, 0.5)
            while not STOP and time.time() < deadline:
                time.sleep(min(0.25, deadline - time.time()))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
