#!/usr/bin/env python3
"""
Paper Figure: data-plane delay/failure sensitivity (module F, supports §5.7).

Layout (single column, 3.33" wide, two stacked panels sharing the x axis):
  (a) All-candidates-failed rate (ACF), log y axis.
  (b) Bind throughput in pods/s, linear y axis.
  Both panels: x = three data-plane profiles (none / delay / delay+fail),
  four bars per group = event-driven K=0, event-driven K=2, globSync K=0,
  globSync K=2.  Whiskers span the min and max of that cell's trials
  (n=3 event-driven, n=5 periodic).

Colour encodes the mechanism setting (red = K=0 same-code baseline, green =
K=2 with w=0.5), shade encodes the synchronization paradigm (dark =
event-driven, light = single-partition globSync at G=1 s), matching
plot-fig-occupancy-intervals.py.

Data
----
Everything is read from the anchored module-F bundle
`bin/module-f-48rounds-20260913/`, collected 2026-09-12..13 — the only campaign
`experiments/stage-F-report.md` §1 clears for the paper.  The two earlier
batches (the 45-round pilot at K=4 / diffSync M=5 with Gödel arms, and the
36-round synthetic-profile batch) are void: the pilot's parameters do not match
the design, and both were hit by the KWOK dual-controller defect that made
`Dreal` rounds actually measure `Z0` behaviour.  Do not plot them.

  rounds.csv   49 rows, 48 with kept=True, one row per round:
    F1-E  E2 vs E3, Z0 + Dreal,   3 trials, 12 rounds
    F1-P  P1 vs P4, Z0 + Dreal,   5 trials, 20 rounds
    F2-E  E2 vs E3, Dreal-F1,     3 trials,  6 rounds
    F2-P  P1 vs P4, Dreal-F1,     5 trials, 10 rounds

The single `kept=False` row is F2-P order 9, whose observed startup-failure
rate put the configured 1% outside its Wilson interval; it is dropped here and
kept in the archive for audit.

Delay profile: `Dreal` is four weighted buckets
`[581, 830] / (830, 1109] / (1109, 2384] / (2384, 4768]` ms, weights
5000/4000/900/100, derived from the upstream Kubernetes scalability CI job
`gce-5000Nodes` in its burst phase (pod-startup P50/P90/P99 =
830 / 1109 / 2384 ms), with the two ends bounded by 0.7*P50 and 2*P99.
Theoretical mean 0.933 s, measured 0.96 s.  `Dreal-F1` adds a 1% post-bind
startup-failure injection.  The buckets are anchored to public real-node
measurements; the injection itself is synthetic (a KWOK Stage).

Metric definitions
------------------
Panel (b) plots `q_bind_at_t99` = 9900 / t99, the placement rate at a uniform
completion cut point, which is what the bundle's own DATA.md §5(b) prescribes
for cross-condition comparison.  The archived `throughput_raw_pods_per_s`
divides 10,000 pods by *each round's own* window end, so it measures how long a
round ran rather than how fast it scheduled, and under the periodic profiles the
endgame dominates that window: at one pod per node with zero headroom the run
only finishes once the last pending pod is matched to the last free node, which
a periodic view reveals once per G.  Tails reach 74-76% of the whole window
there, against 3.6-8.0% on the event-driven arms, so the raw field inflates the
periodic delay gain to +414% and deflates the delay+fail one to +92%; at T99
both read +53%.  `--throughput-metric raw` reproduces the whole-window view.

Right-censoring only affects the raw field.  Three F2-P rounds hit the runner's
1000 s ceiling and are archived at the 10.0 pods/s floor, but the truncation
happens after 9999/10000 pods are placed, so t90 and t99 are fully observed and
the rounds are valid steady-state data (DATA.md §5(a)).  They are hatched in
panel (b) only when the raw metric is selected.

Panel (a) plots ACF over the same [round start, T99] prefix, so both panels
share one cut point.  The archived `acf_rate` is a whole-window aggregate and
the endgame is pure conflict, which inflates it on exactly the two periodic
data-plane profiles whose tails are long: vanilla at `delay` reads 74.4% over
the whole window against 52.9% up to T99, and ParKour at `delay+fail` reads
58.6% against 28.4%.  The event-driven arms and periodic `none` move by at most
0.06 pp between the two, which is how we know the cut point itself introduces
no offset.  `--acf-metric whole` reproduces the archived whole-window view.

The windowed values come from `bin/module-f-acf-windowed-20260916/`, rebuilt
from the binder's raw Prometheus counters at 1 s resolution rather than from
the 15 s archived range series: the binder restarts once per round so its
counters start at zero, making the prefix rate acf(T)/(bind_success(T)+acf(T))
with the runner's own denominator convention.  Every round was checked against
its archived round-summary.json before use; see that directory's MANIFEST.md.

Usage:
    python plot-fig-dataplane-sensitivity.py
    python plot-fig-dataplane-sensitivity.py --show
    python plot-fig-dataplane-sensitivity.py --paper-figs-dir ''   # no promote
"""

import argparse
import csv
import os
import shutil
import statistics
import warnings

import matplotlib.patches as mpatches
import matplotlib.pyplot as plt
import numpy as np
import seaborn as sns
from matplotlib.gridspec import GridSpec
from matplotlib.ticker import (FuncFormatter, LogLocator, MultipleLocator,
                               NullFormatter)

warnings.filterwarnings("ignore", message=".*iCCP.*")

# ---------------------------------------------------------------------------
#  Paths
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.abspath(os.path.join(_HERE, "..", ".."))
DEFAULT_BUNDLE = os.path.join(_REPO, "bin", "module-f-48rounds-20260913",
                              "module-f-48rounds-20260913")
DEFAULT_ACF_WINDOWED = os.path.join(_REPO, "bin",
                                    "module-f-acf-windowed-20260916",
                                    "acf-windowed.csv")
DEFAULT_OUTPUT_DIR = os.path.join(_REPO, "experiments", "results", "figures")
DEFAULT_PAPER_FIG_DIR = os.path.join(_REPO, "paper", "figs")
DEFAULT_STEM = "Fig-dataplane-sensitivity"
DEFAULT_PAPER_STEM = "dataplane-sensitivity"
DEFAULT_WIDTH = 3.33       # ACM sigplan \columnwidth
DEFAULT_HEIGHT = 1.28      # panel area, excluding the legend strip

# Which round belongs to which plotted cell.  Method -> (paradigm, K).
METHODS = {
    "E2": ("event", 0),        # ParKour codebase, event-driven, K=0, w=0
    "E3": ("event", 2),        # ParKour, event-driven, K=2, w=0.5
    "P1": ("periodic", 0),     # ParKour codebase, globSync M=1 G=1s, K=0, w=0
    "P4": ("periodic", 2),     # ParKour, globSync M=1 G=1s, K=2, w=0.5
}
# Archive profile name -> figure x-axis label.
PROFILE_KEYS = {"Z0": "none", "Dreal": "delay", "Dreal-F1": "delay+fail"}
PROFILES = ["none", "delay", "delay+fail"]
PARADIGMS = ["event", "periodic"]
BARS = [("event", 0), ("event", 2), ("periodic", 0), ("periodic", 2)]
# Trials per cell, by paradigm: the design gives periodic more repeats.
EXPECTED_TRIALS = {"event": 3, "periodic": 5}
# Throughput definitions selectable from the command line; see the module
# docstring for why the uniform cut point is the default.
THROUGHPUT_COLUMNS = {
    "t99": ("q_bind_at_t99", "(b) Pods/s"),
    "raw": ("throughput_raw_pods_per_s", "(b) Pods/s"),
}
# ACF definitions.  The windowed one is joined in from the reconstruction CSV;
# the whole-window one is the field the runner archived.
ACF_COLUMNS = {
    "t99": ("t99_acf_rate", "(a) ACF%"),
    "whole": ("acf_rate", "(a) ACF%"),
}

# ---------------------------------------------------------------------------
#  Colours — same palette as plot-fig-occupancy-intervals.py
# ---------------------------------------------------------------------------
_rdbu = sns.color_palette("RdBu", 11)
C_RED_D = "#{:02x}{:02x}{:02x}".format(
    int(_rdbu[1][0] * 255), int(_rdbu[1][1] * 255), int(_rdbu[1][2] * 255))
C_RED_L = "#{:02x}{:02x}{:02x}".format(
    int(_rdbu[3][0] * 255), int(_rdbu[3][1] * 255), int(_rdbu[3][2] * 255))
C_GREEN_D = "#238b45"
C_GREEN_L = "#74c476"
C_EDGE = "#333333"

BAR_COLOR = {
    ("event", 0): C_RED_D,
    ("event", 2): C_GREEN_D,
    ("periodic", 0): C_RED_L,
    ("periodic", 2): C_GREEN_L,
}

RC_PARAMS = {
    "font.size": 7.5,
    "axes.labelsize": 7.0,
    "axes.titlesize": 7.5,
    "xtick.labelsize": 6.8,
    "ytick.labelsize": 6.8,
    "legend.fontsize": 6.5,
    "axes.linewidth": 0.7,
    "pdf.fonttype": 42,
    "ps.fonttype": 42,
    # Arial for text and mathtext alike. Left alone, the family arrives only
    # as a side effect of seaborn's style dict and mathtext keeps its own
    # DejaVu set, so the $[0,T_{99}]$ legend title renders in another face.
    "font.family": "sans-serif",
    "font.sans-serif": ["Arial", "Helvetica", "DejaVu Sans"],
    "mathtext.fontset": "custom",
    "mathtext.rm": "Arial",
    "mathtext.it": "Arial:italic",
    "mathtext.bf": "Arial:bold",
}
ANNOT_FONTSIZE = 6.5       # printed size floor for this paper
ERR_KW = {"elinewidth": 0.6, "capthick": 0.6, "ecolor": C_EDGE, "capsize": 1.2}


# ---------------------------------------------------------------------------
#  Loading — one record per valid round
# ---------------------------------------------------------------------------

def _flag(value):
    """Interpret the CSV's boolean-ish cells, treating blanks as false."""
    return str(value).strip().lower() == "true"


def load_rounds(bundle, throughput_metric="t99", acf_metric="t99",
                acf_windowed=DEFAULT_ACF_WINDOWED):
    """Read the anchored bundle's analysis table.

    Args:
        bundle: Path to the directory holding `rounds.csv`.
        throughput_metric: Key into THROUGHPUT_COLUMNS selecting which
            throughput definition panel (b) receives.
        acf_metric: Key into ACF_COLUMNS. "t99" joins the windowed rate in from
            `acf_windowed`; "whole" uses the archived whole-window field.
        acf_windowed: Path to the reconstruction CSV.

    Returns:
        (records, audit) where `records` holds one entry per kept round and
        `audit` holds the startup-failure gate numbers of the F2 matrices,
        including the dropped round the validity paragraph quotes.

    Raises:
        RuntimeError: If a windowed ACF is requested but some kept round has no
            verified reconstruction, which would silently mix cut points.
    """
    column = THROUGHPUT_COLUMNS[throughput_metric][0]
    acf_column = ACF_COLUMNS[acf_metric][0]
    windowed = {}
    if acf_metric != "whole":
        with open(acf_windowed, newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if _flag(row["verified"]):
                    windowed[(row["run_id"], row["round"])] = row
    path = os.path.join(bundle, "rounds.csv")
    with open(path, newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))

    records, audit = [], []
    for row in rows:
        if row["experiment"].startswith("F2"):
            audit.append({
                "matrix": row["experiment"],
                "order": row["order"],
                "method": row["method"],
                "kept": _flag(row["kept"]),
                "failure_rate": float(row["injection_observed"] or "nan"),
                "gate_pass": _flag(row["injection_pass"]),
                "semantics_ok": row["injection_failed"] == row["failed_semantics_valid"],
            })
        if not _flag(row["kept"]) or row["method"] not in METHODS:
            continue
        paradigm, k = METHODS[row["method"]]
        if acf_metric == "whole":
            acf_value = float(row["acf_rate"])
        else:
            key = (row["run_id"], row["round_dir"])
            if key not in windowed:
                raise RuntimeError(
                    f"no verified windowed ACF for {key}; rerun acf_window.py "
                    "against Prometheus or pass --acf-metric whole")
            acf_value = float(windowed[key][acf_column])
        records.append({
            "paradigm": paradigm,
            "k": k,
            "method": row["method"],
            "profile": PROFILE_KEYS[row["profile"]],
            "trial": int(row["trial"]),
            "throughput": float(row[column]),
            "acf": acf_value,
            "acf_whole": float(row["acf_rate"]),
            "tail_fraction": float(row["tail_fraction"] or "nan"),
            "censored": _flag(row["censored"]),
            "source": row["experiment"],
        })
    return records, audit


def aggregate(records):
    """Group the rounds into the twelve plotted cells.

    Returns:
        dict keyed by (paradigm, k, profile) with the mean, min, max and the
        per-trial lists of both metrics, plus how many rounds of that cell had
        a right-censored saturation window.

    Raises:
        RuntimeError: If a cell does not hold the trial count its paradigm
            expects, which would mean the bundle is incomplete.
    """
    cells = {}
    for record in records:
        key = (record["paradigm"], record["k"], record["profile"])
        cells.setdefault(key, []).append(record)
    for key, rounds in cells.items():
        expected = EXPECTED_TRIALS[key[0]]
        if len(rounds) != expected:
            raise RuntimeError(
                f"cell {key} has {len(rounds)} trials, expected {expected}")
    out = {}
    for key, rounds in cells.items():
        rounds.sort(key=lambda r: r["trial"])
        tput = [r["throughput"] for r in rounds]
        acf = [r["acf"] for r in rounds]
        censored = [r for r in rounds if r["censored"]]
        uncensored = [r["throughput"] for r in rounds if not r["censored"]]
        out[key] = {
            "tput": tput, "tput_mean": statistics.mean(tput),
            "tput_min": min(tput), "tput_max": max(tput),
            "acf": acf, "acf_mean": statistics.mean(acf),
            "acf_min": min(acf), "acf_max": max(acf),
            "n": len(rounds),
            "censored": len(censored),
            "tput_mean_uncensored": (statistics.mean(uncensored)
                                     if uncensored else float("nan")),
            "acf_whole_mean": statistics.mean(r["acf_whole"] for r in rounds),
            "tail_fraction": statistics.mean(
                r["tail_fraction"] for r in rounds
                if r["tail_fraction"] == r["tail_fraction"]),
            "method": rounds[0]["method"], "source": rounds[0]["source"],
        }
    return out


# ---------------------------------------------------------------------------
#  Drawing
# ---------------------------------------------------------------------------

def _acf_label(percent):
    """Value printed above an ACF bar: 2 decimals below 1, 1 decimal below 10."""
    if percent < 1:
        return f"{percent:.2f}"
    if percent < 10:
        return f"{percent:.1f}"
    return f"{percent:.0f}"


def _percent_fmt(value, _pos=None):
    """Log-axis tick label, bare: 0.5 -> '0.5', 5 -> '5', 50 -> '50'.

    The unit rides in the panel's y label instead of on every tick, which keeps
    the two panels' tick columns the same width so their labels line up.
    """
    return f"{value:g}" if value < 1 else f"{value:.0f}"


def _draw_panel(ax, cells, metric, log_scale, floor, top, ylabel, hatch_censored=False):
    """Draw one panel: four grouped bars per profile with min-max whiskers.

    Args:
        ax:        Target axes.
        cells:     Aggregated cells from aggregate().
        metric:    "acf" (drawn in percent) or "tput" (pods/s).
        log_scale: Whether the y axis is logarithmic.
        floor:     Bottom of the y axis (also the bar baseline on a log axis).
        top:       Top of the y axis.
        ylabel:    Y-axis label, which also carries the panel tag.
        hatch_censored: Mark cells holding a right-censored round. Only
            meaningful for the whole-window throughput, whose mean is then a
            lower bound; the T99 cut point is observed in every round.
    """
    x = np.arange(len(PROFILES), dtype=float)
    offsets = [-0.30, -0.10, 0.10, 0.30]
    scale = 100.0 if metric == "acf" else 1.0
    for offset, (paradigm, k) in zip(offsets, BARS):
        for xi, profile in zip(x + offset, PROFILES):
            cell = cells[(paradigm, k, profile)]
            value = cell[f"{metric}_mean"] * scale
            low = cell[f"{metric}_min"] * scale
            high = cell[f"{metric}_max"] * scale
            hatch = ("///" if (hatch_censored and metric == "tput"
                               and cell["censored"]) else None)
            ax.bar(xi, value - floor, bottom=floor, width=0.185,
                   color=BAR_COLOR[(paradigm, k)], edgecolor=C_EDGE,
                   linewidth=0.4, zorder=3, hatch=hatch,
                   yerr=[[max(0.0, value - low)], [max(0.0, high - value)]],
                   error_kw=ERR_KW)
            text = (_acf_label(value) if metric == "acf" else f"{value:.0f}")
            ax.annotate(text, xy=(xi, high), xytext=(0, 1.2),
                        textcoords="offset points", ha="center", va="bottom",
                        fontsize=ANNOT_FONTSIZE, color=C_EDGE, zorder=5)
    if log_scale:
        ax.set_yscale("log")
        ax.yaxis.set_major_locator(LogLocator(base=10, numticks=5))
        ax.yaxis.set_minor_formatter(NullFormatter())
        ax.yaxis.set_major_formatter(FuncFormatter(_percent_fmt))
    else:
        ax.yaxis.set_major_locator(MultipleLocator(100))
    ax.set_ylim(floor, top)
    ax.set_xticks(x)
    ax.set_xticklabels(PROFILES)
    ax.set_xlim(-0.5, len(PROFILES) - 0.5)
    ax.set_ylabel(ylabel, labelpad=1.5)
    ax.grid(True, axis="y", which="major", ls="--", lw=0.5, alpha=0.45)
    ax.set_axisbelow(True)
    # Top/right stay hidden here: the bar-top value labels sit flush against
    # the axes ceiling and collide with a top spine.
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)


def build_figure(cells, width=DEFAULT_WIDTH, height=DEFAULT_HEIGHT,
                 throughput_metric="t99", acf_metric="t99"):
    """Assemble the two-panel single-column figure."""
    sns.set_style("ticks")
    plt.rcParams.update(RC_PARAMS)

    legend_height = 0.28
    total = height + legend_height
    fig = plt.figure(figsize=(width, total))
    gs = GridSpec(2, 1, figure=fig, height_ratios=[1.0, 1.0], hspace=0.30,
                  left=0.168, right=0.995,
                  top=1.0 - (legend_height + 0.06) / total,
                  bottom=0.40 / total)
    ax_acf = fig.add_subplot(gs[0, 0])
    # Headroom above the tallest bar for its printed value, on a log axis.
    acf_top = 110 if acf_metric != "whole" else 165
    _draw_panel(ax_acf, cells, "acf", True, 0.35, acf_top,
                ACF_COLUMNS[acf_metric][1])
    ax_acf.set_xticklabels([])
    ax_tput = fig.add_subplot(gs[1, 0])
    _draw_panel(ax_tput, cells, "tput", False, 0.0, 355,
                THROUGHPUT_COLUMNS[throughput_metric][1],
                hatch_censored=(throughput_metric == "raw"))
    shared_cut = (throughput_metric == "t99" and acf_metric == "t99")
    ax_tput.set_xlabel(
        "Data-plane profile" + (" (both panels over $[0,T_{99}]$)"
                                if shared_cut else ""), labelpad=1.5)

    # Legend names follow the paper's baseline vocabulary (section 5.1). The
    # parameter settings that distinguish these runs from the deployed default
    # (K, w, partition count) belong in the figure caption, not the legend.
    handles = [
        mpatches.Patch(facecolor=C_RED_D, edgecolor=C_EDGE, linewidth=0.4,
                       label="Vanilla (event-driven)"),
        mpatches.Patch(facecolor=C_GREEN_D, edgecolor=C_EDGE, linewidth=0.4,
                       label="ParKour (event-driven)"),
        mpatches.Patch(facecolor=C_RED_L, edgecolor=C_EDGE, linewidth=0.4,
                       label="Vanilla (periodic)"),
        mpatches.Patch(facecolor=C_GREEN_L, edgecolor=C_EDGE, linewidth=0.4,
                       label="ParKour (periodic)"),
    ]
    fig.legend(handles=handles, loc="upper center", bbox_to_anchor=(0.5, 1.0),
               ncol=2, columnspacing=0.9, handlelength=1.4, handletextpad=0.35,
               labelspacing=0.18, frameon=False,
               fontsize=RC_PARAMS["legend.fontsize"])
    return fig


# ---------------------------------------------------------------------------
#  Reporting
# ---------------------------------------------------------------------------

def print_rounds(records):
    """Print every plotted round so the figure is auditable from the console."""
    header = (f"{'matrix':<6}{'paradigm':<10}{'K':>2}  {'profile':<11}"
              f"{'trial':>6}{'Q_bind':>9}{'ACF':>10}  cens")
    print(header)
    print("-" * len(header))
    for record in sorted(records, key=lambda r: (r["source"], r["paradigm"],
                                                 r["k"], r["profile"],
                                                 r["trial"])):
        print(f"{record['source']:<6}{record['paradigm']:<10}{record['k']:>2}  "
              f"{record['profile']:<11}{record['trial']:>6}"
              f"{record['throughput']:>9.2f}{record['acf'] * 100:>9.4f}%"
              f"{'   yes' if record['censored'] else '   no'}")


def print_cells(cells):
    """Print the plotted cell statistics and the K=2 vs K=0 comparison."""
    header = (f"{'paradigm':<10}{'profile':<12}{'ACF K=0':>9}{'ACF K=2':>9}"
              f"{'ACF red.':>9}{'(whole)':>9}{'tput K=0':>10}{'tput K=2':>10}"
              f"{'gain':>8}{'tail K=0':>9}{'tail K=2':>9}")
    print(header)
    print("-" * len(header))
    for paradigm in PARADIGMS:
        for profile in PROFILES:
            k0 = cells[(paradigm, 0, profile)]
            k2 = cells[(paradigm, 2, profile)]
            whole = (1 - k2["acf_whole_mean"] / k0["acf_whole_mean"]) * 100
            print(f"{paradigm:<10}{profile:<12}"
                  f"{k0['acf_mean'] * 100:>8.2f}%{k2['acf_mean'] * 100:>8.2f}%"
                  f"{(1 - k2['acf_mean'] / k0['acf_mean']) * 100:>8.1f}%"
                  f"{whole:>8.1f}%"
                  f"{k0['tput_mean']:>10.2f}{k2['tput_mean']:>10.2f}"
                  f"{(k2['tput_mean'] / k0['tput_mean'] - 1) * 100:>+7.1f}%"
                  f"{k0['tail_fraction'] * 100:>8.1f}%{k2['tail_fraction'] * 100:>8.1f}%")
    censored_cells = {k: v for k, v in cells.items() if v["censored"]}
    if censored_cells:
        print("\nright-censored cells (throughput is a lower bound):")
        for key, cell in sorted(censored_cells.items()):
            print(f"  {key[0]:<9} K={key[1]} {key[2]:<11} "
                  f"{cell['censored']}/{cell['n']} rounds at the 1000 s ceiling; "
                  f"mean {cell['tput_mean']:.2f} pods/s, "
                  f"uncensored-only {cell['tput_mean_uncensored']:.2f}")


def print_audit(audit):
    """Print the failure-gate trail that the validity paragraph quotes."""
    kept = [a for a in audit if a["kept"]]
    dropped = [a for a in audit if not a["kept"]]
    rates = [a["failure_rate"] * 100 for a in kept]
    print(f"failure matrices: {len(kept)} kept rounds, "
          f"observed startup-failure rate {min(rates):.2f}%-{max(rates):.2f}%, "
          f"gate pass {sum(1 for a in kept if a['gate_pass'])}/{len(kept)}, "
          f"failure semantics valid "
          f"{sum(1 for a in kept if a['semantics_ok'])}/{len(kept)}")
    for entry in dropped:
        print(f"  dropped: {entry['matrix']} order {entry['order']} "
              f"{entry['method']}, failure rate "
              f"{entry['failure_rate'] * 100:.4f}%")


# ---------------------------------------------------------------------------
#  Save
# ---------------------------------------------------------------------------

def save(fig, output_dir, stem, paper_fig_dir, paper_stem):
    """Write PDF/SVG/PNG and copy the vectors into paper/figs."""
    os.makedirs(output_dir, exist_ok=True)
    written = []
    for ext in ("pdf", "svg", "png"):
        path = os.path.join(output_dir, f"{stem}.{ext}")
        fig.savefig(path, dpi=300, bbox_inches="tight", pad_inches=0.01)
        written.append(path)
    if paper_fig_dir:
        os.makedirs(paper_fig_dir, exist_ok=True)
        for ext in ("pdf", "svg"):
            target = os.path.join(paper_fig_dir, f"{paper_stem}.{ext}")
            shutil.copyfile(os.path.join(output_dir, f"{stem}.{ext}"), target)
            written.append(target)
    for path in written:
        print(f"Wrote {path}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(
        description="Paper Figure — module-F data-plane delay/failure "
                    "sensitivity (same-code K=0 vs K=2, two paradigms)")
    ap.add_argument("--bundle", default=DEFAULT_BUNDLE,
                    help="Path to the anchored module-F bundle directory")
    ap.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    ap.add_argument("--stem", default=DEFAULT_STEM)
    ap.add_argument("--paper-figs-dir", default=DEFAULT_PAPER_FIG_DIR,
                    help="Directory receiving PDF/SVG copies; pass '' to skip")
    ap.add_argument("--paper-stem", default=DEFAULT_PAPER_STEM)
    ap.add_argument("--width", type=float, default=DEFAULT_WIDTH,
                    help="Figure width in inches (3.33 = sigplan column width)")
    ap.add_argument("--height", type=float, default=DEFAULT_HEIGHT,
                    help="Panel area height in inches, excluding the legend")
    ap.add_argument("--throughput-metric", choices=sorted(THROUGHPUT_COLUMNS),
                    default="t99",
                    help="'t99' is the uniform cut point the bundle prescribes; "
                         "'raw' reproduces the whole-window field")
    ap.add_argument("--acf-metric", choices=sorted(ACF_COLUMNS), default="t99",
                    help="'t99' matches the throughput cut point; "
                         "'whole' reproduces the archived whole-window rate")
    ap.add_argument("--acf-windowed", default=DEFAULT_ACF_WINDOWED,
                    help="Reconstruction CSV backing the windowed ACF")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()

    rounds, audit_trail = load_rounds(args.bundle, args.throughput_metric,
                                      args.acf_metric, args.acf_windowed)
    print(f"throughput metric: {THROUGHPUT_COLUMNS[args.throughput_metric][0]}")
    print(f"ACF metric:        {ACF_COLUMNS[args.acf_metric][0]}\n")
    print_rounds(rounds)
    print()
    grid = aggregate(rounds)
    print_cells(grid)
    print()
    print_audit(audit_trail)
    print()
    figure = build_figure(grid, args.width, args.height,
                          args.throughput_metric, args.acf_metric)
    save(figure, args.output_dir, args.stem, args.paper_figs_dir,
         args.paper_stem)
    if args.show:
        plt.show()
    else:
        plt.close(figure)
