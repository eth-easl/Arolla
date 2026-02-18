"""Shared data loading and processing utilities for plotting."""

import pandas as pd
from pathlib import Path
from typing import Optional


def load_csv(csv_file: str) -> pd.DataFrame:
    """
    Load and validate CSV file from simulation results.
    
    Args:
        csv_file: Path to CSV file
        
    Returns:
        DataFrame with simulation results
        
    Raises:
        FileNotFoundError: If CSV file doesn't exist
        ValueError: If CSV format is invalid
    """
    csv_path = Path(csv_file)
    
    if not csv_path.exists():
        raise FileNotFoundError(
            f"CSV file not found: {csv_file}\n"
            f"Did you run the experiment first?\n"
            f"Example: python bin/run_experiment.py experiments/yaml/default.yaml"
        )
    
    try:
        df = pd.read_csv(csv_file)
    except Exception as e:
        raise ValueError(f"Failed to read CSV file: {e}")
    
    # Validate required columns
    required_columns = ['timepoint']
    missing = [col for col in required_columns if col not in df.columns]
    if missing:
        raise ValueError(
            f"CSV missing required columns: {missing}\n"
            f"Available columns: {list(df.columns)}"
        )
    
    # Sort by time
    df = df.sort_values(by='timepoint')
    
    # Remove last incomplete time bucket (simulation may not have completed full bucket)
    if len(df) > 1:
        df = df[:-1]
    
    return df


def get_time_range(df: pd.DataFrame, start: Optional[float] = None, 
                   end: Optional[float] = None) -> pd.DataFrame:
    """
    Filter DataFrame to specific time range.
    
    Args:
        df: Input DataFrame
        start: Start time in seconds (None = from beginning)
        end: End time in seconds (None = to end)
        
    Returns:
        Filtered DataFrame
    """
    if start is not None:
        df = df[df['timepoint'] >= start]
    if end is not None:
        df = df[df['timepoint'] <= end]
    return df


def calculate_success_rate(df: pd.DataFrame) -> pd.Series:
    """Calculate success rate from root requests and failures."""
    if 'root_requests' in df.columns and 'failure_root' in df.columns:
        total = df['root_requests']
        failures = df['failure_root']
        # Avoid division by zero
        success_rate = ((total - failures) / total * 100).fillna(0)
        return success_rate
    return None


def get_base_name(name: str) -> str:
    """Strip replica suffix from client name (e.g. 'client.1' → 'client')."""
    if not isinstance(name, str):
        return str(name)
    parts = name.rsplit('.', 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return name


def load_and_aggregate(csv_path: str):
    """
    Load a simulation CSV and aggregate replicas into base clients.

    Returns:
        (df_by_client, df_global, clients) where:
          - df_by_client: DataFrame with 'base_client' column (replicas merged)
          - df_global: DataFrame aggregated across all clients
          - clients: sorted list of unique base_client names
    """
    raw_df = load_csv(csv_path)

    # -- Columns to sum vs take max/mean --
    SUM_COLS = ['root_requests', 'retries', 'success_root', 'completed',
                'failure_root', 'failure_retry', 'failure_queue_full',
                'failure_deadline', 'failure_server', 'total_request', 'total_failure']
    LATENCY_COLS = ['p50', 'p90', 'p95', 'p99', 'p99.9', 'Max']

    def _make_agg_dict(df):
        agg = {c: 'sum' for c in SUM_COLS if c in df.columns}
        if 'queue_size' in df.columns:
            agg['queue_size'] = 'sum'
        if 'queue_avg_at_attempt_end' in df.columns:
            agg['queue_avg_at_attempt_end'] = 'mean'
        for lc in LATENCY_COLS:
            if lc in df.columns:
                agg[lc] = 'max'
        return agg

    # 1. Aggregate replicas by base client
    if 'client_id' in raw_df.columns:
        raw_df['base_client'] = raw_df['client_id'].apply(get_base_name)
        agg_dict = _make_agg_dict(raw_df)
        df_by_client = raw_df.groupby(['timepoint', 'base_client']).agg(agg_dict).reset_index()
        clients = sorted(df_by_client['base_client'].unique())
    else:
        df_by_client = raw_df.copy()
        df_by_client['base_client'] = 'unknown'
        clients = []

    # 2. Global aggregation (sum across all clients per time bucket)
    numeric_cols = [c for c in df_by_client.columns
                    if c not in ('timepoint', 'base_client', 'client_id')
                    and df_by_client[c].dtype.kind in 'biufc']
    agg_global = {c: 'sum' for c in numeric_cols}
    if 'queue_avg_at_attempt_end' in agg_global:
        agg_global['queue_avg_at_attempt_end'] = 'mean'
    for lc in LATENCY_COLS:
        if lc in agg_global:
            agg_global[lc] = 'max'
    df_global = df_by_client.groupby('timepoint').agg(agg_global).reset_index()

    return df_by_client, df_global, clients

