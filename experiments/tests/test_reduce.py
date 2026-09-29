"""reduce.py on synthetic raw results.

Each artifact family is reduced to the record its description implies, and a
whole archive reduced from a small registry passes validation. The cells are
the real registry's, with fewer trials, so the raw configurations the fixtures
write are the declared parameters as the runners record them.
"""

import argparse
import copy
import importlib.util
import json
import os
import statistics
from pathlib import Path

import pytest

from common import data
from common import registry as reg
from common import schema
from common.verify import FATAL, verify
from rawdata import (Collector, Overhead, Round, Trial, godel_record, occupancy_record,
                     overhead_record, recorded_params, round_record, run_record,
                     write_godel_run, write_quality, write_round, write_run,
                     write_windowed_acf)

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DECLARED = reg.cell_index(reg.build())


def load_script(name, filename):
    spec = importlib.util.spec_from_file_location(name, os.path.join(EXPERIMENTS, "scripts", filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


reducer = load_script("reduce_archive", "reduce.py")
process_results = reducer._load_script("process_results", "process-results.py")


def cell(board, name, trials=2):
    declared = copy.deepcopy(DECLARED[(board, name)])
    declared["trials"] = trials
    return declared


def registry_of(*cells):
    return {"schema": reg.SCHEMA, "boards": reg.BOARDS, "cells": list(cells), "inputs": {}}


def public(params):
    return {k: v for k, v in recorded_params(params).items() if k in schema.PUBLIC_PARAMS}


def test_scheduler_trials(tmp_path):
    b1 = cell("B1", "B1-1000n-E1")
    # Trial 2 timed out in both phases; its latency pods must not count.
    trials = [Trial(1, 93.123456, acf=3, bind_conflicts=40),
              Trial(2, 604.5, acf=1, bind_conflicts=2, not_scheduled=145,
                    latency_timeouts=3)]
    write_run(str(tmp_path), "B1", b1, trials)

    payload, inputs = reducer.reduce_board(process_results, registry_of(b1), str(tmp_path), "B1")
    (archived,) = payload["cells"]
    assert archived["trials"] == [run_record(trial, b1["params"]) for trial in trials]
    assert archived["recorded"] == public(b1["params"])
    assert archived["recorded_trials"] == 2
    assert set(inputs) == {"B1-1000n-E1"}


def test_godel_trials(tmp_path):
    godel = cell("godel", "B2-2000n-E2-godel")
    trials = [(Trial(1, 11.2, acf=4), Collector(120, 300, 0.0566, 180.0)),
              (Trial(2, 10.9), Collector(118, 290, 0.0557, 183.5))]
    write_godel_run(str(tmp_path), "godel-new", godel, trials)

    payload, _ = reducer.reduce_godel(process_results, registry_of(godel), str(tmp_path))
    (archived,) = payload["cells"]
    assert archived["trials"] == [godel_record(t, c, godel["params"]) for t, c in trials]
    assert archived["recorded_trials"] == 2


def test_quality_histograms(tmp_path):
    ablation = cell("ablation", "AbP3-MP")
    run_dir = write_run(str(tmp_path), "ablation", ablation,
                        [Trial(1, 50.0, acf=9, bind_conflicts=70), Trial(2, 51.0)])
    write_quality(os.path.join(run_dir, "trial-1"), 87.5, (9000, 9600, 9900), 10000)

    payload, _ = reducer.reduce_quality(registry_of(ablation), str(tmp_path))
    # Trial 2 recorded no histogram, so it has no quality record.
    assert payload["cells"] == [{"cell": "AbP3-MP", "trials": [{
        "trial": 1, "selected_node_score_mean": 87.5, "candidate_rank_count": 10000,
        "candidate_rank_cumulative": [9000, 9600, 9900]}]}]


def test_occupancy_intervals(tmp_path):
    b2 = cell("B2", "B2-20000n-E2")
    trials = [Trial(1, 40.0, acf=500, bind_conflicts=900), Trial(2, 44.4, acf=620)]
    write_run(str(tmp_path), "B2", b2, trials)

    payload, _ = reducer.reduce_occupancy(registry_of(b2), str(tmp_path))
    assert list(payload["scenarios"]) == ["b2-20k-event-driven"]
    (block,) = payload["scenarios"]["b2-20k-event-driven"]["configs"].values()
    assert block["cell"] == "B2-20000n-E2"
    for archived, trial in zip(block["trials"], trials, strict=True):
        expected = occupancy_record(trial, b2["params"])
        assert archived["trial"] == expected["trial"]
        for got, want in zip(archived["intervals"], expected["intervals"], strict=True):
            assert got["interval"] == want["interval"]
            for metric in schema.OCCUPANCY_METRICS:
                assert got[metric] == pytest.approx(want[metric], rel=1e-9)


def test_overhead_trials(tmp_path):
    b2 = cell("B2", "B2-10000n-P4")
    # Trial 2's collector recorded no dispatcher metric, so its record holds null.
    trials = [Trial(1, 48.25, acf=30, bind_conflicts=400,
                    overhead=Overhead(algo_p99=0.3361, e2e_p99=20.041,
                                      scheduler_cpu=(2.5, 2.25, 2.75), binder_cpu=1.744)),
              Trial(2, 47.5, acf=28, bind_conflicts=390, overhead=Overhead(dispatcher_cpu=None))]
    run_dir = write_run(str(tmp_path), "B2", b2, trials)

    payload, inputs = reducer.reduce_overhead(process_results, registry_of(b2), str(tmp_path))
    assert payload == {"schema": schema.SCHEMA, "kind": "overhead", "cells": [
        {"cell": "B2-10000n-P4", "trials": [overhead_record(trial) for trial in trials]}]}
    assert payload["cells"][0]["trials"][1]["dispatcher_cpu"] is None

    # The digest covers the files the overhead fields come from, and only those.
    metrics = Path(run_dir, "trial-1", "metrics-saturation")
    digest = inputs["B2-10000n-P4"]
    (metrics / "snap_bind_success.json").write_text("{}", encoding="utf-8")
    assert reducer.reduce_overhead(process_results, registry_of(b2), str(tmp_path))[1] == inputs
    (metrics / "binder_cpu.json").write_text("{}", encoding="utf-8")
    assert reducer.reduce_overhead(
        process_results, registry_of(b2), str(tmp_path))[1]["B2-10000n-P4"] != digest


def test_dataplane_rounds(tmp_path):
    f = cell("F", "F1-P-P4-Dreal")
    rounds = [Round("F1-P-anchored", f"round-0{n}-trial-0{n}-P4-Dreal", f, order=n, trial=n,
                    kept=n != 2, throughput=200.0 + n, acf_rate=0.1 * n, t99_acf_rate=0.05 * n,
                    censored=n == 3)
              for n in (1, 2, 3)]
    for item in rounds:
        write_round(str(tmp_path), item)
    write_windowed_acf(str(tmp_path), rounds)

    payload, inputs = reducer.reduce_dataplane(str(tmp_path))
    rows = payload["rounds"]
    assert [{k: row[k] for k in schema.ROUND_FIELDS} for row in rows] == [
        round_record(item) for item in rounds]
    assert all(row["recorded"] == public(
        {k: v for k, v in f["params"].items() if k != "dataplane_profile"}) for row in rows)
    assert set(inputs) == {f"F1-P-anchored/{item.round_dir}" for item in rounds} | {
        "acf-windowed.csv"}


def test_reduced_archive_passes_verify(tmp_path):
    results, anchored = str(tmp_path / "results"), str(tmp_path / "anchored")
    runs = {
        "B1": cell("B1", "B1-1000n-E1"), "B2": cell("B2", "B2-20000n-E2"),
        "B3": cell("B3", "B3-N2-P4"), "K": cell("K", "S-P-K2-glob"),
        "P": cell("P", "S-P-P05-same"), "ablation": cell("ablation", "AbP3-MP"),
    }
    for board, declared in runs.items():
        trials = [Trial(1, 30.25, acf=5, bind_conflicts=50),
                  Trial(2, 31.5, acf=6, bind_conflicts=55,
                        not_scheduled=100 if board == "B1" else 0,
                        latency_timeouts=2 if board == "B1" else 0)]
        run_dir = write_run(results, reg.BOARDS[board]["directory"], declared, trials)
        if board == "ablation":
            for trial in trials:
                write_quality(os.path.join(run_dir, f"trial-{trial.number}"),
                              90.0, (9500, 9800, 9900), 10000)
    overhead = cell("B2", "B2-10000n-E3")
    overhead_trials = [Trial(1, 40.5, acf=2, bind_conflicts=30,
                             overhead=Overhead(scheduler_cpu=(1.5, 1.75), binder_cpu=0.4)),
                       Trial(2, 41.0, acf=3, bind_conflicts=35,
                             overhead=Overhead(scheduler_cpu=(1.25, 1.5), binder_cpu=0.5))]
    write_run(results, reg.BOARDS["B2"]["directory"], overhead, overhead_trials)
    godel = cell("godel", "B2-2000n-E2-godel")
    write_godel_run(results, reg.BOARDS["godel"]["directory"], godel,
                    [(Trial(1, 11.2), Collector(120, 300, 0.0566, 180.0)),
                     (Trial(2, 10.9), Collector(118, 290, 0.0557, 183.5))])
    f = cell("F", "F1-P-P4-Dreal")
    rounds = [Round("F1-P-anchored", f"round-0{n}-trial-0{n}-P4-Dreal", f, order=n, trial=n,
                    kept=n != 2, throughput=200.0, acf_rate=0.1, t99_acf_rate=0.05)
              for n in (1, 2, 3)]
    for item in rounds:
        write_round(anchored, item)
    write_windowed_acf(anchored, rounds)

    registry_path = str(tmp_path / "registry.json")
    data.write_text_atomic(registry_path, data.canonical_json(
        registry_of(*runs.values(), overhead, godel, f)) + "\n")
    archive = str(tmp_path / "archive")
    for name, text in reducer.build(results, anchored, registry_path).items():
        data.write_text_atomic(os.path.join(archive, name), text)

    checks = verify(archive, registry_path, reducer_sha256=schema.reducer_sha256())
    assert [c for c in checks if c.severity == FATAL and not c.passed] == []
    assert next(c for c in checks if c.name == "reducer_version").passed

    # The overhead export joins the records to the kept runs: with two trials
    # the filter keeps both, so each field is the median of the two records.
    exporter = load_script("export_figure_data", "export-figure-data.py")
    table = exporter.export_overhead(exporter.Inputs(archive, registry_path),
                                     argparse.Namespace())
    (row,) = table["rows"]
    records = [overhead_record(trial) for trial in overhead_trials]
    runs_b2 = [run_record(trial, overhead["params"]) for trial in overhead_trials]
    for field in schema.OVERHEAD_TRIAL_FIELDS[1:]:
        assert row[field] == statistics.median(record[field] for record in records)
    throughput = statistics.median(r["throughput_pods_per_s"] for r in runs_b2)
    assert row["pods_per_core_s"] == throughput / row["scheduler_cpu_total"]
    assert (row["arm"], row["num_nodes"], row["n"]) == ("event-parkour-diff", 10000, 2)
    assert json.loads(json.dumps(table)) == table  # JSON-serialisable as exported


def test_reduce_boards_writes_an_archive_of_their_figures(tmp_path):
    # A reader re-ran two boards: the reduction needs no other board's results
    # and no module-F package, and the archive passes verification for them.
    results = str(tmp_path / "results")
    b2, overhead = cell("B2", "B2-20000n-E2"), cell("B2", "B2-10000n-E3")
    for declared in (b2, overhead):
        write_run(results, reg.BOARDS["B2"]["directory"], declared,
                  [Trial(1, 40.5, acf=2, bind_conflicts=30), Trial(2, 41.0, acf=3, bind_conflicts=35)])
    godel = cell("godel", "B2-2000n-E2-godel")
    write_godel_run(results, reg.BOARDS["godel"]["directory"], godel,
                    [(Trial(1, 11.2), Collector(120, 300, 0.0566, 180.0)),
                     (Trial(2, 10.9), Collector(118, 290, 0.0557, 183.5))])
    registry_path = str(tmp_path / "registry.json")
    data.write_text_atomic(registry_path, data.canonical_json(registry_of(
        b2, overhead, godel, cell("K", "S-P-K2-glob"), cell("F", "F1-P-P4-Dreal"))) + "\n")

    texts = reducer.build(results, None, registry_path, boards=["B2", "godel"])
    assert set(texts) == {"boards/B2.json", "godel.json", "occupancy.json", "overhead.json",
                          "manifest.json"}
    archive = str(tmp_path / "archive")
    for name, text in texts.items():
        data.write_text_atomic(os.path.join(archive, name), text)
    checks = verify(archive, registry_path, reducer_sha256=schema.reducer_sha256(),
                    boards=["B2", "godel"])
    assert [c for c in checks if c.severity == FATAL and not c.passed] == []


@pytest.mark.parametrize("argv, message", [
    (["--results", "r", "--boards", "B2"], "--out"),
    (["--results", "r", "--boards", "B2", "--out", "a", "--check"], "--check"),
    (["--results", "r"], "--anchored"),
    (["--boards", "F", "--out", "a"], "--anchored"),
    (["--anchored", "p", "--boards", "F", "B2", "--out", "a"], "--results"),
])
def test_reduce_rejects_what_it_cannot_do(argv, message, capsys):
    with pytest.raises(SystemExit):
        reducer.main(argv)
    assert message in capsys.readouterr().err


def test_reducer_digest_ignores_line_endings(tmp_path, monkeypatch):
    # The repository stores the reducer with LF; a Windows checkout may hold CRLF.
    current = schema.reducer_sha256()
    for ending in (b"\n", b"\r\n"):
        root = tmp_path / ending.hex()
        for relative in schema.REDUCER_SOURCES:
            source = Path(EXPERIMENTS, relative).read_bytes().replace(b"\r\n", b"\n")
            Path(root, relative).parent.mkdir(parents=True, exist_ok=True)
            Path(root, relative).write_bytes(source.replace(b"\n", ending))
        monkeypatch.setattr(schema, "_EXPERIMENTS", str(root))
        assert schema.reducer_sha256() == current
