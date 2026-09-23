#!/usr/bin/env python3
"""Compare two campaign saves under the reconciliation rule.

Run the offline loop once normally and once with ``--drop-events``, then point
this at the two saves. Everything but attribution must be identical; if it is
not, the campaign has started taking losses from the event stream and will
drift the first time DCS swallows one.

    python tools/diff_saves.py saves/with-events.json saves/no-events.json
"""

from __future__ import annotations

import argparse
import difflib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from campaign.audit import (  # noqa: E402  (path bootstrap must run first)
    attributions,
    strip_event_derived,
)


def _canonical(state: dict) -> list[str]:
    return json.dumps(state, indent=2, sort_keys=True).splitlines(keepends=True)


def compare(left: Path, right: Path) -> int:
    a = json.loads(left.read_text(encoding="utf-8"))
    b = json.loads(right.read_text(encoding="utf-8"))
    diff = list(
        difflib.unified_diff(
            _canonical(strip_event_derived(a)),
            _canonical(strip_event_derived(b)),
            fromfile=str(left),
            tofile=str(right),
        )
    )
    if diff:
        sys.stdout.writelines(diff)
        print(
            f"\nRECONCILIATION BROKEN: {len(diff)} differing lines outside "
            f"attribution. Events changed the campaign, not just its story.",
            file=sys.stderr,
        )
        return 1

    left_attr, right_attr = attributions(a), attributions(b)
    print(f"campaign state identical: {len(left_attr)} loss record(s)")
    for i, (x, y) in enumerate(zip(left_attr, right_attr)):
        if x != y:
            print(f"  loss {i}: attribution {x!r} -> {y!r}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="diff_saves", description=__doc__)
    parser.add_argument("left", type=Path)
    parser.add_argument("right", type=Path)
    args = parser.parse_args(argv)
    return compare(args.left, args.right)


if __name__ == "__main__":
    raise SystemExit(main())
