import sys
from pathlib import Path

import pytest

from obd_engine.security.checks import inside


def test_inside_accepts_children_and_rejects_siblings(tmp_path):
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs2").mkdir()
    assert inside(tmp_path / "logs" / "a.csv", tmp_path / "logs")
    assert not inside(tmp_path / "logs2" / "a.csv", tmp_path / "logs")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows paths are case-insensitive")
def test_inside_ignores_case_on_windows(tmp_path):
    (tmp_path / "Logs").mkdir()
    assert inside(Path(str(tmp_path).swapcase()) / "Logs" / "a.csv", tmp_path / "logs")
