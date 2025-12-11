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
