"""The batch drivers run the registry.

For every board the drivers run, the runs `--dry-run` plans must be the board's
registry cells exactly: the same names, parameters and trial counts, written
into the board's directory and stamped with their cell. Their hardcoded loops
once disagreed with the published runs; this is the check that catches it.
"""

import os
import re
import shutil
import subprocess

import pytest

from common import registry as reg

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS = os.path.join(EXPERIMENTS, "scripts")
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(not (BASH and shutil.which("python3")),
                                reason="the drivers need bash, and python3 for the registry")

REGISTRY = reg.load()
OPTIONS = {"--name": "name", "--nodes": "num_nodes", "--schedulers": "num_schedulers",
           "--backup": "num_backup", "--penalty": "conflict_penalty", "--strategy": "strategy",
           "--strategy-seed": "strategy_seed", "--sync-period": "sync_period",
           "--partitions": "num_partitions", "--sync-pattern": "sync_pattern",
           "--trials": "trials", "--variance": "capacity_variance",
           "--pods-per-node": "pods_per_node", "--cpu-request": "cpu_request",
           "--memory-request": "memory_request", "--results-dir": "results_dir",
           "--board": "board", "--cell": "cell", "--prometheus-url": "prometheus_url"}
FLAGS = {"--preserve-nodes", "--collect-logs"}


def plan(script, *args):
    """The runs a driver's dry run plans, as {field: value} dicts."""

    result = subprocess.run([BASH, os.path.join(SCRIPTS, script), "--dry-run",
                             "--results-root", "/RR", *args],
                            capture_output=True, text=True, encoding="utf-8", errors="replace")
    assert result.returncode == 0, result.stdout + result.stderr
    runs = []
    for line in re.sub(r"\\\r?\n\s*", " ", result.stdout).splitlines():
        if "[DRY RUN]" not in line:
            continue
        tokens = line.split("[DRY RUN]", 1)[1].split()[1:]
        run, index = {}, 0
        while index < len(tokens):
            if tokens[index] in FLAGS:
                index += 1
                continue
            run[OPTIONS[tokens[index]]] = tokens[index + 1]
            index += 2
        runs.append(run)
    return runs


def strict(value):
    """Numbers compare as numbers; unlike registry.normalize, nothing else is
    rewritten, so a glob run planned with ten partitions shows."""

    try:
        return float(value)
    except (TypeError, ValueError):
        return value


def planned(runs):
    return sorted(tuple(sorted((k, strict(v)) for k, v in run.items() if k != "prometheus_url"))
                  for run in runs)


def declared(board, name_of=lambda cell: cell["cell"], paradigm=None):
    directory = REGISTRY["boards"][board]["directory"]
    runs = []
    for cell in reg.cells(REGISTRY, board=board):
        if paradigm and cell["paradigm"] != paradigm:
            continue
        runs.append({**cell["params"], "name": name_of(cell), "trials": cell["trials"],
                     "results_dir": f"/RR/{directory}", "board": board, "cell": cell["cell"]})
    return planned(runs)


@pytest.mark.parametrize("board", ["B1", "B2", "B3", "K", "P", "ablation"])
def test_batch_run_plans_each_board_as_declared(board):
    assert planned(plan("batch-run.sh", "--group", board)) == declared(board)


def test_ablation_paradigm_groups_split_the_board():
    assert planned(plan("batch-run.sh", "--group", "C-event")) == declared("ablation", paradigm="event")
    assert planned(plan("batch-run.sh", "--group", "C-periodic")) == declared("ablation", paradigm="periodic")


def test_registry_group_is_every_board():
    boards = ("B1", "B2", "B3", "K", "P", "ablation")
    assert planned(plan("batch-run.sh", "--group", "registry")) == sorted(
        run for board in boards for run in declared(board))


def test_godel_driver_plans_the_godel_board():
    assert planned(plan("batch-run-godel.sh", "--group", "all")) == declared(
        "godel", name_of=lambda cell: cell["cell"][: -len("-godel")])


def test_trials_option_overrides_the_declared_count():
    assert {run["trials"] for run in plan("batch-run.sh", "--group", "B3", "--trials", "2")} == {"2"}
