#!/usr/bin/env python3
"""
Apply the outlier filter uniformly across all evaluation boards and emit
filtered medians + per-cell audit log to a markdown report.

The filter is `common/trials.py`, the one the figure export applies, so the
numbers here and the numbers in the figures come from the same rule. See that
module for the rule itself.

The report reads the per-trial table process-results.py writes for each board,
`<results>/<board>/summary.csv`, so unlike the figure pipeline it needs the raw
results. It writes into the work directory unless told otherwise.

Usage:
    python apply-outlier-filter.py --results <root>              # write experiments/work/filtered-eval-data.md
    python apply-outlier-filter.py --results <root> --out <file>
    python apply-outlier-filter.py --results <root> --stdout     # also print to stdout
"""

import argparse
import csv
import statistics
import sys
from collections import defaultdict
from pathlib import Path

_EXPERIMENTS = Path(__file__).resolve().parents[1]
if str(_EXPERIMENTS) not in sys.path:
    sys.path.insert(0, str(_EXPERIMENTS))

from common.trials import FILTER_VERSION, as_float, filter_cell  # noqa: E402

BOARDS = ["B1", "B2", "B3", "K", "P", "ablation", "strategy"]

DEFAULT_OUT = _EXPERIMENTS / "work" / "filtered-eval-data.md"


def load_board(results: Path, board: str) -> dict:
    """Return: {exp_name: [trial_dict, ...]}."""
    path = results / board / "summary.csv"
    if not path.is_file():
        return {}
    cells = defaultdict(list)
    with open(path, encoding="utf-8") as f:
        for row in csv.DictReader(f):
            exp = row.get("experiment", "")
            cells[exp].append(row)
    return cells


def audited_filter(trials):
    """Return (kept_trials, drops) from the shared filter, where drops is a list
    of (trial_label, reason, dur, tp, acf) for the audit log."""
    kept, dropped = filter_cell(trials)
    by_label = {str(t.get("trial", "?")): t for t in trials}
    if len(by_label) != len(trials):
        raise SystemExit(f"{trials[0].get('experiment')}: a trial number appears twice")
    drops = []
    for label, reason in dropped:
        t = by_label[label]
        drops.append((label, reason,
                      as_float(t.get("scheduling_duration_s")),
                      as_float(t.get("throughput_pods_per_s")),
                      as_float(t.get("acf_rate"))))
    return kept, drops


def aggregate_cell(trials):
    """Return dict of summary stats from kept trials."""
    def med(field):
        xs = [as_float(t.get(field)) for t in trials]
        xs = [x for x in xs if x is not None]
        if not xs:
            return None
        return statistics.median(xs)

    return {
        "n":       len(trials),
        "tput":    med("throughput_pods_per_s"),
        "acf":     med("acf_rate"),
        "bcr":     med("bind_conflict_rate"),
        "algo99":  med("algo_p99_ms"),
        "e2e99":   med("e2e_p99_ms"),
        "sched_cpu":  med("scheduler_cpu_total"),
        "sched_mem":  med("scheduler_mem_rss_total"),
        "binder_cpu": med("binder_cpu"),
        "binder_mem": med("binder_mem_rss"),
        "disp_cpu":   med("dispatcher_cpu"),
        "disp_mem":   med("dispatcher_mem_rss"),
        "dur":     med("scheduling_duration_s"),
    }


def fmt(x, p=2, na="—"):
    if x is None:
        return na
    return f"{x:.{p}f}"


def fmt_pct(x, p=2, na="—"):
    if x is None:
        return na
    return f"{100*x:.{p}f}%"


def fmt_gi(x, p=2, na="—"):
    if x is None:
        return na
    return f"{x / (1024 ** 3):.{p}f}"


def process_all(results: Path):
    by_board = {}
    audit = []   # (board, exp, drops)
    for b in BOARDS:
        cells = load_board(results, b)
        bdata = {}
        for exp, trials in sorted(cells.items()):
            kept, drops = audited_filter(trials)
            agg = aggregate_cell(kept)
            agg["n_total"] = len(trials)
            agg["dropped"] = len(drops)
            bdata[exp] = agg
            if drops:
                audit.append((b, exp, drops))
        by_board[b] = bdata
    return by_board, audit


# ────────────────────────────────────────────────────────────────────────────
#  Markdown emitters per board
# ────────────────────────────────────────────────────────────────────────────

def emit_audit(audit, lines):
    lines.append("## 0. Outlier audit log")
    lines.append("")
    lines.append(f"Filter rule (`common/trials.py`, {FILTER_VERSION}): "
                 "`duration > max(Q3+1.5×IQR, 2×median)` OR "
                 "`metrics hole (acf=bind=0)` OR `no scheduled pods`. "
                 "Cells with <4 valid trials skip the duration test.")
    lines.append("")
    lines.append("| Board | Experiment | Trial | Reason | tp | acf |")
    lines.append("|---|---|---|---|---|---|")
    for b, exp, drops in audit:
        for trial, reason, dur, tp, acf in drops:
            tps = fmt(tp, 1) if tp is not None else "—"
            acfs = fmt_pct(acf, 2) if acf is not None else "—"
            lines.append(f"| {b} | {exp} | {trial} | {reason} | {tps} | {acfs} |")
    lines.append("")


def emit_b1(d, lines):
    lines.append("## 1.2 B1 (low-contention scale-out, co-located microservice scenario)")
    lines.append("")
    lines.append("Configs: ppn=29, cpu=1c, mem=8Gi, V=0.6, sync_period=0.1, "
                 "partitions=1, sync_pattern=diff, strategy=QualityFirst.")
    lines.append("")
    lines.append("| Scale | E1 tp | E1 ACF | E2 tp | E2 ACF | E3 tp | E3 ACF | E2/E1 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for n in [1000, 2000, 5000]:
        row = [f"{n}n"]
        e1tp = d.get(f"B1-{n}n-E1", {}).get("tput")
        for s in ["E1", "E2", "E3"]:
            c = d.get(f"B1-{n}n-{s}", {})
            row.append(fmt(c.get("tput"), 1))
            row.append(fmt_pct(c.get("acf"), 2))
        e2tp = d.get(f"B1-{n}n-E2", {}).get("tput")
        if e1tp and e2tp:
            row.append(f"{e2tp/e1tp:.2f}×")
        else:
            row.append("—")
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")
    # Sample-size note
    lines.append("*n (kept/total)*:")
    pieces = []
    for n in [1000, 2000, 5000]:
        for s in ["E1", "E2", "E3"]:
            c = d.get(f"B1-{n}n-{s}", {})
            if c:
                pieces.append(f"{s}{n//1000}K={c.get('n',0)}/{c.get('n_total',0)}")
    lines.append("- " + " · ".join(pieces))
    lines.append("")


def emit_b2(d, lines):
    lines.append("## 5.2 B2 (high-contention Pareto, 4 scales x 7 configs)")
    lines.append("")
    lines.append("Configs: ppn=1, cpu=24c, mem=192Gi, V=0.6, partitions=1 for "
                 "P1/P4 (glob), partitions=10 for P2/P3 (same/diff).")
    lines.append("")
    lines.append("| Scale | Strategy | tp (pods/s) | ACF | n |")
    lines.append("|---|---|---|---|---|")
    for n in [2000, 5000, 10000, 20000]:
        for s in ["E1", "E2", "E3", "P1", "P2", "P3", "P4"]:
            c = d.get(f"B2-{n}n-{s}")
            if not c:
                continue
            lines.append(f"| {n}n | {s} | {fmt(c['tput'],1)} | "
                         f"{fmt_pct(c['acf'],2)} | "
                         f"{c['n']}/{c['n_total']} |")
    lines.append("")


def emit_b3(d, lines):
    lines.append("## 1.3 B3 (scheduler-count scalability, 10K nodes x 4 configs x 5 N)")
    lines.append("")
    lines.append("Configs: 10000n / V=0.6 / N ∈ {2,4,6,8,10}. "
                 "E2: K=0,p=0,sp=0.1,diff; E3: K=2,p=0.5,sp=0.1,diff; "
                 "P1: K=0,p=0,sp=1.0,glob; P4: K=2,p=0.5,sp=1.0,glob.")
    lines.append("")
    lines.append("| N | E2 tp | E2 ACF | E3 tp | E3 ACF | P1 tp | P1 ACF | P4 tp | P4 ACF |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for N in [2, 4, 6, 8, 10]:
        row = [str(N)]
        for s in ["E2", "E3", "P1", "P4"]:
            c = d.get(f"B3-N{N}-{s}", {})
            row.append(fmt(c.get("tput"), 1))
            row.append(fmt_pct(c.get("acf"), 2))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def emit_ablation(d, lines):
    lines.append("## 2.1 Ablation (10K nodes / V=0.6 / 10 sched)")
    lines.append("")
    lines.append("| Config | K | p | Event tp | Event ACF | Periodic tp | Periodic ACF |")
    lines.append("|---|---|---|---|---|---|---|")
    for ab, suffix, K, p in [("0", "base", 0, 0), ("1", "M", 2, 0),
                              ("2", "P", 0, 0.5), ("3", "MP", 2, 0.5)]:
        e = d.get(f"AbE{ab}-{suffix}", {})
        pp = d.get(f"AbP{ab}-{suffix}", {})
        lines.append(f"| Ab{ab} ({suffix}) | {K} | {p} | "
                     f"{fmt(e.get('tput'),1)} | "
                     f"{fmt_pct(e.get('acf'),2)} | "
                     f"{fmt(pp.get('tput'),1)} | "
                     f"{fmt_pct(pp.get('acf'),2)} |")
    lines.append("")


def emit_overhead(d, lines):
    """Overhead from B2-10000n and B2-20000n cells."""
    lines.append("## 3. Overhead (CPU/MEM, B2 saturation snapshot)")
    lines.append("")
    lines.append("CPU in cores (summed over 10 schedulers); MEM in GiB (summed over 10 schedulers).")
    lines.append("")
    for size in [10000, 20000]:
        lines.append(f"### 10K → {size}n")
        lines.append("")
        lines.append("| Config | sched CPU | binder CPU | disp CPU | "
                     "sched MEM (Gi) | binder MEM (Gi) | algo P99 (ms) | e2e P99 (ms) |")
        lines.append("|---|---|---|---|---|---|---|---|")
        configs = ["E1", "E2", "E3", "P1", "P2", "P3", "P4"] if size == 10000 \
                  else ["E2", "E3", "P1", "P4"]
        for s in configs:
            c = d.get(f"B2-{size}n-{s}")
            if not c:
                continue
            lines.append("| " + " | ".join([
                s,
                fmt(c.get("sched_cpu"), 2),
                fmt(c.get("binder_cpu"), 2),
                fmt(c.get("disp_cpu"), 2),
                fmt_gi(c.get("sched_mem"), 2),
                fmt_gi(c.get("binder_mem"), 2),
                fmt(c.get("algo99"), 1),
                fmt(c.get("e2e99"), 0),
            ]) + " |")
        lines.append("")


def emit_k_sweep(d, lines):
    lines.append("## 5.1 K-sweep (10K / N=10 / V=0.6)")
    lines.append("")
    lines.append("Event: sp=0.1, partitions=1, sync_pattern=diff. "
                 "Periodic glob: partitions=1; same/diff: partitions=10. "
                 "All at p=0.3, strategy=QualityFirst.")
    lines.append("")
    lines.append("| K | E tp | E ACF | E bcr | "
                 "P-glob tp | P-glob ACF | P-glob bcr | "
                 "P-same tp | P-same ACF | P-same bcr | "
                 "P-diff tp | P-diff ACF | P-diff bcr |")
    lines.append("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
    for K in [0, 1, 2, 4]:
        row = [str(K)]
        for prefix in [f"S-E-K{K}", f"S-P-K{K}-glob", f"S-P-K{K}-same",
                       f"S-P-K{K}-diff"]:
            c = d.get(prefix, {})
            row.append(fmt(c.get("tput"), 1))
            row.append(fmt_pct(c.get("acf"), 2))
            row.append(fmt_pct(c.get("bcr"), 2))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def emit_p_sweep(d, lines):
    lines.append("## 5.3 p-sweep (penalty weight, 10K / N=10 / V=0.6)")
    lines.append("")
    lines.append("Event: sp=0.1, partitions=1, sync_pattern=diff. "
                 "Periodic: sp=1.0, partitions=10. "
                 "All at K=4, strategy=QualityFirst.")
    lines.append("")
    lines.append("| p | E tp | E ACF | "
                 "glob tp | glob ACF | same tp | same ACF | diff tp | diff ACF |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for tag, p in [("00", 0.0), ("01", 0.1), ("03", 0.3), ("05", 0.5), ("07", 0.7)]:
        row = [f"{p}"]
        e = d.get(f"S-E-P{tag}-diff", {})
        row.append(fmt(e.get("tput"), 1))
        row.append(fmt_pct(e.get("acf"), 2))
        for sp in ["glob", "same", "diff"]:
            c = d.get(f"S-P-P{tag}-{sp}", {})
            row.append(fmt(c.get("tput"), 1))
            row.append(fmt_pct(c.get("acf"), 2))
        lines.append("| " + " | ".join(row) + " |")
    lines.append("")


def emit_strategy(d, lines):
    lines.append("## 5.2-strategy (selection strategy sweep, 10K / N=10 / V=0.6)")
    lines.append("")
    lines.append("All at K=4, p=0.3. Periodic: partitions=10.")
    lines.append("")
    lines.append("**Event-driven (sync_pattern=diff)**")
    lines.append("")
    lines.append("| Strategy | tp | ACF | bcr |")
    lines.append("|---|---|---|---|")
    for strat in ["QualityFirst", "WeightedRandom"]:
        c = d.get(f"S-E-Strategy-{strat}-diff")
        if not c:
            continue
        lines.append(f"| {strat} | {fmt(c.get('tput'),1)} | "
                     f"{fmt_pct(c.get('acf'),2)} | "
                     f"{fmt_pct(c.get('bcr'),2)} |")
    lines.append("")
    lines.append("**Periodic (3 strategies × 3 sync_patterns)**")
    lines.append("")
    lines.append("| Strategy | sync_pattern | tp | ACF | bcr |")
    lines.append("|---|---|---|---|---|")
    for strat in ["QualityFirst", "WeightedRandom", "LatencyFirst"]:
        for sp in ["glob", "same", "diff"]:
            c = d.get(f"S-P-Strategy-{strat}-{sp}")
            if not c:
                continue
            lines.append(f"| {strat} | {sp} | {fmt(c.get('tput'),1)} | "
                         f"{fmt_pct(c.get('acf'),2)} | "
                         f"{fmt_pct(c.get('bcr'),2)} |")
    lines.append("")
    lines.append("**ParSync variants** (partition-grain LF/QF + ParSync rotation)")
    lines.append("")
    lines.append("| Strategy | sync_pattern | tp | ACF | bcr |")
    lines.append("|---|---|---|---|---|")
    for strat in ["QualityFirstParSync", "LatencyFirstParSync"]:
        for sp in ["same", "diff"]:
            c = d.get(f"S-P-Strategy-{strat}-{sp}")
            if not c:
                continue
            lines.append(f"| {strat} | {sp} | {fmt(c.get('tput'),1)} | "
                         f"{fmt_pct(c.get('acf'),2)} | "
                         f"{fmt_pct(c.get('bcr'),2)} |")
    lines.append("")


# ────────────────────────────────────────────────────────────────────────────
#  Top-level
# ────────────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--results", type=Path, required=True,
                    help="results root with one directory per board, each "
                         "holding process-results.py's summary.csv")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT,
                    help="report path (default: experiments/work/filtered-eval-data.md)")
    ap.add_argument("--stdout", action="store_true",
                    help="also print result to stdout")
    args = ap.parse_args()

    by_board, audit = process_all(args.results)

    lines = []
    lines.append("# Filtered evaluation data (outlier-cleaned)")
    lines.append("")
    lines.append(f"_Generated by `apply-outlier-filter.py`._")
    lines.append("")
    emit_audit(audit, lines)
    emit_b1(by_board.get("B1", {}), lines)
    emit_b2(by_board.get("B2", {}), lines)
    emit_b3(by_board.get("B3", {}), lines)
    emit_ablation(by_board.get("ablation", {}), lines)
    emit_overhead(by_board.get("B2", {}), lines)
    emit_k_sweep(by_board.get("K", {}), lines)
    emit_p_sweep(by_board.get("P", {}), lines)
    emit_strategy(by_board.get("strategy", {}), lines)

    text = "\n".join(lines) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out}")
    if args.stdout:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
