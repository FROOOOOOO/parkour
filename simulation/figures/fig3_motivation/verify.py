#!/usr/bin/env python3
"""Verify and lock the Figure 3 cache.

Beyond the shared matrix checks, this package re-runs work points that the
Figure 4 and Figure 5 matrices already cover, so verification also asserts
bit-for-bit parity with those hash-locked caches. A parity failure means the
model or the workload constants drifted between packages, which would
invalidate any cross-figure comparison in the paper.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.validation import verify_matrix
from figures.fig3_motivation.experiment import (
    MANIFEST,
    MATRIX_KIND,
    RAW_CACHE,
    REPORT,
    VERIFIED_CACHE,
    cases,
    constants,
)

PEERS = (
    ("fig4_conflict", SIM_ROOT / "figures" / "fig4_conflict" / "data" / "verified.json"),
    (
        "fig5_multicandidate/robustness",
        SIM_ROOT
        / "figures"
        / "fig5_multicandidate"
        / "robustness"
        / "data"
        / "verified.json",
    ),
)


def _index(path: Path) -> dict[tuple, float]:
    """Index one verified cache by work point, dropping the seed-free case id."""

    data = json.loads(path.read_text(encoding="utf-8"))
    table: dict[tuple, float] = {}
    for run in data["runs"]:
        config = run["config"]
        key = (
            run["num_schedulers"],
            config["num_tiers"],
            config["sync_mode"],
            config["sync_gap_cycles"],
            config["num_backup"],
            config["list_mode"],
            config["penalty_weight"],
            config["penalty_scope"],
            int(run["seed"]),
        )
        table[key] = float(run["summary"]["total_conflict_rate"])
    return table


def _parity() -> list[str]:
    """Compare every shared work point against the peer caches."""

    mine = _index(VERIFIED_CACHE)
    lines: list[str] = []
    for name, path in PEERS:
        if not path.exists():
            lines.append(f"| `{name}` | SKIP | cache absent |")
            continue
        theirs = _index(path)
        shared = sorted(set(mine) & set(theirs))
        mismatches = [key for key in shared if mine[key] != theirs[key]]
        status = "PASS" if shared and not mismatches else ("FAIL" if mismatches else "SKIP")
        detail = f"{len(shared)} shared runs, {len(mismatches)} mismatched"
        lines.append(f"| `{name}` | {status} | {detail} |")
        if mismatches:
            raise AssertionError(
                f"Figure 3 disagrees with {name} on {len(mismatches)} shared runs; "
                "the model or workload constants drifted between packages"
            )
    return lines


def main() -> None:
    """Validate the Figure 3 raw matrix, emit its verified cache, check parity."""

    manifest = verify_matrix(
        raw_path=RAW_CACHE,
        verified_path=VERIFIED_CACHE,
        manifest_path=MANIFEST,
        report_path=REPORT,
        matrix_kind=MATRIX_KIND,
        cases=cases(),
        constants=constants(),
    )
    rows = _parity()
    with REPORT.open("a", encoding="utf-8") as handle:
        handle.write("\n## Cross-package parity\n\n")
        handle.write("| Peer cache | Result | Detail |\n|---|---|---|\n")
        handle.write("\n".join(rows) + "\n")
    print(f"{REPORT}: {manifest['checks']}/{manifest['checks']} PASS")
    for row in rows:
        print(f"  parity {row}")


if __name__ == "__main__":
    main()
