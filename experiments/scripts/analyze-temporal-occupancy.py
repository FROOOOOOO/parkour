#!/usr/bin/env python3
"""Summarize scheduling attempts and ACF by workload-fill occupancy interval."""

import argparse
import json
import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


SCENARIOS = [
    {
        "key": "low-contention-5k",
        "title": "Low contention (5k nodes)",
        "raw_resource_at_full": 0.90625,
        "configs": [
            ("B1", "B1-5000n-E2_", "Vanilla (event-driven)"),
            ("B1", "B1-5000n-E3_", "ParKour (event-driven)"),
        ],
    },
    {
        "key": "high-contention-10k-n2",
        "title": "High contention (10k nodes, 2 schedulers)",
        "raw_resource_at_full": 0.75,
        "configs": [
            ("B3", "B3-N2-E2_", "Vanilla (event-driven)"),
            ("B3", "B3-N2-E3_", "ParKour (event-driven)"),
            ("B3", "B3-N2-P1_", "Vanilla (periodic)"),
            ("B3", "B3-N2-P4_", "ParKour (periodic)"),
        ],
    },
]

INTERVALS = [
    ("0-80%", 0.0, 0.8),
    ("80-90%", 0.8, 0.9),
    ("90-100%", 0.9, 1.0),
]


def _b2_scenarios() -> list[dict]:
    scenarios = []
    for nodes, scale in ((2000, "2k"), (5000, "5k"), (10000, "10k"),
                         (20000, "20k")):
        scenarios.extend(
            [
                {
                    "key": f"b2-{scale}-event-driven",
                    "title": f"B2 {scale} nodes (event-driven)",
                    "raw_resource_at_full": 0.75,
                    "configs": [
                        ("B2", f"B2-{nodes}n-E1_", "Single kube-scheduler"),
                        ("B2", f"B2-{nodes}n-E2_", "Vanilla (event-driven)"),
                        ("B2", f"B2-{nodes}n-E3_", "ParKour (event-driven)"),
                    ],
                },
                {
                    "key": f"b2-{scale}-periodic",
                    "title": f"B2 {scale} nodes (periodic)",
                    "raw_resource_at_full": 0.75,
                    "configs": [
                        ("B2", f"B2-{nodes}n-P1_", "Vanilla (periodic)"),
                        ("B2", f"B2-{nodes}n-P2_", "sameSync"),
                        ("B2", f"B2-{nodes}n-P3_", "diffSync"),
                        ("B2", f"B2-{nodes}n-P4_", "ParKour (periodic)"),
                    ],
                },
            ]
        )
    return scenarios


LOG_TIME_RE = re.compile(r"^I(\d{4}) (\d\d:\d\d:\d\d\.\d+)")
POD_STATUS_RE = re.compile(
    r"namespace\(([^)]+)\).*Pods: (\d+) out of (\d+) created, "
    r"(\d+) running .*?, (\d+) pending scheduled,"
)


def _read_json(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return json.load(stream)


def _find_experiment(results_root: Path, group: str, prefix: str) -> Path:
    matches = sorted(path for path in (results_root / group).glob(f"{prefix}*")
                     if path.is_dir())
    if len(matches) != 1:
        raise RuntimeError(
            f"Expected one experiment for {group}/{prefix}*, found {len(matches)}"
        )
    return matches[0]


def _log_epoch(day: str, clock: str) -> float:
    parsed = datetime.strptime(
        f"2026{day} {clock}", "%Y%m%d %H:%M:%S.%f"
    ).replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _occupancy_boundaries(log_path: Path, expected_pods: int) -> list[float]:
    """Return UTC epochs at 0%, 80%, 90%, and 100% workload fill."""
    thresholds = [
        0,
        math.ceil(expected_pods * 0.8),
        math.ceil(expected_pods * 0.9),
        expected_pods,
    ]
    start = None
    states: dict[str, int] = {}
    crossings: dict[int, float] = {}

    with log_path.open(encoding="utf-8", errors="replace") as stream:
        for line in stream:
            time_match = LOG_TIME_RE.match(line)
            if not time_match:
                continue
            timestamp = _log_epoch(time_match.group(1), time_match.group(2))

            if 'Creating saturation pods" started' in line:
                start = timestamp
                crossings[0] = timestamp
                continue
            if start is None:
                continue
            if 'Deleting saturation pods" started' in line:
                break

            status_match = POD_STATUS_RE.search(line)
            if not status_match or "controlledBy(saturation-" not in line:
                continue
            namespace = status_match.group(1)
            target = int(status_match.group(3))
            running = int(status_match.group(4))
            pending_scheduled = int(status_match.group(5))
            if target == 0:
                continue

            states[namespace] = running + pending_scheduled
            assigned = sum(states.values())
            for threshold in thresholds[1:]:
                if threshold not in crossings and assigned >= threshold:
                    crossings[threshold] = timestamp

    missing = [threshold for threshold in thresholds if threshold not in crossings]
    if missing:
        raise RuntimeError(f"{log_path}: missing occupancy crossings {missing}")
    return [crossings[threshold] for threshold in thresholds]


def _matrix_sum(path: Path) -> list[tuple[float, float]]:
    data = _read_json(path)
    totals: dict[float, float] = defaultdict(float)
    for result in data.get("data", {}).get("result", []):
        for timestamp, raw_value in result.get("values", []):
            value = float(raw_value)
            if math.isfinite(value):
                totals[float(timestamp)] += value
    return sorted(totals.items())


def _integrate(points: list[tuple[float, float]], start: float, end: float) -> float:
    if end <= start or not points:
        return 0.0
    timestamps = np.asarray([point[0] for point in points], dtype=float)
    values = np.asarray([point[1] for point in points], dtype=float)
    inside = timestamps[(timestamps > start) & (timestamps < end)]
    grid = np.concatenate(([start], inside, [end]))
    samples = np.interp(grid, timestamps, values)
    return float(np.trapezoid(samples, grid))


def _summary_value(path: Path, *keys: str) -> float:
    value: Any = _read_json(path)
    for key in keys:
        value = value[key]
    return float(value)


def _process_trial(
    trial_dir: Path, expected_pods: int
) -> dict:
    boundaries = _occupancy_boundaries(trial_dir / "cl2.log", expected_pods)
    acf_points = _matrix_sum(
        trial_dir / "metrics-saturation" / "all_candidates_failed_rate.json"
    )
    exact_acf = _summary_value(
        trial_dir / "metrics-saturation" / "snap_summary.json",
        "binding",
        "all_candidates_failed",
    )

    raw_acf = [
        _integrate(acf_points, boundaries[index], boundaries[index + 1])
        for index in range(3)
    ]
    raw_total = sum(raw_acf)
    if exact_acf == 0:
        interval_acf = [0.0, 0.0, 0.0]
    elif raw_total > 0:
        interval_acf = [value * exact_acf / raw_total for value in raw_acf]
    else:
        durations = [
            boundaries[index + 1] - boundaries[index] for index in range(3)
        ]
        duration_total = sum(durations)
        interval_acf = [
            exact_acf * duration / duration_total for duration in durations
        ]

    pod_boundaries = [
        0,
        math.ceil(expected_pods * 0.8),
        math.ceil(expected_pods * 0.9),
        expected_pods,
    ]
    intervals = []
    for index, (name, _, _) in enumerate(INTERVALS):
        duration = boundaries[index + 1] - boundaries[index]
        unique_pods = pod_boundaries[index + 1] - pod_boundaries[index]
        acf = interval_acf[index]
        scheduled_attempts = unique_pods + acf
        intervals.append(
            {
                "interval": name,
                "start_seconds": boundaries[index] - boundaries[0],
                "end_seconds": boundaries[index + 1] - boundaries[0],
                "duration_seconds": duration,
                "unique_bound_pods": unique_pods,
                "delta_acf_estimate": acf,
                "acf_rate_estimate": (
                    acf / scheduled_attempts if scheduled_attempts > 0 else 0.0
                ),
                "delta_scheduled_attempts_estimate": scheduled_attempts,
                "mean_scheduling_throughput": scheduled_attempts / duration,
                "mean_useful_placement_throughput": unique_pods / duration,
            }
        )

    return {
        "trial": int(trial_dir.name.split("-")[-1]),
        "exact_total_acf": exact_acf,
        "estimated_total_acf": sum(interval_acf),
        "intervals": intervals,
    }


def _stats(values: list[float]) -> dict:
    array = np.asarray(values, dtype=float)
    return {
        "median": float(np.median(array)),
        "q1": float(np.percentile(array, 25)),
        "q3": float(np.percentile(array, 75)),
        "min": float(np.min(array)),
        "max": float(np.max(array)),
        "n": len(values),
    }


def _aggregate_trials(trials: list[dict]) -> list[dict]:
    output = []
    fields = [
        "duration_seconds",
        "unique_bound_pods",
        "delta_acf_estimate",
        "acf_rate_estimate",
        "delta_scheduled_attempts_estimate",
        "mean_scheduling_throughput",
        "mean_useful_placement_throughput",
    ]
    for interval_index, (name, _, _) in enumerate(INTERVALS):
        summary: dict[str, Any] = {"interval": name}
        for field in fields:
            summary[field] = _stats(
                [trial["intervals"][interval_index][field] for trial in trials]
            )
        output.append(summary)
    return output


def collect(results_root: Path, scenarios: list[dict], scope: str) -> dict:
    output = {
        "metadata": {
            "scope": scope,
            "trial_selection": (
                "All five raw trials per configuration; no outlier filtering."
            ),
            "occupancy_definition": (
                "Unique bound pods divided by the configured workload capacity "
                "(num_nodes * pods_per_node)."
            ),
            "scheduled_definition": (
                "Unique bound pods plus all-candidate failures, because each ACF "
                "causes one additional scheduling attempt."
            ),
            "acf_allocation": (
                "The 1-minute ACF event-rate series is integrated per interval "
                "and scaled to the exact per-trial ACF counter delta."
            ),
            "intervals": [name for name, _, _ in INTERVALS],
        },
        "scenarios": {},
    }

    for scenario in scenarios:
        scenario_output = {
            "title": scenario["title"],
            "raw_resource_utilization_at_100_percent_fill": scenario[
                "raw_resource_at_full"
            ],
            "configs": {},
        }
        for group, prefix, label in scenario["configs"]:
            experiment_dir = _find_experiment(results_root, group, prefix)
            config = _read_json(experiment_dir / "config.json")
            params = config["parameters"]
            expected_pods = (
                int(params["num_nodes"]) * int(params["pods_per_node"])
            )
            trials = [
                _process_trial(trial_dir, expected_pods)
                for trial_dir in sorted(experiment_dir.glob("trial-*"))
                if (trial_dir / "cl2.log").exists()
            ]
            scenario_output["configs"][label] = {
                "experiment": experiment_dir.name,
                "expected_unique_pods": expected_pods,
                "trials": trials,
                "summary": _aggregate_trials(trials),
            }
        output["scenarios"][scenario["key"]] = scenario_output
    return output


def _cell(stats: dict, digits: int = 1) -> str:
    pattern = f"{{:.{digits}f}}"
    return (
        f"{pattern.format(stats['median'])} "
        f"[{pattern.format(stats['q1'])}, {pattern.format(stats['q3'])}]"
    )


def _percent_cell(stats: dict) -> str:
    scaled = {
        key: value * 100 if isinstance(value, (int, float)) else value
        for key, value in stats.items()
    }
    return _cell(scaled, digits=2)


def write_markdown(data: dict, output_path: Path) -> None:
    title = (
        "# B2 Temporal Scheduling by Slot Occupancy"
        if data["metadata"]["scope"] == "b2"
        else "# Temporal Scheduling by Workload-Fill Occupancy"
    )
    lines = [
        title,
        "",
        "Values are median [Q1, Q3] across five trials.",
        "",
        "| Scenario | Configuration | Occupancy | Time span (s) | "
        "Delta scheduled attempts | Delta ACF | ACF rate | "
        "Scheduling throughput (attempts/s) | Useful throughput (pods/s) |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for scenario in data["scenarios"].values():
        for config in scenario["configs"].values():
            label = next(
                key for key, value in scenario["configs"].items()
                if value is config
            )
            for interval in config["summary"]:
                lines.append(
                    f"| {scenario['title']} | {label} | {interval['interval']} | "
                    f"{_cell(interval['duration_seconds'])} | "
                    f"{_cell(interval['delta_scheduled_attempts_estimate'])} | "
                    f"{_cell(interval['delta_acf_estimate'])} | "
                    f"{_percent_cell(interval['acf_rate_estimate'])} | "
                    f"{_cell(interval['mean_scheduling_throughput'])} | "
                    f"{_cell(interval['mean_useful_placement_throughput'])} |"
                )
    lines.extend(
        [
            "",
            "Notes:",
            "",
            "- All five raw trials are included; no outlier filtering is applied.",
            "- Occupancy is workload-fill occupancy, not raw CPU utilization.",
            (
                "- At 100% slot fill, B2 high contention uses 75% of aggregate "
                "raw CPU/memory requests."
                if data["metadata"]["scope"] == "b2"
                else "- At 100% fill, low contention uses 90.625% of raw "
                "CPU/memory; high contention uses 75%."
            ),
            "- Interval boundaries use unique `running + pending scheduled` pods "
            "from CL2 logs.",
            "- Delta ACF is interval-estimated because the saved Prometheus series "
            "uses a 1-minute moving rate; its three intervals sum to the exact "
            "per-trial ACF counter delta.",
            "- Useful throughput counts unique bound pods; scheduling throughput "
            "also counts attempts that later fail all candidates.",
            "- ACF rate is Delta ACF divided by Delta scheduled attempts within "
            "the occupancy interval.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def _signed(value: float, unit: str) -> str:
    if abs(value) < 0.05:
        return f"0.0{unit}"
    return f"{value:+.1f}{unit}"


def write_b2_response_comparison(data: dict, output_path: Path) -> None:
    lines = [
        "# B2 Response Comparison",
        "",
        "Each cell reports ParKour relative to the matched Vanilla baseline as "
        "`useful-throughput change; ACF-rate change`.",
        "",
        "| Nodes / synchronization | 0-80% slots | 80-90% slots | 90-100% slots |",
        "|---|---:|---:|---:|",
    ]
    for scale in ("2k", "5k", "10k", "20k"):
        for mode, mode_label, baseline, parkour in (
            (
                "event-driven",
                "event-driven",
                "Vanilla (event-driven)",
                "ParKour (event-driven)",
            ),
            (
                "periodic",
                "periodic",
                "Vanilla (periodic)",
                "ParKour (periodic)",
            ),
        ):
            configs = data["scenarios"][f"b2-{scale}-{mode}"]["configs"]
            cells = []
            for base_row, parkour_row in zip(
                configs[baseline]["summary"], configs[parkour]["summary"]
            ):
                base_throughput = base_row[
                    "mean_useful_placement_throughput"
                ]["median"]
                parkour_throughput = parkour_row[
                    "mean_useful_placement_throughput"
                ]["median"]
                throughput_change = (
                    parkour_throughput / base_throughput - 1
                ) * 100
                acf_rate_change = (
                    parkour_row["acf_rate_estimate"]["median"]
                    - base_row["acf_rate_estimate"]["median"]
                ) * 100
                cells.append(
                    f"TP {_signed(throughput_change, '%')}; "
                    f"ACF {_signed(acf_rate_change, ' pp')}"
                )
            lines.append(
                f"| {scale} / {mode_label} | {cells[0]} | {cells[1]} | "
                f"{cells[2]} |"
            )
    lines.extend(
        [
            "",
            "Notes:",
            "",
            "- TP is the relative change in median useful placement throughput.",
            "- ACF is the percentage-point change in median interval ACF rate; "
            "negative values indicate a reduction.",
            "- All five raw trials are included without outlier filtering.",
            "",
        ]
    )
    output_path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    script_dir = Path(__file__).resolve().parent
    default_results = script_dir.parent / "results"
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", type=Path, default=default_results)
    parser.add_argument("--scope", choices=("selected", "b2"), default="selected")
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    parser.add_argument("--comparison-out", type=Path)
    args = parser.parse_args()

    if args.scope == "b2":
        scenarios = _b2_scenarios()
        default_stem = "Table-B2-temporal-occupancy"
    else:
        scenarios = SCENARIOS
        default_stem = "Table-temporal-occupancy"
    json_out = args.json_out or (
        default_results / "figures" / f"{default_stem}.json"
    )
    markdown_out = args.markdown_out or (
        default_results / "figures" / f"{default_stem}.md"
    )
    comparison_out = args.comparison_out or (
        default_results / "figures" / "Table-B2-response-comparison.md"
    )

    data = collect(args.results.resolve(), scenarios, args.scope)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    with json_out.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, indent=2)
        stream.write("\n")
    write_markdown(data, markdown_out)
    print(f"Wrote {json_out}")
    print(f"Wrote {markdown_out}")
    if args.scope == "b2":
        write_b2_response_comparison(data, comparison_out)
        print(f"Wrote {comparison_out}")


if __name__ == "__main__":
    main()
