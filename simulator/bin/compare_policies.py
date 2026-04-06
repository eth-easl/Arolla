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

# Client colors — LaTeX xcolor definitions at !55 fill (55% color + 45% white)
# retryred=(200,50,50), goodputgreen=(34,139,34),
# calmblue=(50,100,180), warnorg=(230,140,20)
CLIENT_COLORS = {
    'three_retries': '#E18E8E',          # retryred!55
    'exponential_backoff_jitter': '#85BF85',  # goodputgreen!55
    'circuit_breaker': '#8EAAD6',        # calmblue!55
    'retry_budget': '#F1C07E',           # warnorg!55
    'no_retries': '#b0b0b0',
}

# Base policy colors (full saturation, for policy-level plots)
POLICY_COLORS_BASE = {
    'policy-cb': '#C83232',      # retryred
    'policy-rb': '#E68C14',      # warnorg
    'policy-aimd': '#3264B4',    # calmblue
    'policy-arolla': '#228B22',  # goodputgreen
}

# Policy colors at !55 fill (for bar charts)
POLICY_COLORS_FILL = {
    'policy-cb': '#E18E8E',      # retryred!55
    'policy-rb': '#F1C07E',      # warnorg!55
    'policy-aimd': '#8EAAD6',    # calmblue!55
    'policy-arolla': '#85BF85',  # goodputgreen!55
}

CLIENT_LABELS = {
    'three_retries': 'Client 1: Fixed Retry',
    'exponential_backoff_jitter': 'Client 2: Backoff (Jitter)',
    'circuit_breaker': 'Client 3: Circuit Breaker',
    'retry_budget': 'Client 4: Retry Budget',
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


def load_admission_stats(result_dir):
    """Load per-tenant retry admission stats from admission_stats.json.

    Returns dict mapping base_client -> {'requested': N, 'admitted': M},
    or empty dict if file not found.
    """
    stats_file = result_dir / "admission_stats.json"
    if not stats_file.exists():
        return {}
    with open(stats_file) as f:
        raw = json.load(f)
    # Flatten: raw is {service_name: {tenant_id: {requested, admitted}}}
    # Aggregate across services and group by base client name
    per_base = {}
    for svc_stats in raw.values():
        for tenant_id, counts in svc_stats.items():
            # tenant_id is e.g. "three_retries.0" or "three_retries"
            parts = tenant_id.rsplit('.', 1)
            base = parts[0] if len(parts) == 2 and parts[1].isdigit() else tenant_id
            if base not in per_base:
                per_base[base] = {'requested': 0, 'admitted': 0}
            per_base[base]['requested'] += counts.get('requested', 0)
            per_base[base]['admitted'] += counts.get('admitted', 0)
    return per_base


def compute_metrics(df_by_client, clients, fail_start, fail_end,
                    admission_stats=None):
    """Compute comparison metrics for one policy run.

    Args:
        admission_stats: Optional dict from load_admission_stats().
            If provided, uses server-admitted retries for fairness.

    Returns dict with:
        - amplification: retries / root_requests during failure
        - retry_efficiency: successful retries / total retries during failure
        - per_client_sr: {client_name: success_rate} during failure
        - avg_sr: average success rate across clients
        - fairness_gap: max - min per-client success rate
        - retry_fairness_gap: gap in server-admitted retry share
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
    total_success_retry = total_retries - total_failure_retry

    # Per-client metrics during failure
    per_client_sr = {}
    per_client_amp = {}
    per_client_retry_eff = {}
    per_client_retries = {}
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
        per_client_retries[client] = c_retries

    sr_values = list(per_client_sr.values())
    avg_sr = np.mean(sr_values) if sr_values else 0.0
    fairness_gap = max(sr_values) - min(sr_values) if sr_values else 0.0

    # Jain's fairness index: (sum(x))^2 / (n * sum(x^2))
    n = len(sr_values)
    if n > 0 and sum(x ** 2 for x in sr_values) > 0:
        jains = (sum(sr_values) ** 2) / (n * sum(x ** 2 for x in sr_values))
    else:
        jains = 0.0

    # Server-admitted retries (from admission_stats or fallback to CSV)
    if admission_stats:
        per_client_admitted = {c: admission_stats.get(c, {}).get('admitted', 0)
                               for c in clients}
    else:
        per_client_admitted = per_client_retries

    total_admitted = sum(per_client_admitted.values())

    # Retry efficiency: successful retries / admitted retries
    retry_eff = (total_success_retry / total_admitted * 100
                 if total_admitted > 0 else 0.0)
    for client in clients:
        c_admitted = per_client_admitted.get(client, 0)
        c_success_retry = per_client_retries[client] - (
            df_fail[df_fail['base_client'] == client]['failure_retry'].sum()
        )
        per_client_retry_eff[client] = (
            c_success_retry / c_admitted * 100 if c_admitted > 0 else 0.0
        )

    # Retry allocation fairness
    if total_admitted > 0:
        retry_shares = {c: r / total_admitted * 100
                        for c, r in per_client_admitted.items()}
        retry_fairness_gap = max(retry_shares.values()) - min(retry_shares.values())
    else:
        retry_shares = {c: 0.0 for c in clients}
        retry_fairness_gap = 0.0

    return {
        'amplification': amplification,
        'retry_efficiency': retry_eff,
        'per_client_sr': per_client_sr,
        'per_client_amp': per_client_amp,
        'per_client_retry_eff': per_client_retry_eff,
        'per_client_retries': per_client_retries,
        'per_client_admitted': per_client_admitted,
        'per_client_retry_shares': retry_shares,
        'avg_sr': avg_sr,
        'fairness_gap': fairness_gap,
        'retry_fairness_gap': retry_fairness_gap,
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

    x = np.arange(len(policies)) * 1.4   # wider spacing between groups
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 6))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_amp'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        bars = ax.bar(x + offset, values, width, label=label, color=color,
                       edgecolor='white', linewidth=0.5)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.01,
                    f'{val:.2f}', ha='center', va='bottom', fontsize=11,
                    fontweight='medium')

    # Light dashed separators between policy groups
    for mid in (x[:-1] + x[1:]) / 2:
        ax.axvline(mid, color='grey', linewidth=0.5, linestyle=(0, (8, 6)), alpha=0.4)

    ax.set_ylabel('Amplification (retries / root requests)')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.15),
              ncol=2, frameon=False, fontsize=14)

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

    x = np.arange(len(policies)) * 1.4   # wider spacing between groups
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 5))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_retry_eff'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        bars = ax.bar(x + offset, values, width, label=label, color=color,
                       edgecolor='white', linewidth=0.5)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    f'{val:.1f}', ha='center', va='bottom', fontsize=11,
                    fontweight='medium')

    # Light dashed separators between policy groups
    for mid in (x[:-1] + x[1:]) / 2:
        ax.axvline(mid, color='grey', linewidth=0.5, linestyle=(0, (8, 6)), alpha=0.4)

    ax.set_ylabel('Retry Efficiency (%)')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    all_vals = [v for p in policies for v in metrics_by_policy[p]['per_client_retry_eff'].values()]
    ax.set_ylim(0, max(max(all_vals) * 1.3, 10) if all_vals else 10)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.15),
              ncol=2, frameon=False, fontsize=14)

    plt.tight_layout()
    out = output_dir / f'retry_efficiency.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_success_rate(metrics_by_policy, output_dir, fmt='pdf'):
    """Grouped bar chart: per-client success rate for each policy.

    Style matches reference figure: zoomed y-axis, value labels on bars,
    clean background with minimal spines.
    """
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    first = metrics_by_policy[policies[0]]
    available = set(first['per_client_sr'].keys())
    clients = [c for c in CLIENT_ORDER if c in available]

    x = np.arange(len(policies)) * 1.4   # wider spacing between groups
    n_clients = len(clients)
    width = 0.8 / n_clients

    # Collect all values to set zoomed y-axis
    all_vals = []
    for p in policies:
        for c in clients:
            all_vals.append(metrics_by_policy[p]['per_client_sr'].get(c, 0))

    fig, ax = plt.subplots(figsize=(10, 5))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_sr'].get(client, 0) for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        bars = ax.bar(x + offset, values, width, label=label, color=color,
                       edgecolor='white', linewidth=0.5)

        # Value labels on each bar
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.2,
                    f'{val:.1f}', ha='center', va='bottom', fontsize=11,
                    fontweight='medium')

    # Light dashed separators between policy groups
    for mid in (x[:-1] + x[1:]) / 2:
        ax.axvline(mid, color='grey', linewidth=0.5, linestyle=(0, (8, 6)), alpha=0.4)

    ax.set_ylabel('Success Rate (%)')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])

    # Zoomed y-axis to highlight differences (like reference figure)
    ymin = max(0, min(all_vals) - 3)
    ymax = max(all_vals) + 3
    ax.set_ylim(ymin, ymax)

    # Clean styling: remove top/right spines, light grid
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)

    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.15),
              ncol=2, frameon=False, fontsize=14)

    plt.tight_layout()
    out = output_dir / f'success_rate.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_success_rate_avg(metrics_by_policy, output_dir, fmt='pdf'):
    """Single bar per policy: average success rate across all clients."""
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    labels = [POLICY_LABELS.get(p, p) for p in policies]
    avg_srs = [metrics_by_policy[p]['avg_sr'] for p in policies]
    colors = [POLICY_COLORS_FILL.get(p, 'gray') for p in policies]

    x = np.arange(len(policies))
    fig, ax = plt.subplots(figsize=(10, 6))

    bars = ax.bar(x, avg_srs, width=0.55, color=colors,
                  edgecolor='white', linewidth=0.5)

    for bar, val in zip(bars, avg_srs):
        ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.15,
                f'{val:.1f}', ha='center', va='bottom', fontsize=13,
                fontweight='bold')

    # Zoomed y-axis
    ymin = max(0, min(avg_srs) - 3)
    ymax = max(avg_srs) + 3
    ax.set_ylim(ymin, ymax)

    ax.set_xticks(x)
    ax.set_xticklabels(labels, ha='center')
    ax.set_title('Success Rate During Failure Phase', fontweight='bold')
    ax.set_ylabel('Success Rate (%)')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)

    plt.tight_layout()
    out = output_dir / f'success_rate_avg.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def plot_fairness(metrics_by_policy, output_dir, fmt='pdf'):
    """Grouped bar chart: per-client retry share for each policy.

    Shows retry allocation fairness — the share of total retries each client
    received during the failure period. Equal shares (25% each with 4 clients)
    indicates perfect fairness.
    """
    policies = [p for p in POLICY_ORDER if p in metrics_by_policy]
    first = metrics_by_policy[policies[0]]
    available = set(first['per_client_retry_shares'].keys())
    clients = [c for c in CLIENT_ORDER if c in available]

    x = np.arange(len(policies)) * 1.4   # wider spacing between groups
    n_clients = len(clients)
    width = 0.8 / n_clients

    fig, ax = plt.subplots(figsize=(10, 6))

    for i, client in enumerate(clients):
        values = [metrics_by_policy[p]['per_client_retry_shares'].get(client, 0)
                  for p in policies]
        offset = (i - n_clients / 2 + 0.5) * width
        color = CLIENT_COLORS.get(client, f'C{i}')
        label = CLIENT_LABELS.get(client, client)
        bars = ax.bar(x + offset, values, width, label=label, color=color,
                       edgecolor='white', linewidth=0.5)
        for bar, val in zip(bars, values):
            ax.text(bar.get_x() + bar.get_width() / 2, bar.get_height() + 0.3,
                    f'{val:.1f}', ha='center', va='bottom', fontsize=11,
                    fontweight='medium')

    # Light dashed separators between policy groups
    for mid in (x[:-1] + x[1:]) / 2:
        ax.axvline(mid, color='grey', linewidth=0.5, linestyle=(0, (8, 6)), alpha=0.4)

    ax.set_ylabel('Retry Share (%)')
    ax.set_xticks(x)
    ax.set_xticklabels([POLICY_LABELS.get(p, p) for p in policies])
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)
    ax.legend(loc='upper center', bbox_to_anchor=(0.5, 1.15),
              ncol=2, frameon=False, fontsize=14)

    plt.tight_layout()
    out = output_dir / f'fairness.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


def print_summary(metrics_by_policy):
    """Print a formatted summary table."""
    header = f"{'Policy':<20} {'Amplification':>14} {'Retry Eff (%)':>14} {'Avg SR (%)':>11} {'SR Gap':>8} {'Retry Gap':>10} {'Jains':>7}"
    print("\n" + "=" * len(header))
    print(header)
    print("-" * len(header))
    for policy in POLICY_ORDER:
        if policy not in metrics_by_policy:
            continue
        m = metrics_by_policy[policy]
        label = POLICY_LABELS.get(policy, policy)
        print(f"{label:<20} {m['amplification']:>14.3f} {m['retry_efficiency']:>14.1f} "
              f"{m['avg_sr']:>11.1f} {m['fairness_gap']:>8.1f} "
              f"{m['retry_fairness_gap']:>10.1f} {m['jains_index']:>7.3f}")
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
            'retry_fairness_gap_pp': m['retry_fairness_gap'],
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
        admission = load_admission_stats(result_dir)
        metrics = compute_metrics(df_by_client, clients, fail_start, fail_end,
                                  admission_stats=admission)
        metrics_by_policy[policy_name] = metrics

    # Step 3: Print summary
    print_summary(metrics_by_policy)

    # Step 4: Generate plots
    print(f"\nGenerating comparison plots in {output_dir}/...")

    # Publication-quality: Fira Sans, large fonts
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Fira Sans'],
        'font.size': 18,
        'axes.titlesize': 20,
        'axes.labelsize': 18,
        'xtick.labelsize': 16,
        'ytick.labelsize': 16,
        'legend.fontsize': 14,
    })

    plot_amplification(metrics_by_policy, output_dir, fmt=args.format)
    plot_retry_efficiency(metrics_by_policy, output_dir, fmt=args.format)
    plot_success_rate(metrics_by_policy, output_dir, fmt=args.format)
    plot_success_rate_avg(metrics_by_policy, output_dir, fmt=args.format)
    plot_fairness(metrics_by_policy, output_dir, fmt=args.format)
    save_summary_csv(metrics_by_policy, output_dir)

    print(f"\nDone. All outputs in {output_dir}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
