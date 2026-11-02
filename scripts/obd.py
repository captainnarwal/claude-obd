#!/usr/bin/env python3
"""Cross-platform launcher for the claude-obd engine (Linux, macOS, Windows; stdlib only, no bash needed).

  obd.py <command> [args] --json    run an engine command from the isolated venv
  obd.py mcp [--workspace DIR]      run the MCP server (stdout is the protocol)
  obd.py setup [--install-uv]       build the venv in ${CLAUDE_PLUGIN_DATA}/venv, then self-test
  obd.py doctor                     check the environment (always exits 0; read the JSON)

Exit code 4 means environment error (engine not set up).
"""
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

SELF = Path(__file__).resolve()
ROOT = Path(os.environ.get("CLAUDE_PLUGIN_ROOT") or SELF.parent.parent)


def _default_data() -> Path:
    """Skill Bash calls don't get CLAUDE_PLUGIN_DATA, but .mcp.json does. Mirror Claude Code's layout
    (plugins/cache/<marketplace>/<plugin>/<version> -> plugins/data/<plugin>-<marketplace>) so both use one venv."""
    parts = ROOT.parts
    if len(parts) > 4 and parts[-4] == "cache" and parts[-5] == "plugins":
        return Path(*parts[:-4]) / "data" / f"{parts[-2]}-{parts[-3]}"
    return ROOT / ".obd-plugin-data"


DATA = Path(os.environ.get("CLAUDE_PLUGIN_DATA") or _default_data())
VENV = DATA / "venv"
IS_WIN = sys.platform == "win32"


def venv_python() -> Path:
    return VENV / ("Scripts/python.exe" if IS_WIN else "bin/python")


def read_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return {}


def plugin_version() -> str:
    return str(read_json(ROOT / ".claude-plugin" / "plugin.json").get("version", ""))


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def fail(code: str, message: str, hint: str) -> "None":
    emit({"status": "error", "summary": message, "error": {"code": code, "message": message, "hint": hint}})
    sys.exit(4)


def setup_required(reason: str, mcp: bool) -> "None":
    if mcp:
        sys.stderr.write(f"obd-analyst MCP server: engine not set up ({reason}). "
                         "Run /obd setup in Claude Code, or `python scripts/obd.py setup`.\n")
        sys.exit(4)
    emit({"status": "error", "summary": "The engine is not set up. Run /obd setup.",
          "error": {"code": "SETUP_REQUIRED", "message": reason,
                    "hint": "Run /obd setup (takes 1-3 minutes, needs internet once)."}})
    sys.exit(4)


def engine_env() -> dict:
    env = dict(os.environ)
    env.setdefault("OBD_REFERENCE_DIR", str(ROOT / "reference"))
    env.setdefault("OBD_SAMPLE", str(ROOT / "samples" / "sample-drive.csv"))
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    return env


def resolve_python(mcp: bool) -> str:
    override = os.environ.get("OBD_PYTHON")
    if override:  # developer / test override
        return override
    py = venv_python()
    if not py.exists():
        setup_required("No engine environment found.", mcp)
    setup_version = read_json(DATA / "setup.json").get("engine_version")
    if not setup_version:
        setup_required("setup.json missing.", mcp)
    pv = plugin_version()
    if pv and pv != setup_version:
        setup_required(f"Engine {setup_version} does not match plugin {pv}.", mcp)
    return str(py)


def run_engine(args: list, mcp: bool = False) -> "None":
    cmd = [resolve_python(mcp), "-m", "obd_engine.mcp" if mcp else "obd_engine", *args]
    env = engine_env()
    if IS_WIN:  # os.execv on Windows detaches from the parent and breaks stdio; wait and propagate the code
        sys.exit(subprocess.call(cmd, env=env))
    os.execve(cmd[0], cmd, env)


# ---------------------------------------------------------------- doctor
def run_quiet(cmd: list, env: dict) -> int:
    try:
        return subprocess.run(cmd, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    except OSError:
        return 1


def doctor() -> None:
    checks = []

    def add(name, ok, detail, fix=""):
        checks.append({"check": name, "ok": ok, "detail": detail, "fix": fix})

    uv = find_uv()
    if uv:
        try:
            ver = subprocess.run([uv, "--version"], capture_output=True, text=True).stdout.strip()
        except OSError:
            ver = uv
        add("uv", True, ver)
    else:
        add("uv", False, "uv not found (the MCP server and /obd setup need it)",
            "Run /obd setup (installs uv after asking), or install from https://docs.astral.sh/uv/")
    if venv_python().exists():
        add("venv", True, "engine environment present")
        pv, sv = plugin_version(), read_json(DATA / "setup.json").get("engine_version")
        if pv == sv:
            add("engine_version", True, f"engine {sv} matches plugin")
        else:
            add("engine_version", False, f"engine {sv or 'none'} vs plugin {pv}", "Run /obd setup to refresh the environment")
        env = {k: v for k, v in os.environ.items() if k != "OBD_PYTHON"}
        if run_quiet([sys.executable, str(SELF), "selftest", "--json"], env) == 0:
            add("selftest", True, "pipeline passed on the bundled sample")
        else:
            add("selftest", False, "self-test failed",
                "Run /obd setup; if it persists run `python scripts/obd.py selftest` and read the stage detail")
        if run_quiet([sys.executable, str(SELF), "list", "--json"], env) == 0:
            add("workspace", True, "current folder readable")
        else:
            add("workspace", False, "could not read .obd/ in the current folder", "Run /obd clean in this folder, then re-analyze")
    else:
        add("venv", False, "no engine environment found", "Run /obd setup")
    ok = all(c["ok"] for c in checks)
    emit({"status": "ok" if ok else "partial",
          "summary": "Everything looks healthy." if ok else "Some checks failed; see fixes.",
          "data": {"checks": checks}})
    sys.exit(0)


# ---------------------------------------------------------------- setup
def find_uv() -> str:
    found = shutil.which("uv")
    if found:
        return found
    local = Path.home() / ".local" / "bin" / ("uv.exe" if IS_WIN else "uv")
    return str(local) if local.exists() else ""


def log(msg: str) -> None:
    sys.stderr.write(f"[obd setup] {msg}\n")


def install_uv() -> None:
    log("Installing uv (official installer from astral.sh)...")
    if IS_WIN:
        cmd = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", "irm https://astral.sh/uv/install.ps1 | iex"]
    else:
        cmd = ["sh", "-c", "curl -LsSf https://astral.sh/uv/install.sh | sh"]
    subprocess.run(cmd, stdout=sys.stderr, stderr=sys.stderr)


def setup(install: bool) -> None:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
    except OSError:
        fail("SETUP_FAILED", f"Cannot create {DATA}", "Check folder permissions.")
    engine_version = plugin_version()
    uv = find_uv()
    if not uv and install:
        install_uv()
        uv = find_uv()
    if not uv:
        fail("NEEDS_UV", "uv (the Python environment manager from astral.sh) is not installed",
             "Ask the user for permission, then run setup with --install-uv (installs uv for this user only).")
    log(f"Using uv: {uv}")
    env = dict(os.environ, UV_PROJECT_ENVIRONMENT=str(VENV))
    # uv won't rebuild a same-version local package, so force it, unless the venv already has this engine version
    # (e.g. the MCP server built it first; on Windows its running obd-mcp.exe can't be replaced).
    installed = ""
    if venv_python().exists():
        installed = subprocess.run([str(venv_python()), "-c", "import obd_engine; print(obd_engine.__version__)"],
                                   capture_output=True, text=True).stdout.strip()
    reinstall = [] if installed == engine_version and not os.environ.get("OBD_FORCE_REINSTALL") else ["--reinstall-package", "obd-engine"]
    rc = subprocess.call([uv, "sync", "--frozen", "--no-dev", "--no-editable", *reinstall,
                          "--python", "3.12", "--project", str(ROOT / "engine")], env=env, stdout=sys.stderr)
    if rc != 0:
        fail("SETUP_FAILED", "uv could not build the engine environment",
             "Check your internet connection or proxy, then re-run /obd setup. If an older engine is installed and the obd MCP server "
             "is running (Windows locks it), disconnect it in /mcp first. Details are in the output above.")
    installer = "uv"

    vpy = str(venv_python())
    py_version = subprocess.run([vpy, "-c", "import sys; print(sys.version.split()[0])"],
                                capture_output=True, text=True).stdout.strip()
    log("Running self-test...")
    env = dict(engine_env(), CLAUDE_PLUGIN_ROOT=str(ROOT), CLAUDE_PLUGIN_DATA=str(DATA), OBD_PYTHON=vpy)
    res = subprocess.run([vpy, "-m", "obd_engine", "selftest", "--json"], env=env, capture_output=True, text=True, encoding="utf-8")
    if res.returncode != 0:
        sys.stdout.write(res.stdout)
        log("Self-test failed (see JSON on stdout).")
        sys.exit(4)
    (DATA / "setup.json").write_text(json.dumps({
        "engine_version": engine_version, "python_version": py_version, "installer": installer,
        "setup_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}) + "\n", encoding="utf-8")
    emit({"status": "ok",
          "summary": f"Setup complete (engine {engine_version}, Python {py_version}, via {installer}). "
                     "Put your scanner exports in a folder and say: analyze my log - or run /obd scan.",
          "data": {"engine_version": engine_version, "python_version": py_version, "installer": installer, "venv": str(VENV)}})


def main(argv: list) -> None:
    if argv and argv[0] == "mcp":
        run_engine(argv[1:], mcp=True)
    elif argv and argv[0] == "setup":
        setup("--install-uv" in argv[1:])
    elif argv and argv[0] == "doctor":
        doctor()
    else:
        run_engine(argv)


if __name__ == "__main__":
    main(sys.argv[1:])
