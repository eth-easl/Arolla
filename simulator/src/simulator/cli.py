"""Minimal CLI shim for the ``ms-sim`` console entrypoint."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def main() -> int:
    """Forward to the existing workflow scripts in ``simulator/bin``."""
    parser = argparse.ArgumentParser(
        prog="ms-sim",
        description="Convenience wrapper for simulator scripts.",
    )
    parser.add_argument(
        "--workflow",
        action="store_true",
        help="Run bin/workflow.py (default command if no mode is selected).",
    )
    parser.add_argument(
        "--run-experiment",
        action="store_true",
        help="Run bin/run_experiment.py instead of workflow.py.",
    )
    parser.add_argument(
        "args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to the selected script (prefix with -- if needed).",
    )
    ns = parser.parse_args()

    if ns.workflow and ns.run_experiment:
        parser.error("Choose at most one of --workflow or --run-experiment")

    repo_root = Path(__file__).resolve().parents[2]
    bin_dir = repo_root / "bin"
    script = bin_dir / ("run_experiment.py" if ns.run_experiment else "workflow.py")
    if not script.exists():
        print(f"Error: script not found: {script}", file=sys.stderr)
        return 1

    forwarded = list(ns.args)
    if forwarded and forwarded[0] == "--":
        forwarded = forwarded[1:]

    cmd = [sys.executable, str(script), *forwarded]
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
