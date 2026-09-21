#!/usr/bin/env python3
"""Verify the Figure 4 cache and lock its plotting manifest."""

from __future__ import annotations

import sys
from pathlib import Path

FIGURE_DIR = Path(__file__).resolve().parent
SIM_ROOT = FIGURE_DIR.parents[1]
if str(SIM_ROOT) not in sys.path:
    sys.path.insert(0, str(SIM_ROOT))

from common.validation import verify_matrix
from figures.fig4_conflict.experiment import (
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
