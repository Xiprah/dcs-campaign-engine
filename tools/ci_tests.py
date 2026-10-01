"""Run the test suite the way CI does, and refuse a green that tested nothing.

    python tools/ci_tests.py --lua required   # lupa installed: nothing may skip
    python tools/ci_tests.py --lua absent     # standard library only

It is ``python -m unittest discover -s tests -t .`` plus the one check that
command cannot make. The Lua-backed tests skip cleanly when `lupa` is missing,
which is right on a developer's machine and wrong in CI: a run where lupa
failed to install would report green while the Lua client - the half of the
system that has never run inside DCS - went unexecuted. So:

``--lua required``
    lupa must import and must run Lua 5.1 code *before* any test runs, and
    after the run not one test may have been skipped, for any reason.

``--lua absent``
    lupa must *not* be importable (otherwise this is not the standard-library
    configuration it claims to be), every skip must be a lupa skip, and there
    must be at least one, so the Lua modules are known to have been discovered.

Either way, a run that collected no tests fails. Exit status is 0 only when
every check passed. Standard library only.
"""

from __future__ import annotations

import argparse
import os
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def fail(message: str) -> int:
    print(f"\nCI CHECK FAILED: {message}", file=sys.stderr, flush=True)
    return 1


def preflight_lupa_required() -> str | None:
    """Prove Lua 5.1 actually executes here. Returns an error, or None."""
    try:
        import lupa
        from lupa import lua51
    except ImportError as exc:
        return (
            f"lupa is required but does not import ({exc}). Install "
            "requirements-dev.txt; without it every Lua-backed test would "
            "skip and the run would be green while testing no Lua at all."
        )
    runtime = lua51.LuaRuntime()
    if runtime.lua_version != (5, 1):
        return f"lupa's lua51 runtime reports Lua {runtime.lua_version}, not 5.1"
    if runtime.eval("1 + 1") != 2:
        return "lupa imported but the Lua runtime cannot evaluate 1 + 1"
    print(f"preflight: lupa {lupa.__version__}, Lua 5.1 executes", flush=True)
    return None


def preflight_lupa_absent() -> str | None:
    try:
        import lupa  # noqa: F401
    except ImportError:
        print("preflight: lupa is not importable (standard library only)", flush=True)
        return None
    return (
        "lupa is importable, so this is not the standard-library-only "
        "configuration. Run it before installing requirements-dev.txt."
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--lua",
        required=True,
        choices=("required", "absent"),
        help="required: lupa must work and nothing may skip; "
        "absent: lupa must be missing and only lupa tests may skip",
    )
    args = parser.parse_args(argv)

    os.chdir(ROOT)
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    print(f"python {sys.version.split()[0]} on {sys.platform}", flush=True)

    error = (
        preflight_lupa_required() if args.lua == "required" else preflight_lupa_absent()
    )
    if error:
        return fail(error)

    suite = unittest.TestLoader().discover("tests", top_level_dir=".")
    result = unittest.TextTestRunner(verbosity=1).run(suite)

    skipped = result.skipped
    print(
        f"\nsummary: {result.testsRun} run, {len(skipped)} skipped, "
        f"{len(result.failures)} failed, {len(result.errors)} errors",
        flush=True,
    )
    for reason, count in Counter(reason for _, reason in skipped).most_common():
        print(f"  {count} skipped: {reason}")

    if not result.wasSuccessful():
        return fail("the test suite did not pass")
    if result.testsRun == 0:
        return fail("no tests were collected")

    if args.lua == "required":
        if skipped:
            for test, reason in skipped[:20]:
                print(f"  skipped: {test.id()}: {reason}")
            return fail(
                f"{len(skipped)} test(s) skipped with lupa installed. In this "
                "configuration every test must run."
            )
    else:
        foreign = [(t, r) for t, r in skipped if "lupa" not in r]
        if foreign:
            return fail(
                f"{len(foreign)} test(s) skipped for a reason other than "
                f"missing lupa, e.g. {foreign[0][0].id()}: {foreign[0][1]}"
            )
        if not skipped:
            return fail(
                "nothing skipped without lupa, so the Lua-backed tests were "
                "not discovered at all"
            )

    print(f"\nCI CHECK PASSED (--lua {args.lua})", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
