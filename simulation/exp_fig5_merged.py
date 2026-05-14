# Copyright 2025 the ParaScheduling Authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
Paper Figure 5 (merged Fig 5 + Fig 6): Mechanism characterization.

Layout: 2 rows x 4 cols, double-column (~7.0 in wide).

Rows = conflict type:
  Row 1: bind-race      (event-driven total rate; dotted lines)
  Row 2: stale-state    (periodic - event-driven; solid lines)

Cols = mechanism aspect:
  Col 1: K-sweep (K in {0,1,2}) vs G       — multi-candidate isolated
  Col 2: w-sweep (w in {0,0.3,0.5}) vs M   — penalty isolated
  Col 3: 4 configs vs V                    — combined effect
         vanilla (K=0,w=0) / +M (K=2,w=0) / +P (K=0,w=0.5) / +M+P (K=2,w=0.5)
  Col 4: top-K vs weighted-random          — selection strategy bar (K=2)

Color logic:
  col 1 yellow gradient  (K=0=gray, K=1=light yellow, K=2=dark yellow)
  col 2 blue gradient    (w=0=gray, w=0.3=light blue, w=0.5=dark blue)
  col 3 reuses cols 1-2: vanilla=gray, +M=dark yellow, +P=dark blue,
                         +M+P=green (yellow + blue = ParKour green)
  col 4 strategy: weighted-random=blue, top-K=red

Usage:
    python exp_fig5_merged.py            # ensure data + save figure
    python exp_fig5_merged.py --show     # save + display
    python exp_fig5_merged.py --rerun    # force re-run all sims this script needs
"""

import argparse
import json
import math
import os
import warnings

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import matplotlib.lines as mlines
import numpy as np
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, ".."))
DATA_PATH = os.path.join(_HERE, "data", "results.json")
FIG_DIR = os.path.join(_REPO, "paper", "figs")

# ---------------------------------------------------------------------------
#  Parameter grids
# ---------------------------------------------------------------------------
SYNC_GAPS = [0.5, 1.0, 2.5, 5.0]
M_VALUES  = [1, 2, 4, 8]
V_VALUES  = [0.0, 0.5, 1.0, 2.0]

K_VALUES = [0, 1, 2]              # col 1
W_VALUES = [0.0, 0.3, 0.5]        # col 2

# Col 3: (label, num_backup, probability_weight, color_key)
C3_CONFIGS = [
    ("vanilla", 0, 0.0, "vanilla"),
    ("+M",      2, 0.0, "M"),
    ("+P",      0, 0.5, "P"),
    ("+M+P",    2, 0.5, "MP"),
]

# Col 4: (label, select_strategy)
C4_STRATEGIES = [
    ("Weighted random", "weighted_random"),
    ("Top-(K+1)",       "top_k"),
]

# ---------------------------------------------------------------------------
#  Colors
# ---------------------------------------------------------------------------
_rdbu     = sns.color_palette("RdBu", 11)
_ylorbr   = sns.color_palette("YlOrBr", 9)
_blues    = sns.color_palette("Blues",  6)
_greens   = sns.color_palette("Greens", 6)

COLOR_BL = "#999999"        # gray — baseline / vanilla / "off"

# col 1: yellow gradient for K (multi-candidate) — kept on the yellow side
# of YlOrBr to avoid orange/red drift. ColorBrewer 9-step indices: 2=#fee391
# (light yellow), 3=#fec44f (golden yellow); index 4+ starts looking orange.
COLOR_K1 = _ylorbr[2]       # light yellow (K=1)
COLOR_K2 = _ylorbr[3]       # golden yellow (K=2)

# col 2: blue gradient for w (penalty)
COLOR_W1 = _blues[2]        # light blue   (w=0.3)
COLOR_W2 = _blues[4]        # dark blue    (w=0.5)

# col 3: reuse cols 1+2 for "+M" and "+P"; ParKour green for "+M+P"
# Visual mnemonic: yellow (M) + blue (P) = green (combined ParKour).
COLOR_C3 = {
    "vanilla": COLOR_BL,
    "M":       COLOR_K2,
    "P":       COLOR_W2,
    "MP":      _greens[5],  # ParKour green
}

# col 4: strategy bar
COLOR_WR = "#6baed6"        # blue
COLOR_TK = _rdbu[1]         # red

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":             9,
    "axes.labelsize":        9,
    "axes.titlesize":        8.5,
    "xtick.labelsize":       8,
    "ytick.labelsize":       8,
    "legend.fontsize":       7.5,
    "legend.title_fontsize": 8,
    "axes.linewidth":        0.8,
    "lines.linewidth":       1.5,
    "lines.markersize":      2.5,
}

LW         = 1.5
LW_BL      = 1.1
ALPHA_BAND = 0.12

# ---------------------------------------------------------------------------
#  Default parameters (must match utils.DEFAULT_PARAMS)
# ---------------------------------------------------------------------------
_DEFAULT = {
    "num_slot": 20000, "extra_slot": 0, "slot_score_variance": 0.5,
    "pod_per_node": 1, "num_partition": 1, "num_scheduler": 10,
    "sync_gap": 1.0, "always_sync": False, "sync_pattern": "globSync",
    "schedule_strategy": "latency", "num_backup": 0, "update_strategy": "none",
    "probability_weight": 0.0, "select_strategy": "random_weighted",
    "softmax_temperature": 0.0, "task_rate": 4000, "scheduler_rate": 400,
    "batch_rate": 10, "task_duration": 5.0, "dispatch_strategy": "uniform",
}


def _key(overrides: dict) -> str:
    return json.dumps({**_DEFAULT, **overrides}, sort_keys=True)


# ---------------------------------------------------------------------------
#  Data helpers
# ---------------------------------------------------------------------------

def load_store() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)["results"]


def _get(store, overrides) -> dict:
    k = _key(overrides)
    if k not in store:
        raise KeyError(f"Missing cache entry: {overrides}")
    return store[k]


def _saturate_m(M, num_slot=20000, task_duration=5.0, scheduler_rate=400):
    R = int(num_slot * M / task_duration)
    N = math.ceil(R / scheduler_rate)
    return R, N


def _m_overrides(M):
    R, N = _saturate_m(M)
    return {"pod_per_node": M, "task_rate": R, "num_scheduler": N}


def _br(store, overrides):
    """Bind-race rate (event-driven total)."""
    r = _get(store, {**overrides, "always_sync": True})
    return r["mean_rate"], r["std_rate"]


def _ss(store, overrides):
    """Stale-state rate = max(0, periodic - event-driven)."""
    evt = _get(store, {**overrides, "always_sync": True})
    per = _get(store, {**overrides, "always_sync": False})
    ss = max(0.0, per["mean_rate"] - evt["mean_rate"])
    ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)
    return ss, ss_s


# ---------------------------------------------------------------------------
#  Data ensurance — generate any sim configs this figure needs but cache lacks
# ---------------------------------------------------------------------------

def _col1_configs():
    """K-sweep x G x {evt, per}. Already exists from exp_fig5; re-list for
    completeness so a fresh cache can also be built from this script alone."""
    cfgs = []
    for K in K_VALUES:
        for G in SYNC_GAPS:
            for always_sync in (True, False):
                cfgs.append({"sync_gap": G, "pod_per_node": 1,
                             "slot_score_variance": 0.5,
                             "task_rate": 4000, "num_scheduler": 10,
                             "num_backup": K, "probability_weight": 0.0,
                             "always_sync": always_sync})
    return cfgs


def _col2_configs():
    """w-sweep x M x {evt, per}. w=0.3 typically NOT in cache."""
    cfgs = []
    for w in W_VALUES:
        for M in M_VALUES:
            for always_sync in (True, False):
                cfgs.append({**_m_overrides(M),
                             "slot_score_variance": 0.5, "sync_gap": 1.0,
                             "num_backup": 0, "probability_weight": w,
                             "always_sync": always_sync})
    return cfgs


def _col3_configs():
    """4 mech configs x V x {evt, per}."""
    cfgs = []
    for _, B, w, _ in C3_CONFIGS:
        for V in V_VALUES:
            for always_sync in (True, False):
                cfgs.append({"slot_score_variance": V, "pod_per_node": 1,
                             "sync_gap": 1.0,
                             "task_rate": 4000, "num_scheduler": 10,
                             "num_backup": B, "probability_weight": w,
                             "always_sync": always_sync})
    return cfgs


def _col4_configs():
    """Strategy bar at default params, K=2."""
    cfgs = []
    for _, strategy in C4_STRATEGIES:
        for always_sync in (True, False):
            cfgs.append({"select_strategy": strategy, "num_backup": 2,
                         "always_sync": always_sync})
    return cfgs


def ensure_data(force_rerun=False):
    from utils import ensure_results as _ensure
    configs = (_col1_configs() + _col2_configs()
               + _col3_configs() + _col4_configs())
    _ensure(configs, DATA_PATH, num_trials=3, simulation_time=30.0,
            force=force_rerun)


# ---------------------------------------------------------------------------
#  Compute line-chart data per column
# ---------------------------------------------------------------------------

def compute_col1(store):
    """K=0,1,2 across G, for both bind-race and stale-state."""
    out = {"br": [], "ss": []}
    for K in K_VALUES:
        for ctype, fn in [("br", _br), ("ss", _ss)]:
            rates, stds = [], []
            for G in SYNC_GAPS:
                m, s = fn(store, {"sync_gap": G, "pod_per_node": 1,
                                  "slot_score_variance": 0.5,
                                  "task_rate": 4000, "num_scheduler": 10,
                                  "num_backup": K, "probability_weight": 0.0})
                rates.append(m); stds.append(s)
            out[ctype].append((rates, stds, K))
    return out


def compute_col2(store):
    """w=0,0.3,0.5 across M, for both bind-race and stale-state."""
    out = {"br": [], "ss": []}
    for w in W_VALUES:
        for ctype, fn in [("br", _br), ("ss", _ss)]:
            rates, stds = [], []
            for M in M_VALUES:
                m, s = fn(store, {**_m_overrides(M),
                                  "slot_score_variance": 0.5, "sync_gap": 1.0,
                                  "num_backup": 0, "probability_weight": w})
                rates.append(m); stds.append(s)
            out[ctype].append((rates, stds, w))
    return out


def compute_col3(store):
    """4 configs across V, for both bind-race and stale-state."""
    out = {"br": [], "ss": []}
    for label, B, w, ckey in C3_CONFIGS:
        for ctype, fn in [("br", _br), ("ss", _ss)]:
            rates, stds = [], []
            for V in V_VALUES:
                m, s = fn(store, {"slot_score_variance": V, "pod_per_node": 1,
                                  "sync_gap": 1.0,
                                  "task_rate": 4000, "num_scheduler": 10,
                                  "num_backup": B, "probability_weight": w})
                rates.append(m); stds.append(s)
            out[ctype].append((rates, stds, label, ckey))
    return out


def compute_col4(store):
    """top-K vs weighted-random bars (K=2 default config)."""
    out = {"br": [], "ss": []}
    for label, strategy in C4_STRATEGIES:
        evt = _get(store, {"select_strategy": strategy, "num_backup": 2,
                           "always_sync": True})
        per = _get(store, {"select_strategy": strategy, "num_backup": 2,
                           "always_sync": False})
        br_m, br_s = evt["mean_rate"], evt["std_rate"]
        ss_m = max(0.0, per["mean_rate"] - evt["mean_rate"])
        ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)
        out["br"].append((br_m, br_s, label, strategy))
        out["ss"].append((ss_m, ss_s, label, strategy))
    return out


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _line_style(is_bindrace):
    return (":", "o") if is_bindrace else ("-", "s")


def _plot_line(ax, x, rates, stds, color, ls, marker, baseline=False):
    r = np.array(rates); s = np.array(stds)
    lw = LW_BL if baseline else LW
    mfc = "white" if baseline else color
    ax.plot(x, r, color=color, linestyle=ls, linewidth=lw,
            marker=marker, markersize=3.0, markerfacecolor=mfc)
    ax.fill_between(x, np.maximum(0, r - s), r + s,
                    color=color, alpha=ALPHA_BAND, linewidth=0)


def _finalize_line(ax, x, x_labels):
    ax.set_xticks(x)
    ax.set_xticklabels(x_labels)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def draw_col1(ax, lines, is_bindrace):
    """K-sweep across G."""
    ls, marker = _line_style(is_bindrace)
    x = np.array(SYNC_GAPS, dtype=float)
    K_COLORS = {0: COLOR_BL, 1: COLOR_K1, 2: COLOR_K2}
    for rates, stds, K in lines:
        _plot_line(ax, x, rates, stds, K_COLORS[K], ls, marker, baseline=(K == 0))
    _finalize_line(ax, x, ["0.5", "1", "2.5", "5"])


def draw_col2(ax, lines, is_bindrace):
    """w-sweep across M."""
    ls, marker = _line_style(is_bindrace)
    x = np.array(M_VALUES, dtype=float)
    W_COLORS = {0.0: COLOR_BL, 0.3: COLOR_W1, 0.5: COLOR_W2}
    for rates, stds, w in lines:
        _plot_line(ax, x, rates, stds, W_COLORS[w], ls, marker, baseline=(w == 0.0))
    _finalize_line(ax, x, [str(m) for m in M_VALUES])


def draw_col3(ax, lines, is_bindrace):
    """4 mechanism configs across V."""
    ls, marker = _line_style(is_bindrace)
    x = np.array(V_VALUES, dtype=float)
    for rates, stds, label, ckey in lines:
        color = COLOR_C3[ckey]
        _plot_line(ax, x, rates, stds, color, ls, marker,
                   baseline=(ckey == "vanilla"))
    _finalize_line(ax, x, [str(v) for v in V_VALUES])


def draw_col4(ax, bars, is_bindrace):
    """Strategy comparison bar (single config; bars[i] = (val, std, label, strategy))."""
    labels = [b[2] for b in bars]
    values = [b[0] for b in bars]
    errors = [b[1] for b in bars]
    colors = [COLOR_WR if b[3] == "weighted_random" else COLOR_TK for b in bars]
    xs = np.arange(len(bars))
    hatch = "...." if is_bindrace else None
    ax.bar(xs, values, color=colors, edgecolor="white", linewidth=0.6,
           width=0.55, yerr=errors, capsize=3, hatch=hatch,
           error_kw={"linewidth": 0.8, "capthick": 0.6})
    ax.set_xticks(xs)
    ax.set_xticklabels(labels, fontsize=7)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(True, linestyle="--", alpha=0.35, linewidth=0.5, axis="y")


# ---------------------------------------------------------------------------
#  Build figure
# ---------------------------------------------------------------------------

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    print("Loading simulation cache …")
    store = load_store()

    print("Computing col 1 (K-sweep × G) …")
    c1 = compute_col1(store)
    print("Computing col 2 (w-sweep × M) …")
    c2 = compute_col2(store)
    print("Computing col 3 (4 configs × V) …")
    c3 = compute_col3(store)
    print("Computing col 4 (strategy bar) …")
    c4 = compute_col4(store)

    fig = plt.figure(figsize=(7.0, 3.2))
    gs = GridSpec(2, 4, figure=fig,
                  hspace=0.30, wspace=0.42,
                  left=0.06, right=0.985, top=0.85, bottom=0.13)

    # Build subplots: rows = (br, ss), cols = (1..4)
    axes = {}
    for r, ctype in enumerate(["br", "ss"]):
        for c in range(4):
            axes[(ctype, c)] = fig.add_subplot(gs[r, c])

    # ── Draw line panels ───────────────────────────────────────────────────
    for ctype, is_br in [("br", True), ("ss", False)]:
        draw_col1(axes[(ctype, 0)], c1[ctype], is_br)
        draw_col2(axes[(ctype, 1)], c2[ctype], is_br)
        draw_col3(axes[(ctype, 2)], c3[ctype], is_br)
        draw_col4(axes[(ctype, 3)], c4[ctype], is_br)

    # ── x-axis labels (bottom row only) ────────────────────────────────────
    axes[("ss", 0)].set_xlabel("(a) $G$ (s)")
    axes[("ss", 1)].set_xlabel("(b) $M$")
    axes[("ss", 2)].set_xlabel("(c) $V$")
    axes[("ss", 3)].set_xlabel("(d) Strategy")

    # ── y-axis labels (left column only) ───────────────────────────────────
    axes[("br", 0)].set_ylabel("CR (bind-race)")
    axes[("ss", 0)].set_ylabel("CR (stale-state)")

    # ── Single-row legend, height-aligned across all 4 sub-groups ──────────
    # Redundant baseline labels collapsed: gray "vanilla" handle covers
    # K=0 / w=0 / vanilla simultaneously (same color and marker style).
    # K=2 / +M and w=0.5 / +P share colors with their col-3 counterparts,
    # so they are listed as combined entries to avoid duplication.
    legend_handles = [
        mlines.Line2D([], [], color=COLOR_BL, linestyle="-",
                      linewidth=LW_BL, marker="o", markersize=3.0,
                      markerfacecolor="white", label="vanilla"),
        mlines.Line2D([], [], color=COLOR_K1, linestyle="-",
                      linewidth=LW, marker="o", markersize=3.5, label="K=1"),
        mlines.Line2D([], [], color=COLOR_K2, linestyle="-",
                      linewidth=LW, marker="o", markersize=3.5, label="K=2 / +M"),
        mlines.Line2D([], [], color=COLOR_W1, linestyle="-",
                      linewidth=LW, marker="o", markersize=3.5, label="w=0.3"),
        mlines.Line2D([], [], color=COLOR_W2, linestyle="-",
                      linewidth=LW, marker="o", markersize=3.5, label="w=0.5 / +P"),
        mlines.Line2D([], [], color=COLOR_C3["MP"], linestyle="-",
                      linewidth=LW, marker="o", markersize=3.5, label="+M+P"),
        mpatches.Patch(facecolor=COLOR_WR, edgecolor="white",
                       label="Weighted rand."),
        mpatches.Patch(facecolor=COLOR_TK, edgecolor="white",
                       label="Top-(K+1)"),
    ]
    fig.legend(handles=legend_handles, loc="lower center",
               bbox_to_anchor=(0.5, 0.85), ncol=len(legend_handles),
               columnspacing=1.1, handlelength=1.4, handletextpad=0.3,
               frameon=False, fontsize=7)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"mechanism-effect.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure 5 (merged) — mechanism characterization 2x4")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    ap.add_argument("--rerun", action="store_true",
                    help="Force re-run all sims this figure needs (ignoring cache)")
    args = ap.parse_args()

    print("Ensuring simulation data (col 2 w=0.3 entries are typically new) …")
    ensure_data(force_rerun=args.rerun)
    print("Data ready.")

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
