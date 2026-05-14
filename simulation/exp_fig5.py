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
Paper Figure 4(b): Multi-candidate effect — conflict reduction across G/M/V
and comparison of top-K vs weighted-random selection strategies.

Layout: 2 rows x 4 columns

  Row 1 (bind-race):  [(1) vs G] [(2) vs M] [(3) vs V] [(4) strategy bar]
  Row 2 (stale-state): [(5) vs G] [(6) vs M] [(7) vs V] [(8) strategy bar]

Subplots 1–3 and 5–7: line charts with two curves each —
  • Baseline (K=0) : gray dashed  — vanilla parallel scheduling
  • Multi-candidate (K=4) : colored solid — with multi-candidate submission

Subplots 4 and 8: bar charts comparing top-K vs weighted-random selection
  at the default configuration with K=4.

bind_race   = always_sync=True  total rate
stale_state = max(0, periodic_rate - event_rate)

Usage:
    python exp_fig5.py              # run top-K sims if needed + save figure
    python exp_fig5.py --show       # save + display
    python exp_fig5.py --rerun-topk # force re-run top-K simulations
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

# ---------------------------------------------------------------------------
#  Colors
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)
COLOR_B0 = "#999999"   # gray — baseline (K=0)
COLOR_B1 = "#74c476"   # light green — multi-candidate (K=1)
COLOR_B2 = "#238b45"   # dark green — multi-candidate (K=2)
COLOR_WR = "#6baed6"   # blue — weighted-random bar
COLOR_TK = _rdbu[1]    # red — top-K bar

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8,
    "ytick.labelsize":     8,
    "legend.fontsize":     8.5,
    "legend.title_fontsize": 9,
    "axes.linewidth":      0.8,
    "lines.linewidth":     1.5,
    "lines.markersize":    2.5,
}

LW_B2 = 1.5    # multi-candidate line width
LW_B0 = 1.2    # baseline line width
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


def _br(store, overrides):
    """Return bind-race rate and std (mean, std) for a configuration."""
    r = _get(store, {**overrides, "always_sync": True})
    return r["mean_rate"], r["std_rate"]


def _ss(store, overrides):
    """Return stale-state rate and std for a configuration."""
    evt = _get(store, {**overrides, "always_sync": True})
    per = _get(store, {**overrides, "always_sync": False})
    ss = max(0.0, per["mean_rate"] - evt["mean_rate"])
    ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)
    return ss, ss_s


# ---------------------------------------------------------------------------
#  Line-chart data: K=0 vs K=2 across G, M, V
# ---------------------------------------------------------------------------

def _m_overrides(M):
    """Return saturated (task_rate, num_scheduler) for a given M, plus M itself."""
    R, N = _saturate_m(M)
    return {"pod_per_node": M, "task_rate": R, "num_scheduler": N}


def compute_all_line_data(store):
    """Compute all 6 line-chart datasets. Returns a dict keyed by (conflict_type, x_param)."""
    data = {}

    # Common helper: sweep one x_param, using a builder fn for each x_value
    def _sweep(x_values, build_overrides, rate_fn):
        r0, s0, r1, s1, r2, s2 = [], [], [], [], [], []
        for xv in x_values:
            base = build_overrides(xv)
            m0, sd0 = rate_fn(store, {**base, "num_backup": 0})
            m1, sd1 = rate_fn(store, {**base, "num_backup": 1})
            m2, sd2 = rate_fn(store, {**base, "num_backup": 2})
            r0.append(m0); s0.append(sd0)
            r1.append(m1); s1.append(sd1)
            r2.append(m2); s2.append(sd2)
        return r0, s0, r1, s1, r2, s2

    # -- bind-race --
    # vs G: M=1, V=0.5
    data[("br", "G")] = _sweep(SYNC_GAPS,
                               lambda G: {"sync_gap": G, "pod_per_node": 1,
                                          "slot_score_variance": 0.5,
                                          "task_rate": 4000, "num_scheduler": 10},
                               _br)
    # vs M: V=0.5, G=1.0 (R and N saturated per M)
    data[("br", "M")] = _sweep(M_VALUES,
                               lambda M: {**_m_overrides(M),
                                          "slot_score_variance": 0.5,
                                          "sync_gap": 1.0},
                               _br)
    # vs V: M=1, G=1.0
    data[("br", "V")] = _sweep(V_VALUES,
                               lambda V: {"slot_score_variance": V,
                                          "pod_per_node": 1,
                                          "sync_gap": 1.0,
                                          "task_rate": 4000, "num_scheduler": 10},
                               _br)

    # -- stale-state --
    data[("ss", "G")] = _sweep(SYNC_GAPS,
                               lambda G: {"sync_gap": G, "pod_per_node": 1,
                                          "slot_score_variance": 0.5,
                                          "task_rate": 4000, "num_scheduler": 10},
                               _ss)
    data[("ss", "M")] = _sweep(M_VALUES,
                               lambda M: {**_m_overrides(M),
                                          "slot_score_variance": 0.5,
                                          "sync_gap": 1.0},
                               _ss)
    data[("ss", "V")] = _sweep(V_VALUES,
                               lambda V: {"slot_score_variance": V,
                                          "pod_per_node": 1,
                                          "sync_gap": 1.0,
                                          "task_rate": 4000, "num_scheduler": 10},
                               _ss)

    return data


# ---------------------------------------------------------------------------
#  Bar-chart data — top-K vs weighted-random at default params, num_backup=2
# ---------------------------------------------------------------------------

def _bar_configs():
    base = {"num_backup": 2}
    return [
        {**base, "select_strategy": "weighted_random"},
        {**base, "select_strategy": "top_k"},
    ]


def ensure_topk_data(force_rerun=False):
    from utils import ensure_results as _ensure

    configs = []
    for cfg in _bar_configs():
        for always_sync in (True, False):
            configs.append({**cfg, "always_sync": always_sync})

    store = _ensure(configs, DATA_PATH, num_trials=3, simulation_time=30.0,
                    force=force_rerun)
    return store["results"]


def compute_bar_data(store):
    br_wr, br_tk, ss_wr, ss_tk = None, None, None, None
    br_ws, br_ts, ss_ws, ss_ts = None, None, None, None
    try:
        cfg_wr, cfg_tk = _bar_configs()
        evt_wr = _get(store, {**cfg_wr, "always_sync": True})
        per_wr = _get(store, {**cfg_wr, "always_sync": False})
        evt_tk = _get(store, {**cfg_tk, "always_sync": True})
        per_tk = _get(store, {**cfg_tk, "always_sync": False})

        br_wr = evt_wr["mean_rate"]; br_ws = evt_wr["std_rate"]
        br_tk = evt_tk["mean_rate"]; br_ts = evt_tk["std_rate"]

        ss_wr = max(0.0, per_wr["mean_rate"] - evt_wr["mean_rate"])
        ss_ws = math.sqrt(per_wr["std_rate"] ** 2 + evt_wr["std_rate"] ** 2)
        ss_tk = max(0.0, per_tk["mean_rate"] - evt_tk["mean_rate"])
        ss_ts = math.sqrt(per_tk["std_rate"] ** 2 + evt_tk["std_rate"] ** 2)
    except KeyError:
        pass
    return (br_wr, br_ws, br_tk, br_ts), (ss_wr, ss_ws, ss_tk, ss_ts)


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _draw_line_panel(ax, x_values, x_labels, r0, s0, r1, s1, r2, s2, is_bindrace=True):
    """Draw K=0 (gray), K=1 (light green), K=2 (dark green) lines."""
    x = np.array(x_values, dtype=float)
    linestyle = ":" if is_bindrace else "-"
    marker = "o" if is_bindrace else "s"

    # K=0 baseline (gray)
    r0a = np.array(r0); s0a = np.array(s0)
    ax.plot(x, r0a, color=COLOR_B0, linestyle=linestyle,
            linewidth=LW_B0, marker=marker, markersize=3.0, markerfacecolor="white")
    ax.fill_between(x, np.maximum(0, r0a - s0a), r0a + s0a,
                    color=COLOR_B0, alpha=ALPHA_BAND * 0.7, linewidth=0)

    # K=1 multi-candidate (light green)
    r1a = np.array(r1); s1a = np.array(s1)
    ax.plot(x, r1a, color=COLOR_B1, linestyle=linestyle,
            linewidth=LW_B2, marker=marker, markersize=3.5)
    ax.fill_between(x, np.maximum(0, r1a - s1a), r1a + s1a,
                    color=COLOR_B1, alpha=ALPHA_BAND, linewidth=0)

    # K=2 multi-candidate (dark green)
    r2a = np.array(r2); s2a = np.array(s2)
    ax.plot(x, r2a, color=COLOR_B2, linestyle=linestyle,
            linewidth=LW_B2, marker=marker, markersize=3.5)
    ax.fill_between(x, np.maximum(0, r2a - s2a), r2a + s2a,
                    color=COLOR_B2, alpha=ALPHA_BAND, linewidth=0)

    ax.set_xticks(x)
    ax.set_xticklabels(x_labels)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


def _draw_bar_panel(ax, wr_val, wr_std, tk_val, tk_std, is_bindrace=True):
    """Bar chart comparing weighted-random vs top-K."""
    labels = ["Weighted random", "Top-(K+1)"]
    values = [wr_val, tk_val]
    errors = [wr_std, tk_std]
    colors = [COLOR_WR, COLOR_TK]

    xs = np.arange(2)
    if is_bindrace:
        ax.bar(xs, values, color=colors, edgecolor="white", linewidth=0.6,
            width=0.55, yerr=errors, capsize=3, hatch="....",
            error_kw={"linewidth": 0.8, "capthick": 0.6})
    else:
        ax.bar(xs, values, color=colors, edgecolor="white", linewidth=0.6,
            width=0.55, yerr=errors, capsize=3,
            error_kw={"linewidth": 0.8, "capthick": 0.6})

    ax.set_xticks(xs)
    if not is_bindrace:
        ax.set_xlabel("Selection strategy")
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

    print("Computing line-chart data …")
    ld = compute_all_line_data(store)

    print("Computing bar-chart data …")
    (br_wr, br_ws, br_tk, br_ts), (ss_wr, ss_ws, ss_tk, ss_ts) = \
        compute_bar_data(store)
    have_bar = br_wr is not None
    if not have_bar:
        print("  Top-K data not cached — run with --rerun-topk to generate.")

    # ── Layout: 2 rows x 4 cols ───────────────────────────────────────────
    fig = plt.figure(figsize=(7.5, 3.2))
    gs = GridSpec(2, 4, figure=fig,
                  hspace=0.27, wspace=0.50,
                  left=0.07, right=0.97, top=0.88, bottom=0.13)

    # ── Subplot references ─────────────────────────────────────────────────
    axes = {}
    for row, label in [(0, "br"), (1, "ss")]:
        for col, param in enumerate(["G", "M", "V", "bar"]):
            axes[(label, param)] = fig.add_subplot(gs[row, col])

    x_label_map = {"G": ("sync_gap", SYNC_GAPS, ["0.5", "1", "2.5", "5"], "G (s)"),
                   "M": ("pod_per_node", M_VALUES, [str(m) for m in M_VALUES], "M"),
                   "V": ("slot_score_variance", V_VALUES, [str(v) for v in V_VALUES], "V")}

    # ── Draw line panels ───────────────────────────────────────────────────
    for ctype, is_br in [("br", True), ("ss", False)]:
        for col_idx, param in enumerate(["G", "M", "V"]):
            ax = axes[(ctype, param)]
            ax_name, xvals, xlabs, xlabel = x_label_map[param]
            r0, s0, r1, s1, r2, s2 = ld[(ctype, param)]
            _draw_line_panel(ax, xvals, xlabs, r0, s0, r1, s1, r2, s2, is_bindrace=is_br)
            if col_idx == 0:
                if ctype == "ss":
                    ax.set_ylabel("CR (stale-state)")
                elif ctype == "br":
                    ax.set_ylabel("CR (bind-race)")
            if ctype == "ss":
                ax.set_xlabel(xlabel)

    # ── Draw bar panels ────────────────────────────────────────────────────
    for ctype in ["br", "ss"]:
        ax = axes[(ctype, "bar")]
        if have_bar:
            if ctype == "br":
                _draw_bar_panel(ax, br_wr, br_ws, br_tk, br_ts, is_bindrace=True)
            else:
                _draw_bar_panel(ax, ss_wr, ss_ws, ss_tk, ss_ts, is_bindrace=False)

    # ── Top legends: 3 groups, side by side, 2 cols each ───────────────────
    h_b0 = mlines.Line2D([], [], color=COLOR_B0, linestyle="-",
                         linewidth=LW_B0, marker="o", markersize=3.0,
                         markerfacecolor="white", label="Baseline (K=0)")
    h_b1 = mlines.Line2D([], [], color=COLOR_B1, linestyle="-",
                         linewidth=LW_B2, marker="o", markersize=4.0,
                         label="K=1")
    h_b2 = mlines.Line2D([], [], color=COLOR_B2, linestyle="-",
                         linewidth=LW_B2, marker="o", markersize=4.0,
                         label="K=2")
    h_evt = mlines.Line2D([], [], color="#555555", linestyle=":",
                          linewidth=1.3, marker="o", markersize=3.5,
                          label="Bind-race")
    h_per = mlines.Line2D([], [], color="#555555", linestyle="-",
                          linewidth=1.3, marker="s", markersize=3.5,
                          label="Stale-state")
    h_wr = mpatches.Patch(facecolor=COLOR_WR, edgecolor="white",
                          label="Weighted random")
    h_tk = mpatches.Patch(facecolor=COLOR_TK, edgecolor="white",
                          label="Top-(K+1)")

    anchor_y = 0.9
    # Group 1: K=0 / K=1 / K=2 (left)
    fig.legend(handles=[h_b0, h_b1, h_b2], loc="lower center",
               bbox_to_anchor=(0.25, anchor_y), ncol=3,
               columnspacing=0.8, handlelength=1.4, handletextpad=0.3,
               frameon=False, fontsize=7.5)
    # Group 2: Bind-race vs Stale-state (center)
    fig.legend(handles=[h_evt, h_per], loc="lower center",
               bbox_to_anchor=(0.50, anchor_y), ncol=2,
               columnspacing=1.2, handlelength=1.6, handletextpad=0.4,
               frameon=False, fontsize=7.5)
    # Group 3: Weighted random vs Top-K (right)
    fig.legend(handles=[h_wr, h_tk], loc="lower center",
               bbox_to_anchor=(0.86, anchor_y), ncol=2,
               columnspacing=1.2, handlelength=1.2, handletextpad=0.4,
               frameon=False, fontsize=7.5)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"multicandidate-effect.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure 5 — multi-candidate effect across G/M/V")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    ap.add_argument("--rerun-topk", action="store_true",
                    help="Force re-run top-K simulations (ignoring cache)")
    args = ap.parse_args()

    # Ensure top-K data exists
    if args.rerun_topk:
        if os.path.exists(DATA_PATH) and os.path.getsize(DATA_PATH) > 0:
            with open(DATA_PATH, "r", encoding="utf-8") as f:
                cache = json.load(f)
            to_remove = [k for k in cache["results"]
                         if '"select_strategy": "top_k"' in k]
            for k in to_remove:
                del cache["results"][k]
            with open(DATA_PATH, "w", encoding="utf-8") as f:
                json.dump(cache, f, indent=2)
            print(f"Removed {len(to_remove)} top-K cache entries.")

    print("Ensuring top-K simulation data …")
    ensure_topk_data(force_rerun=args.rerun_topk)
    print("Top-K data ready.")

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
