"""``python -m campaign`` - start the campaign engine and listen for DCS.

This module is wiring only. It parses arguments, builds an engine, and hands it
to :mod:`campaign.server`. The one interesting line is marked INTEGRATION SEAM.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

from campaign.api import PAPER_STEP, CampaignEngine
from campaign.server import (
    DEFAULT_HOST,
    DEFAULT_PORT,
    DEFAULT_TICK_PERIOD,
    DEFAULT_TIME_COMPRESSION,
    CampaignServer,
)

DEFAULT_SAVE = Path("saves/campaign.json")

#: Maps a new campaign can be started on. `syria` is the real one; `slice` is
#: the two-base test slice the unit tests are written against, kept for
#: debugging. A save carries its own theater, so this only matters when there
#: is no save yet.
THEATERS = ("syria", "slice")
DEFAULT_THEATER = "syria"


# ---------------------------------------------------------------------------
# INTEGRATION SEAM
# ---------------------------------------------------------------------------
# The transport has no idea what a campaign is, and must not learn. This is the
# only place in the process that names the concrete implementation. Replace the
# body; keep the signature. Anything satisfying campaign.api.CampaignEngine
# works, which is also how tests and tools/fake_dcs.py stay honest.
def build_engine(
    save: Path, theater: str = DEFAULT_THEATER, start: datetime | None = None
) -> CampaignEngine:
    """Load the campaign from `save`, or start a new one on `theater`.

    `start` is a new war's local date and time on the theater's clock; None
    takes the campaign's default. A save keeps its own.
    """
    from campaign.campaign import Campaign  # INTEGRATION SEAM: the real brain

    if save.exists():
        return Campaign.load(save)
    when = {} if start is None else {"start": start}
    if theater == "slice":
        # Campaign()'s own theater and order of battle, so the slice started
        # here is the one the tests build.
        return Campaign(**when)
    if theater == "syria":
        from campaign.oob import build_syria_oob
        from campaign.theater import build_syria_theater

        blue, red = build_syria_oob()
        return Campaign(
            theater=build_syria_theater(),
            inventories={blue.coalition: blue, red.coalition: red},
            **when,
        )
    raise ValueError(f"unknown theater {theater!r}; want one of {', '.join(THEATERS)}")


# ---------------------------------------------------------------------------


class _Persisting:
    """Wraps an engine so the campaign is written to disk when DCS goes away.

    `CampaignEngine` has no save hook and should not grow one: the campaign
    does not know where it lives. The process entry point does, so the
    file-path half of persistence belongs here.
    """

    def __init__(self, engine: CampaignEngine, save: Path) -> None:
        self._engine = engine
        self._save = save

    def __getattr__(self, name: str) -> object:
        return getattr(self._engine, name)

    def on_disconnect(self) -> None:
        self._engine.on_disconnect()
        self.persist()

    def persist(self) -> bool:
        """Write the campaign out, via a temp file and an atomic rename.

        This runs on every DCS disconnect, so an interrupted write is not a rare
        event -- and a save is truncated before it is rewritten, so the naive
        version leaves a zero-length `campaign.json` that the next start cannot
        load. `os.replace` makes the previous save survive anything up to and
        including the process being killed mid-write.

        Returns whether the save was written. The server path only logs a
        failure -- it must not take the transport down -- but `--simulate`
        exits non-zero on it, since writing the save is its whole job.
        """
        saver = getattr(self._engine, "save", None)
        if saver is None:
            logging.getLogger("campaign").warning(
                "engine has no save(path); campaign state will not persist"
            )
            return False
        # Same directory, so the replace is a rename within one filesystem.
        staging = self._save.with_name(self._save.name + ".partial")
        try:
            saver(staging)
            os.replace(staging, self._save)
            logging.getLogger("campaign").info("campaign saved to %s", self._save)
        except Exception:
            logging.getLogger("campaign").exception("saving to %s failed", self._save)
            with contextlib.suppress(OSError):
                staging.unlink()
            return False
        return True


def _load_engine_factory(spec: str) -> CampaignEngine:
    """Build an engine from a ``module:callable`` spec (tests and harnesses)."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"--engine wants 'module:callable', got {spec!r}")
    from importlib import import_module

    return getattr(import_module(module_name), attr)()


def _non_negative(text: str) -> float:
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"want a number, got {text!r}") from None
    if not (0.0 <= value < float("inf")):
        raise argparse.ArgumentTypeError(f"want a finite number >= 0, got {text!r}")
    return value


def _local_datetime(text: str) -> datetime:
    try:
        value = datetime.fromisoformat(text)
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"want a local date and time like 2025-09-22T06:00, got {text!r}"
        ) from None
    if value.tzinfo is not None:
        raise argparse.ArgumentTypeError(
            f"want the theater's local time with no UTC offset, got {text!r}"
        )
    return value


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m campaign",
        description="Dynamic campaign engine for DCS World. Listens; DCS connects out.",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="bind address")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help="bind port")
    parser.add_argument(
        "--save", type=Path, default=DEFAULT_SAVE, help="campaign save file (JSON)"
    )
    parser.add_argument(
        "--theater",
        choices=THEATERS,
        default=None,
        help=f"map to start a new campaign on (default {DEFAULT_THEATER}); "
        "ignored when the save already exists, since a save carries its own",
    )
    parser.add_argument(
        "--start",
        type=_local_datetime,
        default=None,
        metavar="YYYY-MM-DDTHH:MM",
        help="local date and time on the theater's clock at which a new "
        "campaign starts (default 2025-09-22T06:00); ignored when the save "
        "already exists, since a save carries its own",
    )
    parser.add_argument(
        "--tick-period",
        type=float,
        default=DEFAULT_TICK_PERIOD,
        help="seconds between engine ticks",
    )
    parser.add_argument(
        "--time-compression",
        type=_non_negative,
        default=DEFAULT_TIME_COMPRESSION,
        metavar="N",
        help="campaign seconds per wall second while DCS is not connected; "
        "1 is real time, 0 stops the war until DCS connects",
    )
    parser.add_argument(
        "--simulate",
        type=_non_negative,
        default=None,
        metavar="SECONDS",
        help="load the save, advance the war SECONDS campaign seconds with no "
        f"server, save and exit (whole {PAPER_STEP:g}s paper steps; any "
        "remainder is not run)",
    )
    parser.add_argument(
        "--engine",
        default=None,
        help="override the integration seam with a 'module:callable' factory",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="logging verbosity",
    )
    return parser.parse_args(argv)


async def _run(args: argparse.Namespace, engine: _Persisting) -> None:
    server = CampaignServer(
        engine,
        host=args.host,
        port=args.port,
        tick_period=args.tick_period,
        time_compression=args.time_compression,
    )
    # Bind before anything is written: a busy port must not overwrite a live
    # campaign with the empty one this process just built.
    await server.start()
    try:
        await server.serve_forever()
    finally:
        await server.close()
        engine.persist()


def _simulate(seconds: float, engine: _Persisting) -> int:
    """Fast-forward the war with no server, then save it.

    The same fixed steps the transport would deliver at any compression, so a
    war caught up here is the war that running the server for as long would
    have produced -- minus whatever DCS would have changed, since nothing is
    connected.
    """
    steps = int(seconds // PAPER_STEP)
    before = getattr(engine, "clock", None)
    for _ in range(steps):
        engine.advance(PAPER_STEP)
    if not engine.persist():
        print(f"simulated {steps} paper step(s), but the save was not written",
              file=sys.stderr)
        return 1
    after = getattr(engine, "clock", None)
    clock = (
        f"; campaign clock {before:.0f}s -> {after:.0f}s"
        if isinstance(before, (int, float)) and isinstance(after, (int, float))
        else ""
    )
    print(f"simulated {steps} paper step(s) of {PAPER_STEP:g}s{clock}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    if args.theater is not None and args.save.exists() and not args.engine:
        logging.getLogger("campaign").warning(
            "--theater %s ignored: continuing the war in %s, which carries its own",
            args.theater,
            args.save,
        )
    if args.start is not None and args.save.exists() and not args.engine:
        logging.getLogger("campaign").warning(
            "--start %s ignored: continuing the war in %s, which carries its own",
            args.start.isoformat(),
            args.save,
        )
    try:
        engine = (
            _load_engine_factory(args.engine)
            if args.engine
            else build_engine(args.save, args.theater or DEFAULT_THEATER, args.start)
        )
    except ImportError as exc:
        print(
            f"cannot build a campaign engine: {exc}\n"
            "The integration seam in campaign/__main__.py is not wired up yet. "
            "Pass --engine module:callable to run against another implementation.",
            file=sys.stderr,
        )
        return 2
    except (ValueError, KeyError, TypeError) as exc:
        # json.JSONDecodeError is a ValueError; a save from another format
        # version raises ValueError, and a truncated one KeyError. All three
        # mean the same thing to an operator, and a bare traceback tells them
        # nothing about what to do next.
        print(
            f"cannot read the campaign save at {args.save}: {exc}\n"
            "The file is corrupt or from an incompatible save version. Move it "
            "aside to start a new campaign, or restore a backup.",
            file=sys.stderr,
        )
        return 2
    except OSError as exc:
        print(f"cannot read the campaign save at {args.save}: {exc}", file=sys.stderr)
        return 2
    args.save.parent.mkdir(parents=True, exist_ok=True)
    if args.simulate is not None:
        return _simulate(args.simulate, _Persisting(engine, args.save))
    try:
        asyncio.run(_run(args, _Persisting(engine, args.save)))
    except KeyboardInterrupt:
        pass
    except OSError as exc:
        print(f"cannot listen on {args.host}:{args.port}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
