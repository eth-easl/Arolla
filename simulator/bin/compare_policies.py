#!/usr/bin/env python3
"""
Compare server-side retry control policies across multiple metrics.

Runs each policy-*.yaml config, computes per-policy metrics during the failure
period, and generates grouped bar charts for comparison.

Usage:
    cd simulator
    python bin/compare_policies.py experiments/yaml/snowflake/ -o results/policy_comparison
    python bin/compare_policies.py experiments/yaml/snowflake/policy-*.yaml -o results/policy_comparison
    python bin/compare_policies.py experiments/yaml/snowflake/ --skip-run -o results/policy_comparison
"""

import argparse
import json
import sys
import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

# Add bin/ and project root to path
script_dir = Path(__file__).parent.resolve()
sys.path.insert(0, str(script_dir))
sys.path.insert(0, str(script_dir.parent))

from workflow import run_workflow
from plotting.core.data import load_and_aggregate, get_time_range

# Client colors matching plot_all.py
CLIENT_COLORS = {
    'three_retries': 'cornflowerblue',
    'exponential_backoff_jitter': 'green',
    'circuit_breaker': 'purple',
    'retry_budget': 'orange',
    'no_retries': 'grey',
}

CLIENT_LABELS = {
    'three_retries': 'Fixed Retry',
    'exponential_backoff_jitter': 'Exp. Backoff',
    'circuit_breaker': 'Circuit Breaker',
    'retry_budget': 'Retry Budget',
}

POLICY_LABELS = {
    'policy-rb': 'Retry Budget',
    'policy-cb': 'Circuit Breaker',
    'policy-aimd': 'Adaptive Retry Budget',
    'policy-arolla': 'Arolla',
}

# Fixed display order for policies (x-axis)
POLICY_ORDER = [
    'policy-cb',
    'policy-rb',
    'policy-aimd',
    'policy-arolla',
]

# Fixed display order for clients
CLIENT_ORDER = [
    'three_retries',
    'exponential_backoff_jitter',
    'circuit_breaker',
    'retry_budget',
]


def detect_failure_period(result_dir: Path):
    """Auto-detect failure period from fault_events.json."""
    fault_file = result_dir / "fault_events.json"
    if fault_file.exists():
        with open(fault_file) as f:
            events = json.load(f)
        for ev in events:
            if ev.get("event_type") == "partial_failure":
                return ev["start_time_s"], ev["end_time_s"]
    return None, None


def compute_metrics(df_by_client, clients, fail_start, fail_end):
    """Compute comparison metrics for one policy run.

    Returns dict with:
        - amplification: retries / root_requests during failure
        - retry_efficiency: successful retries / total retries during failure
        - per_client_sr: {client_name: success_rate} during failure
        - avg_sr: average success rate across clients
        - fairness_gap: max - min per-client success rate
        - jains_index: Jain's fairness index of per-client success rates
    """
    df_fail = df_by_client[
        (df_by_client['timepoint'] >= fail_start) &
        (df_by_client['timepoint'] <= fail_end)
    ]

    # Aggregate metrics across all clients
    total_retries = df_fail['retries'].sum()
    total_root = df_fail['root_requests'].sum()
    total_failure_retry = df_fail['failure_retry'].sum()

    amplification = total_retries / total_root if total_root > 0 else 0.0
    retry_eff = ((total_retries - total_failure_retry) / total_retries * 100
                 if total_retries > 0 else 0.0)

    # Per-client metrics during failure
    per_client_sr = {}
    per_client_amp = {}
    per_client_retry_eff = {}
    for client in clients:
        sub = df_fail[df_fail['base_client'] == client]
        succ = sub['success_root'].sum()
        fail = sub['failure_root'].sum()
        total = succ + fail
        per_client_sr[client] = (succ / total * 100) if total > 0 else 0.0

        c_retries = sub['retries'].sum()
        c_root = sub['root_requests'].sum()
        c_fail_retry = sub['failure_retry'].sum()
        per_client_amp[client] = c_retries / c_root if c_root > 0 else 0.0
        per_client_retry_eff[client] = (
            (c_retries - c_fail_retry) / c_retries * 100
            if c_retries > 0 else 0.0
        )

    sr_values = list(per_client_sr.values())
    avg_sr = np.mean(sr_values) if sr_values else 0.0
    fairness_gap = max(sr_values) - min(sr_values) if sr_values else 0.0

    # Jain's fairness index: (sum(x))^2 / (n * sum(x^2))
    n = len(sr_values)
    if n > 0 and sum(x ** 2 for x in sr_values) > 0:
        jains = (sum(sr_values) ** 2) / (n * sum(x ** 2 for x in sr_values))
    else:
        jains = 0.0

    return {
        'amplification': amplification,
        'retry_efficiency': retry_eff,
        'per_client_sr': per_client_sr,
        'per_client_amp': per_client_amp,
        'per_client_retry_eff': per_client_retry_eff,
        'avg_sr': avg_sr,
        'fairness_gap': fairness_gap,
        'jains_index': jains,
    }


def run_experiments(yaml_files, output_base, workers=4):
    """Run all policy experiments, return {policy_name: result_dir}."""
    results = {}

    def _run_one(yaml_path):
        name = yaml_path.stem
        ret = run_workflow(
            str(yaml_path),
            output_base=str(output_base),
            verbose=False,
            plot=True,
            use_timestamp_subdir=False,
        )
        return name, ret

    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_one, yf): yf.stem
            for yf in yaml_files
        }
        for future in as_completed(futures):
            name = futures[future]
            try:
                policy_name, ret = future.result()
                if ret == 0:
                    results[policy_name] = output_base / policy_name
                    print(f"  {policy_name}: done")
                else:
                    print(f"  {policy_name}: FAILED (exit code {ret})")
            except Exception as e:
                print(f"  {name}: ERROR — {e}")

    return results


def plot_amplification(metrics_by_policy, output_dir, fmt='pdf'):
    """Grouped bar chart: per-client amplification for each policy."""
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    first = metrics_by_policy[policies[0]]
    available = set(first['per_client_amp'].keys())
    clients = [c for c in CLIENT_ORDER if c in available]

    x = np.arange(len(policies))
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 6))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_amp'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        ax.bar(x + offset, values, width, label=label, color=color, edgecolor='white')

    ax.set_xlabel('Server-Side Policy')
    ax.set_ylabel('Retry Amplification (retries / root requests)')
    ax.set_title('Request Amplification During Failure')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    ax.legend(loc='upper right', frameon=False)
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    out = output_dir / f'amplification.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_retry_efficiency(metrics_by_policy, output_dir, fmt='pdf'):
    """Grouped bar chart: per-client retry efficiency for each policy."""
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    first = metrics_by_policy[policies[0]]
    available = set(first['per_client_retry_eff'].keys())
    clients = [c for c in CLIENT_ORDER if c in available]

    x = np.arange(len(policies))
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 6))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_retry_eff'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        ax.bar(x + offset, values, width, label=label, color=color, edgecolor='white')

    ax.set_xlabel('Server-Side Policy')
    ax.set_ylabel('Retry Efficiency (%)')
    ax.set_title('Retry Efficiency During Failure')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    all_vals = [v for p in policies for v in metrics_by_policy[p]['per_client_retry_eff'].values()]
    ax.set_ylim(0, max(max(all_vals) * 1.3, 10) if all_vals else 10)
    ax.legend(loc='upper right', frameon=False)
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    out = output_dir / f'retry_efficiency.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_success_rate(metrics_by_policy, output_dir, fmt='pdf'):
    """Grouped bar chart: per-client success rate for each policy."""
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    first = metrics_by_policy[policies[0]]
    available = set(first['per_client_sr'].keys())
    clients = [c for c in CLIENT_ORDER if c in available]

    x = np.arange(len(policies))
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 6))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_sr'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        bars = ax.bar(x + offset, values, width, label=label, color=color, edgecolor='white')

    ax.set_xlabel('Server-Side Policy')
    ax.set_ylabel('Success Rate (%)')
    ax.set_title('Per-Client Success Rate During Failure')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    ax.set_ylim(0, 105)
    ax.legend(loc='upper right', frameon=False)
    ax.grid(axis='y', alpha=0.3)

    plt.tight_layout()
    out = output_dir / f'success_rate.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_fairness(metrics_by_policy, output_dir, fmt='pdf'):
    """Bar chart: fairness gap per policy."""
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    labels = [POLICY_LABELS.get(p, p) for p in policies]
    gaps = [metrics_by_policy[p]['fairness_gap'] for p in policies]

    fig, ax = plt.subplots(figsize=(8, 5))

    bars = ax.bar(labels, gaps, color='#d62728', width=0.5, edgecolor='white')
    ax.set_ylabel('Fairness Gap (pp)')
    ax.set_title('Fairness Gap (max - min SR)\nLower is better')
    ax.grid(axis='y', alpha=0.3)
    for bar, val in zip(bars, gaps):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                f'{val:.1f}', ha='center', va='bottom', fontsize=11)

    plt.tight_layout()
    out = output_dir / f'fairness.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def print_summary(metrics_by_policy):
    """Print a formatted summary table."""
    header = f"{'Policy':<20} {'Amplification':>14} {'Retry Eff (%)':>14} {'Avg SR (%)':>11} {'Gap (pp)':>9} {'Jains':>7}"
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for policy in POLICY_ORDER:
        if policy not in metrics_by_policy:
            continue
        m = metrics_by_policy[policy]
        label = POLICY_LABELS.get(policy, policy)
        print(f"{label:<20} {m['amplification']:>14.3f} {m['retry_efficiency']:>14.1f} "
              f"{m['avg_sr']:>11.1f} {m['fairness_gap']:>9.1f} {m['jains_index']:>7.3f}")
    print("=" * len(header))


def save_summary_csv(metrics_by_policy, output_dir):
    """Save summary as CSV."""
    rows = []
    for policy in POLICY_ORDER:
        if policy not in metrics_by_policy:
            continue
        m = metrics_by_policy[policy]
        row = {
            'policy': POLICY_LABELS.get(policy, policy),
            'amplification': m['amplification'],
            'retry_efficiency_pct': m['retry_efficiency'],
            'avg_success_rate_pct': m['avg_sr'],
            'fairness_gap_pp': m['fairness_gap'],
            'jains_index': m['jains_index'],
        }
        # Add per-client success rates
        for client, sr in m['per_client_sr'].items():
            row[f'sr_{client}'] = sr
        rows.append(row)

    df = pd.DataFrame(rows)
    out = output_dir / 'summary.csv'
    df.to_csv(out, index=False)
    print(f"  Saved {out}")


def main():
    parser = argparse.ArgumentParser(
        description='Compare server-side retry control policies',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  cd simulator
  python bin/compare_policies.py experiments/yaml/snowflake/
  python bin/compare_policies.py experiments/yaml/snowflake/policy-*.yaml -o results/policy_comparison
  python bin/compare_policies.py experiments/yaml/snowflake/ --skip-run
        '''
    )
    parser.add_argument('input', nargs='+',
                        help='Directory (globs policy-*.yaml) or list of YAML files')
    parser.add_argument('-o', '--output', default='results/policy_comparison',
                        help='Output directory for plots and summary')
    parser.add_argument('--failure-start', type=float, default=None,
                        help='Failure period start (seconds). Auto-detected if omitted.')
    parser.add_argument('--failure-end', type=float, default=None,
                        help='Failure period end (seconds). Auto-detected if omitted.')
    parser.add_argument('--format', default='pdf', choices=['pdf', 'png', 'svg'],
                        help='Plot format (default: pdf)')
    parser.add_argument('--skip-run', action='store_true',
                        help='Skip simulation, just re-analyze existing results')
    parser.add_argument('--workers', type=int, default=4,
                        help='Parallel workers for running experiments')

    args = parser.parse_args()
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Resolve YAML files
    yaml_files = []
    for inp in args.input:
        p = Path(inp)
        if p.is_dir():
            yaml_files.extend(sorted(p.glob('policy-*.yaml')))
        elif p.exists() and p.suffix == '.yaml':
            yaml_files.append(p)
        else:
            print(f"Warning: skipping {inp}")

    if not yaml_files:
        print("Error: no policy-*.yaml files found")
        return 1

    print(f"Found {len(yaml_files)} policy configs:")
    for yf in yaml_files:
        print(f"  - {yf.name}")

    # Step 1: Run experiments
    # Use a shared results dir so --skip-run can find them
    results_base = output_dir / "runs"

    if not args.skip_run:
        print(f"\nRunning {len(yaml_files)} experiments...")
        result_dirs = run_experiments(yaml_files, results_base, workers=args.workers)
    else:
        print("\nSkipping simulation (--skip-run), loading existing results...")
        result_dirs = {}
        for yf in yaml_files:
            name = yf.stem
            d = results_base / name
            if (d / "output.csv").exists():
                result_dirs[name] = d
            else:
                print(f"  Warning: no results for {name} at {d}")

    if not result_dirs:
        print("Error: no results to analyze")
        return 1

    # Step 2: Compute metrics
    print(f"\nAnalyzing {len(result_dirs)} policy results...")

    # Detect failure period from first result
    fail_start = args.failure_start
    fail_end = args.failure_end
    if fail_start is None or fail_end is None:
        first_dir = next(iter(result_dirs.values()))
        auto_start, auto_end = detect_failure_period(first_dir)
        if auto_start is not None:
            fail_start = fail_start or auto_start
            fail_end = fail_end or auto_end
            print(f"  Auto-detected failure period: {fail_start}s — {fail_end}s")
        else:
            fail_start = fail_start or 20.0
            fail_end = fail_end or 40.0
            print(f"  Using default failure period: {fail_start}s — {fail_end}s")

    metrics_by_policy = {}
    for policy_name, result_dir in sorted(result_dirs.items()):
        csv_path = result_dir / "output.csv"
        df_by_client, df_global, clients = load_and_aggregate(str(csv_path))
        metrics = compute_metrics(df_by_client, clients, fail_start, fail_end)
        metrics_by_policy[policy_name] = metrics

    # Step 3: Print summary
    print_summary(metrics_by_policy)

    # Step 4: Generate plots
    print(f"\nGenerating comparison plots in {output_dir}/...")

    # Set font sizes for publication-quality
    plt.rcParams.update({
        'font.size': 14,
        'axes.titlesize': 16,
        'axes.labelsize': 14,
        'xtick.labelsize': 12,
        'ytick.labelsize': 12,
    })

    plot_amplification(metrics_by_policy, output_dir, fmt=args.format)
    plot_retry_efficiency(metrics_by_policy, output_dir, fmt=args.format)
    plot_success_rate(metrics_by_policy, output_dir, fmt=args.format)
    plot_fairness(metrics_by_policy, output_dir, fmt=args.format)
    save_summary_csv(metrics_by_policy, output_dir)

    print(f"\nDone. All outputs in {output_dir}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
