"""Entry script for the frozen Windows build (see docs/building.md).

PyInstaller needs a script to freeze, and ``python -m campaign`` is a module
invocation, not a script. This is the smallest bridge: it calls the same
``campaign.__main__.main`` that ``python -m campaign`` runs, with the same
argv, so the exe accepts exactly the command line the module does. Nothing
here may grow behaviour of its own; a flag added to campaign/__main__.py
reaches the exe on the next build without touching this file.

The directory is called ``exe/`` rather than ``packaging/`` on purpose:
``packaging`` is a PyPI package PyInstaller itself imports, and a same-named
directory next to the build is a shadowing accident waiting to happen.
"""

from __future__ import annotations

from campaign.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
