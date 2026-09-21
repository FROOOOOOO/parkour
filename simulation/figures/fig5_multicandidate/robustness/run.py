#!/usr/bin/env python3
"""Run the Figure 5 objective-parameter robustness matrix."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parent
SIM_ROOT = STUDY_DIR.parents[2]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.matrix import run_matrix, source_hash
from figures.fig5_multicandidate.robustness.experiment import (
    MATRIX_KIND,
    RAW_CACHE,
    cases,
    constants,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--output", type=Path, default=RAW_CACHE)
    args = parser.parse_args()
    payload = run_matrix(
        matrix_kind=MATRIX_KIND,
        cases=cases(),
        constants=constants(),
        output=args.output,
        source_digest=source_hash(
            [
                SIM_ROOT / "config.py",
                SIM_ROOT / "core.py",
                SIM_ROOT / "common" / "matrix.py",
                STUDY_DIR / "experiment.py",
                Path(__file__),
            ]
        ),
        jobs=args.jobs,
        resume=args.resume,
    )
    print(f"{args.output}: {len(payload['runs'])} runs")


if __name__ == "__main__":
    main()
