"""Read what mission/validate_templates.lua produced and say what it means.

Feed it either the JSON report the validator writes, or a `dcs.log` it was
logged into -- the two carry the same results, and the log is the one that
still exists when `io` was left sanitised. The output is a table of every
case and an exit status:

    0   every required case passed
    1   at least one required case failed
    2   nothing could be read, or the run did not finish

Standard library only, exactly like the rest of this repository.

    python tools/parse_validation.py ~/Saved\\ Games/DCS/Logs/dcs.log
    python tools/parse_validation.py campaign_validation.json --strict

WHAT COUNTS AS A FAILURE

`REJECTED`, `ORPHAN`, `DRIFT` and `ERROR` are failures: DCS refused the
content, or accepted it and made nothing, or the validator is testing values
the client no longer uses, or a case could not be cleaned up.

`UNKNOWN` and `SKIP` are *not* failures by default, because they mean the run
could not determine the answer -- an unrecognised CLSID probe, a cross-check
with no client loaded. `--strict` promotes them, which is what you want when
the run is supposed to have settled every question.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import sys
from pathlib import Path
from typing import Any

#: The prefix mission/validate_templates.lua puts on every line. dcs.log adds
#: its own timestamp and subsystem in front, so this is searched for, not
#: matched at the start of a line.
PREFIX = "[campaign-validate] "

#: Statuses that mean a required case did not pass.
FAILING = ("REJECTED", "ORPHAN", "DRIFT", "ERROR")

#: Statuses that mean the run could not answer the question.
INCONCLUSIVE = ("UNKNOWN", "SKIP")

STATUS_ORDER = ("OK", "REJECTED", "ORPHAN", "DRIFT", "ERROR", "UNKNOWN", "SKIP")


class Report:
    """The parsed run: results plus whatever the run said about itself."""

    def __init__(self) -> None:
        self.results: list[dict[str, Any]] = []
        self.meta: dict[str, Any] = {}
        self.complete = False
        self.source = ""

    @property
    def counts(self) -> dict[str, int]:
        out = {status: 0 for status in STATUS_ORDER}
        for result in self.results:
            out[result["status"]] = out.get(result["status"], 0) + 1
        return out

    def failures(self, strict: bool) -> list[dict[str, Any]]:
        bad = FAILING + (INCONCLUSIVE if strict else ())
        return [
            r for r in self.results if r["required"] and r["status"] in bad
        ]


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes")


def parse_json(text: str) -> Report:
    """The validator's own JSON report."""
    data = json.loads(text)
    if not isinstance(data, dict) or "results" not in data:
        raise ValueError("this JSON is not a validator report (no 'results')")

    report = Report()
    report.source = "json"
    report.complete = _as_bool(data.get("complete"))
    report.meta = {
        key: data[key]
        for key in ("version", "theatre", "origin", "template_source",
                    "mission_time", "leaked")
        if key in data
    }
    for entry in data["results"]:
        report.results.append(
            {
                "id": str(entry.get("id", "?")),
                "kind": str(entry.get("kind", "?")),
                "status": str(entry.get("status", "UNKNOWN")).upper(),
                "required": _as_bool(entry.get("required")),
                "label": str(entry.get("label", "")),
                "error": str(entry.get("error", "")),
                "detail": entry.get("detail") or {},
            }
        )
    return report


def _fields(body: str) -> dict[str, str]:
    """Split `key=value key="value with spaces"` into a dict.

    shlex handles the quoting the Lua side emits; a token without an `=` is
    the leading verb (RESULT, SUMMARY) and is returned under "".
    """
    out: dict[str, str] = {}
    try:
        tokens = shlex.split(body)
    except ValueError:
        tokens = body.split()
    for token in tokens:
        key, sep, value = token.partition("=")
        if sep:
            out[key] = value
        else:
            out.setdefault("", token)
    return out


def parse_log(text: str) -> Report:
    """A dcs.log (or any text) the validator's lines were written into."""
    report = Report()
    report.source = "log"
    pattern = re.compile(re.escape(PREFIX) + "(.*)")

    for line in text.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        body = match.group(1).strip()
        fields = _fields(body)
        verb = fields.get("")

        if verb == "RESULT":
            detail = {
                k: v
                for k, v in fields.items()
                if k not in ("", "status", "required", "id", "kind", "label",
                             "error")
            }
            report.results.append(
                {
                    "id": fields.get("id", "?"),
                    "kind": fields.get("kind", "?"),
                    "status": fields.get("status", "UNKNOWN").upper(),
                    "required": _as_bool(fields.get("required")),
                    "label": fields.get("label", ""),
                    "error": fields.get("error", ""),
                    "detail": detail,
                }
            )
        elif verb == "BEGIN":
            report.meta["version"] = fields.get("version", "")
            report.meta["origin"] = fields.get("origin", "")
            report.meta["origin_source"] = fields.get("origin_source", "")
        elif verb == "SUMMARY":
            report.complete = True
            if "leaked" in fields:
                report.meta["leaked"] = fields["leaked"]
            if "json" in fields:
                report.meta["json"] = fields["json"]

    return report


def read_report(path: Path) -> Report:
    """JSON if it parses as one, otherwise a log."""
    text = path.read_text(encoding="utf-8", errors="replace")
    stripped = text.lstrip()
    if stripped.startswith("{"):
        return parse_json(text)
    return parse_log(text)


# --------------------------------------------------------------------------
# Printing
# --------------------------------------------------------------------------


def _detail_summary(result: dict[str, Any]) -> str:
    detail = result["detail"]
    if not isinstance(detail, dict):
        return ""
    interesting = ("units", "wanted", "retrievable", "retrievable_next",
                   "ammo", "ammo_types", "clsid", "mismatches", "cleaned")
    parts = []
    for key in interesting:
        if key in detail and str(detail[key]) not in ("", "None"):
            value = detail[key]
            if isinstance(value, bool):
                value = "yes" if value else "no"
            parts.append(f"{key}={value}")
    return " ".join(parts)


def render(report: Report, stream=None, strict: bool = False) -> None:
    # Resolved per call, not bound at import: a caller may have replaced
    # sys.stdout since.
    write = (stream if stream is not None else sys.stdout).write

    if report.meta:
        write("run: " + ", ".join(
            f"{k}={v}" for k, v in sorted(report.meta.items())) + "\n\n")

    rows = [("STATUS", "REQ", "ID", "DETAIL / ERROR")]
    for result in report.results:
        note = result["error"] or _detail_summary(result)
        rows.append(
            (
                result["status"],
                "req" if result["required"] else "opt",
                result["id"],
                note,
            )
        )

    widths = [max(len(row[i]) for row in rows) for i in range(3)]
    for index, row in enumerate(rows):
        line = "  ".join(row[i].ljust(widths[i]) for i in range(3))
        write(f"{line}  {row[3]}".rstrip() + "\n")
        if index == 0:
            write("  ".join("-" * widths[i] for i in range(3)) + "  " + "-" * 14 + "\n")

    counts = report.counts
    write("\n" + " ".join(
        f"{status.lower()}={counts.get(status, 0)}" for status in STATUS_ORDER
    ) + f" total={len(report.results)}\n")

    if not report.results:
        write("\nno validator results in this file. Was the script loaded, "
              "and is this the right log?\n")
        return

    if not report.complete:
        write("\nthe run did not finish: no SUMMARY line. Results above are "
              "partial.\n")

    failures = report.failures(strict)
    if failures:
        write(f"\n{len(failures)} required case(s) failed:\n")
        for result in failures:
            write(f"  {result['status']:9} {result['id']}: "
                  f"{result['error'] or result['label']}\n")
    else:
        write("\nevery required case passed"
              + (" (strict)" if strict else "") + ".\n")

    inconclusive = [
        r for r in report.results if r["status"] in INCONCLUSIVE
    ]
    if inconclusive and not strict:
        write(f"{len(inconclusive)} case(s) were not determined "
              "(UNKNOWN/SKIP); rerun with --strict to fail on them.\n")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Turn mission/validate_templates.lua's output into a report."
    )
    parser.add_argument(
        "path", help="the validator's JSON report, or a dcs.log containing its lines"
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="treat UNKNOWN and SKIP on a required case as a failure too",
    )
    args = parser.parse_args(argv)

    path = Path(args.path)
    if not path.is_file():
        print(f"no such file: {path}", file=sys.stderr)
        return 2

    try:
        report = read_report(path)
    except (ValueError, json.JSONDecodeError) as exc:
        print(f"could not read {path}: {exc}", file=sys.stderr)
        return 2

    render(report, strict=args.strict)

    if not report.results:
        return 2
    if not report.complete:
        return 2
    return 1 if report.failures(args.strict) else 0


if __name__ == "__main__":  # pragma: no cover - exercised via main()
    raise SystemExit(main())
