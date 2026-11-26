"""Check for changes in simulator behaviour.

Usage:
    python simulator/bin/check_scenarios.py OLD_CHECKOUT NEW_CHECKOUT [--jobs N]

Run the scenarios from OLD_CHECKOUT/simulator/experiments/yaml using both
versions of the simulator. Report differences and exit with status 1 if
any results differ.

Compare client attempts, service events, queue state, admission statistics,
random generator states and simulation end times.

Limit workload durations to 10 seconds, reducing them further based on
the request rate. Test at most 60 combinations per parameter sweep.

Depends on simulator internals: client.roots, service._events and service._rng.
"""

import copy
import hashlib
import itertools
import json
import os
import subprocess
import sys
import tempfile
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import yaml

MAX_SECONDS = 10
MAX_REQUESTS = 300_000
MAX_SWEEP_POINTS = 60


def scenario_points(checkout: Path):
    """Expand scenario files and parameter sweeps into (name, config) pairs."""
    sys.path.insert(0, str(checkout / "simulator/bin"))
    import run_sweep  # Use the sweep helpers from the old checkout.

    points = []
    for path in sorted((checkout / "simulator/experiments/yaml").rglob("*.yaml")):
        raw = yaml.safe_load(path.read_text())
        if not isinstance(raw, dict) or "services" not in raw:
            continue
        sweeps = list(run_sweep.find_sweeps(raw).values())
        combinations = list(itertools.product(*[s["values"] for s in sweeps])) if sweeps else [()]
        for index, combination in enumerate(combinations[:MAX_SWEEP_POINTS]):
            config = copy.deepcopy(raw)
            config.pop("sweeps", None)
            for sweep, value in zip(sweeps, combination):
                for dotted_path in sweep["paths"]:
                    run_sweep.set_nested_value(config, dotted_path, value)
            shorten(config)
            points.append((f"{path.relative_to(checkout)}#{index}", config))
    return points


def shorten(config):
    """Limit workload duration based on MAX_SECONDS and MAX_REQUESTS."""
    clients = config.get("clients") or []
    workloads = [client["workload"] for client in clients]
    rps = sum(client["workload"]["base_rps"] * client.get("replicas", 1) for client in clients)
    if config.get("workload"):
        workloads.append(config["workload"])
        rps += config["workload"]["base_rps"]
    seconds = max(1, min(MAX_SECONDS, int(MAX_REQUESTS / max(rps, 1))))
    for workload in workloads:
        workload["duration_s"] = min(workload["duration_s"], seconds)


def run_point(point):
    """Return the scenario name and a hash of its results, or an error message."""
    name, config = point
    sys.setrecursionlimit(1_000_000)  # Accommodate long callback chains.
    from simulator.config.loader import ConfigLoader
    from simulator.config.schema import ExperimentConfig

    try:
        sim, clients, workloads, _, services = ConfigLoader.build_simulation(ExperimentConfig(**config))
        for client, workload in zip(clients, workloads):
            workload.drive(sim, lambda s, c=client: c.start_request(s))
        sim.run()
    except Exception as error:
        return name, f"error: {type(error).__name__}: {error}"

    digest = hashlib.sha256()
    for client in clients:
        digest.update(f"{client.cfg.name}|{len(client.roots)}\n".encode())
        for root in client.roots:
            digest.update(repr([
                (a.interval.begin, a.interval.end, a.success, a.drop_reason.name, a.deadline,
                 a.queue_size_at_end)
                for a in root.attempts
            ]).encode())
    for service_name in sorted(services):
        service = services[service_name]
        digest.update(f"{service_name}|{service.in_flight}|{len(service.queue)}\n".encode())
        digest.update(repr([
            (e[0], e[1], e[2], e[3].name, e[4], e[5], e[6]) for e in service._events
        ]).encode())
        digest.update(repr(service._rng.getstate()).encode())
        digest.update(repr(sorted(service.get_admission_stats().items())).encode())
    digest.update(f"end|{sim.timestep}\n".encode())
    return name, digest.hexdigest()


def results_of(checkout: Path, points_file: str, jobs: int):
    """Run the scenarios in a separate process using the specified simulator version."""
    env = dict(os.environ, PYTHONPATH=str(checkout / "simulator/src"))
    results_file = points_file + ".results"
    command = [sys.executable, __file__, "--run", points_file, results_file, str(jobs)]
    subprocess.run(command, env=env, check=True)
    results = json.load(open(results_file))
    os.unlink(results_file)
    return results


def report(lines):
    print("\n".join(lines))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as file:
            file.write("\n".join(lines) + "\n")


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    if sys.argv[1] == "--run":  # Subprocess entry point
        points = json.load(open(sys.argv[2]))
        with ProcessPoolExecutor(max_workers=int(sys.argv[4])) as pool:
            results = dict(pool.map(run_point, points, chunksize=1))
        json.dump(results, open(sys.argv[3], "w"))
        return

    old, new = Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()
    jobs = int(sys.argv[sys.argv.index("--jobs") + 1]) if "--jobs" in sys.argv else os.cpu_count()
    points = scenario_points(old)
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as file:
        json.dump(points, file)
    before = results_of(old, file.name, jobs)
    after = results_of(new, file.name, jobs)
    os.unlink(file.name)

    changed = [name for name, _ in points if before[name] != after[name]]
    failing = [name for name, _ in points if after[name].startswith("error")]
    lines = [f"### Scenario tests: {len(points)} of {len(changed)} produced different results"]
    lines += [f"- `{name}`" for name in changed]
    if failing:
        lines += ["", f"{len(failing)} scenarios fail with the new code:"]
        lines += [f"- `{name}`: {after[name]}" for name in failing]
    if changed:
        lines += ["", "For intentional changes in simulator behaviour, "
                      "document the affected scenarios and reasons in the pull request."]
    report(lines)
    sys.exit(1 if changed else 0)


if __name__ == "__main__":
    main()
