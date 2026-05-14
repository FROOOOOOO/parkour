#!/usr/bin/env python3
"""
Paper Figure (auxiliary): Quality robustness across K (supports §5.5 / §5.4).

Sweeps K in {0, 1, 2, 4} for both paradigms; under Periodic the three sync
patterns (glob/diff/same) are shown as separate lines so the reader can see
that the conclusion is sync-pattern-agnostic. Two panels:

  (a) Mean selected node score vs K — flat across K, supporting the claim
      that multi-candidate fallback imposes no measurable cluster-scope
      quality cost regardless of K.
  (b) Mechanism activation rate (rank > 0) vs K — monotonically rising,
      with diminishing returns past K=2, supporting K=2 as the sweet spot.

Data source: experiments/results/K/S-[EP]-K{0,1,2,4}{,-glob,-diff,-same}/
             trial-*/metrics-saturation/quality.json

Usage:
    python plot-fig-quality-ksweep.py
    python plot-fig-quality-ksweep.py --show
"""

import argparse
import glob
import json
import os
import statistics
import warnings
from typing import Optional

import matplotlib.lines as mlines
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FIG_DIR = os.path.join(_REPO, "paper", "figs")
DEFAULT_K_DIR = os.path.join(_REPO, "experiments", "results", "K")

# Lines: (paradigm, sync_pattern_or_None, label, color, marker, linestyle)
SERIES = [
    ("E", None,    "Event",          "#4575b4", "o",  "-"),
    ("P", "diff",  "Periodic-diff",  "#fd8d3c", "s",  "-"),
    ("P", "glob",  "Periodic-glob",  "#d94801", "D",  "--"),
    ("P", "same",  "Periodic-same",  "#a63603", "^",  ":"),
]
K_VALUES = [0, 1, 2, 4]

RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8.0,
    "ytick.labelsize":     8.0,
    "legend.fontsize":     7.0,
    "axes.linewidth":      0.8,
    "lines.linewidth":     1.4,
    "lines.markersize":    4.0,
}


# ---------------------------------------------------------------------------
#  Data loading
# ---------------------------------------------------------------------------

def _rank_gt0_post_hoc(buckets: dict) -> Optional[float]:
    if not buckets:
        return None
    b_inf = buckets.get("+Inf")
    b_0 = buckets.get("0.0", buckets.get("0"))
    if b_inf is None or b_0 is None or b_inf <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - b_0 / b_inf))


def _group_key(par: str, k: int, sync: Optional[str]) -> str:
    """Build the directory-prefix key for a (paradigm, K, sync) cell."""
    if par == "E":
        return f"S-E-K{k}"
    return f"S-P-K{k}-{sync}"


def _find_group(results_dir: str, key: str) -> Optional[str]:
    matches = sorted(glob.glob(os.path.join(results_dir, f"{key}_*")))
    return matches[-1] if matches else None


def _aggregate(group_dir: str) -> dict:
    scores, ranks, fractions = [], [], []
    for trial in sorted(glob.glob(os.path.join(group_dir, "trial-*"))):
        path = os.path.join(trial, "metrics-saturation", "quality.json")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, "r", encoding="utf-8") as f:
                q = json.load(f)
        except Exception:
            continue
        sn = q.get("selected_node_score") or {}
        rk = q.get("candidate_rank_accepted") or {}
        if sn.get("mean") is not None:
            scores.append(sn["mean"])
        if rk.get("mean") is not None:
            ranks.append(rk["mean"])
        f = rk.get("rank_gt0_fraction")
        if f is None:
            f = _rank_gt0_post_hoc(rk.get("buckets") or {})
        if f is not None:
            fractions.append(f)

    def _mid_std(xs):
        if not xs:
            return None, None
        if len(xs) == 1:
            return xs[0], 0.0
        return statistics.median(xs), statistics.stdev(xs)

    s, ss = _mid_std(scores)
    r, rs = _mid_std(ranks)
    f, fs = _mid_std(fractions)
    return {
        "score": s, "score_std": ss,
        "rank": r, "rank_std": rs,
        "rank_gt0": f, "rank_gt0_std": fs,
        "n": max(len(scores), len(ranks), len(fractions)),
    }


def load_series(results_dir: str) -> dict:
    """Returns {(par, sync): [{k, score, rank_gt0, ...}, ...]}."""
    out: dict = {}
    for par, sync, label, *_ in SERIES:
        seq = []
        for k in K_VALUES:
            key = _group_key(par, k, sync)
            gd = _find_group(results_dir, key)
            if gd is None:
                print(f"  MISS {key}")
                seq.append({"k": k, "score": None, "rank_gt0": None,
                            "score_std": 0.0, "rank_gt0_std": 0.0})
                continue
            agg = _aggregate(gd)
            agg["k"] = k
            agg["key"] = key
            seq.append(agg)
        out[(par, sync)] = seq
    return out


# ---------------------------------------------------------------------------
#  Plotting
# ---------------------------------------------------------------------------

def _plot_metric(ax, series_data: dict, field: str, field_std: str):
    for par, sync, label, color, marker, ls in SERIES:
        seq = series_data.get((par, sync), [])
        ks = [r["k"] for r in seq if r.get(field) is not None]
        ys = [r[field] for r in seq if r.get(field) is not None]
        es = [r.get(field_std) or 0.0 for r in seq if r.get(field) is not None]
        if not ks:
            continue
        ax.errorbar(ks, ys, yerr=es,
                    color=color, marker=marker, linestyle=ls,
                    capsize=2.5, elinewidth=0.7,
                    label=label)


def _draw_score_panel(ax, series_data: dict):
    _plot_metric(ax, series_data, "score", "score_std")
    all_y, all_s = [], []
    for seq in series_data.values():
        for r in seq:
            if r.get("score") is not None:
                all_y.append(r["score"])
                all_s.append(r.get("score_std") or 0.0)
    if all_y:
        margin = max(max(all_s) * 1.5, 2.0)
        ax.set_ylim(min(all_y) - margin, max(all_y) + margin)
    ax.set_xticks(K_VALUES)
    ax.set_xlabel("K (number of fallback candidates)")
    ax.set_ylabel("Mean selected node score")
    ax.set_title("(a) Quality stays flat as K grows", loc="left", pad=4)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)


def _draw_rank_panel(ax, series_data: dict):
    _plot_metric(ax, series_data, "rank_gt0", "rank_gt0_std")
    all_y = []
    for seq in series_data.values():
        for r in seq:
            if r.get("rank_gt0") is not None:
                all_y.append(r["rank_gt0"])
    top = max(all_y) if all_y else 0.05
    ax.set_ylim(0, max(0.05, top * 1.25))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=1))
    ax.set_xticks(K_VALUES)
    ax.set_xlabel("K (number of fallback candidates)")
    ax.set_ylabel("Pods bound at rank > 0")
    ax.set_title("(b) Activation rate rises monotonically with K",
                 loc="left", pad=4)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)


def build_figure(series_data: dict):
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    fig, axes = plt.subplots(1, 2, figsize=(7.0, 2.2))
    fig.subplots_adjust(left=0.08, right=0.99, top=0.80, bottom=0.20, wspace=0.28)

    _draw_score_panel(axes[0], series_data)
    _draw_rank_panel(axes[1], series_data)

    handles = []
    for par, sync, label, color, marker, ls in SERIES:
        handles.append(mlines.Line2D(
            [], [], color=color, marker=marker, linestyle=ls,
            linewidth=1.2, markersize=5.0, label=label,
        ))
    fig.legend(
        handles=handles,
        loc="upper center", ncol=len(handles),
        columnspacing=1.2, handlelength=2.0,
        handletextpad=0.4, frameon=False, fontsize=7.5,
        bbox_to_anchor=(0.5, 1.0),
    )
    return fig


def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"quality-ksweep.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Paper Figure -- Quality robustness across K"
    )
    ap.add_argument("--results", default=DEFAULT_K_DIR,
                    help="K-sweep results dir (default: experiments/results/K)")
    ap.add_argument("--show", action="store_true",
                    help="Display interactively after saving")
    args = ap.parse_args()

    if not os.path.isdir(args.results):
        print(f"results dir not found: {args.results}")
        return 1

    series_data = load_series(args.results)

    print()
    print("K-sweep aggregated quality (median across trials):")
    print(f"  {'series':<18}{'K':>3}{'n':>3}{'score':>9}{'rk>0%':>8}")
    for par, sync, label, *_ in SERIES:
        seq = series_data.get((par, sync), [])
        for r in seq:
            n = r.get("n") or 0
            s = r.get("score")
            f = r.get("rank_gt0")
            print(f"  {label:<18}{r['k']:>3}{n:>3}"
                  f"{(s if s is not None else 0):>9.2f}"
                  f"{((f or 0) * 100):>7.2f}%")
    print()

    fig = build_figure(series_data)
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
