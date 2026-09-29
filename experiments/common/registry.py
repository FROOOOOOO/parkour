#!/usr/bin/env python3
"""The cluster experiment matrix behind the paper's figures, declared once.

Every consumer reads this declaration instead of keeping its own copy: the
reduction step (which recorded runs make up the archive), validation (whether
the archive is complete and matches it), the figure export (which series a
cell belongs to), and the batch runners (which cells to run, and how). A cell's
`figures` also names the overhead table, `overhead`, when the table reports it.

The declaration was derived from the recorded parameters of the runs behind the
camera-ready figures; where the runners or the design document disagreed with
those, the recorded parameters won. The committed artifact is
`experiments/registry.json`; this module generates it and checks that it is
current.

Three conventions:

- `arm` is declared, not derived. It is the series key a figure uses, and it is
  not a pure function of the parameters: the sweep boards (K, P) name their arms
  by paradigm and pattern only.
- `params` use the runner's configuration names. `num_backup` counts fallback
  candidates, so the paper's candidate count is K = num_backup + 1.
- A `glob` cell declares one partition. The dispatcher forces a single partition
  under globSync whatever the runner passes (`NewPartitionAssigner` in
  `para-scheduler/pkg/dispatcher/partition.go`), so that is what ran.

Usage, from the repository root:
    python experiments/common/registry.py            # rewrite experiments/registry.json
    python experiments/common/registry.py --check    # fail if it is stale

and, for the batch drivers, which read the committed registry:
    python3 experiments/common/registry.py --directory B2
    python3 experiments/common/registry.py --rows B2 --columns cell,trials,num_nodes
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
from typing import Any, Iterable

_EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common.data import canonical_json, write_text_atomic  # noqa: E402

SCHEMA = "parkour-registry/v1"
REGISTRY_FILE = os.path.join(_EXPERIMENTS, "registry.json")

# ---------------------------------------------------------------------------
#  Building blocks
# ---------------------------------------------------------------------------

COMMON = {"strategy": "QualityFirst", "strategy_seed": 42, "capacity_variance": 0.6}

#: Low contention packs 29 small pods per node; high contention (HC-V) places
#: one large pod per node, so a node fits exactly one pod.
LOW_CONTENTION = {"pods_per_node": 29, "cpu_request": "1000m", "memory_request": "8Gi"}
HIGH_CONTENTION = {"pods_per_node": 1, "cpu_request": "24000m", "memory_request": "192Gi"}

#: Synchronisation settings; `sync_period` is in seconds.
EVENT = {"sync_period": 0.1, "num_partitions": 1, "sync_pattern": "diff"}
GLOB = {"sync_period": 1.0, "num_partitions": 1, "sync_pattern": "glob"}
SAME = {"sync_period": 1.0, "num_partitions": 10, "sync_pattern": "same"}
DIFF = {"sync_period": 1.0, "num_partitions": 10, "sync_pattern": "diff"}
PATTERNS = {"diff": DIFF, "glob": GLOB, "same": SAME}

VANILLA = {"num_backup": 0, "conflict_penalty": 0.0}
PARKOUR = {"num_backup": 2, "conflict_penalty": 0.5}

#: Scale-out methods: id -> (arm, paradigm, parameter delta).
METHODS = {
    "E1": ("single", "event", {"num_schedulers": 1, **VANILLA, **EVENT}),
    "E2": ("event-vanilla-diff", "event", {**VANILLA, **EVENT}),
    "E3": ("event-parkour-diff", "event", {**PARKOUR, **EVENT}),
    "P1": ("periodic-vanilla-glob", "periodic", {**VANILLA, **GLOB}),
    "P2": ("periodic-vanilla-same", "periodic", {**VANILLA, **SAME}),
    "P3": ("periodic-vanilla-diff", "periodic", {**VANILLA, **DIFF}),
    "P4": ("periodic-parkour-glob", "periodic", {**PARKOUR, **GLOB}),
}

#: Ablation configurations: (arm, name suffix, mechanism parameters).
ABLATION = (
    ("vanilla", "base", VANILLA),
    ("multicandidate", "M", {"num_backup": 2, "conflict_penalty": 0.0}),
    ("penalty", "P", {"num_backup": 0, "conflict_penalty": 0.5}),
    ("parkour", "MP", PARKOUR),
)

#: Data-plane methods: id -> (arm, paradigm, parameter delta).
F_METHODS = {
    "E2": ("event-vanilla", "event", {**VANILLA, **EVENT}),
    "E3": ("event-parkour", "event", {**PARKOUR, **EVENT}),
    "P1": ("periodic-vanilla", "periodic", {**VANILLA, **GLOB}),
    "P4": ("periodic-parkour", "periodic", {**PARKOUR, **GLOB}),
}
#: Data-plane matrices: (experiment, methods, profiles, trials). The periodic
#: paradigm gets more repeats by design.
F_MATRIX = (
    ("F1-E", ("E2", "E3"), ("Z0", "Dreal"), 3),
    ("F1-P", ("P1", "P4"), ("Z0", "Dreal"), 5),
    ("F2-E", ("E2", "E3"), ("Dreal-F1",), 3),
    ("F2-P", ("P1", "P4"), ("Dreal-F1",), 5),
)

#: Boards: where each one's recorded runs live under a results root, and which
#: board of the experiment design it is. Board F is read from its anchored raw
#: package rather than from a results root.
BOARDS = {
    "B1": {"design_board": "B1", "directory": "B1", "unit": "run"},
    "B2": {"design_board": "B2", "directory": "B2", "unit": "run"},
    "B3": {"design_board": "B3", "directory": "B3", "unit": "run"},
    "K": {"design_board": "A", "directory": "K", "unit": "run"},
    "P": {"design_board": "A", "directory": "P", "unit": "run"},
    "ablation": {"design_board": "C", "directory": "ablation", "unit": "run"},
    "godel": {"design_board": "baseline", "directory": "godel-new", "unit": "run"},
    "F": {"design_board": "F", "directory": None, "unit": "round"},
}

#: Inputs that are public and therefore pinned rather than archived.
INPUTS = {
    "alibaba-cluster-trace-v2018": {
        "file": "batch_task.csv",
        "sha256": "6346b0726c6e10466a585c67645af807b425b5be091caf410f5e1aff41a270bc",
        "source": "https://github.com/alibaba/clusterdata/tree/master/cluster-trace-v2018",
        "figures": ["trace-arrival-rate"],
    },
}

SCALE_OUT_NODES = {"B1": (1000, 2000, 5000), "B2": (2000, 5000, 10000, 20000)}
B3_SCHEDULERS = (2, 4, 6, 8, 10)
K_VALUES = (0, 1, 2, 4)
W_VALUES = (0.0, 0.1, 0.3, 0.5, 0.7)
OCCUPANCY_CELLS = {"B2-20000n-E2", "B2-20000n-E3", "B2-20000n-P1", "B2-20000n-P4"}
#: The overhead table's configurations at 10,000 nodes, and the two periodic
#: cells whose algorithm latency the text compares at 20,000 nodes.
OVERHEAD_CELLS = {"B2-10000n-E2", "B2-10000n-E3", "B2-10000n-P1", "B2-10000n-P3",
                  "B2-10000n-P4", "B2-20000n-P1", "B2-20000n-P4"}


def _cell(board: str, name: str, arm: str, paradigm: str, figures: Iterable[str],
          params: dict[str, Any], trials: int, **extra: Any) -> dict[str, Any]:
    return {"board": board, "cell": name, "arm": arm, "paradigm": paradigm,
            "figures": sorted(figures), "params": params, "trials": trials, **extra}


# ---------------------------------------------------------------------------
#  The matrix
# ---------------------------------------------------------------------------

def _scale_out() -> list[dict[str, Any]]:
    cells = []
    for board, profile, methods, figure in (
        ("B1", LOW_CONTENTION, ("E1", "E2", "E3"), "scalability-lowcontention"),
        ("B2", HIGH_CONTENTION, ("E1", "E2", "E3", "P1", "P2", "P3", "P4"), "pareto-all-scales"),
    ):
        for nodes in SCALE_OUT_NODES[board]:
            for method in methods:
                arm, paradigm, delta = METHODS[method]
                name = f"{board}-{nodes}n-{method}"
                figures = [figure]
                if name in OCCUPANCY_CELLS:
                    figures.append("occupancy-intervals-1col")
                if name in OVERHEAD_CELLS:
                    figures.append("overhead")
                params = {"num_nodes": nodes, "num_schedulers": 10,
                          **COMMON, **profile, **delta}
                cells.append(_cell(board, name, arm, paradigm, figures, params, 5))
    for schedulers in B3_SCHEDULERS:
        for method in ("E2", "E3", "P1", "P4"):
            arm, paradigm, delta = METHODS[method]
            params = {"num_nodes": 10000, "num_schedulers": schedulers,
                      **COMMON, **HIGH_CONTENTION, **delta}
            cells.append(_cell("B3", f"B3-N{schedulers}-{method}", arm, paradigm,
                               ["scalability-schedulers"], params, 5))
    return cells


def _sweeps() -> list[dict[str, Any]]:
    """Board A: the K sweep at a provisional w, and the w sweep at K fixed."""

    cells = []
    base = {"num_nodes": 10000, "num_schedulers": 10, **COMMON, **HIGH_CONTENTION}
    for k in K_VALUES:
        mechanism = {"num_backup": k, "conflict_penalty": 0.3}
        cells.append(_cell("K", f"S-E-K{k}", "event", "event", ["robustness"],
                           {**base, **EVENT, **mechanism}, 3))
        for pattern, preset in sorted(PATTERNS.items()):
            cells.append(_cell("K", f"S-P-K{k}-{pattern}", f"periodic-{pattern}",
                               "periodic", ["robustness"],
                               {**base, **preset, **mechanism}, 3))
    for w in W_VALUES:
        tag = f"P{round(w * 10):02d}"
        mechanism = {"num_backup": 4, "conflict_penalty": w}
        cells.append(_cell("P", f"S-E-{tag}-diff", "event", "event", ["robustness"],
                           {**base, **EVENT, **mechanism}, 3))
        for pattern, preset in sorted(PATTERNS.items()):
            cells.append(_cell("P", f"S-P-{tag}-{pattern}", f"periodic-{pattern}",
                               "periodic", ["robustness"],
                               {**base, **preset, **mechanism}, 3))
    return cells


def _ablation() -> list[dict[str, Any]]:
    cells = []
    base = {"num_nodes": 10000, "num_schedulers": 10, **COMMON, **HIGH_CONTENTION}
    for paradigm, letter, preset in (("event", "E", EVENT), ("periodic", "P", GLOB)):
        for index, (arm, suffix, mechanism) in enumerate(ABLATION):
            cells.append(_cell("ablation", f"Ab{letter}{index}-{suffix}", arm, paradigm,
                               ["ablation-quality-a", "ablation-quality-bc"],
                               {**base, **preset, **mechanism}, 5))
    return cells


def _godel() -> list[dict[str, Any]]:
    """The Godel baseline runs that feed a figure: the multi-scheduler E2 runs."""

    cells = []

    def godel(name, nodes, schedulers, profile, figure):
        params = {"num_nodes": nodes, "num_schedulers": schedulers,
                  "capacity_variance": COMMON["capacity_variance"], **profile}
        return _cell("godel", f"{name}-godel", "godel", "event", [figure], params, 5)

    for nodes in SCALE_OUT_NODES["B1"]:
        cells.append(godel(f"B1-{nodes}n-E2", nodes, 10, LOW_CONTENTION,
                           "scalability-lowcontention"))
    for nodes in SCALE_OUT_NODES["B2"]:
        cells.append(godel(f"B2-{nodes}n-E2", nodes, 10, HIGH_CONTENTION,
                           "pareto-all-scales"))
    for schedulers in B3_SCHEDULERS:
        cells.append(godel(f"B3-N{schedulers}-E2", 10000, schedulers, HIGH_CONTENTION,
                           "scalability-schedulers"))
    return cells


def _dataplane() -> list[dict[str, Any]]:
    cells = []
    base = {"num_nodes": 10000, "num_schedulers": 10, **COMMON, **HIGH_CONTENTION}
    for experiment, methods, profiles, trials in F_MATRIX:
        for method in methods:
            arm, paradigm, delta = F_METHODS[method]
            for profile in profiles:
                cells.append(_cell("F", f"{experiment}-{method}-{profile}", arm, paradigm,
                                   ["dataplane-sensitivity"],
                                   {**base, **delta, "dataplane_profile": profile},
                                   trials, experiment=experiment, method=method,
                                   profile=profile))
    return cells


def build() -> dict[str, Any]:
    cells = _scale_out() + _sweeps() + _ablation() + _godel() + _dataplane()
    cells.sort(key=lambda cell: (cell["board"], cell["cell"]))
    return {"schema": SCHEMA, "boards": BOARDS, "cells": cells, "inputs": INPUTS}


def render() -> str:
    return canonical_json(build()) + "\n"


# ---------------------------------------------------------------------------
#  Reading the committed registry
# ---------------------------------------------------------------------------

def load(path: str = REGISTRY_FILE) -> dict[str, Any]:
    import json

    with open(path, encoding="utf-8") as handle:
        registry = json.load(handle)
    if registry.get("schema") != SCHEMA:
        raise SystemExit(f"{path}: schema {registry.get('schema')!r}, expected {SCHEMA}")
    return registry


def file_sha256(path: str = REGISTRY_FILE) -> str:
    with open(path, "rb") as handle:
        return hashlib.sha256(handle.read()).hexdigest()


def cells(registry: dict[str, Any], *, board: str | None = None,
          figure: str | None = None) -> list[dict[str, Any]]:
    return [cell for cell in registry["cells"]
            if (board is None or cell["board"] == board)
            and (figure is None or figure in cell["figures"])]


def cell_index(registry: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    return {(cell["board"], cell["cell"]): cell for cell in registry["cells"]}


#: Parameters the runner records as strings but that are numbers.
NUMERIC_PARAMS = {"num_nodes", "num_schedulers", "num_backup", "strategy_seed",
                  "conflict_penalty", "sync_period", "num_partitions",
                  "pods_per_node", "capacity_variance"}


def normalize(params: dict[str, Any]) -> dict[str, Any]:
    """Canonical form for comparing declared and recorded parameters.

    Numbers compare as floats whichever type the runner wrote them in, and a
    `glob` cell has one partition because the dispatcher forces it.
    """

    out: dict[str, Any] = {}
    for key, value in params.items():
        if key in NUMERIC_PARAMS and value is not None:
            try:
                value = float(value)
            except (TypeError, ValueError):
                pass
        out[key] = value
    if out.get("sync_pattern") == "glob":
        out["num_partitions"] = 1.0
    return out


def rows(registry: dict[str, Any], board: str, columns: list[str]) -> list[str]:
    """A board's cells as tab-separated rows, for the batch drivers.

    A column is a cell field (`cell`, `arm`, `paradigm`, `trials`) or a
    parameter name. A cell without a requested column is an error, not a blank.
    """

    lines = []
    for cell in cells(registry, board=board):
        values = []
        for column in columns:
            value = cell[column] if column in cell else cell["params"].get(column)
            if value is None or isinstance(value, (list, dict)):
                raise SystemExit(f"{board}/{cell['cell']} has no column {column!r}")
            values.append(str(value))
        lines.append("\t".join(values))
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true",
                        help="fail if experiments/registry.json is stale")
    parser.add_argument("--directory", metavar="BOARD",
                        help="print the results directory of BOARD, from the committed registry")
    parser.add_argument("--rows", metavar="BOARD",
                        help="print the cells of BOARD, from the committed registry, one "
                             "tab-separated row each; --columns names the fields")
    parser.add_argument("--columns", default="cell",
                        help="comma-separated fields for --rows (default: cell)")
    args = parser.parse_args(argv)

    if args.directory or args.rows:
        registry = load()
        board = args.directory or args.rows
        if board not in registry["boards"]:
            raise SystemExit(f"unknown board {board!r}; boards: {sorted(registry['boards'])}")
        if args.directory:
            text = registry["boards"][board]["directory"]
        else:
            text = "\n".join(rows(registry, board, args.columns.split(",")))
        # For shell readers: LF line ends on every platform, so that `read`
        # never leaves a carriage return on the last field.
        sys.stdout.buffer.write((text + "\n").encode("utf-8"))
        sys.stdout.flush()
        return 0

    text = render()
    if args.check:
        current = open(REGISTRY_FILE, encoding="utf-8").read() if os.path.exists(REGISTRY_FILE) else ""
        if current != text:
            print(f"stale: {REGISTRY_FILE}; run python experiments/common/registry.py",
                  file=sys.stderr)
            return 1
        print(f"registry is current ({len(build()['cells'])} cells)")
        return 0
    write_text_atomic(REGISTRY_FILE, text)
    print(f"wrote {REGISTRY_FILE} ({len(build()['cells'])} cells)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
