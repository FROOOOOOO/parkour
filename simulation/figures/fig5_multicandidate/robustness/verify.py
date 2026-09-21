#!/usr/bin/env python3
"""Verify and lock the Figure 5 robustness cache."""

from __future__ import annotations

import sys
from pathlib import Path

STUDY_DIR = Path(__file__).resolve().parent
SIM_ROOT = STUDY_DIR.parents[2]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.validation import verify_matrix
from figures.fig5_multicandidate.robustness.experiment import (
    MANIFEST,
    MATRIX_KIND,
    RAW_CACHE,
    REPORT,
    VERIFIED_CACHE,
    cases,
    constants,
)


def main() -> None:
    manifest = verify_matrix(
        raw_path=RAW_CACHE,
        verified_path=VERIFIED_CACHE,
        manifest_path=MANIFEST,
        report_path=REPORT,
        matrix_kind=MATRIX_KIND,
        cases=cases(),
        constants=constants(),
    )
    print(f"{REPORT}: {manifest['checks']}/{manifest['checks']} PASS")


if __name__ == "__main__":
    main()
