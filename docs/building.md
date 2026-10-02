# Building the Windows exe

The engine is standard-library Python, so `python -m campaign` is all a
developer needs. A player should not have to install Python to fly a
campaign, so the engine can also be frozen into a single Windows executable,
`dcs-campaign-engine.exe`, that carries its own interpreter.

The exe *is* the engine: the same code, started through the same
`campaign.__main__.main`, with the same command line. It is not a second
implementation and must never become one.

## Building

On Windows, with Python 3.14, from the repository root:

```
python -m venv .venv-build
.venv-build\Scripts\python -m pip install -r requirements-build.txt
.venv-build\Scripts\python tools\build_exe.py
```

Use a separate virtualenv for the build. The engine and its tests need
nothing installed; keeping PyInstaller out of the environment you test from
keeps it that way. `build/`, `dist/` and `.venv-build/` are gitignored.

`tools/build_exe.py` produces `dist\dcs-campaign-engine.exe` and exits 0, or
exits non-zero on any failure. Before it reports success it runs the fresh
exe twice — `--help`, and `--simulate 0` into a temporary save — so an exe
that froze but cannot start (a module the freezer missed, say) fails the
build instead of reaching a player. The previous exe is deleted before each
build, so a failed build cannot leave a stale one in `dist/`.

What the build does, and why:

| choice | why |
|---|---|
| PyInstaller, pinned in `requirements-build.txt` | 6.22.3 supports CPython 3.14 (`Requires-Python <3.16,>=3.8`, 3.14 in its classifiers). The pin includes its dependencies, so two builds of one commit use one toolchain. |
| one file | a single file is the thing a player can be handed |
| console | the engine is a server that logs to its console; a windowed exe would hide every log line, and Ctrl+C is how it is stopped |
| `exe/engine_entry.py` as the entry | PyInstaller freezes a script, and `python -m campaign` is a module. The entry is three lines that call `campaign.__main__.main`, so new flags reach the exe without touching it. (The directory is not called `packaging/` because PyInstaller imports a PyPI package of that name.) |
| `--collect-submodules campaign` | the real engine is imported lazily in `build_engine`, and `--engine` imports by name at run time; every `campaign.*` module goes in whether or not static analysis saw it |
| `--noupx` | UPX-packed executables are a common antivirus trigger, and PyInstaller uses UPX silently if it happens to be on `PATH` |

On the machine it was developed on (Windows 11, CPython 3.14.3, PyInstaller
6.22.3) a clean build took about 15 seconds and the exe was 9.0 MiB.

## Checking a build

```
python tools\check_exe.py
```

Any Python 3.14 will do; it needs a built exe and nothing else. It is not
part of the test suite, which stays standard-library only and needs no build.
It checks, against the exe:

1. `--help` exits 0.
2. `--simulate 600` from a fresh start writes a save **byte-identical** to the
   one `python -m campaign --simulate 600` writes, and `Campaign.load` reads
   it. The engine is deterministic from a fresh start, so identical bytes are
   evidence that the frozen interpreter ran the same engine to the same
   result. (A full day, `--simulate 86400`, was also byte-identical when this
   was written.)
3. The real loop: the exe serves on a free port, `tools/fake_dcs.py --seed 7`
   plays DCS against it — the depot is destroyed, both VIPER elements recover
   — and the exe is then stopped with a real Ctrl+C, exits 0, and writes its
   save on shutdown.

`--workdir DIR` keeps the saves, the server log and the harness summary.

## Running

Double-click it, or run it from a command prompt with exactly the flags
`python -m campaign` takes:

```
dcs-campaign-engine.exe --port 7777 --save saves\campaign.json
dcs-campaign-engine.exe --save saves\campaign.json --simulate 86400
dcs-campaign-engine.exe --help
```

Everything in the main README about running the engine applies unchanged;
substitute `dcs-campaign-engine.exe` for `python -m campaign`. The usage line
`--help` prints still says `python -m campaign`, because that text lives in
`campaign/__main__.py` and the exe runs it as is.

Things worth knowing:

- **The save path is relative to the working directory.** Double-clicked, the
  default `saves\campaign.json` lands next to the exe. From a shortcut it
  lands under the shortcut's "Start in" folder. Pass `--save` with a full path
  to be sure.
- **Stop it with Ctrl+C, or close the window.** Ctrl+C is the clean
  shutdown: the exe writes the campaign to the save and exits 0 (verified by
  `tools/check_exe.py`). Closing the console window, logging off or shutting
  Windows down also writes the save first: the engine installs a console
  control handler that saves before Windows ends the process. That was
  verified with `python -m campaign` by closing the pseudoconsole it ran in,
  which is what Windows Terminal does when a tab closes (the engine before
  this saved nothing); the exe runs the same code but has not been closed
  that way itself. The save is also written every time DCS disconnects and
  every minute while the engine runs (`--autosave SECONDS`, 0 for off), so a
  crash, a hard kill or a power cut loses at most a minute of war.
- **No firewall prompt by default.** The engine binds `127.0.0.1`, which
  Windows Firewall does not ask about. `--host 0.0.0.0` (DCS on another
  machine) should be expected to prompt.
- **Startup.** A one-file exe unpacks its interpreter into a
  `%TEMP%\_MEI…` folder each time it starts, and removes it on a clean exit.
  Measured here: `--help` takes about 0.55 s against 0.17 s for
  `python -m campaign --help`, and the server is listening about 0.3 s after
  launch. A hard kill (Task Manager, `taskkill /F`) leaves that temp folder
  behind; it is safe to delete.
- **`--engine` only reaches what is inside the exe.** It can name
  `campaign.*` modules; it cannot load a module from disk, because the exe
  carries no import path to your files. Use `python -m campaign` for that.

## What the exe contains, and what it does not

It contains the CPython 3.14 interpreter, the parts of the standard library
the engine imports, and the `campaign` package. That is the whole engine
side; the engine has no other dependencies.

It does **not** contain the DCS side. The mission client —
`mission/campaign_client.lua` and `mission/json.lua` — runs inside DCS, not
in this process, and ships separately. Installing it, including the
`MissionScripting.lua` change it requires, is described in
[`mission/README.md`](../mission/README.md). Nor does the exe contain
`tools/fake_dcs.py`, the tests, or the documentation.

## SmartScreen and antivirus

Expect a fresh build to be flagged, at least sometimes. This is a property of
how the exe is made, not of what it does, and it is worth understanding
before handing it to anyone.

- **SmartScreen.** A downloaded executable carries a "mark of the web", and
  SmartScreen judges it by the reputation of its publisher's code-signing
  certificate. This exe is unsigned, so it has no publisher and no
  reputation, and a player who downloads it will see "Windows protected your
  PC" until they choose *More info → Run anyway*. Every new build is a new,
  unknown file.
- **Antivirus heuristics.** PyInstaller's bootloader — the small native
  program at the front of every PyInstaller exe — is identical across
  thousands of programs, including malware whose authors chose PyInstaller
  because it is easy. Signatures written against that malware sometimes match
  the bootloader itself. A one-file exe also behaves like a packer: it
  carries a compressed payload, unpacks executable code to a temp folder and
  runs it, which is exactly what heuristic scanners are built to distrust.
  This one then opens a listening socket. None of that is malicious, all of
  it looks familiar to a scanner.

What actually helps, roughly in order of effect:

1. **Sign the exe** with a code-signing certificate. This is the real fix for
   SmartScreen, which builds reputation per certificate over time, and it
   reduces antivirus false positives. It costs money and is not done here.
2. **Report false positives** to the vendor that flags the build; the major
   vendors have submission forms for exactly this.
3. **Do not use UPX.** The build already passes `--noupx`.
4. **Build PyInstaller's bootloader from source** so its bytes do not match
   the published one. This sometimes helps and is not done here.
5. **Run from source** with `python -m campaign`. Anyone wary of the exe
   loses nothing by doing this; it is the same engine.
