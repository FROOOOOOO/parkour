"""Validation of the figure-data archive against the registry.

The cluster counterpart of `simulation/common/validation.py`. Every check is
named and has a severity:

- fatal checks guard what the figures depend on. Any failure means the archive
  cannot be trusted, and `verify-results.py` exits non-zero;
- advisory checks report things a reader should know but that do not make the
  archive wrong. They never fail the run: one of them compares
  parameter-identical cells on different boards, which legitimately differ,
  and a check that fails on correct data gets switched off.

Several fatal checks are tautological on the day the registry is derived from
the recorded runs; they become meaningful for every later change to either.
Those that do not depend on the registry at all (accounting, finite values,
unique keys, the manifest) are meaningful from the start.

An archive of some boards only (`reduce.py --boards`) is verified against
those boards: every check covers the record groups it holds, and the matrix
must be complete for its boards.
"""

from __future__ import annotations

import hashlib
import math
import os
from dataclasses import dataclass
from typing import Any, Iterable

from common import registry as reg
from common import schema
from common.trials import filter_cell, median_of

FATAL = "fatal"
ADVISORY = "advisory"


@dataclass
class Check:
    name: str
    severity: str
    passed: bool
    detail: str


def _finite(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return all(_finite(item) for item in value)
    if isinstance(value, dict):
        return all(_finite(item) for item in value.values())
    return False


def _run_boards(archive: dict[str, Any]) -> list[str]:
    """The scheduler boards and the Godel baseline, those the archive holds."""

    held = [board for board in schema.RUN_BOARDS if f"boards/{board}" in archive]
    return held + (["godel"] if "godel" in archive else [])


def _run_groups(archive: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    """(registry board, archived run cell) for the scheduler boards and Godel."""

    for board in _run_boards(archive):
        key = "godel" if board == "godel" else f"boards/{board}"
        for cell in archive[key]["cells"]:
            yield board, cell


def _cells(archive: dict[str, Any], key: str) -> list[dict[str, Any]]:
    """The cells of a record group, or none when the archive does not hold it."""

    return archive[key]["cells"] if key in archive else []


def _mismatched_params(declared: dict[str, Any], recorded: dict[str, Any],
                       ignore: Iterable[str] = ()) -> dict[str, tuple[Any, Any]]:
    want = reg.normalize({k: v for k, v in declared.items() if k not in ignore})
    have = reg.normalize(recorded)
    return {key: (have.get(key), value) for key, value in want.items()
            if have.get(key) != value}


def check_manifest(archive_dir: str, manifest: dict[str, Any], registry_path: str,
                   boards: Iterable[str] | None = None) -> list[Check]:
    expected = set(schema.archive_files(boards))
    recorded = set(manifest.get("files", {}))
    wrong = []
    for name in sorted(expected & recorded):
        with open(os.path.join(archive_dir, name), "rb") as handle:
            if hashlib.sha256(handle.read()).hexdigest() != manifest["files"][name]:
                wrong.append(name)
    return [
        Check("manifest_files", FATAL, not wrong and expected == recorded,
              f"{len(expected)} files hashed as recorded" if not wrong and expected == recorded
              else f"changed: {wrong}; unlisted: {sorted(expected - recorded)}; "
                   f"extra: {sorted(recorded - expected)}"),
        Check("manifest_registry", FATAL,
              manifest.get("registry_sha256") == reg.file_sha256(registry_path),
              "archive was reduced against the current registry"
              if manifest.get("registry_sha256") == reg.file_sha256(registry_path)
              else "registry changed since the archive was reduced; re-run reduce"),
    ]


def check_structure(archive: dict[str, Any]) -> list[Check]:
    problems = []
    for board in schema.RUN_BOARDS:
        payload = archive.get(f"boards/{board}")
        if payload is not None and (payload.get("kind") != "board"
                                    or payload.get("board") != board):
            problems.append(f"boards/{board}: kind or board field")
    for key in schema.GROUP_BOARDS:
        if key in archive and archive[key].get("kind") != key:
            problems.append(f"{key}: kind field")
    for board, cell in _run_groups(archive):
        fields = schema.GODEL_TRIAL_FIELDS if board == "godel" else schema.RUN_TRIAL_FIELDS
        for trial in cell["trials"]:
            missing = [f for f in fields if f not in trial]
            if missing:
                problems.append(f"{board}/{cell['cell']} trial {trial.get('trial')}: {missing}")
    for cell in _cells(archive, "overhead"):
        for trial in cell["trials"]:
            missing = [f for f in schema.OVERHEAD_TRIAL_FIELDS if f not in trial]
            if missing:
                problems.append(f"overhead/{cell['cell']} trial {trial.get('trial')}: {missing}")
    for row in archive.get("dataplane", {}).get("rounds", []):
        missing = [f for f in schema.ROUND_FIELDS if f not in row]
        if missing:
            problems.append(f"round {row.get('run_id')}/{row.get('round_dir')}: {missing}")
    return [Check("schema", FATAL, not problems,
                  "every record carries its declared fields" if not problems
                  else "; ".join(problems[:5]))]


def check_keys(archive: dict[str, Any]) -> list[Check]:
    duplicates = []
    groups = {f"boards/{b}": _cells(archive, f"boards/{b}") for b in schema.RUN_BOARDS}
    for key in ("godel", "quality", "overhead"):
        groups[key] = _cells(archive, key)
    for group, cells in groups.items():
        names = [cell["cell"] for cell in cells]
        duplicates += [f"{group}/{n}" for n in sorted(set(names)) if names.count(n) > 1]
        for cell in cells:
            trials = [trial["trial"] for trial in cell["trials"]]
            duplicates += [f"{group}/{cell['cell']} trial {t}"
                           for t in sorted(set(trials)) if trials.count(t) > 1]
    rounds = [(r["run_id"], r["round_dir"])
              for r in archive.get("dataplane", {}).get("rounds", [])]
    duplicates += [f"round {k}" for k in sorted(set(rounds)) if rounds.count(k) > 1]
    return [Check("unique_keys", FATAL, not duplicates,
                  "cells, trials and rounds are unique" if not duplicates
                  else "; ".join(duplicates[:5]))]


def check_matrix(archive: dict[str, Any], registry: dict[str, Any]) -> list[Check]:
    problems = []
    archived: dict[str, dict[str, dict]] = {}
    for board, cell in _run_groups(archive):
        archived.setdefault(board, {})[cell["cell"]] = cell
    for board in _run_boards(archive):
        declared = {c["cell"]: c for c in reg.cells(registry, board=board)}
        have = archived.get(board, {})
        for name in sorted(set(declared) - set(have)):
            problems.append(f"{board}/{name} missing")
        for name in sorted(set(have) - set(declared)):
            problems.append(f"{board}/{name} not declared")
        for name in sorted(set(declared) & set(have)):
            trials = len(have[name]["trials"])
            if trials != declared[name]["trials"] or have[name].get("recorded_trials") != trials:
                problems.append(f"{board}/{name}: {trials} trials, declared "
                                f"{declared[name]['trials']}, recorded "
                                f"{have[name].get('recorded_trials')}")

    if "quality" in archive:
        problems += _quality_matrix(archive, registry, archived)
    if "overhead" in archive:
        problems += _overhead_matrix(archive, registry, archived)
    if "occupancy" in archive:
        problems += _occupancy_matrix(archive, registry)
    if "dataplane" in archive:
        problems += _dataplane_matrix(archive, registry)
    return [Check("complete_matrix", FATAL, not problems,
                  "every declared cell is archived with its declared trials"
                  if not problems else "; ".join(problems[:5]))]


def _quality_matrix(archive: dict[str, Any], registry: dict[str, Any],
                    archived: dict[str, dict[str, dict]]) -> list[str]:
    problems = []
    wanted = {c["cell"] for c in reg.cells(registry, figure="ablation-quality-bc")}
    quality = {c["cell"]: c for c in archive["quality"]["cells"]}
    if set(quality) != wanted:
        problems.append(f"quality cells {sorted(set(quality) ^ wanted)} differ from the registry")
    for name, cell in quality.items():
        known = {t["trial"] for t in archived.get("ablation", {}).get(name, {}).get("trials", [])}
        if not cell["trials"] or not {t["trial"] for t in cell["trials"]} <= known:
            problems.append(f"quality/{name}: trials do not match the run records")
    return problems


def _overhead_matrix(archive: dict[str, Any], registry: dict[str, Any],
                     archived: dict[str, dict[str, dict]]) -> list[str]:
    # Every trial of a run carries its overhead record, measured or null, so
    # the export can apply the run records' filter to it.
    problems = []
    wanted = {c["cell"]: c for c in reg.cells(registry, figure="overhead")}
    overhead = {c["cell"]: c for c in archive["overhead"]["cells"]}
    if set(overhead) != set(wanted):
        problems.append(f"overhead cells {sorted(set(overhead) ^ set(wanted))} "
                        "differ from the registry")
    for name, cell in overhead.items():
        board = wanted[name]["board"] if name in wanted else None
        runs = {t["trial"] for t in archived.get(board, {}).get(name, {}).get("trials", [])}
        if [t["trial"] for t in cell["trials"]] != sorted(runs):
            problems.append(f"overhead/{name}: trials do not match the run records")
    return problems


def _occupancy_matrix(archive: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    problems = []
    wanted = {c["cell"]: c for c in reg.cells(registry, figure="occupancy-intervals-1col")}
    configs = [block for scenario in archive["occupancy"]["scenarios"].values()
               for block in scenario["configs"].values()]
    if {block["cell"] for block in configs} != set(wanted):
        problems.append("occupancy cells differ from the registry")
    intervals = archive["occupancy"]["metadata"].get("intervals", [])
    for block in configs:
        declared = wanted.get(block["cell"])
        if declared and len(block["trials"]) != declared["trials"]:
            problems.append(f"occupancy/{block['cell']}: {len(block['trials'])} trials")
        for trial in block["trials"]:
            if [i["interval"] for i in trial["intervals"]] != intervals:
                problems.append(f"occupancy/{block['cell']} trial {trial['trial']}: intervals")
    return problems


def _dataplane_matrix(archive: dict[str, Any], registry: dict[str, Any]) -> list[str]:
    problems = []
    declared_f = {(c["experiment"], c["method"], c["profile"]): c
                  for c in reg.cells(registry, board="F")}
    kept: dict[tuple, int] = {key: 0 for key in declared_f}
    for row in archive["dataplane"]["rounds"]:
        key = (row["experiment"], row["method"], row["profile"])
        if key not in declared_f:
            problems.append(f"round {row['run_id']}/{row['round_dir']} matches no registry cell")
        elif row["kept"]:
            kept[key] += 1
    for key, count in kept.items():
        if count != declared_f[key]["trials"]:
            problems.append(f"F/{declared_f[key]['cell']}: {count} kept rounds, "
                            f"declared {declared_f[key]['trials']}")
    return problems


def check_identity(archive: dict[str, Any], registry: dict[str, Any]) -> list[Check]:
    index = reg.cell_index(registry)
    problems = []
    for board, cell in _run_groups(archive):
        declared = index.get((board, cell["cell"]))
        if declared is None:
            continue  # reported by complete_matrix
        wrong = _mismatched_params(declared["params"], cell["recorded"])
        if wrong:
            problems.append(f"{board}/{cell['cell']}: {wrong}")
    declared_f = {(c["experiment"], c["method"], c["profile"]): c
                  for c in reg.cells(registry, board="F")}
    for row in archive.get("dataplane", {}).get("rounds", []):
        declared = declared_f.get((row["experiment"], row["method"], row["profile"]))
        if declared is None:
            continue
        wrong = _mismatched_params(declared["params"], row["recorded"],
                                   ignore=("dataplane_profile",))
        if wrong:
            problems.append(f"round {row['run_id']}/{row['round_dir']}: {wrong}")
    return [Check("config_identity", FATAL, not problems,
                  "recorded parameters equal the declared ones" if not problems
                  else "; ".join(problems[:3]))]


def check_values(archive: dict[str, Any]) -> list[Check]:
    finite = all(_finite(payload) for payload in archive.values())
    problems = []
    for board, cell in _run_groups(archive):
        for trial in cell["trials"]:
            where = f"{board}/{cell['cell']} trial {trial['trial']}"
            if not trial["is_timeout"] and trial["scheduled_pods"] != trial["expected_pods"]:
                problems.append(f"{where}: scheduled != expected without a timeout")
            acf, bind = trial["acf_count"], trial["bind_conflict_count"]
            if acf is not None and bind is not None and bind < acf:
                problems.append(f"{where}: fewer candidate conflicts than escalations")
            rate = trial["acf_rate"]
            if rate is not None and not 0.0 <= float(rate) <= 1.0:
                problems.append(f"{where}: ACF rate outside [0, 1]")
            throughput = trial["throughput_pods_per_s"]
            if throughput is None or float(throughput) <= 0.0:
                problems.append(f"{where}: no positive throughput")
    for cell in _cells(archive, "overhead"):
        for trial in cell["trials"]:
            negative = [field for field in schema.OVERHEAD_TRIAL_FIELDS[1:]
                        if trial.get(field) is not None and float(trial[field]) < 0.0]
            if negative:
                problems.append(f"overhead/{cell['cell']} trial {trial['trial']}: "
                                f"negative {negative}")
    return [
        Check("finite", FATAL, finite, "every number is finite" if finite
              else "the archive holds a NaN or an infinity"),
        Check("accounting", FATAL, not problems,
              "completion, conflict ordering and value ranges hold" if not problems
              else "; ".join(problems[:5])),
    ]


def check_registry(registry: dict[str, Any]) -> list[Check]:
    wrong = [c["cell"] for c in registry["cells"]
             if c["params"].get("sync_pattern") == "glob"
             and c["params"].get("num_partitions") != 1]
    return [Check("glob_single_partition", FATAL, not wrong,
                  "every globSync cell declares one partition" if not wrong
                  else f"cells declaring more: {wrong}")]


# ---------------------------------------------------------------------------
#  Advisory
# ---------------------------------------------------------------------------

def advise_recorded_partitions(archive: dict[str, Any]) -> Check:
    cells = sorted({f"{board}/{cell['cell']}" for board, cell in _run_groups(archive)
                    if cell["recorded"].get("sync_pattern") == "glob"
                    and float(cell["recorded"].get("num_partitions", 1)) != 1.0})
    detail = ("no globSync run records more than one partition" if not cells else
              f"{len(cells)} globSync cells record more partitions than run "
              f"({', '.join(cells)}); the dispatcher forces one partition under "
              "globSync (para-scheduler/pkg/dispatcher/partition.go), so one ran")
    return Check("recorded_glob_partitions", ADVISORY, True, detail)


def advise_baseline_convention(archive: dict[str, Any]) -> Check:
    gaps = []
    for cell in _cells(archive, "godel"):
        kept = filter_cell([dict(t, experiment=cell["cell"]) for t in cell["trials"]])[0]
        cl2 = median_of(kept, "throughput_pods_per_s")
        collector = median_of(kept, "collector_throughput_pods_per_s")
        if cl2 and collector:
            gaps.append((cl2 - collector) / collector * 100.0)
    detail = (f"baseline throughput from the CL2 log differs from the collector's by "
              f"{min(gaps):+.2f}% to {max(gaps):+.2f}% across {len(gaps)} cells "
              "(the camera-ready figures used the collector's)" if gaps
              else "no baseline cells")
    return Check("baseline_convention_gap", ADVISORY, True, detail)


def advise_cross_board(archive: dict[str, Any], registry: dict[str, Any]) -> Check:
    medians: dict[tuple, list[tuple[str, float, float]]] = {}
    index = reg.cell_index(registry)
    for board, cell in _run_groups(archive):
        if board == "godel":
            continue
        declared = index.get((board, cell["cell"]))
        if declared is None:
            continue
        kept = filter_cell([dict(t, experiment=cell["cell"]) for t in cell["trials"]])[0]
        key = tuple(sorted(reg.normalize(declared["params"]).items()))
        medians.setdefault(key, []).append(
            (f"{board}/{cell['cell']}", median_of(kept, "throughput_pods_per_s"),
             median_of(kept, "acf_rate")))
    pairs = []
    for members in medians.values():
        for i, (a, ta, _) in enumerate(members):
            for b, tb, _ in members[i + 1:]:
                if ta and tb:
                    pairs.append((abs(tb - ta) / ta * 100.0, f"{a} vs {b}",
                                  (tb - ta) / ta * 100.0))
    pairs.sort(reverse=True)
    detail = (f"{len(pairs)} parameter-identical pairs across boards; largest median "
              f"throughput difference {pairs[0][1]} ({pairs[0][2]:+.1f}%). Such cells "
              "agree only within run-to-run variance, so this is reported, not failed"
              if pairs else "no parameter-identical cells across boards")
    return Check("cross_board_consistency", ADVISORY, True, detail)


def advise_reducer(manifest: dict[str, Any], current_reducer_sha256: str | None) -> Check:
    if current_reducer_sha256 is None:
        return Check("reducer_version", ADVISORY, True, "reducer digest not computed")
    same = manifest.get("reducer_sha256") == current_reducer_sha256
    return Check("reducer_version", ADVISORY, same,
                 "the archive was produced by the current reducer code" if same else
                 "the reducer code changed since the archive was produced; whoever "
                 "holds the raw results should run reduce.py --check")


def advise_scope(registry: dict[str, Any], boards: Iterable[str] | None) -> Check:
    """Which paper elements an archive of some boards only can rebuild."""

    held = set(schema.ALL_BOARDS if boards is None else boards)
    if held >= set(schema.ALL_BOARDS):
        return Check("archive_scope", ADVISORY, True, "the archive holds every board")
    needs: dict[str, set[str]] = {}
    for cell in registry["cells"]:
        for figure in cell["figures"]:
            needs.setdefault(figure, set()).add(cell["board"])
    rebuilt = sorted(figure for figure, boards_needed in needs.items() if boards_needed <= held)
    return Check("archive_scope", ADVISORY, False,
                 f"an archive of boards {', '.join(b for b in schema.ALL_BOARDS if b in held)} "
                 f"only, which rebuilds {', '.join(rebuilt) or 'no paper element'}")


def verify(archive_dir: str = schema.ARCHIVE_DIR, registry_path: str = reg.REGISTRY_FILE,
           reducer_sha256: str | None = None,
           boards: Iterable[str] | None = None) -> list[Check]:
    """Every check, on the whole archive or, with `boards`, on an archive
    reduced for those boards only."""

    boards = None if boards is None else list(boards)
    registry = reg.load(registry_path)
    archive = schema.load(archive_dir, boards)
    return (check_manifest(archive_dir, archive["manifest"], registry_path, boards)
            + check_structure(archive)
            + check_keys(archive)
            + check_matrix(archive, registry)
            + check_identity(archive, registry)
            + check_values(archive)
            + check_registry(registry)
            + [advise_scope(registry, boards),
               advise_recorded_partitions(archive),
               advise_baseline_convention(archive),
               advise_cross_board(archive, registry),
               advise_reducer(archive["manifest"], reducer_sha256)])
