#!/usr/bin/env python3
"""
Paper Figures: Cross-paradigm ablation + scheduling quality (split).

Layout: two single-column figures.

  (a) Ablation conflict/throughput — grouped bars, ACF on y-axis,
      throughput (pods/s) annotated on bar tops.
  (b)(c) Scheduling quality — vertically stacked panels for the mean
      selected-node score and accepted-rank composition.

All panels share the same x-axis: 4 mechanism configurations
  Vanilla / Multi-candidate only / Penalty only / Both mechanisms
× 2 paradigms: Event (blue) / Periodic (orange).

Data:
  Panel (a): hardcoded from eval-data.md §0.5.4 (outlier-filtered medians).
  Panels (b)(c): loaded from experiments/results/ablation/Ab{E,P}{0..3}-*/
                 trial-*/metrics-saturation/quality.json.
  Panel (c) chart data:
                 experiments/results/ablation/figure9c-rank-summary.json.

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
#  Style — fonttype 42 keeps the PDF/PS text as embedded TrueType subsets.
#  Matplotlib defaults to Type 3, which ACM camera-ready rejects and which
#  also leaves the figure text unsearchable.
# ---------------------------------------------------------------------------
RC_PARAMS = {
    "font.size":             9,
    "axes.labelsize":        9,
    "axes.titlesize":        8.5,
    "xtick.labelsize":       8.0,
    "ytick.labelsize":       8.0,
    "legend.fontsize":       7.5,
    "axes.linewidth":        0.8,
    "pdf.fonttype":          42,
    "ps.fonttype":           42,
    "svg.fonttype":          "none",
    # Arial for text and mathtext alike. Left alone, the family arrives only
    # as a side effect of seaborn's style dict and mathtext keeps its own
    # DejaVu set, so $K$-style labels render in a different face to the axes.
    "font.family":           "sans-serif",
    "font.sans-serif":       ["Arial", "Helvetica", "DejaVu Sans"],
    "mathtext.fontset":      "custom",
    "mathtext.rm":           "Arial",
    "mathtext.it":           "Arial:italic",
    "mathtext.bf":           "Arial:bold",
}

# ---------------------------------------------------------------------------
#  Panel (a) data
# ---------------------------------------------------------------------------
X_LABELS = [
    "Vanilla",
    "Multi-candidate\nonly",
    "Penalty\nonly",
    "ParKour",
]

# Throughput medians (eval-data.md §0.5.4, hardcoded — not shown as error bars)
TPUT_E = [245.7, 263.7, 244.8, 260.6]
TPUT_P = [ 98.1, 119.7, 140.6, 213.5]

PARADIGMS = ["E", "P"]
CONFIGS = [
    ("Ab0", "Vanilla"),
    ("Ab1", "Multi-candidate only"),
    ("Ab2", "Penalty only"),
    ("Ab3", "ParKour"),
]
RANK_CONFIGS = [
    ("Ab1", "Multi-candidate\nonly"),
    ("Ab3", "ParKour"),
]
RANK_COLORS = {
    "E": ["#dbe6f2", "#7aa6d1", "#285a8f"],
    "P": ["#fee5d3", "#fdae6b", "#d95f0e"],
}

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


def _rank_distribution(rank_metric: dict) -> Optional[dict]:
    """Recover exact rank 0/1/2 counts from cumulative histogram buckets."""
    buckets = rank_metric.get("buckets") or {}
    total = rank_metric.get("count", buckets.get("+Inf"))
    if total is None or total <= 0:
        return None

    def _bucket(rank: int) -> Optional[float]:
        value = buckets.get(f"{rank}.0", buckets.get(str(rank)))
        return float(value) if value is not None else None

    b0, b1, b2 = (_bucket(rank) for rank in range(3))
    if b0 is None or b1 is None or b2 is None:
        return None

    counts = [b0, max(0.0, b1 - b0), max(0.0, b2 - b1)]
    fractions = [count / float(total) for count in counts]
    return {
        "total_successful_bindings": float(total),
        "rank_counts": {str(rank): float(count)
                        for rank, count in enumerate(counts)},
        "rank_fractions": {str(rank): float(fraction)
                           for rank, fraction in enumerate(fractions)},
        "cumulative_fractions": {
            "0": float(fractions[0]),
            "1": float(fractions[0] + fractions[1]),
        },
    }


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
    rank_fractions = {str(rank): [] for rank in range(3)}
    cumulative_fractions = {str(rank): [] for rank in range(2)}
    rank_trials = []
    for trial in sorted(glob.glob(os.path.join(group_dir, "trial-*"))):
        q = _load_trial_quality(trial)
        if not q:
            continue
        sn = q.get("selected_node_score") or {}
        rk = q.get("candidate_rank_accepted") or {}
        if sn.get("mean") is not None:
            scores.append(sn["mean"])
        distribution = _rank_distribution(rk)
        f = None
        if distribution is not None:
            f = (distribution["rank_fractions"]["1"]
                 + distribution["rank_fractions"]["2"])
            for rank in range(3):
                rank_fractions[str(rank)].append(
                    distribution["rank_fractions"][str(rank)])
            for rank in range(2):
                cumulative_fractions[str(rank)].append(
                    distribution["cumulative_fractions"][str(rank)])
            rank_trials.append({
                "trial": os.path.basename(trial),
                **distribution,
            })
        elif rk.get("rank_gt0_fraction") is not None:
            f = rk["rank_gt0_fraction"]
        else:
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
    rank_summary = {}
    for rank, values in rank_fractions.items():
        med, q1, q3 = _iqr(values)
        rank_summary[rank] = {"median": med, "q1": q1, "q3": q3}
    cumulative_summary = {}
    for rank, values in cumulative_fractions.items():
        med, q1, q3 = _iqr(values)
        cumulative_summary[rank] = {"median": med, "q1": q1, "q3": q3}
    return {"score_median": s_med, "score_q1": s_q1, "score_q3": s_q3,
            "rank_gt0_median": f_med, "rank_gt0_q1": f_q1, "rank_gt0_q3": f_q3,
            "rank_summary": rank_summary,
            "cumulative_summary": cumulative_summary,
            "rank_trials": rank_trials}


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
            out[cfg][par]["experiment"] = key
            out[cfg][par]["group_dir"] = gd
    return out


def export_rank_data(qdata: dict, output_path: str) -> dict:
    """Write the per-trial and median/IQR data consumed by Figure 9(c)."""
    output = {
        "source": "experiments/results/ablation/*/trial-*/metrics-saturation/quality.json",
        "method": (
            "For each trial, exact rank counts are differences of cumulative "
            "parasched_candidate_rank_accepted buckets. Median, Q1, and Q3 "
            "are then computed independently for each rank fraction."
        ),
        "configurations": {},
    }
    for cfg, label in RANK_CONFIGS:
        clean_label = label.replace("-\n", "-").replace("\n", " ")
        config_out = {"label": clean_label, "paradigms": {}}
        for paradigm, paradigm_label in [("E", "event-driven"),
                                          ("P", "periodic")]:
            data = qdata.get(cfg, {}).get(paradigm)
            if not data:
                continue
            config_out["paradigms"][paradigm_label] = {
                "experiment": data["experiment"],
                "trials": data["rank_trials"],
                "rank_fraction_summary": data["rank_summary"],
                "cumulative_fraction_summary": data["cumulative_summary"],
                "fallback_fraction_summary": {
                    "median": data["rank_gt0_median"],
                    "q1": data["rank_gt0_q1"],
                    "q3": data["rank_gt0_q3"],
                },
            }
        output["configurations"][cfg] = config_out

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, sort_keys=True)
        f.write("\n")
    print(f"Saved rank chart data: {output_path}")
    return output


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
    ax.set_xticklabels(X_LABELS, fontsize=6.5)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)
    return bars_e, bars_p


def _annotate_bars(ax, bars, vals, color, fontsize=6.5, upper_err=None):
    """Annotate bar tops, clearing the bar's own upper error-bar cap.

    Anchoring at ``bar.get_height()`` puts the text inside the IQR whisker, which
    struck through the throughput numbers on the taller periodic bars.
    """
    for i, (bar, v) in enumerate(zip(bars, vals)):
        top = bar.get_height()
        if upper_err is not None:
            top += float(upper_err[i])
        ax.annotate(f"{v:.0f}",
                    xy=(bar.get_x() + bar.get_width() / 2, top),
                    xytext=(0, 2.5), textcoords="offset points",
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

    _annotate_bars(ax, bars_acf_e, TPUT_E, "#1c3f6e", fontsize=6.5,
                   upper_err=_yerr("E","acf_med","acf_q1","acf_q3")[1])
    _annotate_bars(ax, bars_acf_p, TPUT_P, "#a85a16", fontsize=6.5,
                   upper_err=_yerr("P","acf_med","acf_q1","acf_q3")[1])

    ax.set_xticks(x)
    ax.set_xticklabels(X_LABELS, fontsize=7.5)
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
    # The axis carries both ACF and BCR, so it is labelled with the quantity they
    # share rather than with either metric's name. What the bar-top numbers mean
    # is stated in the caption instead of in small print inside the axes.
    ax.set_ylabel("Conflict rate")


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
    ax.set_xticklabels(X_LABELS, fontsize=7.5)
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
                    xytext=(-2, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6.5, color=color)
    for i, (score, base, color) in enumerate(
            zip(scores_p, [base_p]*4, ["#a85a16"]*4)):
        if i == 0 or score == 0 or base == 0:
            continue
        ax.annotate(f"{(score-base)/base*100:+.2f}%",
                    xy=(x[i] + bw / 2, score),
                    xytext=(2, 1.5), textcoords="offset points",
                    ha="center", va="bottom", fontsize=6.5, color=color)

    ax.set_ylabel("Mean score")
    ax.set_title("(a) Scheduling quality", loc="left", pad=3)
    ax.text(0.02, 0.97, "bar-top: Δ vs Vanilla\ndotted = Vanilla baseline",
            transform=ax.transAxes, fontsize=7, color="#555555",
            ha="left", va="top", style="italic")


def draw_activation(ax, qdata):
    x = np.arange(len(RANK_CONFIGS), dtype=float)
    bw = 0.36
    ymax = 0.30
    kw = dict(edgecolor="black", linewidth=0.4)

    for paradigm, offset in [("E", -bw / 2), ("P", bw / 2)]:
        rank_values = np.array([
            [(qdata.get(cfg, {}).get(paradigm) or {})
             .get("rank_summary", {}).get(str(rank), {}).get("median") or 0.0
             for cfg, _ in RANK_CONFIGS]
            for rank in range(3)
        ])
        # Componentwise medians can differ from unity by floating-point noise.
        totals = rank_values.sum(axis=0)
        rank_values = np.divide(
            rank_values, totals, out=np.zeros_like(rank_values),
            where=totals > 0)

        bottom = np.zeros(len(RANK_CONFIGS), dtype=float)
        for rank in [2, 1, 0]:
            bars = ax.bar(
                x + offset, rank_values[rank], width=bw, bottom=bottom,
                color=RANK_COLORS[paradigm][rank], **kw)
            for bar, segment_bottom, value in zip(
                    bars, bottom, rank_values[rank]):
                if value < 0.009:
                    continue
                if rank == 0:
                    visible_height = max(0.0, ymax - segment_bottom)
                    label_y = segment_bottom + visible_height * 0.55
                else:
                    label_y = segment_bottom + value / 2
                ax.annotate(
                    f"{value*100:.1f}%",
                    xy=(bar.get_x() + bar.get_width() / 2,
                        label_y),
                    ha="center", va="center", fontsize=6.0,
                    color="black" if rank == 0 else "white")
            bottom += rank_values[rank]

        # Show IQR at the rank-2 and total-fallback (rank 2+1) boundaries.
        # Both are aggregated per trial rather than summing rank-wise IQRs.
        boundary_summaries = [
            [
                (qdata.get(cfg, {}).get(paradigm) or {})
                .get("rank_summary", {}).get("2", {})
                for cfg, _ in RANK_CONFIGS
            ],
            [
                {
                    "median": (qdata.get(cfg, {}).get(paradigm) or {})
                    .get("rank_gt0_median"),
                    "q1": (qdata.get(cfg, {}).get(paradigm) or {})
                    .get("rank_gt0_q1"),
                    "q3": (qdata.get(cfg, {}).get(paradigm) or {})
                    .get("rank_gt0_q3"),
                }
                for cfg, _ in RANK_CONFIGS
            ],
        ]
        for summaries in boundary_summaries:
            med = np.array([summary.get("median") or 0.0
                            for summary in summaries])
            q1 = np.array([summary.get("q1") or value
                           for summary, value in zip(summaries, med)])
            q3 = np.array([summary.get("q3") or value
                           for summary, value in zip(summaries, med)])
            ax.errorbar(
                x + offset, med,
                yerr=np.vstack([np.maximum(0.0, med - q1),
                                np.maximum(0.0, q3 - med)]),
                fmt="none", ecolor="#333333", elinewidth=0.7, capsize=2.0)

    # Rank 0 dominates every bar. Truncate its upper portion so fallback
    # ranks remain legible, and mark the clipped bars at the top boundary.
    for xpos in np.concatenate([x - bw / 2, x + bw / 2]):
        ax.plot(
            [xpos - bw * 0.28, xpos - bw * 0.05],
            [ymax - 0.008, ymax + 0.008],
            color="black", linewidth=0.7, clip_on=False)
        ax.plot(
            [xpos + bw * 0.05, xpos + bw * 0.28],
            [ymax - 0.008, ymax + 0.008],
            color="black", linewidth=0.7, clip_on=False)

    ax.set_ylim(0, ymax)
    ax.yaxis.set_major_formatter(PercentFormatter(1.0, decimals=0))

    ax.set_xticks(x)
    ax.set_xticklabels([label for _, label in RANK_CONFIGS], fontsize=7.5)
    ax.grid(True, axis="y", linestyle="--", alpha=0.45, linewidth=0.6)
    ax.set_axisbelow(True)
    ax.set_ylabel("Successful bindings\n(rank 0 truncated)")
    ax.set_title("(b) Accepted-rank composition", loc="left", pad=3)


# ---------------------------------------------------------------------------
#  Build figures
# ---------------------------------------------------------------------------

def _legend_handles():
    """Return the legend handles shared by the split paper figures."""
    h_event = mpatches.Patch(facecolor=C_EVENT, edgecolor="black",
                             linewidth=0.4, label="Event-driven")
    h_period = mpatches.Patch(facecolor=C_PERIODIC, edgecolor="black",
                              linewidth=0.4, label="Periodic")
    h_acf = mpatches.Patch(facecolor="white", edgecolor="black",
                           linewidth=0.4, hatch=HATCH_ACF, label="ACF")
    h_bcr = mpatches.Patch(facecolor="white", edgecolor="black",
                           linewidth=0.4, hatch=HATCH_BCR, label="BCR")
    h_rank0 = mpatches.Patch(facecolor="#eeeeee", edgecolor="black",
                             linewidth=0.4, label="Rank 0")
    h_rank1 = mpatches.Patch(facecolor="#999999", edgecolor="black",
                             linewidth=0.4, label="Rank 1")
    h_rank2 = mpatches.Patch(facecolor="#444444", edgecolor="black",
                             linewidth=0.4, label="Rank 2")
    return h_event, h_period, h_acf, h_bcr, h_rank0, h_rank1, h_rank2


def build_figures(ablation_dir: str, rank_data_path: Optional[str] = None):
    """Build separate single-column ablation and scheduling-quality figures."""
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    print("Loading ablation ACF data (IQR) …")
    acf_csv = load_ablation_csv()
    print("Loading quality data (IQR) …")
    qdata = load_quality(ablation_dir)
    if rank_data_path:
        export_rank_data(qdata, rank_data_path)

    h_event, h_period, h_acf, h_bcr, h_rank0, h_rank1, h_rank2 = (
        _legend_handles())

    ablation_fig = plt.figure(figsize=(3.5, 2.1))
    ablation_gs = GridSpec(1, 1, figure=ablation_fig,
                            left=0.13, right=0.985,
                            top=0.82, bottom=0.18)
    ax_abl = ablation_fig.add_subplot(ablation_gs[0, 0])
    draw_ablation(ax_abl, acf_csv)
    ablation_fig.legend(handles=[h_acf, h_bcr, h_event, h_period],
                        loc="lower center", bbox_to_anchor=(0.5, 0.84),
                        ncol=4, frameon=False, fontsize=7.0,
                        columnspacing=0.8, handlelength=1.1,
                        handletextpad=0.25)

    quality_fig = plt.figure(figsize=(3.5, 4.2))
    quality_gs = GridSpec(2, 1, figure=quality_fig,
                          hspace=0.45, left=0.16, right=0.985,
                          top=0.84, bottom=0.10)
    ax_score = quality_fig.add_subplot(quality_gs[0, 0])
    ax_rank = quality_fig.add_subplot(quality_gs[1, 0])

    draw_score(ax_score, qdata)
    draw_activation(ax_rank, qdata)
    quality_fig.legend(handles=[h_event, h_period, h_rank2, h_rank1, h_rank0],
                       loc="lower center", bbox_to_anchor=(0.5, 0.86),
                       ncol=5, frameon=False, fontsize=7.0,
                       columnspacing=0.6, handlelength=1.1,
                       handletextpad=0.25)

    return ablation_fig, quality_fig


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(figures):
    os.makedirs(FIG_DIR, exist_ok=True)
    paths = []
    for stem, fig in zip(("ablation-quality-a", "ablation-quality-bc"),
                         figures):
        for ext in ("pdf", "svg"):
            path = os.path.join(FIG_DIR, f"{stem}.{ext}")
            fig.savefig(path, bbox_inches="tight", pad_inches=0)
            paths.append(path)
    print(f"Saved: {', '.join(paths)}")


# ---------------------------------------------------------------------------
#  Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figures — Ablation + quality (two single-column figures)")
    ap.add_argument("--results", default=DEFAULT_ABLATION_DIR,
                    help="Ablation results dir (default: experiments/results/ablation)")
    ap.add_argument(
        "--rank-data-out",
        help=("Output JSON for Figure 9(c) rank data "
              "(default: <results>/figure9c-rank-summary.json)"))
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    rank_data_path = (
        args.rank_data_out
        or os.path.join(args.results, "figure9c-rank-summary.json")
    )
    figures = build_figures(args.results, rank_data_path)
    save(figures)
    if args.show:
        plt.show()
    else:
        for fig in figures:
            plt.close(fig)
