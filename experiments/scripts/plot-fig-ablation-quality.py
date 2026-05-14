#!/usr/bin/env python3
"""
Paper Figure: Cross-paradigm ablation + scheduling quality (merged).

Layout: figure*, 1 row x 3 panels (equal width, ~7.0" total).

  (a) Ablation conflict/throughput — grouped bars, ACF on y-axis,
      throughput (pods/s) annotated on bar tops.
  (b) Mean selected-node score — zoomed y-axis; Δ% vs vanilla annotated.
  (c) Mechanism activation rate — fraction of pods bound at rank > 0.

All panels share the same x-axis: 4 mechanism configurations
  vanilla / mc. / pen. / ParKour  (= Ab0..Ab3)
× 2 paradigms: Event (blue) / Periodic (orange).

Data:
  Panel (a): hardcoded from eval-data.md §0.5.4 (outlier-filtered medians).
  Panels (b)(c): loaded from experiments/results/ablation/Ab{E,P}{0..3}-*/
                 trial-*/metrics-saturation/quality.json.

Usage:
    python plot-fig-ablation-quality.py
    python plot-fig-ablation-quality.py --show
    python plot-fig-ablation-quality.py --results path/to/ablation
"""

import argparse
import csv
import glob
import json
import os
import statistics
import warnings
from collections import defaultdict
from typing import Optional

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import PercentFormatter

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
FIG_DIR = os.path.join(_REPO, "paper", "figs")
DEFAULT_ABLATION_DIR = os.path.join(_REPO, "experiments", "results", "ablation")
ABLATION_CSV_PATH = os.path.join(DEFAULT_ABLATION_DIR, "summary.csv")

# ---------------------------------------------------------------------------
#  Colors — Event = blue, Periodic = orange (matches ablation + quality figs)
# ---------------------------------------------------------------------------
# Color encodes paradigm only; metric (ACF vs BCR) is encoded by hatch.
C_EVENT    = "#4575b4"   # Event (blue)
C_PERIODIC = "#fd8d3c"   # Periodic (orange)
HATCH_ACF  = "////"      # ACF bars
HATCH_BCR  = r"\\\\"     # BCR bars
PARADIGM_COLORS = {"E": C_EVENT, "P": C_PERIODIC}

# ---------------------------------------------------------------------------
#  Style
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":             9,
    "axes.labelsize":        9,
    "axes.titlesize":        8.5,
    "xtick.labelsize":       8.0,
    "ytick.labelsize":       8.0,
    "legend.fontsize":       7.5,
    "axes.linewidth":        0.8,
}

# ---------------------------------------------------------------------------
#  Panel (a) data
# ---------------------------------------------------------------------------
X_LABELS = ["vanilla", "mc.", "pen.", "ParKour"]

# Throughput medians (eval-data.md §0.5.4, hardcoded — not shown as error bars)
TPUT_E = [245.7, 263.7, 244.8, 260.6]
TPUT_P = [ 98.1, 119.7, 140.6, 213.5]

PARADIGMS = ["E", "P"]
CONFIGS   = [("Ab0", "vanilla"), ("Ab1", "mc."),
             ("Ab2", "pen."),    ("Ab3", "ParKour")]

# Experiment name lookup: (Ab_id, paradigm) → experiment column in summary.csv
_EXP_SUFFIX = {"Ab0": "base", "Ab1": "M", "Ab2": "P", "Ab3": "MP"}


def _f_val(s):
    try:
        v = float(s)
        return v if (v == v) else None  # reject NaN
    except (TypeError, ValueError):
        return None


def _filter_csv_trials(trials):
    """Apply the same outlier filter as apply-outlier-filter.py."""
    valid = [t for t in trials
             if (_f_val(t.get("scheduled_pods")) or 0) > 0]
    if not valid:
        return []
    bc_rates = [_f_val(t.get("bind_conflict_rate")) or 0.0 for t in valid]
    n_evidence = sum(1 for r in bc_rates if r > 0.01)
    after_hole = []
    for t in valid:
        sp  = _f_val(t.get("scheduled_pods"))
        ep  = _f_val(t.get("expected_pods")) or sp
        acf = _f_val(t.get("acf_count"))
        bc  = _f_val(t.get("bind_conflict_count"))
        is_hole = (n_evidence >= 2
                   and acf == 0 and bc == 0
                   and ep and sp / ep >= 0.9)
        if not is_hole:
            after_hole.append(t)
    if len(after_hole) < 4:
        return after_hole
    durs = [_f_val(t.get("scheduling_duration_s")) for t in after_hole]
    valid_durs = [d for d in durs if d is not None]
    if len(valid_durs) < 4:
        return after_hole
    q1, q3 = np.percentile(valid_durs, [25, 75])
    upper = max(q3 + 1.5 * (q3 - q1), 2.0 * np.median(valid_durs))
    return [t for t, d in zip(after_hole, durs)
            if d is not None and d <= upper]


def load_ablation_csv() -> dict:
    """Return {(cfg, par): {'acf_med/q1/q3', 'bcr_med/q1/q3'}} from summary.csv.

    acf_rate  = all-candidates-failed (pod escalated to full reschedule).
    bind_conflict_rate = candidate-level bind failures absorbed by the binder.
    For vanilla/pen. configs (K=0), BCR == ACF; for mc./ParKour (K>0), BCR > ACF.
    """
    by_exp = defaultdict(list)
    with open(ABLATION_CSV_PATH, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            by_exp[row.get("experiment", "")].append(row)

    def _iqr_field(kept, field):
        vals = [_f_val(t.get(field)) for t in kept]
        vals = [v for v in vals if v is not None]
        if not vals:
            return {"med": 0.0, "q1": 0.0, "q3": 0.0}
        med, q1, q3 = np.percentile(vals, [50, 25, 75])
        return {"med": float(med), "q1": float(q1), "q3": float(q3)}

    result = {}
    for cfg, _ in CONFIGS:
        for par in PARADIGMS:
            key = f"Ab{par}{cfg[-1]}-{_EXP_SUFFIX[cfg]}"
            kept = _filter_csv_trials(by_exp[key])
            acf = _iqr_field(kept, "acf_rate")
            bcr = _iqr_field(kept, "bind_conflict_rate")
            result[(cfg, par)] = {
                "acf_med": acf["med"], "acf_q1": acf["q1"], "acf_q3": acf["q3"],
                "bcr_med": bcr["med"], "bcr_q1": bcr["q1"], "bcr_q3": bcr["q3"],
            }
    return result

# ---------------------------------------------------------------------------
#  Quality data loading (panels b, c)
# ---------------------------------------------------------------------------

def _find_group(results_dir: str, key: str) -> Optional[str]:
    matches = sorted(glob.glob(os.path.join(results_dir, f"{key}_*")))
    return matches[-1] if matches else None


def _rank_gt0_post_hoc(buckets: dict) -> Optional[float]:
    if not buckets:
        return None
    b_inf = buckets.get("+Inf")
    b_0   = buckets.get("0.0", buckets.get("0"))
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


def _aggregate_group(group_dir: str) -> dict:
    scores, fractions = [], []
    for trial in sorted(glob.glob(os.path.join(group_dir, "trial-*"))):
        q = _load_trial_quality(trial)
        if not q:
            continue
        sn = q.get("selected_node_score") or {}
        rk = q.get("candidate_rank_accepted") or {}
        if sn.get("mean") is not None:
            scores.append(sn["mean"])
        f = rk.get("rank_gt0_fraction")
        if f is None:
            f = _rank_gt0_post_hoc(rk.get("buckets") or {})
        if f is not None:
            fractions.append(f)

    def _iqr(vals):
        if not vals:
            return None, None, None
        arr = np.array(vals)
        med, q1, q3 = np.percentile(arr, [50, 25, 75])
        return float(med), float(q1), float(q3)

    s_med, s_q1, s_q3 = _iqr(scores)
    f_med, f_q1, f_q3 = _iqr(fractions)
    return {"score_median": s_med, "score_q1": s_q1, "score_q3": s_q3,
            "rank_gt0_median": f_med, "rank_gt0_q1": f_q1, "rank_gt0_q3": f_q3}


def load_quality(results_dir: str) -> dict:
    suffix = {"Ab0": "base", "Ab1": "M", "Ab2": "P", "Ab3": "MP"}
    out: dict = {}
    for cfg, _ in CONFIGS:
        out[cfg] = {}
        for par in PARADIGMS:
            key = f"Ab{par}{cfg[-1]}-{suffix[cfg]}"
            gd = _find_group(results_dir, key)
            if gd is None:
                print(f"  MISS {key}")
                out[cfg][par] = None
                continue
            out[cfg][par] = _aggregate_group(gd)
    return out


# ---------------------------------------------------------------------------
#  Drawing helpers
# ---------------------------------------------------------------------------

def _grouped_bars(ax, y_e, y_p, yerr_e=None, yerr_p=None, bw=0.36):
    x = np.arange(len(X_LABELS), dtype=float)
    kw = dict(edgecolor="black", linewidth=0.4,
              error_kw={"elinewidth": 0.7, "capsize": 2.0, "ecolor": "#333333"})
    bars_e = ax.bar(x - bw / 2, y_e, width=bw, color=C_EVENT,
                    yerr=yerr_e, **kw)
    bars_p = ax.bar(x + bw / 2, y_p, width=bw, color=C_PERIODIC,
                    yerr=yerr_p, **kw)
    ax.set_xticks(x)
    ax.set_xticklabels(X_LABELS)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)
    return bars_e, bars_p


def _annotate_bars(ax, bars, vals, color, fontsize=6.5):
    for bar, v in zip(bars, vals):
        ax.annotate(f"{v:.0f}",
                    xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=fontsize, color=color)


def _iqr_err(d, med_key, q1_key, q3_key):
    """Return asymmetric IQR error array (2, n) for a list of stat dicts."""
    return np.array([
        [max(0.0, d[cfg, par][med_key] - d[cfg, par][q1_key]) for cfg, _ in CONFIGS],
        [max(0.0, d[cfg, par][q3_key] - d[cfg, par][med_key]) for cfg, _ in CONFIGS],
    ] for par in ["E"])  # placeholder; caller builds per-paradigm


def draw_ablation(ax, acf_csv):
    """4 bars per group: ACF-E, BCR-E, ACF-P, BCR-P (left to right)."""
    bw = 0.20
    x = np.arange(len(X_LABELS), dtype=float)
    # Offsets: ACF-E, BCR-E, ACF-P, BCR-P
    offsets = [-1.5 * bw, -0.5 * bw, +0.5 * bw, +1.5 * bw]

    def _vals(par, key):
        return [acf_csv[(cfg, par)][key] for cfg, _ in CONFIGS]

    def _yerr(par, med_k, q1_k, q3_k):
        meds = _vals(par, med_k)
        return np.array([
            [max(0.0, m - acf_csv[(cfg, par)][q1_k]) for m, (cfg, _) in zip(meds, CONFIGS)],
            [max(0.0, acf_csv[(cfg, par)][q3_k] - m) for m, (cfg, _) in zip(meds, CONFIGS)],
        ])

    kw = dict(edgecolor="black", linewidth=0.4,
              error_kw={"elinewidth": 0.7, "capsize": 2.0, "ecolor": "#333333"})

    bars_acf_e = ax.bar(x + offsets[0], _vals("E", "acf_med"), width=bw,
                        color=C_EVENT,    hatch=HATCH_ACF,
                        yerr=_yerr("E","acf_med","acf_q1","acf_q3"), **kw)
    bars_bcr_e = ax.bar(x + offsets[1], _vals("E", "bcr_med"), width=bw,
                        color=C_EVENT,    hatch=HATCH_BCR,
                        yerr=_yerr("E","bcr_med","bcr_q1","bcr_q3"), **kw)
    bars_acf_p = ax.bar(x + offsets[2], _vals("P", "acf_med"), width=bw,
                        color=C_PERIODIC, hatch=HATCH_ACF,
                        yerr=_yerr("P","acf_med","acf_q1","acf_q3"), **kw)
    bars_bcr_p = ax.bar(x + offsets[3], _vals("P", "bcr_med"), width=bw,
                        color=C_PERIODIC, hatch=HATCH_BCR,
                        yerr=_yerr("P","bcr_med","bcr_q1","bcr_q3"), **kw)

    _annotate_bars(ax, bars_acf_e, TPUT_E, "#1c3f6e", fontsize=5.5)
    _annotate_bars(ax, bars_acf_p, TPUT_P, "#a85a16", fontsize=5.5)

    ax.set_xticks(x)
    ax.set_xticklabels(X_LABELS)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)

    # ylim: clear tallest bar + IQR cap
    all_tops = (
        [_vals("E","bcr_med")[i] + _yerr("E","bcr_med","bcr_q1","bcr_q3")[1][i]
         for i in range(len(CONFIGS))]
        + [_vals("P","bcr_med")[i] + _yerr("P","bcr_med","bcr_q1","bcr_q3")[1][i]
           for i in range(len(CONFIGS))]
    )
    ax.set_ylim(0, max(0.05, max(all_tops)) * 1.18)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))
    ax.set_title("(a) Conflicts and throughput", loc="left", pad=3)
    ax.text(0.02, 0.97, "ACF bar-top: throughput (pods/s)",
            transform=ax.transAxes, fontsize=7, color="#555555",
            ha="left", va="top", style="italic")


def draw_score(ax, qdata):
    scores_e = [(qdata.get(cfg, {}).get("E") or {}).get("score_median") or 0.0
                for cfg, _ in CONFIGS]
    scores_p = [(qdata.get(cfg, {}).get("P") or {}).get("score_median") or 0.0
                for cfg, _ in CONFIGS]
    # Asymmetric IQR error bars
    yerr_e = np.array([
        [max(0.0, s - (qdata.get(cfg, {}).get("E") or {}).get("score_q1", s))
         for s, (cfg, _) in zip(scores_e, CONFIGS)],
        [max(0.0, (qdata.get(cfg, {}).get("E") or {}).get("score_q3", s) - s)
         for s, (cfg, _) in zip(scores_e, CONFIGS)]])
    yerr_p = np.array([
        [max(0.0, s - (qdata.get(cfg, {}).get("P") or {}).get("score_q1", s))
         for s, (cfg, _) in zip(scores_p, CONFIGS)],
        [max(0.0, (qdata.get(cfg, {}).get("P") or {}).get("score_q3", s) - s)
         for s, (cfg, _) in zip(scores_p, CONFIGS)]])

    x = np.arange(len(X_LABELS), dtype=float)
    bw = 0.36
    kw = dict(edgecolor="black", linewidth=0.4,
              error_kw={"elinewidth": 0.7, "capsize": 2.0, "ecolor": "#333333"})
    ax.bar(x - bw / 2, scores_e, width=bw, yerr=yerr_e,
           color=C_EVENT, **kw)
    ax.bar(x + bw / 2, scores_p, width=bw, yerr=yerr_p,
           color=C_PERIODIC, **kw)

    ax.set_xticks(x)
    ax.set_xticklabels(X_LABELS)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)

    # Zoom y-axis around cluster mean; add vanilla reference lines.
    all_scores = [v for v in scores_e + scores_p if v > 0]
    if all_scores:
        hi_errs = list(yerr_e[1]) + list(yerr_p[1])
        margin = max(max(hi_errs) * 1.5, 2.0)
        ax.set_ylim(min(all_scores) - margin, max(all_scores) + margin)

    base_e = scores_e[0]
    base_p = scores_p[0]
    ax.axhline(base_e, color=C_EVENT,    linestyle=":", linewidth=0.7, alpha=0.7)
    ax.axhline(base_p, color=C_PERIODIC, linestyle=":", linewidth=0.7, alpha=0.7)

    # Δ% annotations vs paired vanilla.
    for i, (score, base, color) in enumerate(
            zip(scores_e, [base_e]*4, ["#1c3f6e"]*4)):
        if i == 0 or score == 0 or base == 0:
            continue
        ax.annotate(f"{(score-base)/base*100:+.2f}%",
                    xy=(x[i] - bw / 2, score),
                    xytext=(0, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6, color=color)
    for i, (score, base, color) in enumerate(
            zip(scores_p, [base_p]*4, ["#a85a16"]*4)):
        if i == 0 or score == 0 or base == 0:
            continue
        ax.annotate(f"{(score-base)/base*100:+.2f}%",
                    xy=(x[i] + bw / 2, score),
                    xytext=(0, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6, color=color)

    ax.set_ylabel("Mean score")
    ax.set_title("(b) Scheduling quality", loc="left", pad=3)
    ax.text(0.02, 0.97, "bar-top: Δ vs vanilla\ndotted = vanilla baseline",
            transform=ax.transAxes, fontsize=7, color="#555555",
            ha="left", va="top", style="italic")


def draw_activation(ax, qdata):
    fracs_e = [(qdata.get(cfg, {}).get("E") or {}).get("rank_gt0_median") or 0.0
               for cfg, _ in CONFIGS]
    fracs_p = [(qdata.get(cfg, {}).get("P") or {}).get("rank_gt0_median") or 0.0
               for cfg, _ in CONFIGS]
    yerr_e = np.array([
        [max(0.0, f - (qdata.get(cfg, {}).get("E") or {}).get("rank_gt0_q1", f))
         for f, (cfg, _) in zip(fracs_e, CONFIGS)],
        [max(0.0, (qdata.get(cfg, {}).get("E") or {}).get("rank_gt0_q3", f) - f)
         for f, (cfg, _) in zip(fracs_e, CONFIGS)]])
    yerr_p = np.array([
        [max(0.0, f - (qdata.get(cfg, {}).get("P") or {}).get("rank_gt0_q1", f))
         for f, (cfg, _) in zip(fracs_p, CONFIGS)],
        [max(0.0, (qdata.get(cfg, {}).get("P") or {}).get("rank_gt0_q3", f) - f)
         for f, (cfg, _) in zip(fracs_p, CONFIGS)]])

    x = np.arange(len(X_LABELS), dtype=float)
    bw = 0.36
    kw = dict(edgecolor="black", linewidth=0.4,
              error_kw={"elinewidth": 0.7, "capsize": 2.0, "ecolor": "#333333"})
    bars_e = ax.bar(x - bw / 2, fracs_e, width=bw, yerr=yerr_e,
                    color=C_EVENT, **kw)
    bars_p = ax.bar(x + bw / 2, fracs_p, width=bw, yerr=yerr_p,
                    color=C_PERIODIC, **kw)

    # ylim must clear the top of every error bar (median + upper IQR), not
    # just the median.  Add a 15% margin above the tallest bar+cap.
    tops = [f + e for f, e in zip(fracs_e + fracs_p,
                                   yerr_e[1].tolist() + yerr_p[1].tolist())]
    max_top = max(tops) if tops else 0.05
    ax.set_ylim(0, max(0.05, max_top * 1.15))
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=1))

    # Annotate bar tops for non-zero bars.
    for bar, v, color in zip(list(bars_e) + list(bars_p),
                              fracs_e + fracs_p,
                              ["#1c3f6e"]*4 + ["#a85a16"]*4):
        if v <= 1e-6:
            continue
        ax.annotate(f"{v*100:.1f}%",
                    xy=(bar.get_x() + bar.get_width() / 2, bar.get_height()),
                    xytext=(0, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6, color=color)

    ax.set_xticks(x)
    ax.set_xticklabels(X_LABELS)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_ylabel("Fallback rate")
    ax.set_title("(c) mc. activation rate", loc="left", pad=3)


# ---------------------------------------------------------------------------
#  Build figure
# ---------------------------------------------------------------------------

def build_figure(ablation_dir: str):
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    print("Loading ablation ACF data (IQR) …")
    acf_csv = load_ablation_csv()
    print("Loading quality data (IQR) …")
    qdata = load_quality(ablation_dir)

    fig = plt.figure(figsize=(7.0, 2.1))
    gs  = GridSpec(1, 3, figure=fig,
                   width_ratios=[5, 4, 4],
                   wspace=0.38, left=0.065, right=0.985,
                   top=0.82, bottom=0.18)

    ax_abl   = fig.add_subplot(gs[0, 0])
    ax_score = fig.add_subplot(gs[0, 1])
    ax_rank  = fig.add_subplot(gs[0, 2])

    draw_ablation(ax_abl, acf_csv)
    draw_score(ax_score, qdata)
    draw_activation(ax_rank, qdata)

    # Two-group legend: left = color/paradigm, right = hatch/metric.
    # A wider column spacing between the two groups creates visual separation.
    h_event  = mpatches.Patch(facecolor=C_EVENT,    edgecolor="black",
                               linewidth=0.4, label="Event")
    h_period = mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black",
                               linewidth=0.4, label="Periodic")
    h_acf    = mpatches.Patch(facecolor="white", edgecolor="black",
                               linewidth=0.4, hatch=HATCH_ACF, label="ACF")
    h_bcr    = mpatches.Patch(facecolor="white", edgecolor="black",
                               linewidth=0.4, hatch=HATCH_BCR, label="BCR")
    fig.legend(handles=[h_acf, h_bcr, h_event, h_period],
               loc="lower center", bbox_to_anchor=(0.5, 0.84),
               ncol=4, frameon=False, fontsize=7.5,
               columnspacing=1.8, handlelength=1.4, handletextpad=0.4)

    return fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for ext in ("pdf", "svg"):
        p = os.path.join(FIG_DIR, f"ablation-quality.{ext}")
        fig.savefig(p, bbox_inches="tight", pad_inches=0)
        paths.append(p)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure — Ablation + quality (3-panel merged)")
    ap.add_argument("--results", default=DEFAULT_ABLATION_DIR,
                    help="Ablation results dir (default: experiments/results/ablation)")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    fig = build_figure(args.results)
    save(fig)
    if args.show:
        plt.show()
    else:
        plt.close(fig)
