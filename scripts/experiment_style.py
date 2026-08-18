"""
Shared figure styling for the docs/experiments/ studies.

Both `grid_density_analyze.py` and `pano_spacing_analyze.py` publish figures
beside their writeups, and the two are meant to read as one system — so the
palette lives in exactly one place rather than being copied and drifting.

Kept deliberately small: only what is genuinely identical across the studies.
The per-study colour ASSIGNMENTS (which area is blue, which provider is orange)
stay in their own modules, because those are claims about the data.

Importing this module does NOT import matplotlib. The studies defer that import
into their figure functions so the analysis path — which is what runs in CI —
never pays for it; `agg_pyplot()` preserves that.
"""

from __future__ import annotations

# dataviz reference palette. Fixed categorical order (blue, orange, aqua); text
# and grid stay in neutral ink.
CATEGORICAL = ("#2a78d6", "#eb6834", "#1baf7a")
INK = "#0b0b0b"
INK_2 = "#52514e"
GRID = "#e5e4e1"
SURFACE = "#fcfcfb"


def agg_pyplot():
    """pyplot on the headless Agg backend, selected before the first import."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    return plt


def style_axis(ax, ygrid: bool = False) -> None:
    """
    The shared axis treatment: no top/right spines, muted ticks, grid behind.

    `ygrid` is a real difference between the two studies rather than an
    oversight — grid-density's bar and ECDF panels read against horizontal
    rules, while pano-spacing's density and interval plots do not, and drawing
    them there puts lines through the dot-and-whisker rows. It is a parameter
    precisely so sharing this function cannot silently restyle either study's
    committed figures.
    """
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(GRID)
    ax.tick_params(colors=INK_2, labelsize=9)
    if ygrid:
        ax.yaxis.grid(True, color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
