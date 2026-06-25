"""Label-decision logic for classify_runs, now keyed on sustained recovery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from classify_runs import decide_label  # noqa: E402


def test_sustained_plus_latency_is_recovered():
    assert decide_label(sustained_recovered=True, latency_recovered=True) == "recovered"


def test_no_sustained_recovery_is_metastable():
    assert decide_label(sustained_recovered=False, latency_recovered=True) == "metastable"
    assert decide_label(sustained_recovered=False, latency_recovered=False) == "metastable"


def test_sustained_but_high_latency_is_ambiguous():
    assert decide_label(sustained_recovered=True, latency_recovered=False) == "ambiguous"
