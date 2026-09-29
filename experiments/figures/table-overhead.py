#!/usr/bin/env python3
"""Table `overhead`: scheduler overhead at 10,000 nodes.

Five configurations, one row each: algorithm and end-to-end P99 latency, the
scheduler pool's aggregate CPU with its efficiency in scheduled pods per second
per core, and its aggregate memory. The text around the table quotes a few more
numbers from the same data (the binder's and dispatcher's CPU, the latency at
20,000 nodes, relative changes); they are printed after the rows.

Input
    experiments/work/figure-data/overhead.json, written by

        python ../scripts/export-figure-data.py --figure overhead

    This script holds no measurements of its own.

Output
    overhead.tex in the output directory: the table's body rows, the lines
    between \\midrule and \\bottomrule. They are also printed.

Usage
    python table-overhead.py
    python table-overhead.py --output-dir <dir>
"""

from __future__ import annotations

import argparse
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import data as envelope  # noqa: E402

TABLE = "overhead"
DEFAULT_OUTPUT_DIR = os.path.join(_HERE, "output")

#: The table's rows, top to bottom: (arm in the exported data, row label).
ROWS = (
    ("event-vanilla-diff", "Vanilla (event-driven)"),
    ("event-parkour-diff", "ParKour (event-driven)"),
    ("periodic-vanilla-glob", "Vanilla (periodic)"),
    ("periodic-vanilla-diff", "diffSync"),
    ("periodic-parkour-glob", "ParKour (periodic)"),
)
TABLE_NODES = 10000
#: The row whose efficiency the table sets in bold.
EMPHASISED = "periodic-parkour-glob"
#: The size at which the text compares the periodic arms' algorithm latency.
LARGE_NODES = 20000
GIB = 2 ** 30


def _three(value: float) -> str:
    """Three significant figures, trailing zeros kept: 9.89, 6.00, 23.6."""

    return f"{value:#.3g}"


def table_rows(rows: dict[tuple[str, int], dict]) -> list[str]:
    lines = []
    for arm, label in ROWS:
        row = rows[(arm, TABLE_NODES)]
        efficiency = _three(row["pods_per_core_s"])
        if arm == EMPHASISED:
            efficiency = rf"\textbf{{{efficiency}}}"
        cells = [
            label,
            f"{row['algo_p99_ms']:.0f}",
            f"{row['e2e_p99_ms'] / 1000:.1f}",
            f"{_three(row['scheduler_cpu_total'])} ({efficiency})",
            f"{row['scheduler_mem_rss_total'] / GIB:.1f}",
        ]
        lines.append(" & ".join(cells) + r" \\")
    return lines


def _change(new: float, old: float) -> str:
    return f"{(new / old - 1.0) * 100.0:+.2f}%"


def quoted(rows: dict[tuple[str, int], dict]) -> list[str]:
    """The numbers the text around the table states, unrounded where the text
    rounds them, so that each can be traced to its source."""

    at = {arm: rows[(arm, TABLE_NODES)] for arm, _ in ROWS}
    e2, e3 = at["event-vanilla-diff"], at["event-parkour-diff"]
    p1, p3, p4 = (at["periodic-vanilla-glob"], at["periodic-vanilla-diff"],
                  at["periodic-parkour-glob"])
    large = {arm: rows[(arm, LARGE_NODES)]
             for arm in ("periodic-vanilla-glob", "periodic-parkour-glob")}
    five = list(at.values())

    def span(field: str, digits: int) -> str:
        values = [row[field] for row in five]
        return f"{min(values):.{digits}f}-{max(values):.{digits}f} cores"

    return [
        f"end-to-end P99, ParKour against Vanilla: event-driven "
        f"{_change(e3['e2e_p99_ms'], e2['e2e_p99_ms'])}, periodic "
        f"{_change(p4['e2e_p99_ms'], p1['e2e_p99_ms'])}",
        f"algorithm P99 at {LARGE_NODES:,} nodes, periodic: ParKour "
        f"{large['periodic-parkour-glob']['algo_p99_ms']:.1f} ms, Vanilla "
        f"{large['periodic-vanilla-glob']['algo_p99_ms']:.1f} ms",
        f"placements per second, periodic ParKour over Vanilla: "
        f"{p4['throughput_pods_per_s'] / p1['throughput_pods_per_s']:.2f}x",
        f"efficiency of periodic ParKour: {p4['pods_per_core_s']:.3f} pods/(core*s), "
        f"{p4['pods_per_core_s'] / p1['pods_per_core_s']:.3f}x Vanilla periodic, "
        f"{p4['pods_per_core_s'] / p3['pods_per_core_s']:.3f}x diffSync",
        f"event-driven efficiency: Vanilla {e2['pods_per_core_s']:.2f}, "
        f"ParKour {e3['pods_per_core_s']:.2f}",
        f"scheduler memory, ParKour against Vanilla: event-driven "
        f"{_change(e3['scheduler_mem_rss_total'], e2['scheduler_mem_rss_total'])}, "
        f"periodic {_change(p4['scheduler_mem_rss_total'], p1['scheduler_mem_rss_total'])}",
        f"binder CPU {span('binder_cpu', 2)}, dispatcher CPU {span('dispatcher_cpu', 2)}, "
        f"scheduler pool {span('scheduler_cpu_total', 2)}",
    ]


def index(data: dict) -> dict[tuple[str, int], dict]:
    """The exported rows, keyed by (arm, cluster size)."""

    return {(row["arm"], int(row["num_nodes"])): row for row in data["rows"]}


def load_rows(path: str) -> dict[tuple[str, int], dict]:
    return index(envelope.load(path, figure=TABLE))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--data", default=envelope.path_for(_EXPERIMENTS, TABLE),
                        help="exported table data (default: experiments/work/figure-data/)")
    parser.add_argument("--output-dir", default=DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()

    rows = load_rows(args.data)
    lines = table_rows(rows)
    path = envelope.write_text_atomic(os.path.join(args.output_dir, f"{TABLE}.tex"),
                                      "\n".join(lines) + "\n")
    print("\n".join(lines))
    print()
    print("\n".join(quoted(rows)))
    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()
