import json
import sys

import matplotlib
import numpy as np

matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from shapely.geometry import shape

K = sys.argv[1]
ACC = sys.argv[2]
PROP = sys.argv[3]
INK = "#1f2328"
MUTED = "#6b7280"
T = [("kuna", "Kuna, ID"), ("magna", "Magna, UT"), ("carmel", "Carmel-by-the-Sea, CA")]
fig, ax = plt.subplots(1, 3, figsize=(16, 6.2), facecolor="white")
for a, (k, title) in zip(ax, T):
    d = json.load(open(f"{K}/{k}_fit.json"))
    o, n = d["old"], d["new"]
    a.scatter(
        d["road_x"],
        d["road_y"],
        s=0.4,
        c="#b4b8bf",
        linewidths=0,
        rasterized=True,
        label="Census local streets",
    )
    r = np.array(d["rec"])
    if len(r):
        a.scatter(
            r[:, 0],
            r[:, 1],
            s=1.6,
            c=ACC,
            linewidths=0,
            rasterized=True,
            label="Recent 360° sweep (check only)",
        )
    g = shape(d["poly"])
    for i, p in enumerate(g.geoms if g.geom_type == "MultiPolygon" else [g]):
        a.plot(*p.exterior.xy, c=INK, lw=0.9, label="Municipal boundary" if i == 0 else None)
    a.add_patch(
        Rectangle(
            (o[0], o[1]),
            o[2] - o[0],
            o[3] - o[1],
            fill=False,
            ec=MUTED,
            ls="--",
            lw=1.6,
            label="Current grid",
        )
    )
    a.add_patch(
        Rectangle(
            (n[0], n[1]),
            n[2] - n[0],
            n[3] - n[1],
            fill=False,
            ec=PROP,
            lw=2.2,
            label="Proposed grid",
        )
    )
    a.plot(*d["geo"], marker="+", c=INK, ms=8, mew=1.5, ls="", label="GeoNames point")
    pad = 0.012
    a.set_xlim(min(o[0], n[0]) - pad, max(o[2], n[2]) + pad)
    a.set_ylim(min(o[1], n[1]) - pad, max(o[3], n[3]) + pad)
    a.set_aspect(1 / np.cos(np.radians((o[1] + o[3]) / 2)))
    a.set_title(title, color=INK, fontsize=13, loc="left")
    a.tick_params(labelsize=7, colors=MUTED)
    for s in a.spines.values():
        s.set_color("#e5e7eb")
h, l = ax[0].get_legend_handles_labels()
lg = fig.legend(h, l, loc="lower center", ncol=6, frameon=False, fontsize=10, markerscale=1.5)
fig.tight_layout(rect=(0, 0.07, 1, 1))
fig.savefig(K + "/boundaries_map.png", dpi=130)
