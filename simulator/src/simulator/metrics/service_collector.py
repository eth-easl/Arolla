"""
Per-service metrics collector.

Aggregates per-service events from ServiceRuntime into a pandas DataFrame
suitable for time-series plotting.
"""

from typing import Dict, List
import numpy as np
import pandas as pd

from simulator.core.types import DropReason
from simulator.runtime.service import ServiceRuntime
from simulator.utils.time import ns_to_ms


def collect_service_metrics(
    services: Dict[str, ServiceRuntime],
    granularity_s: float = 1.0,
) -> pd.DataFrame:
    """
    Aggregate per-service events into a time-bucketed DataFrame.
    
    Each row = (timepoint, service, latency percentiles, success_rate,
                throughput, queue_avg, retries, failure breakdown).
    
    Returns empty DataFrame if no events recorded.
    """
    MS_TO_NS = 1_000_000  # 1ms in ns
    S_TO_NS = 1_000_000_000  # 1s in ns
    bucket_ns = int(granularity_s * S_TO_NS)
    
    rows = []
    
    for svc_name, svc_rt in services.items():
        events = svc_rt.events
        if not events:
            continue
        
        # Group events by time bucket
        buckets: Dict[int, list] = {}
        for evt in events:
            ts_ns, latency_ns, success, drop_reason, queue_sz, attempt_num, is_retry = evt
            b = int(ts_ns // bucket_ns) * bucket_ns
            if b not in buckets:
                buckets[b] = []
            buckets[b].append(evt)
        
        for bucket_t, bucket_events in sorted(buckets.items()):
            latencies = []
            n_success = 0
            n_fail = 0
            n_retries = 0
            queue_sizes = []
            fail_queue = 0
            fail_deadline = 0
            fail_server = 0
            
            for evt in bucket_events:
                ts_ns, latency_ns, success, drop_reason, queue_sz, attempt_num, is_retry = evt
                latencies.append(latency_ns)
                queue_sizes.append(queue_sz)
                
                if success:
                    n_success += 1
                else:
                    n_fail += 1
                    if drop_reason == DropReason.QUEUE_FULL:
                        fail_queue += 1
                    elif drop_reason == DropReason.DEADLINE:
                        fail_deadline += 1
                    elif drop_reason == DropReason.SERVER_FAILURE:
                        fail_server += 1
                
                if is_retry:
                    n_retries += 1
            
            total = n_success + n_fail
            lat_arr = np.array(latencies, dtype=float) / MS_TO_NS  # convert to ms
            
            rows.append({
                'timepoint': bucket_t / S_TO_NS,
                'service': svc_name,
                'total_requests': total,
                'success': n_success,
                'failure': n_fail,
                'success_rate': n_success / total if total > 0 else 0.0,
                'retries': n_retries,
                'p50': float(np.percentile(lat_arr, 50)) if lat_arr.size else 0,
                'p90': float(np.percentile(lat_arr, 90)) if lat_arr.size else 0,
                'p99': float(np.percentile(lat_arr, 99)) if lat_arr.size else 0,
                'queue_avg': float(np.mean(queue_sizes)) if queue_sizes else 0,
                'fail_queue_full': fail_queue,
                'fail_deadline': fail_deadline,
                'fail_server': fail_server,
            })
    
    if not rows:
        return pd.DataFrame()
    
    return pd.DataFrame(rows)
