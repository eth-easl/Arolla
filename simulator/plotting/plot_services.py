#!/usr/bin/env python3
"""
Per-service metrics plotting for multi-service dependency topologies.

Reads service_metrics.csv and generates per-service plots:
- Latency percentiles (P50/P90/P99) per service
- Success rate per service
- Throughput per service
- Queue depth per service
- Retry count per service
- Failure breakdown per service
"""

import argparse
import sys
from pathlib import Path

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
import matplotlib.ticker as mticker
import pandas as pd
import numpy as np


# Consistent color palette for services
SERVICE_COLORS = [
    '#2196F3',  # blue
    '#FF5722',  # deep orange
    '#4CAF50',  # green
    '#9C27B0',  # purple
    '#FF9800',  # orange
    '#00BCD4',  # cyan
    '#E91E63',  # pink
    '#607D8B',  # blue grey
]


def get_color(idx: int) -> str:
    return SERVICE_COLORS[idx % len(SERVICE_COLORS)]


def plot_latency(df: pd.DataFrame, output_dir: Path, services: list):
    """P50, P90, P99 latency per service."""
    fig, axes = plt.subplots(3, 1, figsize=(12, 10), sharex=True)
    
    for label, col, ax in [('P50', 'p50', axes[0]), ('P90', 'p90', axes[1]), ('P99', 'p99', axes[2])]:
        for i, svc in enumerate(services):
            svc_df = df[df['service'] == svc]
            ax.plot(svc_df['timepoint'], svc_df[col], label=svc, 
                    color=get_color(i), linewidth=1.5, alpha=0.85)
        ax.set_ylabel(f'{label} (ms)')
        ax.legend(loc='upper right', fontsize=8)
        ax.grid(True, alpha=0.3)
    
    axes[-1].set_xlabel('Time (s)')
    fig.suptitle('Per-Service Latency', fontsize=14, fontweight='bold')
    plt.tight_layout()
    fig.savefig(output_dir / 'service_latency.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_latency.png'}")


def plot_success_rate(df: pd.DataFrame, output_dir: Path, services: list):
    """Success rate per service."""
    fig, ax = plt.subplots(figsize=(12, 5))
    
    for i, svc in enumerate(services):
        svc_df = df[df['service'] == svc]
        ax.plot(svc_df['timepoint'], svc_df['success_rate'] * 100, label=svc,
                color=get_color(i), linewidth=1.5, alpha=0.85)
    
    ax.set_ylabel('Success Rate (%)')
    ax.set_xlabel('Time (s)')
    ax.set_ylim(-5, 105)
    ax.legend(loc='lower right', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title('Per-Service Success Rate', fontsize=14, fontweight='bold')
    plt.tight_layout()
    fig.savefig(output_dir / 'service_success_rate.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_success_rate.png'}")


def plot_throughput(df: pd.DataFrame, output_dir: Path, services: list):
    """Requests per bucket per service."""
    fig, ax = plt.subplots(figsize=(12, 5))
    
    for i, svc in enumerate(services):
        svc_df = df[df['service'] == svc]
        ax.plot(svc_df['timepoint'], svc_df['total_requests'], label=svc,
                color=get_color(i), linewidth=1.5, alpha=0.85)
    
    ax.set_ylabel('Requests / interval')
    ax.set_xlabel('Time (s)')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title('Per-Service Throughput', fontsize=14, fontweight='bold')
    plt.tight_layout()
    fig.savefig(output_dir / 'service_throughput.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_throughput.png'}")


def plot_queue(df: pd.DataFrame, output_dir: Path, services: list):
    """Average queue depth per service."""
    fig, ax = plt.subplots(figsize=(12, 5))
    
    for i, svc in enumerate(services):
        svc_df = df[df['service'] == svc]
        ax.plot(svc_df['timepoint'], svc_df['queue_avg'], label=svc,
                color=get_color(i), linewidth=1.5, alpha=0.85)
    
    ax.set_ylabel('Avg Queue Depth')
    ax.set_xlabel('Time (s)')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title('Per-Service Queue Depth', fontsize=14, fontweight='bold')
    plt.tight_layout()
    fig.savefig(output_dir / 'service_queue.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_queue.png'}")


def plot_retries(df: pd.DataFrame, output_dir: Path, services: list):
    """Retry count per service."""
    fig, ax = plt.subplots(figsize=(12, 5))
    
    for i, svc in enumerate(services):
        svc_df = df[df['service'] == svc]
        ax.plot(svc_df['timepoint'], svc_df['retries'], label=svc,
                color=get_color(i), linewidth=1.5, alpha=0.85)
    
    ax.set_ylabel('Retries / interval')
    ax.set_xlabel('Time (s)')
    ax.legend(loc='upper right', fontsize=9)
    ax.grid(True, alpha=0.3)
    ax.set_title('Per-Service Retries', fontsize=14, fontweight='bold')
    plt.tight_layout()
    fig.savefig(output_dir / 'service_retries.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_retries.png'}")


def plot_failure_breakdown(df: pd.DataFrame, output_dir: Path, services: list):
    """Stacked failure breakdown per service."""
    fig, axes = plt.subplots(len(services), 1, figsize=(12, 3 * len(services)), sharex=True)
    if len(services) == 1:
        axes = [axes]
    
    for i, (svc, ax) in enumerate(zip(services, axes)):
        svc_df = df[df['service'] == svc].copy()
        t = svc_df['timepoint']
        
        ax.bar(t, svc_df['fail_server'], width=0.8, label='Server Failure',
               color='#e53935', alpha=0.7)
        ax.bar(t, svc_df['fail_deadline'], width=0.8, bottom=svc_df['fail_server'],
               label='Deadline', color='#FB8C00', alpha=0.7)
        ax.bar(t, svc_df['fail_queue_full'], width=0.8,
               bottom=svc_df['fail_server'] + svc_df['fail_deadline'],
               label='Queue Full', color='#7B1FA2', alpha=0.7)
        
        ax.set_ylabel('Failures')
        ax.set_title(f'Service: {svc}', fontsize=11, fontweight='bold')
        ax.legend(loc='upper right', fontsize=7)
        ax.grid(True, alpha=0.2)
    
    axes[-1].set_xlabel('Time (s)')
    fig.suptitle('Per-Service Failure Breakdown', fontsize=14, fontweight='bold', y=1.01)
    plt.tight_layout()
    fig.savefig(output_dir / 'service_failure_breakdown.png', dpi=150, bbox_inches='tight')
    plt.close(fig)
    print(f"✓ Saved plot to {output_dir / 'service_failure_breakdown.png'}")


def main():
    parser = argparse.ArgumentParser(
        description='Generate per-service metrics plots from service_metrics.csv'
    )
    parser.add_argument('input_dir', help='Directory containing service_metrics.csv')
    parser.add_argument('-o', '--output', default=None,
                       help='Output directory for plots (default: input_dir/plots/services)')
    
    args = parser.parse_args()
    
    input_dir = Path(args.input_dir)
    csv_path = input_dir / 'service_metrics.csv'
    
    if not csv_path.exists():
        print(f"No service_metrics.csv found in {input_dir}")
        return 1
    
    df = pd.read_csv(csv_path)
    if df.empty:
        print("service_metrics.csv is empty, no plots to generate")
        return 0
    
    services = df['service'].unique().tolist()
    print(f"Generating per-service plots for {len(services)} services: {services}")
    
    output_dir = Path(args.output) if args.output else input_dir / 'plots' / 'services'
    output_dir.mkdir(parents=True, exist_ok=True)
    
    plot_latency(df, output_dir, services)
    plot_success_rate(df, output_dir, services)
    plot_throughput(df, output_dir, services)
    plot_queue(df, output_dir, services)
    plot_retries(df, output_dir, services)
    plot_failure_breakdown(df, output_dir, services)
    
    print(f"\n✅ All per-service plots saved to {output_dir}/")
    return 0


if __name__ == '__main__':
    sys.exit(main())
