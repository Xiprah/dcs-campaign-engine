"""`python -m campaign`: the two things the process entry point owes the war.

The wiring itself is thin and uninteresting. What is not is persistence: the
save is written on every DCS disconnect and once more on shutdown, so it is
written often, under interruption, and it is the only thing standing between a
crash and a lost campaign. Two failures here are unrecoverable rather than
merely annoying -- a torn save file, and a torn save file reported as a
traceback -- so both are pinned.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from campaign.__main__ import (
    DEFAULT_AUTOSAVE,
    _console_close_handler,
    _Persisting,
    _run,
    _saving_on_console_close,
    main,
    parse_args,
)
from campaign.campaign import Campaign


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


class _Counting(_Persisting):
    """Persists for real, and remembers on which thread each finished save ran."""

    def __init__(self, engine, save: Path, *, slow: float = 0.0) -> None:
        super().__init__(engine, save)
        self.saves: list[int] = []
        self.slow = slow

    def persist(self) -> bool:
        if self.slow:
            time.sleep(self.slow)
        written = super().persist()
        self.saves.append(threading.get_ident())
        return written


def _serve_for(engine: _Persisting, seconds: float, **overrides) -> None:
    """Run the server path of `python -m campaign` for `seconds`, then stop it."""
    args = argparse.Namespace(host="127.0.0.1", port=0, tick_period=0.01,
                              time_compression=0.0, autosave=DEFAULT_AUTOSAVE)
    vars(args).update(overrides)

    async def go() -> None:
        task = asyncio.create_task(_run(args, engine))
        await asyncio.sleep(seconds)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    asyncio.run(go())


class AutosaveTests(unittest.TestCase):
    """The war runs for days with DCS closed; a crash must not lose them.

    Before this the save was written only when DCS disconnected and when the
    process exited cleanly, so a crash, a power cut or a reboot lost every
    paper step since the last disconnect.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.save = self.dir / "campaign.json"

    def test_the_server_saves_on_a_period_while_it_runs(self) -> None:
        engine = _Counting(Campaign(), self.save)
        _serve_for(engine, 0.5, autosave=0.05)
        # Several periodic saves and the one on the way out, not just that one.
        self.assertGreaterEqual(len(engine.saves), 4, engine.saves)
        self.assertTrue(self.save.exists())

    def test_zero_turns_the_periodic_save_off(self) -> None:
        engine = _Counting(Campaign(), self.save)
        _serve_for(engine, 0.3, autosave=0.0)
        self.assertEqual(len(engine.saves), 1, "only the save on the way out")

    def test_a_minute_by_default_and_settable(self) -> None:
        self.assertEqual(parse_args([]).autosave, 60.0)
        self.assertEqual(parse_args(["--autosave", "15"]).autosave, 15.0)
        self.assertEqual(parse_args(["--autosave", "0"]).autosave, 0.0)
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--autosave", "-1"])


class ConsoleCloseTests(unittest.TestCase):
    """Closing the console window saves, as Ctrl+C already did.

    Windows does not raise KeyboardInterrupt for a closed window: it calls
    the process's console control handlers on a thread of its own and ends
    the process seconds later, so nothing on the way out runs.
    """

    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        # A slow save, so a handler that did not wait for it would return
        # before it finished -- and Windows would end the process mid-write.
        self.engine = _Counting(_Engine(), self.dir / "campaign.json", slow=0.2)

    def _from_windows_thread(self, event: int) -> tuple[bool, int]:
        """Call the handler from another thread while the loop runs, as Windows does.

        Returns what it answered and the loop's thread, and records how many
        saves had finished at the instant it returned in `self.at_return`.
        """

        async def go() -> tuple[bool, int]:
            loop = asyncio.get_running_loop()
            handler = _console_close_handler(loop, self.engine)

            def as_windows_calls_it() -> bool:
                handled = handler(event)
                self.at_return = len(self.engine.saves)
                return handled

            return await loop.run_in_executor(None, as_windows_calls_it), threading.get_ident()

        return asyncio.run(go())

    def test_closing_the_window_saves_on_the_loop_before_returning(self) -> None:
        for event, label in ((2, "window closed"), (5, "log off"), (6, "shutdown")):
            with self.subTest(label):
                self.engine.saves.clear()
                handled, loop_thread = self._from_windows_thread(event)
                self.assertTrue(handled)
                # Saved, and by the loop that owns the campaign, not by the
                # thread Windows called in on.
                self.assertEqual(self.engine.saves, [loop_thread])
                self.assertEqual(self.at_return, 1, "returned before the save finished")
                self.assertTrue((self.dir / "campaign.json").exists())

    def test_ctrl_c_and_ctrl_break_are_left_to_python(self) -> None:
        for event in (0, 1):
            with self.subTest(event=event):
                handled, _ = self._from_windows_thread(event)
                self.assertFalse(handled)
                self.assertEqual(self.engine.saves, [])

    def test_a_closed_loop_is_not_waited_on(self) -> None:
        loop = asyncio.new_event_loop()
        loop.close()
        self.assertFalse(_console_close_handler(loop, self.engine)(2))
        self.assertEqual(self.engine.saves, [])

    def test_it_installs_on_windows_and_is_a_no_op_elsewhere(self) -> None:
        """Not skipped off Windows: CI's Lua run allows no skip but lupa's.

        On Windows a failed install is a logged warning, so no warning means
        the handler went in (and came out); elsewhere there is nothing to
        install, and the block must simply run.
        """
        loop = asyncio.new_event_loop()
        self.addCleanup(loop.close)
        with self.assertNoLogs("campaign", level="WARNING"):
            with _saving_on_console_close(loop, self.engine):
                pass


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
