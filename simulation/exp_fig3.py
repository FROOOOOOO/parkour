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
Paper Figure 1 (motivation): two panels, single-column.

(a) Conflict-cause decomposition vs. scheduler count — grouped stacked bars.
    Source: simulation cache (simulation/data/results.json).
    Each scheduler-count group has two bars:
      * event-driven (solid red): commit-conflict only
      * periodic    (dotted red): commit + stale-state stacked

(b) Throughput vs. scheduler count — line.
    Source: same simulation cache.
    Three lines: ideal-no-conflict (gray dotted), event-driven baseline
    (red solid), periodic baseline (red dashed).

Both panels share one workload (high task-rate, short task-duration) so the
conflict-decomposition story and the throughput-dilution story refer to the
same experimental conditions.

Style/data separation: STYLE + RC_PARAMS define visual constants; DATA
controls which experiments are read; load_*() return numpy arrays; draw_*()
take pre-loaded arrays. Adding more cluster sizes or scheduler counts only
requires editing DATA — no plotting code changes.

Usage:
    python exp_fig3.py            # build once, save figure
    python exp_fig3.py --show     # also display interactively
"""

import argparse
import os
import warnings

import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Patch
from matplotlib.ticker import PercentFormatter
import seaborn as sns

import utils

warnings.filterwarnings("ignore", message=".*iCCP.*")


# ============================================================================
#  STYLE — central visual constants (colors / linestyles / hatches / labels).
#  Project palette convention:
#    gray   = monolithic single scheduler (and ideal-no-conflict reference)
#    red    = parallel-scheduling baseline
#    green  = ParKour (proposed)             — defined for downstream figures
#    solid  / plain patch = event-driven sync paradigm
#    dashed / dotted patch = periodic sync paradigm
# ============================================================================

_rdbu = sns.color_palette("RdBu", 11)
_greens = sns.color_palette("Greens", 6)

STYLE = {
    # Color families
    "color_single":       "#555555",
    "color_ideal":        "#888888",
    "color_baseline_evt": _rdbu[2],     # lighter red, event-driven baseline
    "color_baseline_per": _rdbu[1],     # darker red, periodic baseline
    "color_stale":        _rdbu[-3],    # mid blue, stale state
    "color_bind":         _rdbu[-1],    # dark blue, bind race
    "color_parkour_evt":  _greens[2],   # ParKour event-driven (reserved)
    "color_parkour_per":  _greens[4],   # ParKour periodic (reserved)

    # Linestyles per sync paradigm
    "ls_event":    "-",
    "ls_periodic": "--",
    "ls_ideal":    ":",

    # Bar hatches per sync paradigm
    "hatch_event":    "",       # solid fill
    "hatch_periodic": "....",   # dots

    # Markers
    "marker_event":    "o",
    "marker_periodic": "s",
    "marker_single":   "^",

    # Bar/line widths
    "lw_main":   1.5,
    "lw_ideal":  1.5,
    "bar_edge":  "white",

    # Fill alphas
    "alpha_band": 0.18,
}

# Single-column rendering: figure is saved at native column width (~3.5"),
# so \includegraphics renders 1:1 without scaling. Fonts are final pt sizes.
RC_PARAMS = {
    "font.size":             9,
    "axes.labelsize":        9,
    "axes.titlesize":        8.5,
    "xtick.labelsize":       8,
    "ytick.labelsize":       8,
    "legend.fontsize":       8.5,
    "legend.title_fontsize": 9,
    "axes.linewidth":        0.8,
    "lines.linewidth":       1.5,
    "lines.markersize":      2.5,
}


# ============================================================================
#  DATA — what to load. Edit here when adding cluster sizes / scheduler counts.
# ============================================================================

# Resolve paths relative to this file so the script works from any cwd.
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, ".."))

DATA = {
    # Both panels share one high-rate workload so the conflict-decomposition
    # story (a) and the throughput-dilution story (b) describe the SAME
    # experimental conditions. High-rate / short-duration: schedulers (not
    # nodes) are the throughput bottleneck, exposing parallel-scheduling
    # conflict overhead.
    "sim_cache":            os.path.join(_HERE, "data", "results.json"),
    "sched_counts":         [10, 20, 30, 40],
    "task_rate":            20000,
    "task_duration":        1.0,
    "scheduler_rate":       400,
    "simulation_time":      30.0,
    "slot_score_variance":  1.0,
    # ParKour configuration (used in panel (b) only).
    "parkour_num_backup":        4,
    "parkour_probability_weight": 0.5,

    # Output
    "fig_dir":          os.path.join(_REPO, "paper", "figs"),
    "fig_basename":     "motivation",   # matches paper/parkour.tex \includegraphics
}


# ============================================================================
#  Data loaders — pure: read files, return numpy arrays, no plotting.
# ============================================================================

def _workload_overrides(ns, always_sync, parkour=False):
    """Build the override dict for one (scheduler-count, sync-mode, system) cell.

    Centralizes which fields differ between baseline and ParKour so loaders,
    ensure-data, and lookups stay consistent.
    """
    ovr = {
        "num_scheduler":       ns,
        "task_rate":           DATA["task_rate"],
        "task_duration":       DATA["task_duration"],
        "slot_score_variance": DATA["slot_score_variance"],
        "always_sync":         always_sync,
    }
    if parkour:
        ovr["num_backup"]         = DATA["parkour_num_backup"]
        ovr["probability_weight"] = DATA["parkour_probability_weight"]
    return ovr


def _ensure_sim_data():
    """Ensure simulation cache has all combos needed by panels (b) and (c).

    Sweeps scheduler count × sync mode × {baseline, ParKour}. Panel (b) only
    uses baseline; panel (c) uses both. Missing combos are re-simulated by
    utils.ensure_results.
    """
    combos = []
    for ns in DATA["sched_counts"]:
        for parkour in (False, True):
            combos.append(_workload_overrides(ns, always_sync=False, parkour=parkour))
            combos.append(_workload_overrides(ns, always_sync=True,  parkour=parkour))
    return utils.ensure_results(combos, DATA["sim_cache"])


def _lookup(store, **overrides):
    full = utils._make_full_params(overrides)
    return store["results"][utils._make_param_key(full)]


def load_middle(store):
    """Conflict-rate decomposition per scheduler count.

    Returns:
        {"sched_counts": [...],
         "event_commit": [...],          # event-driven commit conflict
         "periodic_commit": [...],       # periodic commit-conflict (== event total)
         "periodic_stale":  [...]}       # periodic - event = stale-state portion

    Approximation: under always_sync=True (event-driven), all conflicts are
    commit-conflicts. Under always_sync=False (periodic), the same commit
    floor exists plus an additional stale-state component. So:
        stale  = max(0, periodic_total - event_total)
        commit = min(event_total, periodic_total)
    """
    ns_arr = DATA["sched_counts"]
    evt, per = [], []
    for ns in ns_arr:
        evt.append(_lookup(store, **_workload_overrides(ns, True))["mean_rate"])
        per.append(_lookup(store, **_workload_overrides(ns, False))["mean_rate"])
    evt = np.array(evt)
    per = np.array(per)
    commit = np.minimum(evt, per)
    stale = np.maximum(0.0, per - evt)
    return {"sched_counts": np.array(ns_arr),
            "event_commit": evt,
            "periodic_commit": commit,
            "periodic_stale": stale}


def load_right(store):
    """Throughput vs scheduler count under both sync paradigms × {baseline, ParKour}.

    Throughput = total_tasks / mean_finish_time, with std propagated via
    first-order delta method: sigma_tp ~= total_tasks * sigma_t / mean_t^2.
    """
    ns_arr = DATA["sched_counts"]
    total_tasks = DATA["simulation_time"] * DATA["task_rate"]

    def _tp(ns, always_sync, parkour):
        r = _lookup(store, **_workload_overrides(ns, always_sync, parkour))
        tp = total_tasks / r["mean_time"]
        tp_std = total_tasks * r["std_time"] / (r["mean_time"] ** 2)
        return tp, tp_std

    out = {"sched_counts": np.array(ns_arr)}
    for sys_label, parkour in [("base", False), ("parkour", True)]:
        for sync_label, always_sync in [("event", True), ("periodic", False)]:
            tp, std = zip(*(_tp(ns, always_sync, parkour) for ns in ns_arr))
            out[f"{sys_label}_{sync_label}_tp"]  = np.array(tp)
            out[f"{sys_label}_{sync_label}_std"] = np.array(std)

    out["ideal_tp"] = np.array(
        [min(DATA["task_rate"], ns * DATA["scheduler_rate"]) for ns in ns_arr])
    return out


# ============================================================================
#  Drawers — pure: take pre-loaded arrays + STYLE, render onto an axis.
# ============================================================================

def draw_middle(ax, data):
    """Grouped stacked bars: per scheduler count, event vs periodic side by side.

    Event bar  (solid red)  : commit-conflict only.
    Periodic bar (dot patch): commit + stale-state stacked.
    """
    ns = data["sched_counts"]
    x = np.arange(len(ns))
    bw = 0.36   # bar width; two bars per group + gap

    # Event-driven bar (left of each group): commit only, solid patch.
    ax.bar(x - bw/2, data["event_commit"], width=bw,
           color=STYLE["color_bind"],
           hatch=STYLE["hatch_event"],
           edgecolor=STYLE["bar_edge"], linewidth=0.8,
           label="Bind race")

    # Periodic bar (right of each group): commit + stale stacked, dot patch.
    ax.bar(x + bw/2, data["periodic_commit"], width=bw,
           color=STYLE["color_bind"],
           hatch=STYLE["hatch_periodic"],
           edgecolor=STYLE["bar_edge"], linewidth=0.8)
    ax.bar(x + bw/2, data["periodic_stale"], width=bw,
           bottom=data["periodic_commit"],
           color=STYLE["color_stale"],
           hatch=STYLE["hatch_periodic"],
           edgecolor=STYLE["bar_edge"], linewidth=0.8,
           label="Stale state")

    ax.set_xticks(x)
    ax.set_xticklabels([str(n) for n in ns])
    ax.set_xlabel("Number of schedulers")
    ax.set_ylabel("Conflict rate")
    ax.yaxis.set_major_formatter(PercentFormatter(1.0))
    ax.grid(True, axis="y", linestyle="--", alpha=0.5)

    # Reserve headroom above the tallest bar so the in-axes legend doesn't
    # overlap data. Tallest bar = total periodic conflict at the largest N.
    max_bar = float(np.max(data["periodic_commit"] + data["periodic_stale"]))
    ax.set_ylim(0, max(0.05, max_bar) * 1.55)

    paradigm_handles = [
        Patch(color=STYLE["color_bind"], label="Bind race"),
        Patch(color=STYLE["color_stale"], label="Stale state"),
        Patch(facecolor="lightgray", hatch=STYLE["hatch_event"],
              edgecolor="black", label="Event-driven"),
        Patch(facecolor="lightgray", hatch=STYLE["hatch_periodic"],
              edgecolor="black", label="Periodic"),
    ]
    # Single-row 4-column legend pinned to the top of the axes — predictable
    # placement, never over the bars.
    ax.legend(handles=paradigm_handles, loc="best", ncol=2,
              framealpha=0.9, fontsize=6.5,
              columnspacing=0.8, handlelength=1.2, handletextpad=0.4)
    ax.set_title("(a) Conflict decomposition\n under parallelism", pad=3, loc="left")


def draw_right(ax, data):
    x = data["sched_counts"]

    # Four data series: {baseline=red, ParKour=green} × {event=solid, periodic=dashed}.
    series = [
        ("base_event_tp",     "base_event_std",
         STYLE["color_baseline_evt"], STYLE["ls_event"],
         STYLE["marker_event"], "Baseline (event)"),
        ("base_periodic_tp",  "base_periodic_std",
         STYLE["color_baseline_per"], STYLE["ls_periodic"],
         STYLE["marker_periodic"], "Baseline (periodic)"),
        ("parkour_event_tp",   "parkour_event_std",
         STYLE["color_parkour_evt"], STYLE["ls_event"],
         STYLE["marker_event"], "ParKour (event)"),
        ("parkour_periodic_tp", "parkour_periodic_std",
         STYLE["color_parkour_per"], STYLE["ls_periodic"],
         STYLE["marker_periodic"], "ParKour (periodic)"),
    ]
    for tp_key, std_key, color, ls, marker, label in series:
        tp = data[tp_key]
        std = data[std_key]
        ax.plot(x, tp, color=color, linestyle=ls, marker=marker,
                linewidth=STYLE["lw_main"], label=label)
        ax.fill_between(x, tp - std, tp + std,
                        color=color, alpha=STYLE["alpha_band"], linewidth=0)

    # Ideal (no-conflict) — drawn LAST so it sits on top of any data line that
    # nearly coincides with it (ParKour event is ~99.7% of ideal).
    # High z-order, dark color, dash-dot style for unambiguous visibility.
    ax.plot(x, data["ideal_tp"],
            color=STYLE["color_ideal"],
            linestyle=STYLE["ls_ideal"],
            linewidth=STYLE["lw_ideal"],
            zorder=10,
            label="Ideal (no conflict)")

    ax.set_xticks(x)
    ax.set_xlabel("Number of schedulers")
    ax.set_ylabel("Throughput (pods/s)")
    ax.ticklabel_format(axis="y", style="sci", scilimits=(0, 0), useMathText=True)
    ax.yaxis.get_offset_text().set_size(7)
    ax.grid(True, linestyle="--", alpha=0.5)
    ax.legend(loc="best", framealpha=0.9, fontsize=6.5)
    ax.set_title("(b) ParKour recovers wasted capacity", pad=3, loc="left")


# ============================================================================
#  Top-level: load all data, render figure, save.
# ============================================================================

def build_figure():
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    print("Loading panels (a)+(b) — simulation cache ...")
    store = _ensure_sim_data()
    middle = load_middle(store)
    right = load_right(store)

    fig, axes = plt.subplots(1, 2, figsize=(5.0, 2.0),
                             gridspec_kw={"width_ratios": [4, 6]})
    draw_middle(axes[0], middle)
    draw_right(axes[1], right)

    plt.tight_layout(pad=0.2, w_pad=0.8)
    return fig


def save(fig):
    out_dir = DATA["fig_dir"]
    os.makedirs(out_dir, exist_ok=True)
    base = DATA["fig_basename"]
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(out_dir, f"{base}.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Paper Figure 1 (motivation)")
    ap.add_argument("--show", action="store_true",
                    help="Display figure interactively after saving")
    args = ap.parse_args()

    fig = build_figure()
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
