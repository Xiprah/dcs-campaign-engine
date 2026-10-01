#!/usr/bin/env python3
"""Prove a built ``dist/dcs-campaign-engine.exe`` runs the same engine as
``python -m campaign``. Windows only; needs a built exe, nothing else.

    python tools/build_exe.py      (with requirements-build.txt installed)
    python tools/check_exe.py      (any Python 3.14 that can run the engine)

Three checks, each against the frozen exe:

1. ``--help`` exits 0.
2. ``--simulate 600`` from a fresh start writes a save that is byte-identical
   to the one ``python -m campaign --simulate 600`` writes, and that
   ``Campaign.load`` reads. The engine is deterministic from a fresh start, so
   identical bytes mean the frozen interpreter ran the same code to the same
   result.
3. The real loop: the exe serves on a free port, ``tools/fake_dcs.py`` plays
   DCS against it, then the exe is stopped with a real Ctrl+C -- the way a
   player stops it -- and must exit 0 having written its save on shutdown.

Not part of the test suite on purpose: tests/ stays standard-library only and
does not need a build. This script starts processes and always stops them,
including the one-file bootloader's child, before it returns.
"""

from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_EXE = ROOT / "dist" / "dcs-campaign-engine.exe"

# Sent from a separate, console-less helper: attach to the server's console and
# raise Ctrl+C there, ignoring it ourselves. This is the only way to deliver a
# real Ctrl+C (not a kill) to another process's console on Windows.
CTRL_C_HELPER = r"""
import ctypes, sys
k = ctypes.windll.kernel32
k.FreeConsole()
if not k.AttachConsole(int(sys.argv[1])):
    sys.exit(f"AttachConsole failed: {ctypes.GetLastError()}")
k.SetConsoleCtrlHandler(None, True)
if not k.GenerateConsoleCtrlEvent(0, 0):
    sys.exit(f"GenerateConsoleCtrlEvent failed: {ctypes.GetLastError()}")
"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def kill_tree(proc: subprocess.Popen[bytes]) -> None:
    """Hard-stop a process and its children (the one-file exe is two)."""
    if proc.poll() is None:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                       capture_output=True)
        proc.wait(timeout=30)


def check_help(exe: Path) -> bool:
    run = subprocess.run([str(exe), "--help"], capture_output=True, text=True,
                         timeout=120)
    ok = run.returncode == 0 and "--simulate" in run.stdout
    print(f"[{'PASS' if ok else 'FAIL'}] --help exited {run.returncode}")
    return ok


def check_simulate(exe: Path, python: str, tmp: Path) -> bool:
    exe_save, py_save = tmp / "exe.json", tmp / "py.json"
    for stale in (exe_save, py_save):  # a fresh start on both sides
        stale.unlink(missing_ok=True)
    runs = {
        "exe": subprocess.run([str(exe), "--save", str(exe_save), "--simulate", "600"],
                              capture_output=True, text=True, timeout=300),
        "python": subprocess.run([python, "-m", "campaign", "--save", str(py_save),
                                  "--simulate", "600"],
                                 cwd=ROOT, capture_output=True, text=True, timeout=300),
    }
    for name, run in runs.items():
        if run.returncode != 0:
            print(f"[FAIL] {name} --simulate 600 exited {run.returncode}\n{run.stderr}")
            return False
    identical = exe_save.read_bytes() == py_save.read_bytes()
    print(f"[{'PASS' if identical else 'FAIL'}] --simulate 600: exe save "
          f"({exe_save.stat().st_size} bytes) "
          f"{'byte-identical to' if identical else 'DIFFERS from'} python -m campaign's")
    loaded = load_save(exe_save)
    print(f"[{'PASS' if loaded else 'FAIL'}] Campaign.load reads the exe's save")
    return identical and loaded


def load_save(path: Path) -> bool:
    sys.path.insert(0, str(ROOT))
    from campaign.campaign import Campaign

    try:
        Campaign.load(path)
    except Exception as exc:  # report, do not crash the checker
        print(f"       Campaign.load({path.name}) raised {exc!r}")
        return False
    return True


def wait_for(path: Path, needle: str, proc: subprocess.Popen[bytes],
             timeout: float) -> float | None:
    start = time.perf_counter()
    while time.perf_counter() - start < timeout:
        if needle in path.read_text(encoding="utf-8", errors="replace"):
            return time.perf_counter() - start
        if proc.poll() is not None:
            return None
        time.sleep(0.02)
    return None


def check_loop(exe: Path, python: str, tmp: Path, seed: int) -> bool:
    port = free_port()
    save, log, summary = tmp / "loop.json", tmp / "server.log", tmp / "fake_dcs.json"
    for stale in (save, summary):
        stale.unlink(missing_ok=True)
    print(f"       server: {exe.name} --port {port} --save {save}")
    with log.open("wb") as log_fh:
        started = time.perf_counter()
        server = subprocess.Popen(
            [str(exe), "--port", str(port), "--save", str(save)],
            stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NEW_CONSOLE,
            startupinfo=_hidden_window(),
        )
        try:
            return _drive_loop(server, started, python, port, save, log, summary, seed)
        finally:
            kill_tree(server)


def _drive_loop(server: subprocess.Popen[bytes], started: float, python: str,
                port: int, save: Path, log: Path, summary: Path, seed: int) -> bool:
    if wait_for(log, "listening on", server, 120) is None:
        print(f"[FAIL] server never listened\n{log.read_text(errors='replace')}")
        return False
    print(f"[PASS] exe listening {time.perf_counter() - started:.2f}s after launch "
          f"(pid {server.pid})")

    fake = subprocess.run(
        [python, str(ROOT / "tools" / "fake_dcs.py"), "--port", str(port),
         "--seed", str(seed), "--summary", str(summary)],
        cwd=ROOT, capture_output=True, text=True, timeout=600,
    )
    report = json.loads(summary.read_text(encoding="utf-8")) if summary.exists() else {}
    messages = report.get("messages", [])
    destroyed = any(o.get("target_alive") == 0 for o in report.get("outcomes", []))
    recovered = [m for m in messages if "recovered" in m]
    loop_ok = fake.returncode == 0 and destroyed and bool(recovered)
    print(f"[{'PASS' if loop_ok else 'FAIL'}] fake_dcs --seed {seed} exited "
          f"{fake.returncode}; target destroyed: {destroyed}; recovered: {recovered}")
    if not loop_ok:
        print(fake.stdout[-4000:], fake.stderr[-4000:])

    saves_before = log.read_text(errors="replace").count("campaign saved to")
    helper = subprocess.run([python, "-c", CTRL_C_HELPER, str(server.pid)],
                            capture_output=True, text=True,
                            creationflags=subprocess.DETACHED_PROCESS)
    if helper.returncode != 0:
        print(f"[FAIL] could not send Ctrl+C: {helper.stderr.strip()}")
        return False
    try:
        code = server.wait(timeout=60)
    except subprocess.TimeoutExpired:
        print("[FAIL] exe did not exit within 60s of Ctrl+C")
        return False
    text = log.read_text(errors="replace")
    wrote = text.count("campaign saved to") > saves_before
    shutdown_ok = code == 0 and wrote and save.exists() and load_save(save)
    print(f"[{'PASS' if shutdown_ok else 'FAIL'}] Ctrl+C: exe exited {code}; "
          f"save written on shutdown: {wrote}; save loads: {save.exists() and load_save(save)}")
    if shutdown_ok:
        state = json.loads(save.read_text(encoding="utf-8"))
        for pkg in state["packages"].values():
            print(f"       {pkg['id']} {pkg['callsign']} ({pkg['coalition']}) -> "
                  f"{pkg['target_id']}: {pkg['state']}; elements "
                  f"{[e['state'] for e in pkg['elements']]}")
        print(f"       war_result: {state['war_result']}")
    return loop_ok and shutdown_ok


def _hidden_window() -> subprocess.STARTUPINFO:
    info = subprocess.STARTUPINFO()
    info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    info.wShowWindow = 0  # SW_HIDE
    return info


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--exe", type=Path, default=DEFAULT_EXE)
    parser.add_argument("--python", default=sys.executable,
                        help="interpreter for the python -m campaign reference run "
                             "and for tools/fake_dcs.py")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--workdir", type=Path, default=None,
                        help="keep saves, the server log and the fake_dcs summary "
                             "here instead of a temporary directory")
    args = parser.parse_args()
    if sys.platform != "win32":
        print("check_exe: Windows only", file=sys.stderr)
        return 2
    if not args.exe.is_file():
        print(f"check_exe: no exe at {args.exe}; run tools/build_exe.py", file=sys.stderr)
        return 2
    with tempfile.TemporaryDirectory(prefix="check_exe_") as name:
        tmp = args.workdir or Path(name)
        tmp.mkdir(parents=True, exist_ok=True)
        results = [
            check_help(args.exe),
            check_simulate(args.exe, args.python, tmp),
            check_loop(args.exe, args.python, tmp, args.seed),
        ]
    ok = all(results)
    print("check_exe: all checks passed" if ok else "check_exe: FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
