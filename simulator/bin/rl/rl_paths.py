"""Shared helpers for *where RL scripts write their output*.

Goal: keep generated artifacts (eval plots, benchmark CSVs) out of version
control and, in particular, out of the committed reference model under
``simulator/models/``. Training runs already land in the git-ignored
``simulator/trained_models/`` tree; this module gives the eval/benchmark scripts
one consistent rule for everything else.

Convention
----------
* Model lives under ``trained_models/`` (your own runs) → write benchmark/eval
  output *next to the model*, inside that git-ignored run directory. This keeps a
  run and its evaluation together.
* Model lives under ``models/`` (the committed reference model) → redirect output
  to ``simulator/outputs/rl/<suite>/<model_name>/`` so the committed model folder
  stays clean. ``outputs/`` is git-ignored.

Import it via the RL ``sys.path`` bootstrap::

    import _pathsetup  # noqa: F401
    from rl_paths import default_output_dir
"""

from __future__ import annotations

from pathlib import Path

# .../simulator (this file is simulator/bin/rl/rl_paths.py)
SIM_ROOT = Path(__file__).resolve().parents[2]
MODELS_DIR = SIM_ROOT / "models"
OUTPUTS_DIR = SIM_ROOT / "outputs" / "rl"


def _is_inside(path: Path, parent: Path) -> bool:
    """True when ``path`` is the same as or nested under ``parent``."""
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def default_output_dir(model_path: str, suite: str) -> Path:
    """Return a clean default output directory for a benchmark/eval suite.

    ``model_path`` is the path passed on the CLI (with or without ``.zip``).
    ``suite`` is a short name for the run, e.g. ``"istio_retry_budget_benchmarks"``.
    """
    model_dir = Path(model_path).resolve().parent
    candidate = model_dir / suite
    # Never write generated artifacts into the committed models/ tree.
    if _is_inside(candidate, MODELS_DIR):
        return OUTPUTS_DIR / suite / model_dir.name
    return candidate
