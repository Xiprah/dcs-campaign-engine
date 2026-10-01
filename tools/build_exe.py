#!/usr/bin/env python3
"""Freeze the campaign engine into ``dist/dcs-campaign-engine.exe``.

    python -m venv .venv-build
    .venv-build\\Scripts\\python -m pip install -r requirements-build.txt
    .venv-build\\Scripts\\python tools\\build_exe.py

Run it with an interpreter that has requirements-build.txt installed. It exits
0 only if the exe was built *and* boots: ``--help`` and a zero-second
``--simulate`` are run against the fresh exe before this script reports
success, so a build that freezes but cannot start (a module PyInstaller failed
to find, say) is a failed build, not a broken release. See docs/building.md for
what the exe contains and what it does not.

Choices, and why:

* One file, console. The engine is a server that logs to its console; a
  windowed exe would swallow every log line, and one file is the thing a
  player can be handed.
* ``--noupx``. UPX-packed executables are a classic antivirus heuristic
  trigger, and PyInstaller uses UPX silently if it happens to be on PATH.
* ``--collect-submodules campaign`` / ``--collect-data campaign``. The real
  engine is imported lazily inside ``build_engine``, and ``--engine`` imports
  by name at run time; collecting the whole package means no ``campaign.*``
  module (or data file a later change adds to the package) is left out
  because static analysis did not see it imported.
* Everything PyInstaller writes goes under ``build/`` (work files and the
  generated .spec) or ``dist/`` (the exe), both gitignored. The previous exe
  is deleted first, so a failed build can never leave a stale one behind for
  CI to upload.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
NAME = "dcs-campaign-engine"
ENTRY = ROOT / "exe" / "engine_entry.py"
DIST = ROOT / "dist"
WORK = ROOT / "build" / "pyinstaller"
SPEC_DIR = ROOT / "build"
EXE = DIST / f"{NAME}.exe"
SMOKE_TIMEOUT = 120.0  # seconds; a cold one-file start unpacks to %TEMP% first


def fail(message: str) -> int:
    print(f"build_exe: FAILED: {message}", file=sys.stderr)
    return 1


def pyinstaller_command() -> list[str]:
    return [
        sys.executable, "-m", "PyInstaller",
        "--onefile",
        "--console",
        "--name", NAME,
        "--noupx",
        "--clean",
        "--noconfirm",
        "--log-level", "WARN",
        "--paths", str(ROOT),
        "--collect-submodules", "campaign",
        "--collect-data", "campaign",
        "--distpath", str(DIST),
        "--workpath", str(WORK),
        "--specpath", str(SPEC_DIR),
        str(ENTRY),
    ]


def smoke(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str] | str:
    """Run the exe; return the result, or a reason it could not be judged."""
    try:
        return subprocess.run(
            [str(EXE), *args], cwd=cwd, capture_output=True, text=True,
            timeout=SMOKE_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return f"`{EXE.name} {' '.join(args)}` did not exit within {SMOKE_TIMEOUT:g}s"
    except OSError as exc:
        return f"could not start {EXE}: {exc}"


def main() -> int:
    if sys.platform != "win32":
        return fail("the exe is a Windows build; run this on Windows "
                    "(PyInstaller does not cross-compile)")
    try:
        import PyInstaller  # noqa: F401  (presence check only)
    except ImportError:
        return fail(f"PyInstaller is not installed for {sys.executable}; "
                    "pip install -r requirements-build.txt first")
    from PyInstaller import __version__ as pyinstaller_version

    if not ENTRY.is_file():
        return fail(f"entry script {ENTRY} is missing")

    # Clean state: no stale exe can survive a failed build.
    for path in (EXE, WORK):
        if path.is_dir():
            shutil.rmtree(path)
        elif path.exists():
            path.unlink()

    print(f"build_exe: PyInstaller {pyinstaller_version} on Python "
          f"{sys.version.split()[0]} ({sys.executable})", flush=True)
    started = time.perf_counter()
    result = subprocess.run(pyinstaller_command(), cwd=ROOT)
    elapsed = time.perf_counter() - started
    if result.returncode != 0:
        return fail(f"PyInstaller exited {result.returncode}")
    if not EXE.is_file():
        return fail(f"PyInstaller exited 0 but {EXE} does not exist")

    # Prove the frozen exe starts and that the lazily imported engine made it
    # into the bundle: --simulate 0 builds a real Campaign and writes a save.
    help_run = smoke(["--help"], ROOT)
    if isinstance(help_run, str):
        return fail(help_run)
    if help_run.returncode != 0 or "--simulate" not in help_run.stdout:
        return fail(f"`{EXE.name} --help` exited {help_run.returncode}\n"
                    f"{help_run.stdout}{help_run.stderr}")
    with tempfile.TemporaryDirectory(prefix="build_exe_") as tmp:
        save = Path(tmp) / "smoke.json"
        sim_run = smoke(["--save", str(save), "--simulate", "0"], Path(tmp))
        if isinstance(sim_run, str):
            return fail(sim_run)
        if sim_run.returncode != 0 or not save.is_file():
            return fail(f"`{EXE.name} --simulate 0` exited {sim_run.returncode} "
                        f"(save written: {save.is_file()})\n"
                        f"{sim_run.stdout}{sim_run.stderr}")

    size_mib = EXE.stat().st_size / (1024 * 1024)
    print(f"build_exe: built {EXE.relative_to(ROOT)} ({size_mib:.1f} MiB) "
          f"in {elapsed:.1f}s; --help and --simulate 0 smoke runs passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
