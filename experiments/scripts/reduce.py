#!/usr/bin/env python3
"""Reduce recorded cluster results to the committed figure-data archive.

This is the only stage that reads raw results. Everything downstream of the
archive (validation, export, plotting) runs from the repository alone, so this
script is what connects a set of recorded runs to the figures. It reads, and
never writes, the raw results.

Inputs are named by argument, never by a machine-local default:

    --results   a results root holding one directory per board, as the registry
                names them (B1, B2, B3, K, P, ablation, and the Godel baseline)
    --anchored  the anchored module-F raw package: `module-f/<run_id>/round-*/`
                and `acf-windowed.csv`, which pull-windowed-acf.py writes

The archive is written to `experiments/archive/`. With `--check`, nothing is
written; the script rebuilds the archive in memory and fails unless it matches
the committed files byte for byte, which is how whoever holds the raw results
confirms the archive is theirs.

With `--boards`, only those boards are reduced, into a directory of their own
(`--out`): the archive then holds their records and every other record group
whose boards are all among them, and verify-results.py and export-figure-data.py
take it for the figures those boards feed. A reader who re-measured part of the
matrix gets its figures this way.

Usage:
    python experiments/scripts/reduce.py --results <root> --anchored <package>
    python experiments/scripts/reduce.py --results <root> --anchored <package> --check
    python experiments/scripts/reduce.py --results <root> --boards B2 godel --out <dir>
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import importlib.util
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Callable

_HERE = os.path.dirname(os.path.abspath(__file__))
_EXPERIMENTS = os.path.abspath(os.path.join(_HERE, ".."))
if _EXPERIMENTS not in sys.path:
    sys.path.insert(0, _EXPERIMENTS)

from common import registry as reg  # noqa: E402
from common import schema  # noqa: E402
from common.data import write_text_atomic  # noqa: E402

TRIAL_RE = re.compile(r"^trial-(\d+)$")


def _load_script(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_HERE, filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _read_json(path: str) -> dict[str, Any]:
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _public(params: dict[str, Any]) -> dict[str, Any]:
    return {key: params[key] for key in schema.PUBLIC_PARAMS if key in params}


def _trial_dirs(run_dir: str) -> list[tuple[int, str]]:
    found = []
    for name in os.listdir(run_dir):
        match = TRIAL_RE.match(name)
        if match and os.path.isdir(os.path.join(run_dir, name)):
            found.append((int(match.group(1)), os.path.join(run_dir, name)))
    return sorted(found)


def _run_inputs(run_dir: str) -> list[str]:
    """The raw files a scheduler run contributes: its configuration and, per
    trial, the CL2 log, the timing record and every saturation-phase metric."""

    paths = [os.path.join(run_dir, "config.json")]
    for _, trial_dir in _trial_dirs(run_dir):
        for name in ("cl2.log", "timing.json"):
            path = os.path.join(trial_dir, name)
            if os.path.exists(path):
                paths.append(path)
        paths.extend(glob.glob(os.path.join(trial_dir, "metrics-saturation", "*.json")))
    return paths


def _index_runs(results_root: str, directory: str,
                name_of: Callable[[dict[str, Any]], str]) -> dict[str, tuple[str, dict]]:
    """Map each recorded run's name to its directory and configuration."""

    root = os.path.join(results_root, directory)
    if not os.path.isdir(root):
        raise SystemExit(f"missing board directory: {root}")
    runs: dict[str, tuple[str, dict]] = {}
    for config_path in sorted(glob.glob(os.path.join(root, "*", "config.json"))):
        config = _read_json(config_path)
        name = name_of(config)
        if name in runs:
            raise SystemExit(
                f"two recorded runs named {name!r}: {runs[name][0]} and "
                f"{os.path.dirname(config_path)}; the archive needs exactly one"
            )
        runs[name] = (os.path.dirname(config_path), config)
    return runs


def _run_for(runs: dict[str, tuple[str, dict]], cell: dict[str, Any]) -> tuple[str, dict]:
    if cell["cell"] not in runs:
        raise SystemExit(f"no recorded run for registry cell {cell['board']}/{cell['cell']}")
    return runs[cell["cell"]]


# ---------------------------------------------------------------------------
#  Record groups
# ---------------------------------------------------------------------------

def reduce_board(pr, registry: dict, results_root: str, board: str):
    runs = _index_runs(results_root, registry["boards"][board]["directory"],
                       lambda config: config.get("name"))
    cells, inputs = [], {}
    for cell in reg.cells(registry, board=board):
        run_dir, config = _run_for(runs, cell)
        trials = []
        for _, trial_dir in _trial_dirs(run_dir):
            row = pr.process_trial(run_dir, trial_dir, config)
            trials.append({field: row[field] for field in schema.RUN_TRIAL_FIELDS})
        cells.append({"cell": cell["cell"],
                      "recorded": _public(config.get("parameters", {})),
                      "recorded_trials": config.get("num_trials"),
                      "trials": trials})
        inputs[cell["cell"]] = schema.tree_digest(run_dir, _run_inputs(run_dir))
    payload = {"schema": schema.SCHEMA, "kind": "board", "board": board, "cells": cells}
    return payload, inputs


def reduce_godel(pr, registry: dict, results_root: str):
    """The Godel baseline, with throughput from the CL2 log like every other arm.

    Throughput, completion and duration come from process-results.py's own CL2
    path, so the baseline and the ParKour arms share one computation. The
    conflict fields come from the collector summary: the CL2 log carries no
    bind outcomes, and that summary is this repository's collector output.
    """

    runs = _index_runs(results_root, registry["boards"]["godel"]["directory"],
                       lambda config: f"{config.get('experiment_name')}-godel")
    cells, inputs = [], {}
    for cell in reg.cells(registry, board="godel"):
        run_dir, config = _run_for(runs, cell)
        # process_trial reads run parameters from a `parameters` block, which the
        # baseline runner does not write; give it the three it needs.
        wrapped = {"name": config.get("experiment_name"),
                   "parameters": {key: config[key] for key in
                                  ("num_nodes", "num_schedulers", "pods_per_node")}}
        trials = []
        for _, trial_dir in _trial_dirs(run_dir):
            row = pr.process_trial(run_dir, trial_dir, wrapped)
            summary = _read_json(os.path.join(trial_dir, "metrics-saturation",
                                              "snap_summary.json"))
            binding, scheduling = summary.get("binding", {}), summary.get("scheduling", {})
            trials.append({
                "trial": row["trial"],
                "expected_pods": row["expected_pods"],
                "scheduled_pods": row["scheduled_pods"],
                "scheduling_duration_s": row["scheduling_duration_s"],
                "throughput_pods_per_s": row["throughput_pods_per_s"],
                "is_timeout": row["is_timeout"],
                "acf_rate": binding.get("conflict_rate"),
                "acf_count": binding.get("failure"),
                "bind_conflict_rate": binding.get("conflict_rate"),
                "bind_conflict_count": binding.get("conflict"),
                "collector_throughput_pods_per_s": scheduling.get("throughput_pods_per_sec"),
            })
        recorded = {key: config[key] for key in
                    ("num_nodes", "num_schedulers", "pods_per_node",
                     "cpu_request", "memory_request") if key in config}
        if "variance" in config:
            recorded["capacity_variance"] = config["variance"]
        cells.append({"cell": cell["cell"], "recorded": recorded,
                      "recorded_trials": config.get("num_trials"), "trials": trials})
        inputs[cell["cell"]] = schema.tree_digest(run_dir, _run_inputs(run_dir))
    return {"schema": schema.SCHEMA, "kind": "godel", "cells": cells}, inputs


def reduce_quality(registry: dict, results_root: str):
    """Per-trial placement quality of the ablation runs.

    `mean` is the accepted candidate's mean framework score; the rank histogram
    is kept as its cumulative counts at ranks 0, 1 and 2 plus the total, which
    is all the figure needs to recover the rank shares.
    """

    runs = _index_runs(results_root, registry["boards"]["ablation"]["directory"],
                       lambda config: config.get("name"))
    cells, inputs = [], {}
    for cell in reg.cells(registry, figure="ablation-quality-bc"):
        run_dir, _ = _run_for(runs, cell)
        trials, paths = [], []
        for number, trial_dir in _trial_dirs(run_dir):
            path = os.path.join(trial_dir, "metrics-saturation", "quality.json")
            if not os.path.isfile(path):
                continue
            paths.append(path)
            quality = _read_json(path)
            selected = quality.get("selected_node_score") or {}
            rank = quality.get("candidate_rank_accepted") or {}
            buckets = rank.get("buckets") or {}
            trials.append({
                "trial": number,
                "selected_node_score_mean": selected.get("mean"),
                "candidate_rank_count": rank.get("count", buckets.get("+Inf")),
                "candidate_rank_cumulative": [
                    buckets.get(f"{r}.0", buckets.get(str(r))) for r in range(3)
                ],
            })
        cells.append({"cell": cell["cell"], "trials": trials})
        inputs[cell["cell"]] = schema.tree_digest(run_dir, paths)
    return {"schema": schema.SCHEMA, "kind": "quality", "cells": cells}, inputs


def reduce_occupancy(registry: dict, results_root: str):
    """Per-trial occupancy intervals of the B2 runs the occupancy figure plots.

    The interval analysis is analyze-temporal-occupancy.py's, run on just those
    runs; it returns its table rather than writing one into the raw results.
    """

    occupancy = _load_script("analyze_temporal_occupancy", "analyze-temporal-occupancy.py")
    wanted = {cell["cell"] for cell in reg.cells(registry, figure="occupancy-intervals-1col")}
    scenarios = []
    for scenario in occupancy.b2_scenarios():
        configs = [config for config in scenario["configs"]
                   if config[1].rstrip("_") in wanted]
        if configs:
            scenarios.append({**scenario, "configs": configs})
    table = occupancy.collect(Path(results_root).resolve(), scenarios, "b2")

    runs = _index_runs(results_root, registry["boards"]["B2"]["directory"],
                       lambda config: config.get("name"))
    scenarios: dict[str, Any] = {}
    inputs = {}
    for scenario_key, scenario in sorted(table["scenarios"].items()):
        for label, block in sorted(scenario["configs"].items()):
            cell_name = block["experiment"].rsplit("_", 2)[0]
            if cell_name not in wanted:
                continue
            trials = [{"trial": trial["trial"],
                       "intervals": [{"interval": interval["interval"],
                                      **{m: interval[m] for m in schema.OCCUPANCY_METRICS}}
                                     for interval in trial["intervals"]]}
                      for trial in block["trials"]]
            scenarios.setdefault(scenario_key, {"configs": {}})["configs"][label] = {
                "cell": cell_name, "trials": trials}
            run_dir, _ = runs[cell_name]
            inputs[cell_name] = schema.tree_digest(run_dir, _run_inputs(run_dir))
    found = {c["cell"] for s in scenarios.values() for c in s["configs"].values()}
    if found != wanted:
        raise SystemExit(f"occupancy analysis did not cover {sorted(wanted - found)}")
    payload = {"schema": schema.SCHEMA, "kind": "occupancy",
               "metadata": table["metadata"], "scenarios": scenarios}
    return payload, inputs


def reduce_overhead(pr, registry: dict, results_root: str):
    """Per-trial resource use and latency of the runs the overhead table reports.

    The values are process-results.py's, computed from each trial's
    saturation-phase metrics as collect-metrics.sh records them; a metric the
    collector did not record is null. Which trials count is decided later, by
    the export, from the run records of the same cells.
    """

    indexes: dict[str, dict[str, tuple[str, dict]]] = {}
    cells, inputs = [], {}
    for cell in reg.cells(registry, figure="overhead"):
        board = cell["board"]
        if board not in schema.RUN_BOARDS:
            raise SystemExit(f"overhead cell {board}/{cell['cell']} is not a scheduler run")
        if board not in indexes:
            indexes[board] = _index_runs(results_root, registry["boards"][board]["directory"],
                                         lambda config: config.get("name"))
        run_dir, config = _run_for(indexes[board], cell)
        trials, paths = [], []
        for _, trial_dir in _trial_dirs(run_dir):
            row = pr.process_trial(run_dir, trial_dir, config)
            trials.append({field: row[field] for field in schema.OVERHEAD_TRIAL_FIELDS})
            paths += [path for path in (os.path.join(trial_dir, "metrics-saturation", name)
                                        for name in schema.OVERHEAD_SOURCES)
                      if os.path.isfile(path)]
        cells.append({"cell": cell["cell"], "trials": trials})
        inputs[cell["cell"]] = schema.tree_digest(run_dir, paths)
    return {"schema": schema.SCHEMA, "kind": "overhead", "cells": cells}, inputs


def _dig(tree: Any, dotted: str) -> Any:
    for part in dotted.split("."):
        if not isinstance(tree, dict):
            return None
        tree = tree.get(part)
    return tree


def reduce_dataplane(anchored: str):
    """The anchored module-F rounds, flattened, with their windowed ACF.

    The windowed ACF was reconstructed from the binder's counters at 1 s
    resolution by the package's `acf_window.py` while those counters were still
    held by the monitoring server; it is an input here, not something this step
    can recompute.
    """

    module_f = os.path.join(anchored, "module-f")
    windowed_path = os.path.join(anchored, "acf-windowed.csv")
    if not os.path.isdir(module_f) or not os.path.isfile(windowed_path):
        raise SystemExit(f"{anchored} is not an anchored module-F package "
                         "(needs module-f/ and acf-windowed.csv)")
    with open(windowed_path, newline="", encoding="utf-8") as handle:
        windowed = {(row["run_id"], row["round"]): row for row in csv.DictReader(handle)}

    rounds, inputs = [], {}
    for run_id in sorted(os.listdir(module_f)):
        run_path = os.path.join(module_f, run_id)
        for round_dir in sorted(d for d in os.listdir(run_path) if d.startswith("round-")):
            path = os.path.join(run_path, round_dir)
            sources = [os.path.join(path, "round-summary.json"),
                       os.path.join(path, "completion-curve.json"),
                       os.path.join(path, "control-plane-source", "config.json")]
            summary, curve, config = (_read_json(p) for p in sources)
            window = windowed.get((run_id, round_dir))
            verified = bool(window) and str(window.get("verified")).strip().lower() == "true"
            rounds.append({
                "run_id": run_id,
                "round_dir": round_dir,
                "experiment": summary.get("experiment"),
                "order": summary.get("order"),
                "trial": summary.get("trial"),
                "method": summary.get("method"),
                "profile": summary.get("dataplane_profile"),
                "kept": summary.get("valid"),
                "censored": _dig(curve, "landmarks.tail_right_censored"),
                "q_bind_at_t99": _dig(curve, "landmarks.q_bind_at_t99"),
                "throughput_raw_pods_per_s": _dig(summary, "control_plane.throughput_pods_per_s"),
                "acf_rate": _dig(summary, "control_plane.acf_rate"),
                "tail_fraction": _dig(curve, "landmarks.tail_fraction"),
                "injection_observed": _dig(summary, "failure_injection.observed_failure_rate"),
                "injection_pass": _dig(summary, "failure_injection.pass"),
                "injection_failed": _dig(summary, "failure_injection.failed"),
                "failed_semantics_valid": _dig(summary, "failure_injection.failed_semantics_valid"),
                "t99_acf_rate": float(window["t99_acf_rate"]) if verified else None,
                "windowed_acf_verified": verified,
                "recorded": _public(config.get("parameters", {})),
            })
            inputs[f"{run_id}/{round_dir}"] = schema.tree_digest(path, sources)
    inputs["acf-windowed.csv"] = schema.file_sha256(windowed_path)
    return {"schema": schema.SCHEMA, "kind": "dataplane", "rounds": rounds}, inputs


# ---------------------------------------------------------------------------
#  Driver
# ---------------------------------------------------------------------------

def build(results_root: str | None, anchored: str | None,
          registry_path: str = reg.REGISTRY_FILE,
          boards: list[str] | None = None) -> dict[str, str]:
    """Every archive file's text, keyed by archive-relative path; with `boards`,
    the files of an archive of those boards only (`schema.archive_files`)."""

    registry = reg.load(registry_path)
    pr = _load_script("process_results", "process-results.py")
    wanted = set(schema.archive_files(boards))

    payloads: dict[str, dict] = {}
    inputs: dict[str, dict] = {}
    for board in schema.RUN_BOARDS:
        name = schema.board_file(board)
        if name in wanted:
            payloads[name], inputs[name] = reduce_board(pr, registry, results_root, board)
    for key, reducer in (("godel", lambda: reduce_godel(pr, registry, results_root)),
                         ("quality", lambda: reduce_quality(registry, results_root)),
                         ("occupancy", lambda: reduce_occupancy(registry, results_root)),
                         ("overhead", lambda: reduce_overhead(pr, registry, results_root)),
                         ("dataplane", lambda: reduce_dataplane(anchored))):
        name = schema.FILES[key]
        if name in wanted:
            payloads[name], inputs[name] = reducer()

    texts = {name: schema.render(payload) for name, payload in payloads.items()}
    manifest = {
        "schema": schema.SCHEMA,
        "kind": "manifest",
        "registry_sha256": reg.file_sha256(registry_path),
        "reducer_sha256": schema.reducer_sha256(),
        "files": {name: hashlib.sha256(text.encode("utf-8")).hexdigest()
                  for name, text in sorted(texts.items())},
        "inputs": inputs,
    }
    texts[schema.FILES["manifest"]] = schema.render(manifest)
    return texts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--results",
                        help="results root with one directory per board")
    parser.add_argument("--anchored",
                        help="anchored module-F raw package (board F)")
    parser.add_argument("--boards", nargs="+", choices=schema.ALL_BOARDS, metavar="BOARD",
                        help="reduce only these boards (default: every board); "
                             f"one or more of {', '.join(schema.ALL_BOARDS)}")
    parser.add_argument("--out",
                        help="archive directory (default: experiments/archive; "
                             "required with --boards)")
    parser.add_argument("--check", action="store_true",
                        help="write nothing; fail unless the archive matches")
    args = parser.parse_args(argv)

    boards = None
    if args.boards is not None and set(args.boards) != set(schema.ALL_BOARDS):
        boards = [board for board in schema.ALL_BOARDS if board in args.boards]
        if args.check:
            parser.error("--check compares a whole archive; drop --boards")
        if args.out is None:
            parser.error("an archive of some boards goes to a directory of its own; pass --out")
    held = boards or schema.ALL_BOARDS
    if any(board != "F" for board in held) and not args.results:
        parser.error("--results is required")
    if "F" in held and not args.anchored:
        parser.error("--anchored is required for board F")
    out = args.out or schema.ARCHIVE_DIR

    texts = build(args.results, args.anchored, boards=boards)
    if args.check:
        stale = []
        for name, text in sorted(texts.items()):
            path = os.path.join(out, name)
            current = open(path, encoding="utf-8").read() if os.path.exists(path) else None
            if current != text:
                stale.append(name)
        for name in stale:
            print(f"differs: {name}", file=sys.stderr)
        if stale:
            return 1
        print(f"archive reproduced byte for byte ({len(texts)} files)")
        return 0

    for name, text in sorted(texts.items()):
        path = write_text_atomic(os.path.join(out, name), text)
        print(f"wrote {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
