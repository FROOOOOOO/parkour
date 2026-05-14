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
Paper Figure 4(a): Conflict decomposition — model validation.

Layout: 2×2 data panels, single-column width (~3.5 in).
  Rows   = conflict type (Bind-race / Stale-state)
  Columns = varied parameter (M / V)

  Top legend: paradigm (left) + M=V color pairs (right)
  [       M          |         V         ]
  [ Bind-race  (M)   | Bind-race   (V)   ]  ← event-driven, dotted
  [ Stale-state (M)  | Stale-state  (V)  ]  ← periodic,     solid

bind_race   = always_sync=True  total rate  (equals bind-race component)
stale_state = max(0, periodic_rate - event_rate)

Line styles:
  Bind-race   : dotted + circle  = event-driven
  Stale-state : solid + square   = periodic

Color: red → blue maps from most-conflict to least-conflict parameter value.
  M=1↔V=2.0 (red), M=2↔V=1.0, M=4↔V=0.5, M=8↔V=0.0 (blue)

Usage:
    python exp_fig4.py           # save figure
    python exp_fig4.py --show    # save + display
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
SYNC_GAPS = [0.5, 1.0, 2.5, 5.0]     # x-axis
M_VALUES  = [1, 2, 4, 8]              # pod_per_node legend (row 1)
V_VALUES  = [0.0, 0.5, 1.0, 2.0]     # slot_score_variance legend (row 2)

# ---------------------------------------------------------------------------
#  Color palette: red = more conflict, blue = less conflict
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)

# M=1 has most stale-state → red; M=8 least → blue
M_COLORS = [_rdbu[1], _rdbu[3], _rdbu[7], _rdbu[9]]

# V=2.0 has most conflict → red; V=0.0 least → blue
V_COLORS = [_rdbu[9], _rdbu[7], _rdbu[3], _rdbu[1]]

# ---------------------------------------------------------------------------
#  Style for single-column readability
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

LW_MAIN = 1.5   # periodic solid lines (stale-state)
LW_EVT  = 1.5   # event-driven dotted lines (bind-race)
ALPHA_BAND = 0.12

# ---------------------------------------------------------------------------
#  Data defaults (must match ParaScheduling / utils.DEFAULT_PARAMS)
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
#  Data loading
# ---------------------------------------------------------------------------

def load_store() -> dict:
    with open(DATA_PATH, encoding="utf-8") as f:
        return json.load(f)["results"]


def _get(store, overrides) -> dict:
    k = _key(overrides)
    if k not in store:
        raise KeyError(f"Missing cache entry: {overrides}")
    return store[k]


def compute_m_sweep(store):
    """
    For each (M, G): compute bind-race and stale-state rates.

    Returns four dicts keyed by M: bind_rate, bind_std, stale_rate, stale_std.
    Each value is a list aligned to SYNC_GAPS.
    """
    bind_rate = {M: [] for M in M_VALUES}
    bind_std  = {M: [] for M in M_VALUES}
    stale_rate = {M: [] for M in M_VALUES}
    stale_std  = {M: [] for M in M_VALUES}

    for M in M_VALUES:
        R = int(20000 * M / 5.0)          # saturate: R = num_slot * M / task_duration
        N = math.ceil(R / 400)             # num_scheduler at amplifier=1
        for G in SYNC_GAPS:
            base = {"pod_per_node": M, "task_rate": R, "num_scheduler": N, "sync_gap": G}
            evt = _get(store, {**base, "always_sync": True})
            per = _get(store, {**base, "always_sync": False})

            br  = evt["mean_rate"]
            br_s = evt["std_rate"]
            ss  = max(0.0, per["mean_rate"] - evt["mean_rate"])
            ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)

            bind_rate[M].append(br)
            bind_std[M].append(br_s)
            stale_rate[M].append(ss)
            stale_std[M].append(ss_s)

    return bind_rate, bind_std, stale_rate, stale_std


def compute_v_sweep(store):
    """
    For each (V, G): compute bind-race and stale-state rates.

    Returns four dicts keyed by V: bind_rate, bind_std, stale_rate, stale_std.
    Each value is a list aligned to SYNC_GAPS.
    """
    bind_rate  = {V: [] for V in V_VALUES}
    bind_std   = {V: [] for V in V_VALUES}
    stale_rate = {V: [] for V in V_VALUES}
    stale_std  = {V: [] for V in V_VALUES}

    for V in V_VALUES:
        for G in SYNC_GAPS:
            base = {"slot_score_variance": V, "task_rate": 4000,
                    "num_scheduler": 10, "sync_gap": G}
            evt = _get(store, {**base, "always_sync": True})
            per = _get(store, {**base, "always_sync": False})

            br   = evt["mean_rate"]
            br_s = evt["std_rate"]
            ss   = max(0.0, per["mean_rate"] - evt["mean_rate"])
            ss_s = math.sqrt(per["std_rate"] ** 2 + evt["std_rate"] ** 2)

            bind_rate[V].append(br)
            bind_std[V].append(br_s)
            stale_rate[V].append(ss)
            stale_std[V].append(ss_s)

    return bind_rate, bind_std, stale_rate, stale_std


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def draw_bind_panel(ax, param_values, colors, bind_rate, bind_std):
    """
    Left column: bind-race rate vs G.

    Dotted lines (event-driven) only — bind-race is paradigm-independent
    so one paradigm suffices; dotted distinguishes clearly from solid stale-state.
    """
    x = np.array(SYNC_GAPS)
    all_vals = []
    for v, color in zip(param_values, colors):
        br   = np.array(bind_rate[v])
        br_s = np.array(bind_std[v])
        ax.plot(x, br, color=color, linestyle=":",
                linewidth=LW_EVT, marker="o", markersize=3.5)
        ax.fill_between(x, br - br_s, br + br_s,
                        color=color, alpha=ALPHA_BAND, linewidth=0)
        all_vals.extend((br + br_s).tolist())

    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xticks(x)
    ax.set_xticklabels(["0.5", "1", "2.5", "5"], rotation=30, ha="right")
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    top = max(all_vals) * 1.25
    ax.set_ylim(0, top)


def draw_stale_panel(ax, param_values, colors, stale_rate, stale_std):
    """
    Right column: stale-state rate vs G.

    Solid (periodic) lines only — event-driven reference lines (≈0) are omitted
    to reduce clutter; the contrast with bind-race panels conveys the message.
    """
    x = np.array(SYNC_GAPS)
    for v, color in zip(param_values, colors):
        ss   = np.array(stale_rate[v])
        ss_s = np.array(stale_std[v])
        ax.plot(x, ss, color=color, linestyle="-",
                linewidth=LW_MAIN, marker="s", markersize=3.5)
        ax.fill_between(x, np.maximum(0, ss - ss_s), ss + ss_s,
                        color=color, alpha=ALPHA_BAND, linewidth=0)

    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.set_xticks(x)
    ax.set_xticklabels(["0.5", "1", "2.5", "5"], rotation=30, ha="right")
    ax.grid(True, linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_ylim(bottom=0)


# ---------------------------------------------------------------------------
#  Top-level: load → compute → draw → save
# ---------------------------------------------------------------------------

def build_figure():
    """
    Layout (GridSpec, 3 rows):
      row 0 (legend): top unified legend spanning both columns
                       Left  — paradigm (event-driven / periodic), single column
                       Right — M=X / V=Y color pairs, 2 rows × 2 cols
                               Same color per row: M=1↔V=2.0 (red), …, M=8↔V=0.0 (blue)
      row 1: Bind-race     (event-driven, dotted)
      row 2: Stale-state   (periodic, solid)
    Col 0: varied by M; Col 1: varied by V.
    Right y-axis of rightmost panels annotated with "Bind-race" / "Stale-state".
    """
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    print("Loading simulation cache …")
    store = load_store()
    print("Computing M sweep …")
    m_bind, m_bind_s, m_stale, m_stale_s = compute_m_sweep(store)
    print("Computing V sweep …")
    v_bind, v_bind_s, v_stale, v_stale_s = compute_v_sweep(store)

    # Legend row needs ~0.30 height_ratio to accommodate 2 legend rows at fontsize 7.5.
    fig = plt.figure(figsize=(3.5, 3))
    L, R = 0.14, 0.86
    gs = GridSpec(3, 2, figure=fig,
                  height_ratios=[0.25, 1, 1],
                  hspace=0.35, wspace=0.46,
                  left=L, right=R, top=0.98, bottom=0.08)

    # ── legend axes (invisible) ───────────────────────────────────────────────
    ax_leg = fig.add_subplot(gs[0, :])
    ax_leg.axis("off")

    # ── data axes (row=conflict type, col=parameter) ──────────────────────────
    ax00 = fig.add_subplot(gs[1, 0])   # bind-race,   M
    ax01 = fig.add_subplot(gs[1, 1])   # bind-race,   V
    ax10 = fig.add_subplot(gs[2, 0])   # stale-state, M
    ax11 = fig.add_subplot(gs[2, 1])   # stale-state, V

    # ── draw data ────────────────────────────────────────────────────────────
    draw_bind_panel(ax00, M_VALUES, M_COLORS, m_bind, m_bind_s)
    draw_bind_panel(ax01, V_VALUES, V_COLORS, v_bind, v_bind_s)
    draw_stale_panel(ax10, M_VALUES, M_COLORS, m_stale, m_stale_s)
    draw_stale_panel(ax11, V_VALUES, V_COLORS, v_stale, v_stale_s)

    # ── column titles (parameter label) ───────────────────────────────────────
    ax00.set_title("$M$", pad=3)
    ax01.set_title("$V$", pad=3)

    # ── axis labels ──────────────────────────────────────────────────────────
    ax00.set_ylabel("CR (bind-race)")
    ax10.set_ylabel("CR (stale-state)")
    ax10.set_xlabel("$G$ (s)")
    ax11.set_xlabel("$G$ (s)")

    # ── top unified legend ────────────────────────────────────────────────────
    # Left column: paradigm (stacked, ncol=1)
    h_event = mlines.Line2D([], [], color="#555555", linestyle=":",
                             linewidth=1.6, marker="o", markersize=4,
                             label="Bind-race")
    h_per   = mlines.Line2D([], [], color="#555555", linestyle="-",
                             linewidth=1.6, marker="s", markersize=4,
                             label="Stale-state")
    leg_paradigm = ax_leg.legend(
        handles=[h_event, h_per], loc="center left",
        bbox_to_anchor=(-0.1, 0.4), ncol=1,
        columnspacing=0.8, handlelength=1.6, handletextpad=0.4,
        frameon=False, fontsize=7.5)
    ax_leg.add_artist(leg_paradigm)

    # Right columns: 4 combined "M=X/V=Y" patches, 2 rows × 2 cols.
    # M_COLORS[i] == V_COLORS[3-i]: same color encodes same conflict level.
    #   row1: M=1/V=2.0 (red)        | M=2/V=1.0 (light red)
    #   row2: M=4/V=0.5 (light blue) | M=8/V=0.0 (blue)
    mv_handles = [
        mpatches.Patch(facecolor=M_COLORS[i], edgecolor="none",
                    label=rf"$M={M_VALUES[i]}, V={V_VALUES[3 - i]}$")
        for i in range(4)
    ]
    ax_leg.legend(
        handles=mv_handles, loc="center left",
        bbox_to_anchor=(0.25, 0.4), ncol=2,
        columnspacing=0.7, handlelength=1.0, handletextpad=0.4,
        frameon=False, fontsize=7.5)

    return fig


def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"conflict-decomposition.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Paper Figure 4(a) — conflict decomposition")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
