#!/usr/bin/env python3
"""Render the documentation's matrix tables from the registry.

`experiments/registry.json` declares the cluster matrix once. The tables below
are renderings of it, so that no document keeps a copy of its own that can
drift from what ran:

    experiments/matrix.md    every board and cell, with the parameters the
                             published runs used (the whole file is generated)
    experiments/README.md    the table of paper elements, between the lines
                             <!-- BEGIN GENERATED: paper-elements --> and
                             <!-- END GENERATED: paper-elements -->

Usage:
    python experiments/scripts/generate-docs.py            # rewrite both
    python experiments/scripts/generate-docs.py --check    # fail if either is stale
"""

from __future__ import annotations

import argparse
import os
import sys
import textwrap
from collections import defaultdict
from typing import Any

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import registry as reg  # noqa: E402
from common.data import write_text_atomic  # noqa: E402

MATRIX_FILE = os.path.join(_EXPERIMENTS, "matrix.md")
README_FILE = os.path.join(_EXPERIMENTS, "README.md")
BEGIN = "<!-- BEGIN GENERATED: paper-elements -->"
END = "<!-- END GENERATED: paper-elements -->"
#: Parameters every cell that sets them shares; stated once rather than per row.
SHARED = ("strategy", "strategy_seed", "capacity_variance")
BOARD_ORDER = ("B1", "B2", "B3", "K", "P", "ablation", "godel", "F")


def _renderer(figure: str) -> str:
    for kind in ("plot", "table"):
        relative = f"figures/{kind}-{figure}.py"
        if os.path.exists(os.path.join(_EXPERIMENTS, relative)):
            return relative
    raise SystemExit(f"no renderer for {figure!r} under experiments/figures/")


def _table(header: list[str], rows: list[list[Any]], align: str) -> list[str]:
    """A Markdown table; `align` holds one of 'l' or 'r' per column."""

    rule = ["---:" if a == "r" else "---" for a in align]
    lines = ["| " + " | ".join(header) + " |", "| " + " | ".join(rule) + " |"]
    lines += ["| " + " | ".join(str(value) for value in row) + " |" for row in rows]
    return lines


def paper_elements(registry: dict[str, Any]) -> list[str]:
    """One row per figure or table the registry feeds, and the pinned inputs."""

    cells: dict[str, list[dict]] = defaultdict(list)
    for cell in registry["cells"]:
        for figure in cell["figures"]:
            cells[figure].append(cell)
    rows = []
    for figure in sorted(cells):
        members = cells[figure]
        boards = sorted({cell["board"] for cell in members}, key=BOARD_ORDER.index)
        runs = sum(cell["trials"] for cell in members)
        rows.append([f"`{figure}`", ", ".join(boards), len(members), runs,
                     f"[`{_renderer(figure)}`]({_renderer(figure)})"])
    for name, spec in sorted(registry["inputs"].items()):
        for figure in spec["figures"]:
            rows.append([f"`{figure}`", f"public input `{name}`, pinned by SHA-256",
                         "—", "—", f"[`{_renderer(figure)}`]({_renderer(figure)})"])
    rows.sort(key=lambda row: row[0])
    return _table(["Paper element", "Boards", "Cells", "Runs", "Rendered by"],
                  rows, "llrrl")


def _sync(cell: dict[str, Any]) -> str:
    params = cell["params"]
    if "sync_pattern" not in params:
        return "—"
    kind = "event" if cell["paradigm"] == "event" else params["sync_pattern"]
    return f"{kind}, {params['sync_period']:g} s"


def _k(params: dict[str, Any]) -> str:
    backups = params.get("num_backup")
    return "—" if backups is None else f"{backups + 1} ({backups})"


def _value(params: dict[str, Any], key: str) -> Any:
    value = params.get(key)
    return "—" if value is None else value


def board_table(board: str, cells: list[dict[str, Any]]) -> list[str]:
    header = ["Cell", "Arm", "Paradigm", "Nodes", "Schedulers", "K (backups)", "w",
              "Sync", "Partitions", "Pods/node", "Pod request"]
    align = "lllrrrrlrrl"
    if board == "F":
        header.insert(3, "Profile")
        align = align[:3] + "l" + align[3:]
    header += ["Runs", "Figures"]
    align += "rl"
    rows = []
    for cell in cells:
        params = cell["params"]
        row = [f"`{cell['cell']}`", cell["arm"], cell["paradigm"],
               f"{params['num_nodes']:,}", params["num_schedulers"], _k(params),
               _value(params, "conflict_penalty"), _sync(cell),
               _value(params, "num_partitions"), params["pods_per_node"],
               f"{params['cpu_request']} / {params['memory_request']}"]
        if board == "F":
            row.insert(3, cell["profile"])
        row += [cell["trials"], ", ".join(f"`{figure}`" for figure in cell["figures"])]
        rows.append(row)
    return _table(header, rows, align)


def matrix(registry: dict[str, Any]) -> str:
    by_board: dict[str, list[dict]] = defaultdict(list)
    for cell in registry["cells"]:
        by_board[cell["board"]].append(cell)
    shared = []
    for key in SHARED:
        values = {cell["params"][key] for cell in registry["cells"] if key in cell["params"]}
        if len(values) == 1:
            shared.append(f"`{key}` = {values.pop()}")

    lines = [
        "# Cluster Experiment Matrix",
        "",
        "<!-- Generated from registry.json by scripts/generate-docs.py; do not edit. -->",
        "",
        "Every cell behind the paper's cluster figures and its overhead table, with the",
        "parameters the published runs used, as [registry.json](registry.json) declares",
        "them. [reconciliation.md](reconciliation.md) lists where the figures differ from",
        "the camera-ready ones, and [experiment-design.md](experiment-design.md) explains",
        "what each board asks.",
        "",
        "K is the candidate-list length the paper uses: the runner's `num_backup`",
        "fallbacks plus the preferred node. `w` is the conflict-rate penalty weight.",
        "Event-driven cells apply updates as they arrive (`sync_period` 0.1 s); the",
        "periodic patterns are globSync (`glob`), sameSync (`same`) and diffSync (`diff`).",
        *textwrap.wrap(f"Every cell that sets them shares {', '.join(shared)}. A board's "
                       "runs are trials, except on board F, whose unit is a round.", 80),
        "",
        "## Paper elements",
        "",
        *paper_elements(registry),
        "",
        "## Boards",
        "",
    ]
    rows = []
    for board in BOARD_ORDER:
        spec = registry["boards"][board]
        cells = by_board[board]
        rows.append([board, spec["design_board"],
                     f"`{spec['directory']}/`" if spec["directory"] else "anchored module-F package",
                     spec["unit"], len(cells), sum(cell["trials"] for cell in cells)])
    lines += _table(["Board", "Design board", "Results directory", "Unit", "Cells", "Runs"],
                    rows, "llllrr")
    for board in BOARD_ORDER:
        lines += ["", f"## Board {board}", "", *board_table(board, by_board[board])]
    return "\n".join(lines) + "\n"


def readme(text: str, registry: dict[str, Any]) -> str:
    if text.count(BEGIN) != 1 or text.count(END) != 1:
        raise SystemExit(f"{README_FILE}: expected one {BEGIN} ... {END} block")
    head, rest = text.split(BEGIN)
    _, tail = rest.split(END)
    newline = "\r\n" if "\r\n" in text else "\n"
    block = "\n".join(["", *paper_elements(registry), ""]).replace("\n", newline)
    return head + BEGIN + block + END + tail


def _read(path: str) -> str | None:
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8", newline="") as handle:
        return handle.read()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="write nothing; fail if a generated table is stale")
    args = parser.parse_args(argv)

    registry = reg.load()
    outputs = {MATRIX_FILE: matrix(registry),
               README_FILE: readme(_read(README_FILE) or "", registry)}
    if args.check:
        stale = [path for path, text in outputs.items() if _read(path) != text]
        for path in stale:
            print(f"stale: {path}; run python experiments/scripts/generate-docs.py",
                  file=sys.stderr)
        if stale:
            return 1
        print(f"generated documentation is current ({len(outputs)} files)")
        return 0
    for path, text in outputs.items():
        if path == README_FILE:
            with open(path, "w", encoding="utf-8", newline="") as handle:
                handle.write(text)
        else:
            write_text_atomic(path, text)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
