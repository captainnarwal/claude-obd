import json

import pytest

from obd_engine.cli.main import main
from tools.synthetic_log_generator.generate import generate


@pytest.fixture
def ws(tmp_path, monkeypatch):
    """A fresh working folder; the engine reads it through OBD_WORKSPACE."""
    monkeypatch.setenv("OBD_WORKSPACE", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture
def run(ws, capsys):
    def _run(*args):
        code = main([*map(str, args), "--json"])
        out = capsys.readouterr().out.strip()
        return code, json.loads(out.splitlines()[-1])

    return _run


@pytest.fixture(scope="session")
def logs(tmp_path_factory):
    d = tmp_path_factory.mktemp("logs")
    out = {
        "clean": generate(d / "clean.csv", 900, 2.5, "torque", set(), seed=21),
        "faulty": generate(d / "faulty.csv", 1200, 2.5, "torque", {"lean", "overheat", "spike", "dropout"}, seed=11),
        "generic": generate(d / "generic.csv", 600, 10, "generic", {"sentinel", "reset"}, seed=5),
        "generic_lean": generate(d / "generic_lean.csv", 900, 10, "generic", {"lean"}, seed=8),
    }
    return out


@pytest.fixture
def copy_log(ws, logs):
    import shutil

    def _copy(key, name=None):
        dst = ws / (name or logs[key].name)
        shutil.copy(logs[key], dst)
        return dst

    return _copy
