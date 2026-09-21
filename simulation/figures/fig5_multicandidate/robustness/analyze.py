#!/usr/bin/env python3
"""Analyze whether stale-conflict absorption grows monotonically with G."""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from statistics import mean, pstdev

STUDY_DIR = Path(__file__).resolve().parent
SIM_ROOT = STUDY_DIR.parents[2]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.validation import load_verified
from figures.fig5_multicandidate.robustness.experiment import (
    ANALYSIS,
    ANALYSIS_JSON,
    FALLBACK_BUDGETS,
    MANIFEST,
    SCHEDULER_COUNTS,
    TIER_WIDTHS,
    VERIFIED_CACHE,
    constants,
)

GAPS = (0.5, 1.0, 2.5, 5.0)
PRACTICAL_TOLERANCE_PP = 0.1


def _index(data: dict) -> dict[tuple[int, int, int, int | None], list[dict]]:
    grouped: dict[
        tuple[int, int, int, int | None], list[dict]
    ] = defaultdict(list)
    for run in data["runs"]:
        width = constants().num_nodes // int(run["config"]["num_tiers"])
        grouped[
            (
                int(run["num_schedulers"]),
                width,
                int(run["config"]["num_backup"]),
                run["config"]["sync_gap_cycles"],
            )
        ].append(run)
    return grouped


def _rates_by_seed(runs: list[dict]) -> dict[int, float]:
    return {
        int(run["seed"]): float(run["summary"]["total_conflict_rate"])
        for run in runs
    }


def _stale_by_seed(
    grouped: dict[tuple[int, int, int, int | None], list[dict]],
    schedulers: int,
    width: int,
    fallbacks: int,
    gap: float,
) -> dict[int, float]:
    event = _rates_by_seed(grouped[(schedulers, width, fallbacks, None)])
    periodic = _rates_by_seed(
        grouped[(schedulers, width, fallbacks, int(gap / 0.1))]
    )
    return {
        seed: max(0.0, periodic[seed] - event[seed])
        for seed in sorted(event)
    }


def _summary(values: list[float]) -> dict[str, float]:
    return {
        "mean": mean(values),
        "std": pstdev(values),
        "min": min(values),
        "max": max(values),
    }


def build_analysis(data: dict) -> dict:
    grouped = _index(data)
    rows = []
    for schedulers in SCHEDULER_COUNTS:
        for width in TIER_WIDTHS:
            baseline = {
                gap: _stale_by_seed(grouped, schedulers, width, 0, gap)
                for gap in GAPS
            }
            for fallbacks in FALLBACK_BUDGETS[1:]:
                absorption = []
                paired_absorption_pp = []
                total_absorption = []
                for gap in GAPS:
                    treatment = _stale_by_seed(
                        grouped, schedulers, width, fallbacks, gap
                    )
                    paired = [
                        baseline[gap][seed] - treatment[seed]
                        for seed in sorted(baseline[gap])
                    ]
                    absorption.append(_summary(paired))
                    paired_absorption_pp.append(
                        {
                            str(seed): (
                                baseline[gap][seed] - treatment[seed]
                            )
                            * 100.0
                            for seed in sorted(baseline[gap])
                        }
                    )

                    reference_total = _rates_by_seed(
                        grouped[(schedulers, width, 0, int(gap / 0.1))]
                    )
                    treatment_total = _rates_by_seed(
                        grouped[
                            (
                                schedulers,
                                width,
                                fallbacks,
                                int(gap / 0.1),
                            )
                        ]
                    )
                    total_absorption.append(
                        _summary(
                            [
                                reference_total[seed]
                                - treatment_total[seed]
                                for seed in sorted(reference_total)
                            ]
                        )
                    )

                means_pp = [item["mean"] * 100.0 for item in absorption]
                steps_pp = [
                    right - left
                    for left, right in zip(means_pp, means_pp[1:])
                ]
                seed_steps_pp = [
                    {
                        seed: right[seed] - left[seed]
                        for seed in left
                    }
                    for left, right in zip(
                        paired_absorption_pp, paired_absorption_pp[1:]
                    )
                ]
                rows.append(
                    {
                        "m": schedulers,
                        "width": width,
                        "K": fallbacks,
                        "stale_absorption": absorption,
                        "total_absorption": total_absorption,
                        "means_pp": means_pp,
                        "steps_pp": steps_pp,
                        "seed_steps_pp": seed_steps_pp,
                        "exact_monotone": all(
                            step >= -1e-12 for step in steps_pp
                        ),
                        "practical_monotone": all(
                            step >= -PRACTICAL_TOLERANCE_PP
                            for step in steps_pp
                        ),
                        "endpoint_increase": means_pp[-1] >= means_pp[0],
                    }
                )

    exact = sum(row["exact_monotone"] for row in rows)
    practical = sum(row["practical_monotone"] for row in rows)
    endpoint = sum(row["endpoint_increase"] for row in rows)
    violations = [
        {
            "m": row["m"],
            "width": row["width"],
            "K": row["K"],
            "from_G": GAPS[index],
            "to_G": GAPS[index + 1],
            "step_pp": step,
            "seed_steps_pp": row["seed_steps_pp"][index],
            "unanimous_negative": all(
                value < 0
                for value in row["seed_steps_pp"][index].values()
            ),
        }
        for row in rows
        for index, step in enumerate(row["steps_pp"])
        if step < -PRACTICAL_TOLERANCE_PP
    ]
    by_k = {}
    for fallbacks in FALLBACK_BUDGETS[1:]:
        selected = [row for row in rows if row["K"] == fallbacks]
        by_k[str(fallbacks)] = {
            "exact_monotone": sum(row["exact_monotone"] for row in selected),
            "practical_monotone": sum(
                row["practical_monotone"] for row in selected
            ),
            "total": len(selected),
            "mean_absorption_pp": [
                mean(row["means_pp"][index] for row in selected)
                for index in range(len(GAPS))
            ],
        }
    return {
        "matrix": {
            "m": list(SCHEDULER_COUNTS),
            "width": list(TIER_WIDTHS),
            "K": list(FALLBACK_BUDGETS),
            "G_seconds": list(GAPS),
            "seeds": data["meta"]["seeds"],
        },
        "criterion": {
            "definition": (
                "stale(K=0,G)-stale(K,G), paired by seed; "
                "stale=max(0,periodic-event)"
            ),
            "practical_tolerance_pp": PRACTICAL_TOLERANCE_PP,
        },
        "summary": {
            "sequences": len(rows),
            "exact_monotone": exact,
            "practical_monotone": practical,
            "endpoint_increase": endpoint,
            "violations": len(violations),
            "unanimous_negative_violations": sum(
                item["unanimous_negative"] for item in violations
            ),
        },
        "by_k": by_k,
        "violations": sorted(
            violations, key=lambda item: item["step_pp"]
        ),
        "rows": rows,
    }


def _format_sequence(values: list[float]) -> str:
    return " / ".join(f"{value:.2f}" for value in values)


def write_report(analysis: dict) -> None:
    summary = analysis["summary"]
    lines = [
        "# Figure 5 objective-parameter robustness",
        "",
        "Absorption is measured as the paired stale-state proxy reduction:",
        "",
        "`stale(K=0,G) - stale(K,G)`, where "
        "`stale=max(0, periodic-event)`.",
        "",
        f"The grid contains {summary['sequences']} `(m,W,K>0)` sequences, "
        "each evaluated at G=0.5/1/2.5/5 s with three matched seeds.",
        "",
        "## Summary",
        "",
        f"- Exact nondecreasing sequences: "
        f"{summary['exact_monotone']}/{summary['sequences']}",
        f"- Nondecreasing within "
        f"{analysis['criterion']['practical_tolerance_pp']:.1f} pp tolerance: "
        f"{summary['practical_monotone']}/{summary['sequences']}",
        f"- Endpoint absorption at G=5 s >= G=0.5 s: "
        f"{summary['endpoint_increase']}/{summary['sequences']}",
        f"- Practical negative transitions: {summary['violations']}",
        f"- Negative transitions reproduced by all three seeds: "
        f"{summary['unanimous_negative_violations']}/"
        f"{summary['violations']}",
        "",
        "## Aggregated by K",
        "",
        "| K | exact monotone | practical monotone | "
        "mean absorption at G=0.5/1/2.5/5 s (pp) |",
        "|---:|---:|---:|---|",
    ]
    for fallbacks, item in analysis["by_k"].items():
        lines.append(
            f"| {fallbacks} | {item['exact_monotone']}/{item['total']} | "
            f"{item['practical_monotone']}/{item['total']} | "
            f"{_format_sequence(item['mean_absorption_pp'])} |"
        )

    lines += [
        "",
        "## Per-setting sequences",
        "",
        "| m | W | K | absorption at G=0.5/1/2.5/5 s (pp) | "
        "exact monotone | practical monotone |",
        "|---:|---:|---:|---|:---:|:---:|",
    ]
    for row in analysis["rows"]:
        lines.append(
            f"| {row['m']} | {row['width']} | {row['K']} | "
            f"{_format_sequence(row['means_pp'])} | "
            f"{'yes' if row['exact_monotone'] else 'no'} | "
            f"{'yes' if row['practical_monotone'] else 'no'} |"
        )

    lines += [
        "",
        "## Practical negative transitions",
        "",
    ]
    if analysis["violations"]:
        lines += [
            "| m | W | K | transition | change (pp) | 3/3 seeds |",
            "|---:|---:|---:|---|---:|:---:|",
        ]
        for item in analysis["violations"]:
            lines.append(
                f"| {item['m']} | {item['width']} | {item['K']} | "
                f"{item['from_G']:g}s -> {item['to_G']:g}s | "
                f"{item['step_pp']:.3f} | "
                f"{'yes' if item['unanimous_negative'] else 'no'} |"
            )
    else:
        lines.append("None.")
    lines.append("")
    ANALYSIS.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    data = load_verified(VERIFIED_CACHE, MANIFEST)
    analysis = build_analysis(data)
    ANALYSIS_JSON.write_text(
        json.dumps(analysis, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    write_report(analysis)
    summary = analysis["summary"]
    print(
        f"exact={summary['exact_monotone']}/{summary['sequences']}, "
        f"practical={summary['practical_monotone']}/{summary['sequences']}, "
        f"violations={summary['violations']}"
    )
    print(ANALYSIS)


if __name__ == "__main__":
    main()
