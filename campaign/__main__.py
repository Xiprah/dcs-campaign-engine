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
from pathlib import Path

from campaign.api import CampaignEngine
from campaign.server import DEFAULT_HOST, DEFAULT_PORT, DEFAULT_TICK_PERIOD, CampaignServer

DEFAULT_SAVE = Path("saves/campaign.json")


# ---------------------------------------------------------------------------
# INTEGRATION SEAM
# ---------------------------------------------------------------------------
# The transport has no idea what a campaign is, and must not learn. This is the
# only place in the process that names the concrete implementation. Replace the
# body; keep the signature. Anything satisfying campaign.api.CampaignEngine
# works, which is also how tests and tools/fake_dcs.py stay honest.
def build_engine(save: Path) -> CampaignEngine:
    """Load the campaign from `save`, or start a new one if it is absent."""
    from campaign.campaign import Campaign  # INTEGRATION SEAM: the real brain

    return Campaign.load(save) if save.exists() else Campaign()


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

    def persist(self) -> None:
        """Write the campaign out, via a temp file and an atomic rename.

        This runs on every DCS disconnect, so an interrupted write is not a rare
        event -- and a save is truncated before it is rewritten, so the naive
        version leaves a zero-length `campaign.json` that the next start cannot
        load. `os.replace` makes the previous save survive anything up to and
        including the process being killed mid-write.
        """
        saver = getattr(self._engine, "save", None)
        if saver is None:
            logging.getLogger("campaign").warning(
                "engine has no save(path); campaign state will not persist"
            )
            return
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


def _load_engine_factory(spec: str) -> CampaignEngine:
    """Build an engine from a ``module:callable`` spec (tests and harnesses)."""
    module_name, _, attr = spec.partition(":")
    if not module_name or not attr:
        raise SystemExit(f"--engine wants 'module:callable', got {spec!r}")
    from importlib import import_module

    return getattr(import_module(module_name), attr)()


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
        "--tick-period",
        type=float,
        default=DEFAULT_TICK_PERIOD,
        help="seconds between engine ticks",
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
        engine, host=args.host, port=args.port, tick_period=args.tick_period
    )
    # Bind before anything is written: a busy port must not overwrite a live
    # campaign with the empty one this process just built.
    await server.start()
    try:
        await server.serve_forever()
    finally:
        await server.close()
        engine.persist()


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    try:
        engine = (
            _load_engine_factory(args.engine) if args.engine else build_engine(args.save)
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
