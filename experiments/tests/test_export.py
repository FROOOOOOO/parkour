"""The export reproduces the locked figure data, and the registry and the
documents rendered from it are current."""

import argparse
import importlib.util
import os

import pytest

from common import data, registry, schema

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

#: The body of the paper's overhead table, as published.
PUBLISHED_OVERHEAD = [
    r"Vanilla (event-driven) & 252 & 19.9 & 14.9 (16.4) & 15.8 \\",
    r"ParKour (event-driven) & 247 & 20.0 & 16.2 (16.3) & 16.3 \\",
    r"Vanilla (periodic) & 320 & 19.8 & 17.4 (3.93) & 17.1 \\",
    r"diffSync & 324 & 19.9 & 9.89 (6.00) & 16.6 \\",
    r"ParKour (periodic) & 336 & 20.0 & 23.6 (\textbf{8.85}) & 18.9 \\",
]


def load(name, *parts):
    spec = importlib.util.spec_from_file_location(name, os.path.join(EXPERIMENTS, *parts))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_exporter():
    return load("export_figure_data", "scripts", "export-figure-data.py")


def test_exported_data_matches_lock():
    exporter = load_exporter()
    inputs = exporter.Inputs(schema.ARCHIVE_DIR, registry.REGISTRY_FILE)
    options = argparse.Namespace(throughput_metric="t99", acf_metric="t99")
    lock = exporter.read_lock()
    assert set(lock) == set(exporter.EXPORTERS)
    for figure, (build, _) in exporter.EXPORTERS.items():
        assert data.data_sha256(build(inputs, options)) == lock[figure], figure


def test_partial_archive_exports_the_figures_of_its_boards(cut_archive):
    # An archive of B2 and the baseline rebuilds what those boards feed,
    # exactly, and refuses a figure that needs another board.
    exporter = load_exporter()
    inputs = exporter.Inputs(cut_archive(["B2", "godel"]), registry.REGISTRY_FILE)
    options = argparse.Namespace(throughput_metric="t99", acf_metric="t99")
    lock = exporter.read_lock()
    for figure in ("pareto-all-scales", "occupancy-intervals-1col", "overhead"):
        build, _ = exporter.EXPORTERS[figure]
        assert data.data_sha256(build(inputs, options)) == lock[figure], figure
    for figure, board in (("robustness", "K|P"), ("scalability-schedulers", "B3"),
                          ("dataplane-sensitivity", "F")):
        build, _ = exporter.EXPORTERS[figure]
        with pytest.raises(SystemExit, match=f"needs board ({board})"):
            build(inputs, options)


def test_overhead_table_is_the_published_one():
    exporter = load_exporter()
    table = load("table_overhead", "figures", "table-overhead.py")
    exported = exporter.export_overhead(
        exporter.Inputs(schema.ARCHIVE_DIR, registry.REGISTRY_FILE), argparse.Namespace())
    assert table.table_rows(table.index(exported)) == PUBLISHED_OVERHEAD


def test_registry_is_current():
    with open(registry.REGISTRY_FILE, encoding="utf-8") as handle:
        assert handle.read() == registry.render()


def test_documents_rendered_from_the_registry_are_current():
    assert load("generate_docs", "scripts", "generate-docs.py").main(["--check"]) == 0
