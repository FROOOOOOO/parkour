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
Paper Figure 6: Conflict-rate penalty — simulation micro-validation.

Layout: 3 rows x 2 columns, single-column width (~3.5 in).

  Rows    = varied parameter (G / M / V)
  Columns = conflict type (Bind-race / Stale-state)

Each panel shows 3 curves:
  • Baseline (B=0, w=0)          : gray dashed
  • Penalty only (B=0, w=0.5)    : light green
  • Combined (B=2, w=0.5)        : dark green

bind_race   = always_sync=True  total rate
stale_state = max(0, periodic_rate - event_rate)

Usage:
    python exp_fig6.py           # run missing sims + save figure
    python exp_fig6.py --show    # save + display
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
COLOR_BL  = "#999999"   # gray — baseline
COLOR_P   = "#74c476"   # light green — penalty only
COLOR_PMC = "#238b45"   # dark green — penalty + multi-candidate

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8,
    "ytick.labelsize":     8,
    "legend.fontsize":     8,
    "legend.title_fontsize": 9,
    "axes.linewidth":      0.8,
    "lines.linewidth":     1.5,
    "lines.markersize":    2.5,
}

LW = 1.3
ALPHA_BAND = 0.12

# ---------------------------------------------------------------------------
#  Default parameters
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
    r = _get(store, {**overrides, "always_sync": True})
    return r["mean_rate"], r["std_rate"]


def _ss(store, overrides):
    evt = _get(store, {**overrides, "always_sync": True})
    per = _get(store, {**overrides, "always_sync": False})
    ss = max(0.0, per["mean_rate"] - evt["mean_rate"])
    ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)
    return ss, ss_s


# ---------------------------------------------------------------------------
#  Ensure penalty data exists in cache
# ---------------------------------------------------------------------------

def _m_overrides(M):
    R, N = _saturate_m(M)
    return {"pod_per_node": M, "task_rate": R, "num_scheduler": N}


def ensure_penalty_data(force_rerun=False):
    """Generate all configs needed for penalty figure and run missing ones."""
    from utils import ensure_results as _ensure

    configs = []
    for always_sync in (True, False):
        # G sweep: M=1, V=0.5, w=0.5, B=0 and B=2
        for B in (0, 2):
            for G in SYNC_GAPS:
                configs.append({"sync_gap": G, "pod_per_node": 1,
                                "slot_score_variance": 0.5,
                                "task_rate": 4000, "num_scheduler": 10,
                                "probability_weight": 0.5, "num_backup": B,
                                "always_sync": always_sync})
        # M sweep: V=0.5, G=1.0, w=0.5, B=0 and B=2
        for B in (0, 2):
            for M in M_VALUES:
                configs.append({**_m_overrides(M),
                                "slot_score_variance": 0.5, "sync_gap": 1.0,
                                "probability_weight": 0.5, "num_backup": B,
                                "always_sync": always_sync})
        # V sweep: M=1, G=1.0, w=0.5, B=0 and B=2
        for B in (0, 2):
            for V in V_VALUES:
                configs.append({"slot_score_variance": V, "pod_per_node": 1,
                                "sync_gap": 1.0, "task_rate": 4000,
                                "num_scheduler": 10,
                                "probability_weight": 0.5, "num_backup": B,
                                "always_sync": always_sync})

    _ensure(configs, DATA_PATH, num_trials=3, simulation_time=30.0,
            force=force_rerun)


# ---------------------------------------------------------------------------
#  Line-chart data — three configs per sweep
# ---------------------------------------------------------------------------

Config = tuple  # (num_backup, probability_weight)

CONFIGS = [
    (0, 0.0),   # baseline
    (0, 0.5),   # penalty only
    (2, 0.5),   # combined
]

CONFIG_STYLES = [
    {"color": COLOR_BL,  "baseline": True,  "linewidth": 1.0, "markerfacecolor": "white", "label": "Baseline"},
    {"color": COLOR_P,   "baseline": False, "linewidth": LW,  "label": "Penalty (w=0.5)"},
    {"color": COLOR_PMC, "baseline": False, "linewidth": LW,  "label": "Combined (w=0.5, K=2)"},
]


def compute_all_line_data(store):
    data = {}

    def _sweep(x_values, build_overrides, rate_fn):
        out = []
        for B, w in CONFIGS:
            r_list, s_list = [], []
            for xv in x_values:
                base = build_overrides(xv)
                m, sd = rate_fn(store, {**base, "num_backup": B,
                               "probability_weight": w})
                r_list.append(m); s_list.append(sd)
            out.append((r_list, s_list))
        return out  # list of (rates, stds) per config

    for ctype, rate_fn in [("br", _br), ("ss", _ss)]:
        data[(ctype, "G")] = _sweep(SYNC_GAPS,
            lambda G: {"sync_gap": G, "pod_per_node": 1,
                       "slot_score_variance": 0.5,
                       "task_rate": 4000, "num_scheduler": 10}, rate_fn)
        data[(ctype, "M")] = _sweep(M_VALUES,
            lambda M: {**_m_overrides(M),
                       "slot_score_variance": 0.5, "sync_gap": 1.0}, rate_fn)
        data[(ctype, "V")] = _sweep(V_VALUES,
            lambda V: {"slot_score_variance": V, "pod_per_node": 1,
                       "sync_gap": 1.0, "task_rate": 4000,
                       "num_scheduler": 10}, rate_fn)

    return data


# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------

def _draw_line_panel(ax, x_values, x_labels, sweep_data, is_bindrace=True):
    """Draw 3 lines (baseline, penalty, combined) on a panel."""
    x = np.array(x_values, dtype=float)
    style = ":" if is_bindrace else "-"
    marker = "o" if is_bindrace else "s"

    for i, (rates, stds) in enumerate(sweep_data):
        cs = CONFIG_STYLES[i]
        r = np.array(rates); s = np.array(stds)
        ax.plot(x, r, color=cs["color"], linestyle=style,
                linewidth=cs["linewidth"], marker=marker, markersize=3.0,
                markerfacecolor=cs.get("markerfacecolor", cs["color"]))
        ax.fill_between(x, np.maximum(0, r - s), r + s,
                        color=cs["color"], alpha=ALPHA_BAND, linewidth=0)

    ax.set_xticks(x)
    ax.set_xticklabels(x_labels)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)


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

    # ── Layout: 3 rows x 2 cols ───────────────────────────────────────────
    fig = plt.figure(figsize=(3.5, 4.4))
    gs = GridSpec(3, 2, figure=fig,
                  hspace=0.45, wspace=0.40,
                  left=0.14, right=0.96, top=0.88, bottom=0.07)

    row_params = [
        ("G", SYNC_GAPS, ["0.5", "1", "2.5", "5"], "G (s)"),
        ("M", M_VALUES,  [str(m) for m in M_VALUES], "M"),
        ("V", V_VALUES,  [str(v) for v in V_VALUES], "V"),
    ]

    for row_idx, (param, xvals, xlabs, xlabel) in enumerate(row_params):
        for col_idx, (ctype, is_br) in enumerate([("br", True), ("ss", False)]):
            ax = fig.add_subplot(gs[row_idx, col_idx])
            sweep_data = ld[(ctype, param)]
            _draw_line_panel(ax, xvals, xlabs, sweep_data, is_bindrace=is_br)

            # y-label on left column only
            if col_idx == 0:
                ax.set_ylabel("Conflict rate")
            # x-label on every row
            ax.set_xlabel(xlabel)

    # ── Top legends: 2 rows, centered ─────────────────────────────────────
    h_bl  = mlines.Line2D([], [], color=COLOR_BL,  linestyle=(0, (4, 3)),
                          linewidth=1.0, marker="o", markersize=3.0,
                          markerfacecolor="white", label="Baseline")
    h_p   = mlines.Line2D([], [], color=COLOR_P,   linestyle="-",
                          linewidth=LW, marker="o", markersize=3.5,
                          label="Penalty (w=0.5)")
    h_pmc = mlines.Line2D([], [], color=COLOR_PMC, linestyle="-",
                          linewidth=LW, marker="o", markersize=3.5,
                          label="Combined (w=0.5, K=2)")
    h_br  = mlines.Line2D([], [], color="#555555", linestyle=":",
                          linewidth=1.3, marker="o", markersize=3.5,
                          label="Bind-race")
    h_ss  = mlines.Line2D([], [], color="#555555", linestyle="-",
                          linewidth=1.3, marker="s", markersize=3.5,
                          label="Stale-state")

    # Row 1: baseline / penalty / combined (3 cols, centered)
    fig.legend(handles=[h_bl, h_p, h_pmc], loc="lower center",
               bbox_to_anchor=(0.5, 0.92), ncol=3,
               columnspacing=1.0, handlelength=1.4, handletextpad=0.4,
               frameon=False, fontsize=7.5)
    # Row 2: bind-race / stale-state (2 cols, centered)
    fig.legend(handles=[h_br, h_ss], loc="lower center",
               bbox_to_anchor=(0.5, 0.88), ncol=2,
               columnspacing=1.5, handlelength=1.4, handletextpad=0.5,
               frameon=False, fontsize=7.5)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"penalty-effect.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure 6 — penalty effect across G/M/V")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    ap.add_argument("--rerun", action="store_true",
                    help="Force re-run penalty simulations (ignoring cache)")
    args = ap.parse_args()

    if args.rerun and os.path.exists(DATA_PATH) and os.path.getsize(DATA_PATH) > 0:
        with open(DATA_PATH, "r", encoding="utf-8") as f:
            cache = json.load(f)
        to_remove = [k for k in cache["results"]
                     if json.loads(k).get("probability_weight") == 0.5
                     and json.loads(k).get("num_backup") in (0, 2)]
        for k in to_remove:
            del cache["results"][k]
        with open(DATA_PATH, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2)
        print(f"Removed {len(to_remove)} penalty cache entries.")

    print("Ensuring penalty simulation data …")
    ensure_penalty_data(force_rerun=args.rerun)
    print("Penalty data ready.")

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
