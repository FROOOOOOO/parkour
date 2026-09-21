"""Common cache validation and manifest generation for paper figures."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from pathlib import Path
from typing import Any

from config import ModelConstants, TRIAL_SEEDS

from common.matrix import CACHE_SCHEMA, FigureCase, registry_hash


def sha256_file(path: Path) -> str:
    """Return a file's SHA-256 digest."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _finite_tree(value: Any) -> bool:
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return True
    if isinstance(value, (int, float)):
        return math.isfinite(float(value))
    if isinstance(value, list):
        return all(_finite_tree(item) for item in value)
    if isinstance(value, dict):
        return all(_finite_tree(item) for item in value.values())
    return False


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{path.name}.", dir=path.parent
    )
    try:
        # newline="\n" is required, not cosmetic: these files are hash-locked by
        # sha256_file(), and Windows text mode would translate the trailing "\n"
        # into "\r\n". Git then normalises it back to LF on commit (.gitattributes
        # sets *.json text eol=lf), so the checked-out bytes no longer match the
        # manifest and every cache fails its integrity check on the next machine.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(
                payload,
                stream,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            stream.write("\n")
        os.replace(temporary, path)
        os.chmod(path, 0o644)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def verify_matrix(
    *,
    raw_path: Path,
    verified_path: Path,
    manifest_path: Path,
    report_path: Path,
    matrix_kind: str,
    cases: list[FigureCase],
    constants: ModelConstants,
) -> dict:
    """Validate one figure cache and emit a hash-locked verified copy."""

    raw = json.loads(raw_path.read_text(encoding="utf-8"))
    expected_meta = {
        "matrix_kind": matrix_kind,
        "fixed_constants": constants.to_dict(),
        "seeds": list(TRIAL_SEEDS),
        "case_count": len(cases),
        "run_count": len(cases) * len(TRIAL_SEEDS),
        "registry_sha256": registry_hash(cases),
    }
    checks: list[dict[str, Any]] = []

    def record(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    record(
        "schema",
        raw.get("schema_version") == CACHE_SCHEMA,
        f"expected {CACHE_SCHEMA}",
    )
    meta = raw.get("meta", {})
    for key, value in expected_meta.items():
        record(f"meta.{key}", meta.get(key) == value, f"expected {value!r}")

    expected_cases = {case.case_id: case for case in cases}
    expected_keys = {
        (case.case_id, seed)
        for case in cases
        for seed in TRIAL_SEEDS
    }
    observed_keys: set[tuple[str, int]] = set()
    duplicate = False
    configs_match = True
    accounting = True
    completion = True
    finite = True
    for run in raw.get("runs", []):
        key = (run["case_id"], int(run["seed"]))
        duplicate |= key in observed_keys
        observed_keys.add(key)
        case = expected_cases.get(run["case_id"])
        if case is None:
            configs_match = False
        else:
            config = dict(run["config"])
            config.pop("trial_seed", None)
            configs_match &= config == case.config.case_dict()
            configs_match &= int(run["num_schedulers"]) == case.num_schedulers

        summary = run["summary"]
        accounting &= (
            summary["attempts"]
            == summary["successes"]
            + summary["local_failures"]
            + summary["binder_failures"]
        )
        accounting &= (
            summary["candidate_checks"]
            == summary["successes"] + summary["candidate_rejections"]
        )
        completion &= summary["successes"] == constants.num_nodes
        completion &= 0 < summary["elapsed_cycles"] <= constants.max_cycles
        finite &= _finite_tree(run)

    record("unique_run_keys", not duplicate, "case_id/seed keys are unique")
    record(
        "complete_matrix",
        observed_keys == expected_keys,
        f"expected {len(expected_keys)} runs, observed {len(observed_keys)}",
    )
    record("config_identity", configs_match, "configs match the figure registry")
    record("accounting", accounting, "attempt and candidate counters balance")
    record("completion", completion, "every run fills the cluster within its guard")
    record("finite_json", finite, "all numeric values are finite")

    passed = all(check["passed"] for check in checks)
    if not passed:
        failed = [check["name"] for check in checks if not check["passed"]]
        raise ValueError(f"figure-cache verification failed: {failed}")

    verified = {
        **raw,
        "validation": {
            "passed": True,
            "checks": checks,
            "raw_sha256": sha256_file(raw_path),
        },
    }
    _write_json(verified_path, verified)
    verified_sha = sha256_file(verified_path)
    manifest = {
        "matrix_kind": matrix_kind,
        "verified_cache": verified_path.name,
        "verified_sha256": verified_sha,
        "raw_cache": raw_path.name,
        "raw_sha256": sha256_file(raw_path),
        "registry_sha256": expected_meta["registry_sha256"],
        "checks": len(checks),
    }
    _write_json(manifest_path, manifest)

    lines = [
        f"# {matrix_kind} verification",
        "",
        f"- Raw SHA-256: `{manifest['raw_sha256']}`",
        f"- Verified SHA-256: `{verified_sha}`",
        f"- Matrix: {len(cases)} cases / {len(expected_keys)} runs",
        f"- Result: {len(checks)}/{len(checks)} PASS",
        "",
        "| Check | Result | Detail |",
        "|---|---|---|",
    ]
    for check in checks:
        lines.append(
            f"| `{check['name']}` | PASS | {check['detail']} |"
        )
    lines.append("")
    with report_path.open("w", encoding="utf-8", newline="\n") as stream:
        stream.write("\n".join(lines))
    return manifest


def load_verified(cache_path: Path, manifest_path: Path) -> dict:
    """Load a verified cache only when its sidecar hash still matches."""

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    actual = sha256_file(cache_path)
    if actual != manifest.get("verified_sha256"):
        raise ValueError(
            f"verified cache SHA mismatch: expected "
            f"{manifest.get('verified_sha256')}, got {actual}"
        )
    data = json.loads(cache_path.read_text(encoding="utf-8"))
    validation = data.get("validation", {})
    if validation.get("passed") is not True:
        raise ValueError("cache validation flag is not true")
    if any(check.get("passed") is not True for check in validation.get("checks", [])):
        raise ValueError("cache contains a failed validation check")
    return data
