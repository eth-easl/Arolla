"""Shared ``sys.path`` bootstrap for the RL scripts.

The RL entry points live under ``simulator/bin/rl/{train,eval,benchmark,analyze}/``
and import each other as flat modules (for example ``from eval_rl_scenario import
...``) as well as the shared ``matplotlib_safe`` helper. When a script is run
directly, Python only adds *its own* folder to ``sys.path``, so those cross-folder
imports would fail.

Importing this module once (``import _pathsetup``) puts the simulator source tree
and every RL script folder on ``sys.path`` so direct execution keeps working no
matter which subfolder a script lives in. It is safe to import from several
scripts in the same process; entries are de-duplicated.
"""

from __future__ import annotations

import sys
from pathlib import Path

_RL_DIR = Path(__file__).resolve().parent  # .../simulator/bin/rl
SIM_ROOT = _RL_DIR.parents[1]              # .../simulator

_SEARCH_PATHS = [
    SIM_ROOT / "src",
    _RL_DIR,
    _RL_DIR / "train",
    _RL_DIR / "eval",
    _RL_DIR / "benchmark",
    _RL_DIR / "analyze",
]

for _path in _SEARCH_PATHS:
    _entry = str(_path)
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
