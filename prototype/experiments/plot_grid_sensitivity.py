#!/usr/bin/env python3
"""
plot_grid_sensitivity.py — heatmap visualization for grid (cartesian) sweeps.

Reads a grid sweep YAML config and the corresponding output directory.
For 2-parameter grids, produces a single heatmap. For 3-parameter grids,
produces a row of heatmaps sliced along the third parameter.

Cell color encodes recovery time (seconds). Cells where the system never
recovered are rendered with a white X marker + hatched overlay so they
stay visually distinct from "recovered, but slowly".

Usage:
  python3 plot_grid_sensitivity.py <grid_config.yaml> <sweep_root> [output.pdf]

Examples:
  # Retry budget 2D grid:
  python3 plot_grid_sensitivity.py \\
      sweeps/rb-grid.yaml \\
      outputs/nsdi/rb-grid/<ts>/post-cart-stress-open

  # Arolla 3D grid:
  python3 plot_grid_sensitivity.py \\
      sweeps/arolla-grid.yaml \\
      outputs/nsdi/arolla-grid/<ts>/post-cart-stress-open
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd
import yaml
from matplotlib.colors import TwoSlopeNorm
from matplotlib.patches import Rectangle

# ---------------------------------------------------------------------------
# Colormap configuration
# ---------------------------------------------------------------------------

# Divergent colormap split at THRESHOLD_SEC: values below (fast recovery)
# get cool colors, values above (slow recovery) get warm colors.
# Never-recovered cells are overlaid with white hatching + X (see draw_heatmap).
THRESHOLD_SEC = 10.0


# ---------------------------------------------------------------------------
# Style
# ---------------------------------------------------------------------------

def _paper_style():
    plt.rcParams.update({
        "font.family":      "sans-serif",
        "font.sans-serif":  ["DejaVu Sans", "Helvetica", "Arial"],
        "font.size":        11,
        "axes.labelsize":   12,
        "axes.titlesize":   11,
        "xtick.labelsize":  10,
        "ytick.labelsize":  10,
        "legend.fontsize":  10,
        "axes.spines.top":   False,
        "axes.spines.right": False,
        "axes.linewidth":    0.8,
        "figure.dpi":        150,
    })


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def parse_dir_label(label: str, param_names: List[str]) -> Optional[Dict[str, str]]:
    """
    Parse a grid directory name like 'r=0.01__t=0.0__capacity=5' into a dict.
    Returns None if the label can't be parsed or doesn't match the expected
    parameter set.

    Uses "__" (double underscore) as the pair separator so parameter names
    with single underscores (e.g. min_retry_concurrency) parse correctly.
    """
    parts = label.split("__")
    out: Dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            return None
        k, v = part.split("=", 1)
        out[k] = v
    if set(out.keys()) != set(param_names):
        return None
    return out


def load_grid_data(
    sweep_dir: Path,
    param_names: List[str],
    policy: str,
) -> List[Tuple[Dict[str, str], Optional[float]]]:
    """
    Returns a list of (param_values, recovery_sec) pairs.
    recovery_sec is None when the system never recovered.
    """
    out: List[Tuple[Dict[str, str], Optional[float]]] = []
    for val_dir in sorted(sweep_dir.iterdir()):
        if not val_dir.is_dir():
            continue
        summary = val_dir / "summary.csv"
        if not summary.exists():
            continue
        parsed = parse_dir_label(val_dir.name, param_names)
        if parsed is None:
            continue

        df = pd.read_csv(summary)
        rows = df[df["policy"] == policy]
        if rows.empty:
            continue
        rec = rows.iloc[0].get("recovery_sec")
        try:
            rec_val = float(rec)
            if np.isnan(rec_val):
                rec_val = None
        except (ValueError, TypeError):
            rec_val = None

        out.append((parsed, rec_val))
    return out


# ---------------------------------------------------------------------------
# Axis value coercion / ordering
# ---------------------------------------------------------------------------

def _coerce(v: str) -> float:
    """Parse value as float, stripping duration suffix if present."""
    try:
        return float(v)
    except ValueError:
        pass
    if v.endswith("ms"):
        return float(v[:-2]) / 1000.0
    if v.endswith("s"):
        return float(v[:-1])
    raise ValueError(f"cannot coerce {v!r} to float")


def _sort_values(values: List[str]) -> List[str]:
    """Sort values numerically (including duration strings)."""
    return sorted(values, key=_coerce)


# ---------------------------------------------------------------------------
# Heatmap rendering
# ---------------------------------------------------------------------------

def draw_heatmap(
    ax,
    x_values: List[str],
    y_values: List[str],
    grid: np.ndarray,
    never_mask: np.ndarray,
    x_label: str,
    y_label: str,
    title: Optional[str],
    vmin: float,
    vmax: float,
    cmap,
    norm,
    show_ylabels: bool = True,
):
    """Draw one heatmap. Hatched overlay + X marks never-recovered cells."""
    im = ax.imshow(
        grid,
        aspect="auto",
        origin="lower",
        cmap=cmap,
        norm=norm,
        interpolation="nearest",
    )

    # Never-recovered overlay: white hatching + X marker on top of whatever
    # base color the colormap assigned to that cell.
    for (j, i), is_never in np.ndenumerate(never_mask):
        if is_never:
            ax.add_patch(Rectangle(
                (i - 0.5, j - 0.5), 1, 1,
                fill=False, hatch="///",
                edgecolor="#ffffff", linewidth=0, zorder=3,
            ))
            ax.plot(i, j, marker="x", color="#ffffff",
                    markersize=8, markeredgewidth=2, zorder=4)

    # Numeric cell labels (helpful when grid is small enough to read).
    if grid.size <= 40:
        for (j, i), v in np.ndenumerate(grid):
            if never_mask[j, i] or np.isnan(v):
                continue
            # Pick black or white based on colormap brightness at this value.
            rgba = cmap(norm(v))
            luminance = 0.299 * rgba[0] + 0.587 * rgba[1] + 0.114 * rgba[2]
            text_color = "white" if luminance < 0.55 else "black"
            ax.text(
                i, j, f"{v:.0f}",
                ha="center", va="center", fontsize=8,
                color=text_color,
            )

    ax.set_xticks(range(len(x_values)))
    ax.set_xticklabels(x_values, rotation=30, ha="right", rotation_mode="anchor")
    ax.set_yticks(range(len(y_values)))
    if show_ylabels:
        ax.set_yticklabels(y_values)
        ax.set_ylabel(y_label)
    else:
        ax.set_yticklabels([])
    ax.set_xlabel(x_label)
    if title:
        ax.set_title(title)
    return im


# ---------------------------------------------------------------------------
# Main plotting logic
# ---------------------------------------------------------------------------

def plot_grid(sweeps: List[Dict], sweep_root: Path, out_path: Path):
    """Produce a heatmap figure for the first sweep in the config."""
    _paper_style()

    if len(sweeps) > 1:
        print(f"[warn] config has {len(sweeps)} sweeps; plotting only the first",
              file=sys.stderr)

    sweep = sweeps[0]
    sweep_name = sweep["name"]
    policy = sweep["policies"][0]
    params = sweep["parameters"]

    if len(params) not in (2, 3):
        print(f"[error] only 2D and 3D grids are supported (got {len(params)} params)",
              file=sys.stderr)
        sys.exit(1)

    param_names = [p["name"] for p in params]
    param_values: Dict[str, List[str]] = {
        p["name"]: _sort_values([str(v) for v in p["values"]])
        for p in params
    }

    sweep_dir = sweep_root / sweep_name
    if not sweep_dir.is_dir():
        print(f"[error] not a directory: {sweep_dir}", file=sys.stderr)
        sys.exit(1)

    data = load_grid_data(sweep_dir, param_names, policy)
    if not data:
        print(f"[error] no valid summary.csv files under {sweep_dir}", file=sys.stderr)
        sys.exit(1)

    # Color range from recovered cells only.
    recovered = [rt for _, rt in data if rt is not None]
    if not recovered:
        print("[warn] no recovered cells — all 'never'", file=sys.stderr)
        vmin, vmax = 0.0, 1.0
    else:
        vmin = 0.0
        vmax = max(recovered)

    # Divergent colormap: cool (blue) below THRESHOLD_SEC, warm (red) above.
    # TwoSlopeNorm maps the two halves onto [0, 0.5] and [0.5, 1] of the
    # colormap independently, so the split at 10s is visually crisp even
    # when vmax >> THRESHOLD_SEC.
    cmap = plt.get_cmap("RdBu_r")
    if vmax > THRESHOLD_SEC and vmin < THRESHOLD_SEC:
        norm = TwoSlopeNorm(vmin=vmin, vcenter=THRESHOLD_SEC, vmax=vmax)
    else:
        # Degenerate case: all cells on one side of the threshold.
        # Fall back to a plain linear norm to avoid TwoSlopeNorm errors.
        from matplotlib.colors import Normalize
        norm = Normalize(vmin=vmin, vmax=max(vmax, vmin + 1))

    # --- 2D grid: single heatmap ---
    if len(params) == 2:
        x_name, y_name = param_names[0], param_names[1]
        x_values = param_values[x_name]
        y_values = param_values[y_name]

        grid = np.full((len(y_values), len(x_values)), np.nan)
        never = np.zeros_like(grid, dtype=bool)
        for parsed, rt in data:
            xi = x_values.index(parsed[x_name])
            yi = y_values.index(parsed[y_name])
            if rt is None:
                never[yi, xi] = True
                grid[yi, xi] = vmax
            else:
                grid[yi, xi] = rt

        fig, ax = plt.subplots(figsize=(3.6, 3.0))
        im = draw_heatmap(
            ax, x_values, y_values, grid, never,
            x_label=x_name, y_label=y_name, title=None,
            vmin=vmin, vmax=vmax, cmap=cmap, norm=norm,
        )
        cbar = fig.colorbar(im, ax=ax, shrink=0.85, pad=0.02)
        cbar.set_label(f"Recovery time (s)")
        fig.tight_layout()

    # --- 3D grid: row of heatmaps, sliced by the third parameter ---
    else:
        slice_name = param_names[-1]
        xy_names = [n for n in param_names if n != slice_name]
        x_name, y_name = xy_names[0], xy_names[1]
        slice_values = param_values[slice_name]
        x_values = param_values[x_name]
        y_values = param_values[y_name]

        n_slices = len(slice_values)
        fig, axes = plt.subplots(
            1, n_slices,
            figsize=(2.4 * n_slices + 1.5, 3.2),
            sharey=True,
        )
        if n_slices == 1:
            axes = [axes]

        for idx, s_val in enumerate(slice_values):
            grid = np.full((len(y_values), len(x_values)), np.nan)
            never = np.zeros_like(grid, dtype=bool)
            for parsed, rt in data:
                if parsed[slice_name] != s_val:
                    continue
                xi = x_values.index(parsed[x_name])
                yi = y_values.index(parsed[y_name])
                if rt is None:
                    never[yi, xi] = True
                    grid[yi, xi] = vmax
                else:
                    grid[yi, xi] = rt

            im = draw_heatmap(
                axes[idx], x_values, y_values, grid, never,
                x_label=x_name, y_label=y_name,
                title=f"{slice_name} = {s_val}",
                vmin=vmin, vmax=vmax, cmap=cmap, norm=norm,
                show_ylabels=(idx == 0),
            )

        fig.tight_layout(rect=[0, 0, 0.95, 1])
        cbar = fig.colorbar(im, ax=axes, shrink=0.85, pad=0.02, location="right")
        cbar.set_label(f"Recovery time (s)  |  split at {THRESHOLD_SEC:g}s")

    fig.savefig(out_path, bbox_inches="tight")
    plt.close(fig)
    print(f"  wrote {out_path}")


# ---------------------------------------------------------------------------
# Config loading + main
# ---------------------------------------------------------------------------

def load_sweep_config(config_path: Path) -> List[Dict]:
    with open(config_path) as f:
        config = yaml.safe_load(f)
    defaults = config.get("defaults", {})
    sweeps = []
    for s in config["sweeps"]:
        base = dict(defaults)
        base.update(s.get("base", {}))
        s["base"] = base
        sweeps.append(s)
    return sweeps


def main():
    if len(sys.argv) < 3:
        print(__doc__)
        sys.exit(1)

    config_path = Path(sys.argv[1])
    sweep_root = Path(sys.argv[2])
    out_path = Path(sys.argv[3]) if len(sys.argv) > 3 else \
        sweep_root / f"{config_path.stem}.pdf"

    if not config_path.exists():
        print(f"[error] config not found: {config_path}", file=sys.stderr)
        sys.exit(1)
    if not sweep_root.is_dir():
        print(f"[error] not a directory: {sweep_root}", file=sys.stderr)
        sys.exit(1)

    sweeps = load_sweep_config(config_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"figure -> {out_path}")
    plot_grid(sweeps, sweep_root, out_path)


if __name__ == "__main__":
    main()
