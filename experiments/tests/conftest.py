"""Make `common` importable from the experiment tests, and share their fixtures."""

import os
import shutil
import sys

import pytest

EXPERIMENTS = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if EXPERIMENTS not in sys.path:
    sys.path.insert(0, EXPERIMENTS)

from common import data, schema  # noqa: E402


@pytest.fixture
def cut_archive(tmp_path):
    """Copy the committed archive down to some boards, as `reduce.py --boards`
    writes one: their files, and a manifest that lists only those."""

    def cut(boards):
        target = tmp_path / "partial-archive"
        keep = set(schema.archive_files(boards))
        for name in keep:
            os.makedirs(os.path.dirname(target / name), exist_ok=True)
            shutil.copyfile(os.path.join(schema.ARCHIVE_DIR, name), target / name)
        manifest = schema.read(schema.ARCHIVE_DIR, schema.FILES["manifest"])
        for key in ("files", "inputs"):
            manifest[key] = {name: value for name, value in manifest[key].items() if name in keep}
        data.write_text_atomic(str(target / schema.FILES["manifest"]), schema.render(manifest))
        return str(target)

    return cut
