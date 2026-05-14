#!/usr/bin/env python3
"""
Paper Figure: Scheduling Quality Trade-off (supports §5.5).

Reads quality.json files produced by pull-quality-metrics.py from each
ablation trial directory, aggregates across trials (median +/- std), and
draws a 2-panel figure.

Layout: 1 row x 2 columns.
  (a) Mean selected node score per config (8 bars: Ab0..Ab3 x Event/Periodic)
      The y-axis is zoomed around the cluster mean (~620) so small absolute
      shifts are visible. Each ParKour bar is annotated with its delta vs
      the paired vanilla baseline (Ab0). The story under cluster scoring
      (NodeResourcesFit etc., reflecting dynamic resource availability) is
      that ParKour ties or modestly exceeds vanilla — the simulation's
      2-4% cost (i.i.d. preference scoring) does not survive the change of
      score semantics.
  (b) rank>0 fraction (% of pods that needed multi-candidate fallback).
      Positioned as "mechanism activation rate" rather than a quality
      cost — orthogonal to the score panel.

Both panels carry error bars showing trial-level std.

Data source: experiments/results/ablation/Ab[EP][0-3]-*/trial-*/
             metrics-saturation/quality.json

Usage:
    python plot-fig-quality.py
    python plot-fig-quality.py --show
    python plot-fig-quality.py --results experiments/results/ablation
"""

import argparse
import glob
import json
import os
import statistics
import warnings
from typing import Optional

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FIG_DIR = os.path.join(_REPO, "paper", "figs")
DEFAULT_RESULTS_DIR = os.path.join(_REPO, "experiments", "results", "ablation")

# ---------------------------------------------------------------------------
#  Colours — matches plot-fig-ablation.py paradigm palette
# ---------------------------------------------------------------------------
C_EVENT    = "#4575b4"   # blue
C_PERIODIC = "#fd8d3c"   # orange

RC_PARAMS = {
    "font.size":           9,
    "axes.labelsize":      9,
    "axes.titlesize":      8.5,
    "xtick.labelsize":     8.0,
    "ytick.labelsize":     8.0,
    "legend.fontsize":     7.5,
    "axes.linewidth":      0.8,
}

# Order of configs along the x-axis of both panels.
CONFIGS = [
    ("Ab0", "baseline"),
    ("Ab1", "+M"),
    ("Ab2", "+P"),
    ("Ab3", "+M+P"),
]
PARADIGMS = ["E", "P"]


# ---------------------------------------------------------------------------
#  Data loading and aggregation
# ---------------------------------------------------------------------------

def _find_group(results_dir: str, key: str) -> Optional[str]:
    matches = sorted(glob.glob(os.path.join(results_dir, f"{key}_*")))
    return matches[-1] if matches else None


def _rank_gt0_post_hoc(buckets: dict) -> Optional[float]:
    """Robust rank>0 fraction computation tolerating both '0' and '0.0' keys."""
    if not buckets:
        return None
    b_inf = buckets.get("+Inf")
    b_0 = buckets.get("0.0", buckets.get("0"))
    if b_inf is None or b_0 is None or b_inf <= 0:
        return None
    return max(0.0, min(1.0, 1.0 - b_0 / b_inf))


def _load_trial_quality(trial_dir: str) -> Optional[dict]:
    path = os.path.join(trial_dir, "metrics-saturation", "quality.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _aggregate(group_dir: str) -> dict:
    """Aggregate trial-level quality metrics into median +/- std."""
    scores: list[float] = []
    ranks: list[float] = []
    fractions: list[float] = []

    for trial in sorted(glob.glob(os.path.join(group_dir, "trial-*"))):
        q = _load_trial_quality(trial)
        if not q:
            continue
        sn = q.get("selected_node_score") or {}
        rk = q.get("candidate_rank_accepted") or {}
        if sn.get("mean") is not None:
            scores.append(sn["mean"])
        if rk.get("mean") is not None:
            ranks.append(rk["mean"])
        # rank>0 may be null in older quality.json files due to a key bug;
        # recompute from raw buckets.
        f = rk.get("rank_gt0_fraction")
        if f is None:
            f = _rank_gt0_post_hoc(rk.get("buckets") or {})
        if f is not None:
            fractions.append(f)

    def _mid_std(vals):
        if not vals:
            return None, None
        if len(vals) == 1:
            return vals[0], 0.0
        return statistics.median(vals), statistics.stdev(vals)

    s_med, s_std = _mid_std(scores)
    r_med, r_std = _mid_std(ranks)
    f_med, f_std = _mid_std(fractions)
    return {
        "n_trials": max(len(scores), len(ranks), len(fractions)),
        "score_median": s_med, "score_std": s_std,
        "rank_median": r_med, "rank_std": r_std,
        "rank_gt0_median": f_med, "rank_gt0_std": f_std,
    }


def load_all(results_dir: str) -> dict:
    suffix = {"Ab0": "base", "Ab1": "M", "Ab2": "P", "Ab3": "MP"}
    out: dict = {}
    for cfg, _ in CONFIGS:
        out[cfg] = {}
        for par in PARADIGMS:
            key = f"Ab{par}{cfg[-1]}-{suffix[cfg]}"
            group_dir = _find_group(results_dir, key)
            if group_dir is None:
                print(f"  MISS {key}: no matching group dir under {results_dir}")
                out[cfg][par] = None
                continue
            agg = _aggregate(group_dir)
            agg["group_dir"] = os.path.relpath(group_dir)
            agg["key"] = key
            out[cfg][par] = agg
    return out


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _grouped_bars(ax, data, field_med, field_std, paradigm_colors):
    n = len(CONFIGS)
    x = np.arange(n, dtype=float)
    bw = 0.36
    bars = {}
    for i, par in enumerate(PARADIGMS):
        offset = (-bw / 2) if i == 0 else (bw / 2)
        meds, stds = [], []
        for cfg, _ in CONFIGS:
            agg = data.get(cfg, {}).get(par)
            meds.append((agg or {}).get(field_med) or 0.0)
            stds.append((agg or {}).get(field_std) or 0.0)
        bars[par] = ax.bar(
            x + offset, meds, width=bw, yerr=stds, capsize=2.0,
            color=paradigm_colors[par], edgecolor="black", linewidth=0.4,
            error_kw={"elinewidth": 0.7, "ecolor": "#222222"},
            label="Event" if par == "E" else "Periodic",
        )
        bars[par][-1].set_hatch("//")
    ax.set_xticks(x)
    ax.set_xticklabels([f"{cfg}\n({tag})" for cfg, tag in CONFIGS])
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)
    return bars


def _draw_score_panel(ax, data: dict):
    """Panel (a): mean selected node score with deltas vs paired vanilla.

    Y-axis zoomed around the cluster mean so small shifts (1-2%) are
    legible. Bars show absolute median; bar-top annotation shows the
    signed delta vs Ab0 of the same paradigm.
    """
    bars = _grouped_bars(
        ax, data, "score_median", "score_std",
        {"E": C_EVENT, "P": C_PERIODIC},
    )

    all_scores, all_stds = [], []
    for cfg, _ in CONFIGS:
        for par in PARADIGMS:
            agg = data.get(cfg, {}).get(par) or {}
            v, s = agg.get("score_median"), agg.get("score_std")
            if v is not None:
                all_scores.append(v)
                all_stds.append(s or 0.0)
    if all_scores:
        margin = max(max(all_stds) * 1.5, 2.0)
        lo = min(all_scores) - margin
        hi = max(all_scores) + margin
        ax.set_ylim(lo, hi)

    ax.set_ylabel("Mean selected node score")
    ax.set_title("(a) Cluster-scope quality vs. vanilla", loc="left", pad=4)

    # Vanilla baseline reference lines (Ab0 medians) for each paradigm.
    base = {}
    for par in PARADIGMS:
        v = (data.get("Ab0", {}).get(par) or {}).get("score_median")
        base[par] = v
        if v is not None:
            color = C_EVENT if par == "E" else C_PERIODIC
            ax.axhline(v, color=color, linestyle=":", linewidth=0.7, alpha=0.65)

    # Annotate Ab1/Ab2/Ab3 bars with delta vs paired vanilla.
    for par in PARADIGMS:
        b0 = base.get(par)
        if b0 is None:
            continue
        for bar, (cfg, _) in zip(bars[par], CONFIGS):
            if cfg == "Ab0":
                continue
            agg = data.get(cfg, {}).get(par) or {}
            v = agg.get("score_median")
            if v is None:
                continue
            delta_pct = (v - b0) / b0 * 100.0
            sign = "+" if delta_pct >= 0 else ""
            color = "#1c3f6e" if par == "E" else "#a85a16"
            ax.annotate(
                f"{sign}{delta_pct:.2f}%",
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 1.5), textcoords="offset points",
                ha="center", va="bottom",
                fontsize=6.2, color=color,
            )

    ax.text(
        0.005, 0.97, "Bar-top: Δ vs vanilla (Ab0); dotted = vanilla baseline",
        transform=ax.transAxes, fontsize=6.5, color="#444444",
        ha="left", va="top", style="italic",
    )


def _draw_rank_panel(ax, data: dict):
    """Panel (b): rank>0 fraction = mechanism activation rate."""
    bars = _grouped_bars(
        ax, data, "rank_gt0_median", "rank_gt0_std",
        {"E": C_EVENT, "P": C_PERIODIC},
    )

    all_f = []
    for cfg, _ in CONFIGS:
        for par in PARADIGMS:
            agg = data.get(cfg, {}).get(par) or {}
            v = agg.get("rank_gt0_median")
            if v is not None:
                all_f.append(v)
    top = max(all_f) if all_f else 0.05
    ax.set_ylim(0, max(0.05, top * 1.4))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=1))

    ax.set_ylabel("Pods bound at rank > 0")
    ax.set_title("(b) Mechanism activation rate", loc="left", pad=4)

    # Annotate bar tops with the median %.
    for par in PARADIGMS:
        for bar, (cfg, _) in zip(bars[par], CONFIGS):
            agg = data.get(cfg, {}).get(par) or {}
            v = agg.get("rank_gt0_median")
            if v is None or v <= 1e-6:
                continue
            color = "#1c3f6e" if par == "E" else "#a85a16"
            ax.annotate(
                f"{v * 100:.1f}%",
                xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                xytext=(0, 1.5), textcoords="offset points",
                ha="center", va="bottom",
                fontsize=6.2, color=color,
            )


def build_figure(data: dict):
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    fig, axes = plt.subplots(1, 2, figsize=(3.5, 2.2))
    fig.subplots_adjust(left=0.08, right=0.99, top=0.78, bottom=0.18, wspace=0.28)

    _draw_score_panel(axes[0], data)
    _draw_rank_panel(axes[1], data)

    h_event    = mpatches.Patch(facecolor=C_EVENT, edgecolor="black",
                                linewidth=0.4, label="Event")
    h_periodic = mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black",
                                linewidth=0.4, label="Periodic")
    h_parkour  = mpatches.Patch(facecolor="white", edgecolor="black",
                                linewidth=0.4, hatch="//", label="ParKour (Ab3)")
    fig.legend(
        handles=[h_event, h_periodic, h_parkour],
        loc="upper center", ncol=3,
        columnspacing=1.4, handlelength=1.4,
        handletextpad=0.4, frameon=False, fontsize=8,
        bbox_to_anchor=(0.5, 1.0),
    )
    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"quality.{ext}")
        fig.savefig(p, bbox_inches="tight")
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Paper Figure -- Scheduling quality trade-off"
    )
    ap.add_argument("--results", default=DEFAULT_RESULTS_DIR,
                    help="Ablation results dir (default: experiments/results/ablation)")
    ap.add_argument("--show", action="store_true",
                    help="Display interactively after saving")
    args = ap.parse_args()

    if not os.path.isdir(args.results):
        print(f"results dir not found: {args.results}")
        return 1

    data = load_all(args.results)
    print()
    print("Aggregated quality metrics (median across trials):")
    print(f"  {'cfg':<6}{'par':<5}{'n':<4}{'score':>8}{'rank':>7}{'rk>0%':>8}")
    for cfg, _ in CONFIGS:
        for par in PARADIGMS:
            agg = data.get(cfg, {}).get(par)
            if not agg:
                print(f"  {cfg:<6}{par:<5}{'-':<4}{'-':>8}{'-':>7}{'-':>8}")
                continue
            print(f"  {cfg:<6}{par:<5}{agg['n_trials']:<4}"
                  f"{(agg['score_median'] or 0):>8.2f}"
                  f"{(agg['rank_median'] or 0):>7.3f}"
                  f"{((agg['rank_gt0_median'] or 0) * 100):>7.2f}%")
    print()

    fig = build_figure(data)
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
