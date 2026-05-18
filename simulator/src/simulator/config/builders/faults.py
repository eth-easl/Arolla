"""Fault injection builder functions."""

from typing import List

from simulator.config.schema import (
    LatencyInjectionConfig,
    PartialFailureConfig,
    LoadSpikeConfig,
)
from simulator.core.models import TimeInterval
from simulator.faults.injection import LatencyInjection, PartialFailure, LoadSpike
from simulator.utils.time import s_to_ns, ms_to_ns


def build_latency_injections(configs: List[LatencyInjectionConfig]) -> List[LatencyInjection]:
    return [
        LatencyInjection(
            duration=TimeInterval(begin=s_to_ns(cfg.start_s), end=s_to_ns(cfg.end_s)),
            add_latency=ms_to_ns(cfg.add_latency_ms),
            multiplier=cfg.multiplier,
        )
        for cfg in configs
    ]


def build_partial_failures(configs: List[PartialFailureConfig]) -> List[PartialFailure]:
    return [
        PartialFailure(
            duration=TimeInterval(begin=s_to_ns(cfg.start_s), end=s_to_ns(cfg.end_s)),
            p_fail=cfg.p_fail,
        )
        for cfg in configs
    ]


def build_load_spikes(configs: List[LoadSpikeConfig]) -> List[LoadSpike]:
    return [
        LoadSpike(
            duration=TimeInterval(begin=s_to_ns(cfg.start_s), end=s_to_ns(cfg.end_s)),
            rps_multiplier=cfg.rps_multiplier,
        )
        for cfg in configs
    ]
