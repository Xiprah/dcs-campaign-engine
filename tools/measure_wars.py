#!/usr/bin/env python3
"""Fly offline Syria wars and say what they cost, so a measurement can be repeated.

Every "measured, not asserted" table in docs/design.md is a batch of offline
wars, nobody connected, at the shipped figures. This is the script that flies
them, so the next measurement is the same measurement:

    python tools/measure_wars.py --seeds 0-49
    python tools/measure_wars.py --seeds 0-49 --json wars.json
    python tools/measure_wars.py --seeds 0-49 --root path/to/older/checkout

`--root` measures another checkout's engine with this script, so a change is
compared against the engine before it on the same seeds and the same
definitions. Figures an older engine cannot report (it has no typed sites, so
no radars) print as "-".

Definitions, which every table made with this script shares:

* A war is flown from a new campaign until it has a result and no package is
  still open, or `--days` of campaign time, whichever is first.
* A package counts if it reached its TOT: its TOT is not later than the end
  of the flying, and it was not stood down. Its losses are every element's.
* Depth is the target's size, which on the Syria map is its depth (docs/
  design.md, "Theater: the Syria map"): 4 units shallow, 12 middle, 24 deep.
* A package is escorted if it was fragged with a SEAD element.
* A quarter is a quarter of the war's length, by TOT; anything after the
  result is in the last quarter.
* Site counts are of the *owner's* sites. A radar kill is a radar the site
  lost, a repair one it regained, a shutdown a new or extended `dark_until`.
  "Engaging" is the mean share of a side's sites that could engage, sampled
  every paper step.

It reads the campaign and never changes it, so a war flown here is the war
`python -m campaign --simulate` flies.
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Any

SIDES = ("blue", "red")
DEPTHS = ("shallow", "middle", "deep")
DEPTH_OF_SIZE = {4: "shallow", 12: "middle", 24: "deep"}


def _bootstrap(root: str | None) -> None:
    path = str(Path(root).resolve()) if root else str(Path(__file__).resolve().parents[1])
    if path not in sys.path:
        sys.path.insert(0, path)


def new_syria_war(seed: int):
    """The campaign `python -m campaign --theater syria` starts, at `seed`."""
    from campaign.campaign import Campaign
    from campaign.oob import build_syria_oob
    from campaign.theater import build_syria_theater

    blue, red = build_syria_oob()
    return Campaign(
        theater=build_syria_theater(),
        inventories={blue.coalition: blue, red.coalition: red},
        seed=seed,
    )


def _radars(site) -> int | None:
    return getattr(site, "radars_alive", None)


def _engages(site, t: float) -> bool:
    if site.destroyed:
        return False
    if hasattr(site, "engages_at"):
        return site.engages_at(t)
    return getattr(site, "can_engage", True)


def fly(campaign, *, max_days: float = 10.0) -> dict[str, Any]:
    """Fly `campaign` to its end and measure it. Reads, never writes."""
    from campaign.api import PAPER_STEP
    from campaign.attrition import KIND_FLIGHT
    from campaign.planner import ABORTED

    sites = list(campaign.theater.threats.values())
    radars = {s.id: _radars(s) for s in sites}
    dark = {s.id: getattr(s, "dark_until", None) for s in sites}
    site_events = {c: {"radar_kills": 0, "repairs": 0, "shutdowns": 0} for c in SIDES}
    engaging = {c: 0.0 for c in SIDES}
    owned = {c: sum(1 for s in sites if s.coalition == c) for c in SIDES}
    samples = 0
    for _ in range(int(max_days * 86_400.0 / PAPER_STEP)):
        campaign.advance(PAPER_STEP)
        samples += 1
        for site in sites:
            now = _radars(site)
            before = radars[site.id]
            if now is not None and before is not None:
                if now < before:
                    site_events[site.coalition]["radar_kills"] += before - now
                elif now > before:
                    site_events[site.coalition]["repairs"] += now - before
            radars[site.id] = now
            until = getattr(site, "dark_until", None)
            if until is not None and until != dark[site.id]:
                site_events[site.coalition]["shutdowns"] += 1
            dark[site.id] = until
        for side in SIDES:
            if owned[side]:
                engaging[side] += sum(
                    1 for s in sites if s.coalition == side and _engages(s, campaign.clock)
                ) / owned[side]
        if campaign.war_result is not None and not any(
            p.is_open for p in campaign.packages.values()
        ):
            break

    end = campaign.clock
    result = campaign.war_result
    war_end = result["t"] if result is not None else end
    defeated = sorted(result["defeated"]) if result is not None else []
    if result is None:
        winner = None
    elif len(defeated) == 1:
        winner = next(c for c in SIDES if c not in defeated)
    else:
        winner = "draw"

    lost_by_spawn: dict[str, int] = {}
    for loss in campaign.tracker.losses:
        if loss.entity_kind == KIND_FLIGHT:
            lost_by_spawn[loss.spawn_id] = lost_by_spawn.get(loss.spawn_id, 0) + 1

    targets = campaign.theater.targets
    sides: dict[str, Any] = {}
    for side in SIDES:
        by_depth = {d: [0, 0] for d in DEPTHS}       # packages, airframes lost
        by_quarter = [[0, 0] for _ in range(4)]
        by_escort = [[0, 0], [0, 0]]                 # alone, escorted
        packages = escorted = strike_lost = sead_lost = 0
        for package in campaign.packages.values():
            if package.coalition != side or package.state == ABORTED or package.t_tot > end:
                continue
            lost = {e.role: lost_by_spawn.get(e.spawn_id, 0) for e in package.elements}
            total = sum(lost.values())
            strike_lost += lost.get("strike", 0)
            sead_lost += lost.get("sead", 0)
            packages += 1
            escorted += package.sead is not None
            target = targets.get(package.target_id)
            depth = DEPTH_OF_SIZE.get(target.units_initial) if target else None
            if depth:
                by_depth[depth][0] += 1
                by_depth[depth][1] += total
            quarter = min(3, int(4 * package.t_tot / war_end)) if war_end > 0 else 3
            by_quarter[quarter][0] += 1
            by_quarter[quarter][1] += total
            by_escort[package.sead is not None][0] += 1
            by_escort[package.sead is not None][1] += total
        inventory = campaign.inventories[side]
        sides[side] = {
            "packages": packages,
            "escorted": escorted,
            "airframes_lost": sum(s.airframes_lost for s in inventory.squadrons.values()),
            "strike_lost": strike_lost,
            "sead_lost": sead_lost,
            "by_depth": by_depth,
            "by_quarter": by_quarter,
            "by_escort": by_escort,
            "strike_airframes_left": sum(
                s.airframes_total - s.airframes_lost
                for s in inventory.squadrons.values()
                if not set(s.munitions_total) <= _anti_radiation()
            ),
            "enemy_targets_destroyed": sum(
                1 for t in targets.values() if t.coalition != side and t.destroyed
            ),
            "own_sites_destroyed": sum(1 for s in sites if s.coalition == side and s.destroyed),
            "own_sites": owned[side],
            "own_sites_engaging": engaging[side] / samples if samples else 0.0,
            **site_events[side],
            "typed": any(r is not None for r in radars.values()),
        }
    return {
        "war_hours": war_end / 3600.0,
        "flown_hours": end / 3600.0,
        "winner": winner,
        "defeated": defeated,
        "sides": sides,
    }


def _anti_radiation() -> frozenset[str]:
    from campaign.oob import ANTI_RADIATION_MUNITIONS

    return frozenset(ANTI_RADIATION_MUNITIONS)


def _fly_seed(job: tuple[int, str | None, float]) -> dict[str, Any]:
    seed, root, max_days = job
    _bootstrap(root)
    return {"seed": seed, **fly(new_syria_war(seed), max_days=max_days)}


def _seeds(spec: str) -> list[int]:
    out: list[int] = []
    for part in spec.split(","):
        if "-" in part:
            lo, hi = part.split("-")
            out.extend(range(int(lo), int(hi) + 1))
        else:
            out.append(int(part))
    return out


def _median_range(values: list[float], fmt: str = "{:.0f}") -> str:
    if not values:
        return "-"
    return f"{fmt.format(statistics.median(values))} ({fmt.format(min(values))}-{fmt.format(max(values))})"


def _rate(pairs: list[list[int]]) -> str:
    packages = sum(p for p, _ in pairs)
    lost = sum(n for _, n in pairs)
    return f"{lost / packages:.2f}" if packages else "-"


def report(wars: list[dict[str, Any]]) -> str:
    """The markdown table docs/design.md quotes."""
    n = len(wars)
    wins = {c: sum(1 for w in wars if w["winner"] == c) for c in SIDES}
    draws = sum(1 for w in wars if w["winner"] == "draw")
    unfinished = sum(1 for w in wars if w["winner"] is None)
    seeds = [w["seed"] for w in wars]
    lines = [
        f"{n} offline Syria wars, seeds {min(seeds)} to {max(seeds)}, nobody connected.",
        "",
        "| | blue | red |",
        "|---|---|---|",
        f"| war length, h, median (range) | {_median_range([w['war_hours'] for w in wars], '{:.1f}')} | |",
        f"| wins | {wins['blue']} | {wins['red']} |",
    ]
    if draws or unfinished:
        lines.append(f"| draws / unfinished | {draws} / {unfinished} | |")

    def row(label: str, cell) -> None:
        lines.append(f"| {label} | {cell('blue')} | {cell('red')} |")

    def per(side: str, key: str) -> list[float]:
        return [w["sides"][side][key] for w in wars]

    row("packages that reached their TOT, median (range)",
        lambda c: _median_range(per(c, "packages")))
    row("SEAD-escorted, mean", lambda c: f"{statistics.mean(per(c, 'escorted')):.1f}")
    row("airframes lost, median (range)", lambda c: _median_range(per(c, "airframes_lost"), "{:g}"))
    row("airframes lost, total: strike / SEAD",
        lambda c: f"{sum(per(c, 'strike_lost'))} / {sum(per(c, 'sead_lost'))}")
    row("wars with no airframe lost",
        lambda c: str(sum(1 for v in per(c, "airframes_lost") if v == 0)))
    row("lost per package: shallow / middle / deep", lambda c: " / ".join(
        _rate([w["sides"][c]["by_depth"][d] for w in wars]) for d in DEPTHS))
    row("lost per package by quarter of the war", lambda c: " / ".join(
        _rate([w["sides"][c]["by_quarter"][q] for w in wars]) for q in range(4)))
    row("lost per package: sent alone / escorted (packages)", lambda c: " / ".join(
        f"{_rate([w['sides'][c]['by_escort'][e] for w in wars])}"
        f" ({sum(w['sides'][c]['by_escort'][e][0] for w in wars)})" for e in (0, 1)))
    row("strike airframes left at the end, median (min)", lambda c: (
        f"{statistics.median(per(c, 'strike_airframes_left')):g}"
        f" ({min(per(c, 'strike_airframes_left'))})"))
    row("enemy targets destroyed, median",
        lambda c: f"{statistics.median(per(c, 'enemy_targets_destroyed')):g} of 7")
    row("own sites destroyed, median",
        lambda c: f"{statistics.median(per(c, 'own_sites_destroyed')):g} of {wars[0]['sides'][c]['own_sites']}")
    typed = all(w["sides"][c]["typed"] for w in wars for c in SIDES)

    def typed_median(key: str):
        return lambda c: f"{statistics.median(per(c, key)):g}" if typed else "-"

    row("own radars destroyed, median", typed_median("radar_kills"))
    row("own radars repaired, median", typed_median("repairs"))
    row("own shutdowns, median", typed_median("shutdowns"))
    row("own sites able to engage, mean share of the war",
        lambda c: f"{100 * statistics.mean(per(c, 'own_sites_engaging')):.0f}%")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--seeds", default="0-49", help="e.g. 0-49 or 0,3,7 (default 0-49)")
    parser.add_argument("--root", help="measure the engine in this checkout instead")
    parser.add_argument("--days", type=float, default=10.0,
                        help="stop a war that has not ended after this long (default 10)")
    parser.add_argument("--jobs", type=int, default=None, help="worker processes")
    parser.add_argument("--json", type=Path, help="also write every war's figures here")
    args = parser.parse_args(argv)
    jobs = [(seed, args.root, args.days) for seed in _seeds(args.seeds)]
    with ProcessPoolExecutor(max_workers=args.jobs) as pool:
        wars = sorted(pool.map(_fly_seed, jobs), key=lambda w: w["seed"])
    if args.json:
        args.json.write_text(json.dumps(wars, indent=1), encoding="utf-8")
    print(report(wars))
    return 0


if __name__ == "__main__":
    _bootstrap(None)
    sys.exit(main())
