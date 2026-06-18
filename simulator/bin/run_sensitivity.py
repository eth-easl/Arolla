#!/usr/bin/env python3
"""
Sensitivity analysis: sweep each policy's primary tuning knob across failure
severities to demonstrate configuration burden of static policies.

Computes during-failure metrics (amplification, retry efficiency, success rate,
fairness) from per-client timeseries CSVs.

Generates:
  - sensitivity_sr.pdf        — 2×2: success rate during failure
  - sensitivity_amp.pdf       — 2×2: amplification during failure
  - sensitivity_retry_eff.pdf — 2×2: retry efficiency during failure
  - sensitivity_fairness.pdf  — 2×2: fairness gap during failure
  - punchline.pdf             — oracle-tuned static vs default vs adaptive
  - sensitivity_range.pdf     — bar chart: SR range per policy

Usage:
    cd simulator
    python bin/run_sensitivity.py -o results/sensitivity
    python bin/run_sensitivity.py -o results/sensitivity --skip-run
"""

import argparse
import json
import subprocess
import sys
from collections import OrderedDict
from pathlib import Path

import numpy as np
import pandas as pd
import matplotlib.pyplot as plt


# ── Policy definitions ──────────────────────────────────────────────────────

POLICIES = OrderedDict([
    ('cb', {
        'yaml': 'experiments/yaml/sensitivity/sensitivity-cb.yaml',
        'label': 'Circuit Breaker',
        'param_short': 'failure_threshold',
        'param_label': 'failure_threshold',
        'default_value': 0.1,
        'kind': 'static',
    }),
    ('rb', {
        'yaml': 'experiments/yaml/sensitivity/sensitivity-rb.yaml',
        'label': 'Retry Budget',
        'param_short': 'budget_ratio',
        'param_label': 'budget_ratio',
        'default_value': 0.1,
        'kind': 'static',
    }),
    ('aimd', {
        'yaml': 'experiments/yaml/sensitivity/sensitivity-aimd.yaml',
        'label': 'Adaptive Retry Budget',
        'param_short': 'failure_threshold',
        'param_label': 'failure_threshold',
        'default_value': 0.1,
        'kind': 'adaptive',
    }),
    ('arolla', {
        'yaml': 'experiments/yaml/sensitivity/sensitivity-arolla.yaml',
        'label': 'Arolla',
        'param_short': 'alpha',
        'param_label': 'alpha',
        'default_value': 0.1,
        'kind': 'adaptive',
    }),
])

PFAIL_COLORS = {
    0.1: '#2ca02c',   # green — mild
    0.3: '#1f77b4',   # blue
    0.5: '#ff7f0e',   # orange
    0.7: '#d62728',   # red
    0.9: '#9467bd',   # purple — severe
}

PFAIL_LABELS = {
    0.1: '10%', 0.3: '30%', 0.5: '50%', 0.7: '70%', 0.9: '90%',
}

# Policy colors — LaTeX xcolor at !55 fill
POLICY_COLORS = {
    'cb': '#E18E8E',      # retryred!55
    'rb': '#F1C07E',      # warnorg!55
    'aimd': '#8EAAD6',    # calmblue!55
    'arolla': '#85BF85',  # goodputgreen!55
}


# ── Run sweeps ──────────────────────────────────────────────────────────────

def run_sweeps(output_base):
    """Run all 4 policy sweeps via run_sweep.py."""
    for key, policy in POLICIES.items():
        target_dir = output_base / key
        yaml_path = policy['yaml']
        print(f"\n{'=' * 60}")
        print(f"Running sweep: {policy['label']}")
        print(f"  Config: {yaml_path}")
        print(f"  Output: {target_dir}")
        print(f"{'=' * 60}")

        cmd = [
            sys.executable, 'bin/run_sweep.py',
            yaml_path,
            '--target-dir', str(target_dir),
        ]
        ret = subprocess.call(cmd)
        if ret != 0:
            print(f"  WARNING: {policy['label']} sweep failed (exit {ret})")


# ── Load failure-period metrics from by_client CSVs ─────────────────────────

def parse_filename(fname, param_short):
    """Extract (p_fail, param_value) from filename like 'p_fail_0.5_budget_ratio_0.1.csv'."""
    stem = Path(fname).stem
    parts = stem.split(f'_{param_short}_')
    param_val = float(parts[1])
    pfail_val = float(parts[0].replace('p_fail_', ''))
    return pfail_val, param_val


def compute_client_failure_metrics(csv_path, failure_start, failure_end):
    """Compute during-failure metrics for a single client CSV."""
    df = pd.read_csv(csv_path)
    df_fail = df[(df['timepoint'] >= failure_start) & (df['timepoint'] < failure_end)]

    root = df_fail['root_requests'].sum()
    retries = df_fail['retries'].sum()
    succ = df_fail['success_root'].sum()
    fail_root = df_fail['failure_root'].sum()
    fail_retry = df_fail['failure_retry'].sum()

    total = succ + fail_root
    return {
        'amplification': retries / root if root > 0 else 0.0,
        'success_rate': succ / total * 100 if total > 0 else 0.0,
        'retry_efficiency': (retries - fail_retry) / retries * 100 if retries > 0 else 0.0,
        'retries': retries,  # raw count for retry allocation fairness
    }


def load_failure_metrics(output_base, failure_start=20, failure_end=40):
    """
    Load by_client CSVs and compute during-failure metrics for all policies.

    Returns {policy_key: DataFrame} where each DataFrame has columns:
        p_fail, param_value, avg_sr, avg_amp, avg_retry_eff, fairness_gap,
        retry_fairness_gap, sr_<client>, amp_<client>, ...
    """
    all_metrics = {}

    for key, policy in POLICIES.items():
        by_client_dir = output_base / key / 'by_client'
        if not by_client_dir.exists():
            print(f"  WARNING: {by_client_dir} not found, skipping {policy['label']}")
            continue

        param_short = policy['param_short']
        clients = sorted(d.name for d in by_client_dir.iterdir() if d.is_dir())

        # Load admission summary (server-side admission stats per combo)
        admission_path = output_base / key / 'admission_summary.json'
        admission_data = {}
        if admission_path.exists():
            with open(admission_path) as f:
                admission_data = json.load(f)

        # {(p_fail, param_val): {client: metrics_dict}}
        combo_data = {}
        # {(p_fail, param_val): combo_key_str} for admission lookup
        combo_keys = {}

        for client in clients:
            client_dir = by_client_dir / client
            for csv_file in sorted(client_dir.glob('*.csv')):
                try:
                    pfail, param_val = parse_filename(csv_file.name, param_short)
                except (ValueError, IndexError):
                    continue
                metrics = compute_client_failure_metrics(
                    csv_file, failure_start, failure_end)

                combo_key = (pfail, param_val)
                if combo_key not in combo_data:
                    combo_data[combo_key] = {}
                    # CSV filename stem = admission summary key
                    combo_keys[combo_key] = csv_file.stem
                combo_data[combo_key][client] = metrics

        # Aggregate into rows
        rows = []
        for (pfail, param_val), client_data in sorted(combo_data.items()):
            srs = [v['success_rate'] for v in client_data.values()]
            amps = [v['amplification'] for v in client_data.values()]
            res = [v['retry_efficiency'] for v in client_data.values()]
            retry_counts = [v['retries'] for v in client_data.values()]

            n = len(srs)
            jains = (sum(srs) ** 2) / (n * sum(x ** 2 for x in srs)) if n > 0 and sum(x ** 2 for x in srs) > 0 else 0.0

            # Retry allocation fairness from server-admitted retries
            combo_str = combo_keys.get((pfail, param_val), '')
            admission = admission_data.get(combo_str, {})
            if admission:
                # Use server-admitted retries
                admitted_counts = [admission.get(c, {}).get('admitted', 0)
                                   for c in client_data.keys()]
            else:
                # Fallback: use CSV retries (already-executed retries)
                admitted_counts = retry_counts

            total_admitted = sum(admitted_counts)
            if total_admitted > 0:
                retry_shares = [a / total_admitted * 100 for a in admitted_counts]
                retry_fairness = max(retry_shares) - min(retry_shares)
            else:
                retry_fairness = 0.0

            row = {
                'p_fail': pfail,
                'param_value': param_val,
                'avg_sr': np.mean(srs),
                'avg_amp': np.mean(amps),
                'avg_retry_eff': np.mean(res),
                'fairness_gap': max(srs) - min(srs) if srs else 0.0,
                'retry_fairness_gap': retry_fairness,
                'jains_index': jains,
            }
            # Per-client columns
            for client, m in client_data.items():
                row[f'sr_{client}'] = m['success_rate']
                row[f'amp_{client}'] = m['amplification']
                row[f're_{client}'] = m['retry_efficiency']
                row[f'retries_{client}'] = m['retries']
                # Admission stats
                adm = admission.get(client, {})
                row[f'admitted_{client}'] = adm.get('admitted', 0)
                row[f'requested_{client}'] = adm.get('requested', 0)
            rows.append(row)

        df = pd.DataFrame(rows)
        all_metrics[key] = df
        has_admission = bool(admission_data)
        print(f"  {policy['label']}: {len(df)} combos, "
              f"{len(clients)} clients, failure t=[{failure_start},{failure_end})s"
              f"{' (with admission stats)' if has_admission else ''}")

    return all_metrics


# ── Generic 2×2 sensitivity grid ───────────────────────────────────────────

def _plot_grid(metrics, metric_col, ylabel, title, output_path):
    """2×2 grid: x = parameter value, y = metric, lines = p_fail.
    Shares consistent y-axis limits across all subplots."""
    fig, axes = plt.subplots(2, 2, figsize=(12, 9))

    # Compute global y-axis range across all policies
    all_vals = []
    for key in POLICIES:
        df = metrics.get(key)
        if df is not None and not df.empty and metric_col in df.columns:
            all_vals.extend(df[metric_col].values)

    if all_vals:
        ymin = min(all_vals)
        ymax = max(all_vals)
        margin = (ymax - ymin) * 0.08 if ymax > ymin else 1.0
        ylim = (max(0, ymin - margin), ymax + margin)
    else:
        ylim = None

    for idx, (key, policy) in enumerate(POLICIES.items()):
        ax = axes.flatten()[idx]
        df = metrics.get(key)
        if df is None or df.empty:
            ax.text(0.5, 0.5, 'No data', ha='center', va='center',
                    transform=ax.transAxes)
            ax.set_title(policy['label'])
            continue

        param_vals = sorted(df['param_value'].unique())

        for pfail in sorted(df['p_fail'].unique()):
            sub = df[df['p_fail'] == pfail].sort_values('param_value')
            color = PFAIL_COLORS.get(pfail, 'gray')
            label = f'p_fail = {PFAIL_LABELS.get(pfail, str(pfail))}'
            ax.plot(range(len(param_vals)),
                    sub[metric_col].values,
                    marker='o', linewidth=2, markersize=6,
                    label=label, color=color)

        ax.set_xticks(range(len(param_vals)))
        ax.set_xticklabels([str(v) for v in param_vals])
        ax.set_xlabel(policy['param_label'])
        ax.set_ylabel(ylabel)
        ax.set_title(policy['label'])
        ax.legend(fontsize=8, loc='best')
        ax.grid(True, alpha=0.3)
        if ylim:
            ax.set_ylim(ylim)

        # Mark default value
        if policy['default_value'] in param_vals:
            def_idx = param_vals.index(policy['default_value'])
            ax.axvline(def_idx, color='gray', linestyle='--', alpha=0.4)

    fig.suptitle(title, fontsize=14, y=1.01)
    plt.tight_layout()
    plt.savefig(output_path, bbox_inches='tight')
    plt.close()
    print(f"  Saved {output_path}")


def plot_sensitivity_sr(metrics, output_dir, fmt='pdf'):
    _plot_grid(metrics, 'avg_sr', 'Success Rate (%)',
               'Success Rate During Failure — Parameter Sensitivity',
               output_dir / f'sensitivity_sr.{fmt}')


def plot_sensitivity_amp(metrics, output_dir, fmt='pdf'):
    _plot_grid(metrics, 'avg_amp', 'Amplification (retries / root)',
               'Retry Amplification During Failure — Parameter Sensitivity',
               output_dir / f'sensitivity_amp.{fmt}')


def plot_sensitivity_retry_eff(metrics, output_dir, fmt='pdf'):
    _plot_grid(metrics, 'avg_retry_eff', 'Retry Efficiency (%)',
               'Retry Efficiency During Failure — Parameter Sensitivity',
               output_dir / f'sensitivity_retry_eff.{fmt}')


def plot_sensitivity_fairness(metrics, output_dir, fmt='pdf'):
    _plot_grid(metrics, 'retry_fairness_gap', 'Retry Allocation Fairness (pp)',
               'Retry Allocation Fairness During Failure — Parameter Sensitivity',
               output_dir / f'sensitivity_fairness.{fmt}')


# ── Punchline — oracle vs default vs adaptive ──────────────────────────────

def plot_punchline(metrics, output_dir, fmt='pdf'):
    """
    For each failure severity, compare:
      - Best-tuned RB (oracle): best budget_ratio per p_fail
      - Default RB (budget_ratio=0.1)
      - Worst-tuned RB
      - AIMD at default, Arolla at default
    Shaded band shows configuration burden.
    """
    fig, ax = plt.subplots(figsize=(10, 6))
    pfail_values = sorted(PFAIL_COLORS.keys())

    # RB band
    rb_df = metrics.get('rb')
    if rb_df is not None:
        rb_default = POLICIES['rb']['default_value']
        best_sr, worst_sr, default_sr = [], [], []
        for pfail in pfail_values:
            sub = rb_df[rb_df['p_fail'] == pfail]
            if sub.empty:
                best_sr.append(np.nan); worst_sr.append(np.nan); default_sr.append(np.nan)
                continue
            best_sr.append(sub['avg_sr'].max())
            worst_sr.append(sub['avg_sr'].min())
            def_row = sub[sub['param_value'] == rb_default]
            default_sr.append(def_row['avg_sr'].values[0] if len(def_row) else np.nan)

        x = range(len(pfail_values))
        ax.fill_between(x, worst_sr, best_sr, alpha=0.15, color='#d62728',
                        label='RB range (worst–best tuning)')
        ax.plot(x, best_sr, '--', color='#d62728', alpha=0.6, linewidth=1.5,
                label='RB oracle (best per scenario)')
        ax.plot(x, default_sr, '-o', color='#d62728', linewidth=2,
                markersize=7, label='RB default (ratio=0.1)')

    # Adaptive policies at default
    adaptive_styles = {
        'aimd': {'color': '#1f77b4', 'marker': 's'},
        'arolla': {'color': '#2ca02c', 'marker': '^'},
    }
    for key in ('aimd', 'arolla'):
        policy = POLICIES[key]
        df = metrics.get(key)
        if df is None:
            continue
        sr_at_default = []
        for pfail in pfail_values:
            sub = df[(df['p_fail'] == pfail) &
                     (df['param_value'] == policy['default_value'])]
            sr_at_default.append(sub['avg_sr'].values[0] if len(sub) else np.nan)
        style = adaptive_styles[key]
        ax.plot(range(len(pfail_values)), sr_at_default,
                '-' + style['marker'], color=style['color'],
                linewidth=2, markersize=7,
                label=f"{policy['label']} (default)")

    ax.set_xticks(range(len(pfail_values)))
    ax.set_xticklabels([PFAIL_LABELS.get(p, str(p)) for p in pfail_values])
    ax.set_xlabel('Failure Severity (p_fail)')
    ax.set_ylabel('Success Rate During Failure (%)')
    ax.set_title('Configuration Burden: Static vs Adaptive Policies')
    ax.legend(loc='best', fontsize=9)
    ax.grid(True, alpha=0.3)

    plt.tight_layout()
    out = output_dir / f'punchline.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


# ── Sensitivity range bar chart ─────────────────────────────────────────────

def plot_sensitivity_range(metrics, output_dir, fmt='pdf'):
    """Bar chart: SR range across parameter choices, per policy and p_fail."""
    plt.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Fira Sans'],
        'font.size': 22,
        'axes.labelsize': 22,
        'xtick.labelsize': 20,
        'ytick.labelsize': 20,
        'legend.fontsize': 18,
    })

    short_labels = {
        'cb': 'Circuit Breaker', 'rb': 'Retry Budget',
        'aimd': 'Adaptive Retry Budget', 'arolla': 'Arolla',
    }

    pfail_values = sorted(PFAIL_COLORS.keys())
    x = np.arange(len(pfail_values)) * 1.4   # wider spacing
    n_policies = len(POLICIES)
    width = 0.8 / n_policies

    fig, ax = plt.subplots(figsize=(14, 6))

    for i, (key, policy) in enumerate(POLICIES.items()):
        df = metrics.get(key)
        if df is None:
            continue
        ranges = []
        for pfail in pfail_values:
            sub = df[df['p_fail'] == pfail]
            ranges.append(sub['avg_sr'].max() - sub['avg_sr'].min() if len(sub) else 0)
        offset = (i - n_policies / 2 + 0.5) * width
        ax.bar(x + offset, ranges, width,
               label=short_labels.get(key, policy['label']),
               color=POLICY_COLORS.get(key, f'C{i}'),
               edgecolor='white', linewidth=0.5)

    # Light dashed separators between groups
    for mid in (x[:-1] + x[1:]) / 2:
        ax.axvline(mid, color='grey', linewidth=0.5,
                   linestyle=(0, (8, 6)), alpha=0.4)

    ax.set_xticks(x)
    ax.set_xticklabels([PFAIL_LABELS.get(p, str(p)) for p in pfail_values])
    ax.set_xlabel('Failure Rate')
    ax.set_ylabel('Success Rate Range')
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.grid(axis='y', alpha=0.2, linewidth=0.5)
    ax.legend(loc='upper right', frameon=False, fontsize=18)

    plt.tight_layout()
    out = output_dir / f'sensitivity_range.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


# ── Best-tuned comparison bar chart ──────────────────────────────────────────

def plot_best_metrics(metrics, output_dir, fmt='pdf'):
    """
    For each failure rate, find the best parameter value per policy for each
    metric. Show as a 2×2 grouped bar chart comparing all 4 policies at their
    best-tuned setting.

    "Best" means: highest SR, lowest amplification, highest retry efficiency,
    lowest fairness gap.
    """
    # Fira Sans + publication font sizes
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

    pfail_values = sorted(PFAIL_COLORS.keys())
    x = np.arange(len(pfail_values)) * 1.4   # wider spacing between groups
    n_policies = len(POLICIES)
    width = 0.8 / n_policies

    # Short labels for legend
    short_labels = {
        'cb': 'Circuit Breaker', 'rb': 'Retry Budget',
        'aimd': 'Adaptive Retry Budget', 'arolla': 'Arolla',
    }

    metric_defs = [
        ('avg_sr', 'max', 'Success Rate (%)'),
        ('avg_amp', 'min', 'Amplification'),
        ('avg_retry_eff', 'max', 'Retry Efficiency (%)'),
        ('retry_fairness_gap', 'min', 'Retry Allocation Fairness (pp)'),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(14, 8))

    for ax, (metric_col, direction, ylabel) in zip(
            axes.flatten(), metric_defs):

        for i, (key, policy) in enumerate(POLICIES.items()):
            df = metrics.get(key)
            if df is None:
                continue

            best_vals = []
            for pfail in pfail_values:
                sub = df[df['p_fail'] == pfail]
                if sub.empty:
                    best_vals.append(0)
                elif direction == 'max':
                    best_vals.append(sub[metric_col].max())
                else:
                    best_vals.append(sub[metric_col].min())

            offset = (i - n_policies / 2 + 0.5) * width
            bars = ax.bar(x + offset, best_vals, width,
                          label=short_labels.get(key, policy['label']),
                          color=POLICY_COLORS.get(key, f'C{i}'),
                          edgecolor='white', linewidth=0.5)

        # Light dashed separators between failure-rate groups
        for mid in (x[:-1] + x[1:]) / 2:
            ax.axvline(mid, color='grey', linewidth=0.5,
                       linestyle=(0, (8, 6)), alpha=0.4)

        ax.set_xticks(x)
        ax.set_xticklabels([PFAIL_LABELS.get(p, str(p)) for p in pfail_values])
        ax.set_xlabel('Failure Rate')
        ax.set_ylabel(ylabel)
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        ax.grid(axis='y', alpha=0.2, linewidth=0.5)

    # Single shared legend at the bottom
    handles, labels = axes.flatten()[0].get_legend_handles_labels()
    fig.legend(handles, labels, loc='lower center',
               ncol=4, frameon=False, fontsize=16,
               bbox_to_anchor=(0.5, -0.02))

    plt.tight_layout(rect=[0, 0.04, 1, 1])
    out = output_dir / f'best_metrics.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


# ── SR-optimal trade-off plot ────────────────────────────────────────────────

def plot_sr_optimal_tradeoff(metrics, output_dir, fmt='pdf'):
    """
    For each policy and p_fail, pick the parameter that maximises avg_sr,
    then show all 4 metrics at that *same* parameter choice.
    Reveals trade-offs: optimising SR may hurt amplification or fairness.
    """
    pfail_values = sorted(PFAIL_COLORS.keys())
    x = np.arange(len(pfail_values))
    n_policies = len(POLICIES)
    width = 0.8 / n_policies

    # Pre-compute the SR-optimal row for each policy × p_fail
    # {policy_key: {pfail: Series}}
    best_rows = {}
    for key, policy in POLICIES.items():
        df = metrics.get(key)
        if df is None:
            continue
        best_rows[key] = {}
        for pfail in pfail_values:
            sub = df[df['p_fail'] == pfail]
            if sub.empty:
                continue
            idx = sub['avg_sr'].idxmax()
            best_rows[key][pfail] = sub.loc[idx]

    metric_defs = [
        ('avg_sr', 'Success Rate (%)', 'SR-Optimal Success Rate'),
        ('avg_amp', 'Amplification', 'Amplification at SR-Optimal Param'),
        ('avg_retry_eff', 'Retry Efficiency (%)',
         'Retry Efficiency at SR-Optimal Param'),
        ('retry_fairness_gap', 'Retry Alloc. Fairness (pp)',
         'Retry Allocation Fairness at SR-Optimal Param'),
    ]

    fig, axes = plt.subplots(2, 2, figsize=(12, 9))

    for ax, (metric_col, ylabel, subtitle) in zip(axes.flatten(), metric_defs):
        for i, (key, policy) in enumerate(POLICIES.items()):
            if key not in best_rows:
                continue

            vals = []
            params = []
            for pfail in pfail_values:
                row = best_rows[key].get(pfail)
                if row is None:
                    vals.append(0)
                    params.append('?')
                else:
                    vals.append(row[metric_col])
                    params.append(row['param_value'])

            offset = (i - n_policies / 2 + 0.5) * width
            bars = ax.bar(x + offset, vals, width,
                          label=policy['label'],
                          color=POLICY_COLORS.get(key, f'C{i}'),
                          edgecolor='white')

            for bar, val, pv in zip(bars, vals, params):
                val_fmt = (f'{val:.1f}' if metric_col != 'avg_amp'
                           else f'{val:.2f}')
                pv_fmt = f'{pv:g}' if isinstance(pv, float) else str(pv)
                ax.text(bar.get_x() + bar.get_width() / 2,
                        bar.get_height(),
                        f'{val_fmt}\n({pv_fmt})',
                        ha='center', va='bottom', fontsize=5.5)

        ax.set_xticks(x)
        ax.set_xticklabels([PFAIL_LABELS.get(p, str(p)) for p in pfail_values])
        ax.set_xlabel('Failure Severity (p_fail)')
        ax.set_ylabel(ylabel)
        ax.set_title(subtitle, fontsize=11)
        ax.legend(fontsize=7, loc='best')
        ax.grid(axis='y', alpha=0.3)

    fig.suptitle(
        'Trade-offs at SR-Optimal Parameter Choice',
        fontsize=14, y=1.01)
    plt.tight_layout()
    out = output_dir / f'sr_optimal_tradeoff.{fmt}'
    plt.savefig(out, bbox_inches='tight')
    plt.close()
    print(f"  Saved {out}")


# ── Summary tables ──────────────────────────────────────────────────────────

def print_summary(metrics):
    """Print per-policy summary tables for each metric."""
    for key, policy in POLICIES.items():
        df = metrics.get(key)
        if df is None:
            continue

        param_vals = sorted(df['param_value'].unique())
        pfail_vals = sorted(df['p_fail'].unique())

        for metric, label in [('avg_sr', 'Success Rate (%)'),
                               ('avg_amp', 'Amplification'),
                               ('avg_retry_eff', 'Retry Efficiency (%)'),
                               ('fairness_gap', 'SR Fairness Gap (pp)'),
                               ('retry_fairness_gap', 'Retry Alloc. Fairness (pp)')]:
            print(f"\n{'=' * 70}")
            print(f"  {policy['label']} — {label}  (sweep: {policy['param_label']})")
            print(f"{'=' * 70}")

            fmt = '.1f' if metric != 'avg_amp' else '.3f'
            header = f"{'p_fail':>8}"
            for pv in param_vals:
                header += f"  {pv:>8}"
            header += f"  {'Range':>8}"
            print(header)
            print("-" * len(header))

            for pfail in pfail_vals:
                row_str = f"{pfail:>8.1f}"
                sub = df[df['p_fail'] == pfail].set_index('param_value')
                vals = []
                for pv in param_vals:
                    v = sub.loc[pv, metric] if pv in sub.index else 0
                    row_str += f"  {v:>8{fmt}}"
                    vals.append(v)
                v_range = max(vals) - min(vals)
                row_str += f"  {v_range:>8{fmt}}"
                print(row_str)

    print()


def save_summary_csv(metrics, output_dir):
    """Save combined summary CSV."""
    rows = []
    for key, policy in POLICIES.items():
        df = metrics.get(key)
        if df is None:
            continue
        for _, r in df.iterrows():
            row = {
                'policy': policy['label'],
                'policy_key': key,
                'kind': policy['kind'],
                'param_label': policy['param_label'],
                'param_value': r['param_value'],
                'p_fail': r['p_fail'],
                'avg_sr': r['avg_sr'],
                'avg_amp': r['avg_amp'],
                'avg_retry_eff': r['avg_retry_eff'],
                'fairness_gap': r['fairness_gap'],
                'retry_fairness_gap': r['retry_fairness_gap'],
            }
            rows.append(row)
    out = output_dir / 'sensitivity_summary.csv'
    pd.DataFrame(rows).to_csv(out, index=False)
    print(f"  Saved {out}")


# ── Main ────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Run sensitivity analysis across server-side policies")
    parser.add_argument('-o', '--output', default='results/sensitivity',
                        help='Output directory')
    parser.add_argument('--skip-run', action='store_true',
                        help='Skip experiments, just re-plot from existing results')
    parser.add_argument('--format', default='pdf',
                        help='Plot format (pdf, png)')
    parser.add_argument('--failure-start', type=float, default=20,
                        help='Failure period start (seconds)')
    parser.add_argument('--failure-end', type=float, default=40,
                        help='Failure period end (seconds)')
    args = parser.parse_args()

    output_base = Path(args.output)
    output_base.mkdir(parents=True, exist_ok=True)

    # 1. Run sweeps
    if not args.skip_run:
        print("Running sensitivity sweeps...")
        run_sweeps(output_base)

    # 2. Load failure-period metrics from by_client CSVs
    print("\nLoading failure-period metrics...")
    metrics = load_failure_metrics(
        output_base, args.failure_start, args.failure_end)
    if not metrics:
        print("ERROR: No results found. Run without --skip-run first.")
        return 1

    # 3. Print summary tables
    print_summary(metrics)

    # 4. Generate plots
    plot_dir = output_base / 'plots'
    plot_dir.mkdir(exist_ok=True)
    print("Generating plots...")
    plot_sensitivity_sr(metrics, plot_dir, args.format)
    plot_sensitivity_amp(metrics, plot_dir, args.format)
    plot_sensitivity_retry_eff(metrics, plot_dir, args.format)
    plot_sensitivity_fairness(metrics, plot_dir, args.format)
    plot_punchline(metrics, plot_dir, args.format)
    plot_sensitivity_range(metrics, plot_dir, args.format)
    plot_best_metrics(metrics, plot_dir, args.format)
    plot_sr_optimal_tradeoff(metrics, plot_dir, args.format)

    # 5. Save CSV
    save_summary_csv(metrics, output_base)

    print(f"\nDone. Results in {output_base}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
