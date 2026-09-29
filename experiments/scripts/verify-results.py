#!/usr/bin/env python3
"""Validate the figure-data archive against the registry.

Runs every check in `common/verify.py` and prints one line per check. Exits
non-zero when a fatal check fails; advisory checks are reported but never
change the exit status. Nothing outside the repository is read unless
`--trace` names a copy of the public trace, whose SHA-256 is then checked
against the registry.

An archive that `reduce.py --boards` produced is verified with the same
`--boards`; without it, every board must be there.

Usage:
    python experiments/scripts/verify-results.py
    python experiments/scripts/verify-results.py --report verification.md
    python experiments/scripts/verify-results.py --trace <dir>/batch_task.csv
    python experiments/scripts/verify-results.py --archive <dir> --boards B2 godel
"""

from __future__ import annotations

import argparse
import json
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import registry as reg  # noqa: E402
from common import schema  # noqa: E402
from common.data import write_text_atomic  # noqa: E402
from common.verify import FATAL, Check, verify  # noqa: E402


def check_trace(path: str, registry_path: str) -> Check:
    pinned = reg.load(registry_path)["inputs"]["alibaba-cluster-trace-v2018"]
    if not os.path.isfile(path):
        return Check("trace_input", FATAL, False, f"no file at {path}")
    actual = schema.file_sha256(path)
    return Check("trace_input", FATAL, actual == pinned["sha256"],
                 "the trace matches the pinned SHA-256" if actual == pinned["sha256"]
                 else f"SHA-256 {actual} differs from the pinned {pinned['sha256']}")


def held_by_manifest(archive_dir: str) -> tuple[str, ...] | None:
    """The boards the archive's manifest lists files of; None without a manifest."""

    path = os.path.join(archive_dir, schema.FILES["manifest"])
    if not os.path.isfile(path):
        return None
    with open(path, encoding="utf-8") as handle:
        return schema.held_boards(json.load(handle).get("files", {}))


def report(checks: list[Check]) -> str:
    lines = ["# Archive verification", "",
             "| Check | Severity | Result | Detail |", "|---|---|---|---|"]
    for check in checks:
        result = "PASS" if check.passed else ("FAIL" if check.severity == FATAL else "NOTE")
        lines.append(f"| `{check.name}` | {check.severity} | {result} | {check.detail} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--archive", default=schema.ARCHIVE_DIR)
    parser.add_argument("--registry", default=reg.REGISTRY_FILE)
    parser.add_argument("--boards", nargs="+", choices=schema.ALL_BOARDS, metavar="BOARD",
                        help="the boards an archive of some boards only was reduced for "
                             "(reduce.py --boards); default: every board")
    parser.add_argument("--trace", help="public trace file to check against its pin")
    parser.add_argument("--report", help="also write the results as Markdown")
    args = parser.parse_args(argv)

    if args.boards is None:
        held = held_by_manifest(args.archive)
        if held is not None and set(held) != set(schema.ALL_BOARDS):
            print(f"{args.archive} holds boards {' '.join(held) or 'none'} only; verify it "
                  f"with --boards {' '.join(held)}", file=sys.stderr)
            return 1
    checks = verify(args.archive, args.registry, reducer_sha256=schema.reducer_sha256(),
                    boards=args.boards)
    if args.trace:
        checks.append(check_trace(args.trace, args.registry))

    width = max(len(check.name) for check in checks)
    for check in checks:
        result = "PASS" if check.passed else ("FAIL" if check.severity == FATAL else "NOTE")
        print(f"{result:4}  {check.severity:8}  {check.name:{width}}  {check.detail}")
    if args.report:
        write_text_atomic(args.report, report(checks))

    failed = [check.name for check in checks if check.severity == FATAL and not check.passed]
    if failed:
        print(f"\n{len(failed)} fatal check(s) failed: {', '.join(failed)}", file=sys.stderr)
        return 1
    fatal = sum(1 for check in checks if check.severity == FATAL)
    print(f"\nall {fatal} fatal checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
