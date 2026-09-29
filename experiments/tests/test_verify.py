"""Archive validation: the committed archive passes, and each kind of damage is
caught by the check meant to catch it.

Each negative test edits a copy of the archive and then, unless the test is
about the manifest itself, re-stamps the manifest. That models an edit made
deliberately and recorded, so the check under test is the one that must fire.
"""

import hashlib
import importlib.util
import json
import os
import shutil

import pytest

from common import registry as reg
from common import schema
from common.verify import FATAL, verify

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def failed(checks):
    return {check.name for check in checks if check.severity == FATAL and not check.passed}


@pytest.fixture
def archive(tmp_path):
    target = tmp_path / "archive"
    shutil.copytree(schema.ARCHIVE_DIR, target)
    return str(target)


def edit(archive_dir, relative, mutate, *, restamp=True, allow_nan=False):
    path = os.path.join(archive_dir, relative)
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    mutate(payload)
    text = json.dumps(payload, indent=2, sort_keys=True, allow_nan=allow_nan) + "\n"
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    if restamp:
        manifest_path = os.path.join(archive_dir, schema.FILES["manifest"])
        with open(manifest_path, encoding="utf-8") as handle:
            manifest = json.load(handle)
        manifest["files"][relative] = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(schema.render(manifest))


def test_committed_archive_passes():
    assert failed(verify()) == set()


def test_unrecorded_edit_fails_manifest(archive):
    edit(archive, "boards/B2.json",
         lambda p: p["cells"][0]["trials"][0].update(throughput_pods_per_s=1.0),
         restamp=False)
    assert "manifest_files" in failed(verify(archive))


def test_removed_trial_fails_complete_matrix(archive):
    edit(archive, "boards/B2.json", lambda p: p["cells"][0]["trials"].pop())
    assert failed(verify(archive)) == {"complete_matrix"}


def test_duplicated_cell_fails_unique_keys(archive):
    edit(archive, "boards/K.json", lambda p: p["cells"].append(p["cells"][0]))
    assert failed(verify(archive)) == {"unique_keys"}


def test_changed_parameter_fails_config_identity(archive):
    edit(archive, "boards/B3.json", lambda p: p["cells"][0]["recorded"].update(num_backup=3))
    assert failed(verify(archive)) == {"config_identity"}


def test_nan_fails_finite(archive):
    edit(archive, "godel.json",
         lambda p: p["cells"][0]["trials"][0].update(acf_rate=float("nan")),
         allow_nan=True)
    assert "finite" in failed(verify(archive))


def test_more_escalations_than_conflicts_fails_accounting(archive):
    def mutate(payload):
        trial = payload["cells"][0]["trials"][0]
        trial["acf_count"] = (trial["bind_conflict_count"] or 0) + 1

    edit(archive, "boards/B2.json", mutate)
    assert failed(verify(archive)) == {"accounting"}


def test_removed_overhead_trial_fails_complete_matrix(archive):
    edit(archive, "overhead.json", lambda p: p["cells"][0]["trials"].pop())
    assert failed(verify(archive)) == {"complete_matrix"}


def test_negative_cpu_fails_accounting(archive):
    edit(archive, "overhead.json",
         lambda p: p["cells"][0]["trials"][0].update(scheduler_cpu_total=-1.0))
    assert failed(verify(archive)) == {"accounting"}


def test_dropped_round_fails_complete_matrix(archive):
    def mutate(payload):
        kept = next(i for i, row in enumerate(payload["rounds"]) if row["kept"])
        payload["rounds"].pop(kept)

    edit(archive, "dataplane.json", mutate)
    assert failed(verify(archive)) == {"complete_matrix"}


# ---------------------------------------------------------------------------
#  An archive of some boards only (reduce.py --boards)
# ---------------------------------------------------------------------------

def test_record_groups_name_the_boards_the_registry_gives_them():
    registry = reg.load()
    assert set(schema.ALL_BOARDS) == set(registry["boards"])
    for group, figure in (("quality", "ablation-quality-bc"),
                          ("occupancy", "occupancy-intervals-1col"),
                          ("overhead", "overhead"), ("dataplane", "dataplane-sensitivity")):
        assert set(schema.GROUP_BOARDS[group]) == {
            cell["board"] for cell in reg.cells(registry, figure=figure)}, group
    assert schema.held_boards(schema.archive_files()) == schema.ALL_BOARDS
    for board in schema.ALL_BOARDS:
        assert schema.held_boards(schema.archive_files([board])) == (board,)


def test_partial_archive_is_verified_against_its_boards(cut_archive):
    archive = cut_archive(["B2", "godel"])
    checks = verify(archive, boards=["B2", "godel"])
    assert failed(checks) == set()
    scope = next(check for check in checks if check.name == "archive_scope")
    assert not scope.passed and "boards B2, godel only" in scope.detail
    assert "pareto-all-scales" in scope.detail and "robustness" not in scope.detail

    # Damage is still caught inside the boards it holds.
    edit(archive, "boards/B2.json", lambda p: p["cells"][0]["trials"].pop())
    assert failed(verify(archive, boards=["B2", "godel"])) == {"complete_matrix"}


def test_partial_archive_must_be_named_for_what_it_holds(cut_archive, capsys):
    archive = cut_archive(["B2", "godel"])
    with pytest.raises(SystemExit, match="missing archive file"):
        verify(archive)
    assert "manifest_files" in failed(verify(archive, boards=["B2"]))

    spec = importlib.util.spec_from_file_location(
        "verify_results", os.path.join(EXPERIMENTS, "scripts", "verify-results.py"))
    script = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(script)
    assert script.main(["--archive", archive]) == 1
    assert "verify it with --boards B2 godel" in capsys.readouterr().err
    assert script.main(["--archive", archive, "--boards", "B2", "godel"]) == 0
