from __future__ import annotations

import os
from pathlib import Path


def configure_matplotlib() -> None:
    """Use a workspace-local config/cache dir and a non-interactive backend."""
    project_root = Path(__file__).resolve().parents[3]
    mplconfig_dir = project_root / ".mplconfig"
    mplconfig_dir.mkdir(parents=True, exist_ok=True)

    os.environ.setdefault("MPLCONFIGDIR", str(mplconfig_dir))

    import matplotlib

    matplotlib.use("Agg")
