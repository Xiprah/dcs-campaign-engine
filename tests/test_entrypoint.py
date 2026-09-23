"""`python -m campaign`: the two things the process entry point owes the war.

The wiring itself is thin and uninteresting. What is not is persistence: the
save is written on every DCS disconnect and once more on shutdown, so it is
written often, under interruption, and it is the only thing standing between a
crash and a lost campaign. Two failures here are unrecoverable rather than
merely annoying -- a torn save file, and a torn save file reported as a
traceback -- so both are pinned.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from campaign.__main__ import _Persisting, main


class _Engine:
    """Just enough engine to be persisted. save() is the only part that matters."""

    def __init__(self) -> None:
        self.disconnects = 0
        self.payload: dict[str, object] = {"save_version": 1, "clock": 1200.0}
        self.explode = False

    def on_disconnect(self) -> None:
        self.disconnects += 1

    def save(self, path: str | Path) -> None:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if self.explode:
            # What an interrupted `write_text` leaves behind: the file already
            # truncated, the rest of it never arriving.
            destination.write_text('{"save_ver', encoding="utf-8")
            raise RuntimeError("serialising the campaign failed")
        destination.write_text(json.dumps(self.payload), encoding="utf-8")


class PersistTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.save = self.dir / "campaign.json"
        self.engine = _Engine()
        self.wrapped = _Persisting(self.engine, self.save)

    def test_a_disconnect_writes_the_campaign_through(self) -> None:
        self.wrapped.on_disconnect()
        self.assertEqual(self.engine.disconnects, 1)
        self.assertEqual(json.loads(self.save.read_text(encoding="utf-8")), self.engine.payload)

    def test_no_staging_file_is_left_behind(self) -> None:
        self.wrapped.persist()
        self.assertEqual([p.name for p in self.dir.iterdir()], ["campaign.json"])

    def test_a_failed_save_leaves_the_previous_one_intact(self) -> None:
        self.wrapped.persist()
        good = self.save.read_text(encoding="utf-8")
        self.engine.explode = True
        with self.assertLogs("campaign", level="ERROR"):
            self.wrapped.persist()
        self.assertEqual(
            self.save.read_text(encoding="utf-8"),
            good,
            "a failed save truncated the last good one",
        )
        self.assertEqual([p.name for p in self.dir.iterdir()], ["campaign.json"])

    def test_an_engine_without_save_is_a_warning_not_a_crash(self) -> None:
        class Bare:
            def on_disconnect(self) -> None:
                pass

        with self.assertLogs("campaign", level="WARNING"):
            _Persisting(Bare(), self.save).persist()
        self.assertFalse(self.save.exists())


class CorruptSaveTests(unittest.TestCase):
    """A save that cannot be read must not be a traceback.

    `persist()` is now atomic, but saves predating that, or written by anything
    else, can still be torn -- and the operator meeting one has to be told what
    to do about it, on a run that exits 2 rather than one that dies in
    `json.loads`.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.save = self.dir / "campaign.json"

    def _run(self) -> tuple[int, str]:
        err = io.StringIO()
        with redirect_stderr(err):
            code = main(["--save", str(self.save), "--port", "0"])
        return code, err.getvalue()

    def test_a_zero_length_save_reports_rather_than_traces(self) -> None:
        self.save.write_text("", encoding="utf-8")
        code, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn(str(self.save), err)

    def test_a_truncated_save_reports_rather_than_traces(self) -> None:
        self.save.write_text('{"save_version": 1}', encoding="utf-8")
        code, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn(str(self.save), err)

    def test_a_save_from_another_version_reports_rather_than_traces(self) -> None:
        self.save.write_text('{"save_version": 9999}', encoding="utf-8")
        code, err = self._run()
        self.assertEqual(code, 2)
        self.assertIn(str(self.save), err)


if __name__ == "__main__":
    unittest.main()
