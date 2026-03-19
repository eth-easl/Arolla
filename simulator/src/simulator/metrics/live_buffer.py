from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict

import numpy as np

from simulator.core.types import TimePoint, TimeDuration, DropReason, MS_TO_NS


@dataclass
class LiveMetricsBuffer:
    """
    Sliding-window metrics buffer that receives per-attempt events in real time
    during simulation and returns aggregated observations on demand.
    """
    _events: Deque[tuple] = field(default_factory=deque)

    def record_event(
        self,
        timestamp_ns: TimePoint,
        latency_ns: TimeDuration,
        success: bool,
        drop_reason: DropReason,
        queue_size: int,
        attempt_num: int,
        is_retry: bool,
    ) -> None:
        self._events.append((
            timestamp_ns, latency_ns, success, drop_reason,
            queue_size, attempt_num, is_retry,
        ))

    def get_observation(self, now_ns: TimePoint, window_ns: TimeDuration) -> Dict:
        """
        Return aggregated metrics for events in [now - window, now].
        """
        cutoff = now_ns - window_ns

        # Prune events older than the window
        while self._events and self._events[0][0] < cutoff:
            self._events.popleft()

        if not self._events:
            return self._empty_observation()

        total = 0
        success_count = 0
        failure_count = 0
        retries = 0
        latencies = []
        queue_sum = 0
        fail_queue_full = 0
        fail_deadline = 0
        fail_server = 0

        for ts, lat, succ, reason, qsz, attempt, is_retry in self._events:
            if ts < cutoff:
                continue
            total += 1
            if succ:
                success_count += 1
            else:
                failure_count += 1
                if reason == DropReason.QUEUE_FULL:
                    fail_queue_full += 1
                elif reason == DropReason.DEADLINE:
                    fail_deadline += 1
                elif reason == DropReason.SERVER_FAILURE:
                    fail_server += 1
            if is_retry:
                retries += 1
            latencies.append(lat)
            queue_sum += qsz

        lat_arr = np.array(latencies, dtype=np.float64) / MS_TO_NS if latencies else np.array([0.0])

        return {
            "total_requests": total,
            "success": success_count,
            "failure": failure_count,
            "success_rate": success_count / total if total > 0 else 1.0,
            "error_rate": failure_count / total if total > 0 else 0.0,
            "retries": retries,
            "retry_ratio": retries / total if total > 0 else 0.0,
            "p50": float(np.percentile(lat_arr, 50)),
            "p99": float(np.percentile(lat_arr, 99)),
            "queue_avg": queue_sum / total if total > 0 else 0.0,
            "fail_queue_full": fail_queue_full,
            "fail_deadline": fail_deadline,
            "fail_server": fail_server,
        }

    @staticmethod
    def _empty_observation() -> Dict:
        return {
            "total_requests": 0,
            "success": 0,
            "failure": 0,
            "success_rate": 1.0,
            "error_rate": 0.0,
            "retries": 0,
            "retry_ratio": 0.0,
            "p50": 0.0,
            "p99": 0.0,
            "queue_avg": 0.0,
            "fail_queue_full": 0,
            "fail_deadline": 0,
            "fail_server": 0,
        }