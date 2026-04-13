#!/usr/bin/env python3
"""
Plot recovery time vs failure characteristics from failure_sweep results.

Usage:
  python3 plot_failure_sweep.py <failure_sweep_root>

Example:
  python3 plot_failure_sweep.py outputs/prototype/failure_sweep/20260413_XXXXXX/post-cart-stress-open
  
Expects subdirectories:
  failure-rate/10/ failure-rate/20/ ... failure-rate/100/
  fault-duration/5/ fault-duration/10/ ... fault-duration/60/
"""
import sys
from pathlib import Path

# Add analyze.py's directory to path so we can import from it.
SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from analyze import plot_recovery_vs_sweep

def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    root = Path(sys.argv[1])

    # Panel (a): failure rate
    fr_dir = root / "failure-rate"
    if fr_dir.exists():
        plot_recovery_vs_sweep(
            fr_dir,
            fr_dir.parent / "recovery-vs-failure-rate.pdf",
            x_label="Failure rate (%)",
            format_x="raw",
        )
    else:
        print(f"[skip] {fr_dir} not found")

    # Panel (b): fault duration
    fd_dir = root / "fault-duration"
    if fd_dir.exists():
        plot_recovery_vs_sweep(
            fd_dir,
            fd_dir.parent / "recovery-vs-fault-duration.pdf",
            x_label="Fault duration (s)",
            format_x="raw",
        )
    else:
        print(f"[skip] {fd_dir} not found")


if __name__ == "__main__":
    main()
