"""Sortie rate: readiness, not a count, bounds what a side can fly.

docs/design.md, "Sortie rate". A side frags a package whenever a squadron is
ready for it, so several of its packages can be open at once, under two rules
that stand in for deconfliction: never two open packages against one target,
never one squadron in two open packages. What makes a squadron ready:

  * an airframe that lands spends its squadron's turnaround on the ground;
  * a squadron flies no more aircraft sorties a day than its sustained rate
    times its airframes on strength;
  * a day-only squadron is fragged only for a time on target in daylight,
    reckoned from the campaign's local date and time and the theater's place
    on Earth by NOAA's solar-position formula.

The parameters are the order of battle's. The slice's squadrons carry values
that constrain nothing, through the same code, and the recorded wars in
tests/fixtures/pre_sead_wars.json (tests/test_single_element.py) are the
proof that the slice did not move. Syria's carry published figures.
"""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from pathlib import Path

from campaign.__main__ import build_engine, main, parse_args
from campaign.api import PAPER_STEP
from campaign.campaign import DEFAULT_START, SAVE_VERSION, Campaign
from campaign.oob import (
    SYRIA_SORTIE_RATES,
    UNCONSTRAINED,
    SideInventory,
    SortieRate,
    Squadron,
    build_slice_oob,
    build_syria_oob,
)
from campaign.planner import package_schedule, targets_by_priority
from campaign.protocol import PROTOCOL_VERSION, Hello, Message
from campaign.sun import DAY, SUNRISE_ELEVATION, is_daylight, solar_elevation
from campaign.theater import (
    SYRIA_LATITUDE,
    SYRIA_LONGITUDE,
    SYRIA_UTC_OFFSET,
    Target,
    Theater,
    build_slice_theater,
    build_syria_theater,
    enemy_of,
)

HOUR = 3_600.0

# --------------------------------------------------------------------------
# The sun
# --------------------------------------------------------------------------

#: Published sunrise and sunset for Aleppo, the theater's reference point
#: (36 deg 12' N, 37 deg 12' E), from sunrise-and-sunset.com's 2025 tables
#: (www.sunrise-and-sunset.com/en/sun/syria/aleppo/2025/<month>), local
#: civil time to the minute: (date, sunrise, sunset, UTC offset the table is
#: in). The site states "Timezone UTC+3" for the June and September tables.
#: Its December table is an hour earlier than UTC+3 would put it, and agrees
#: with UTC+2 -- Syria's winter time before it abolished daylight saving in
#: 2022, which the site still applies -- so it is checked at UTC+2.
PUBLISHED_ALEPPO = (
    (date(2025, 6, 1), "05:16", "19:42", 3.0),
    (date(2025, 6, 15), "05:14", "19:49", 3.0),
    (date(2025, 6, 30), "05:18", "19:51", 3.0),
    (date(2025, 9, 1), "06:04", "18:58", 3.0),
    (date(2025, 9, 22), "06:20", "18:27", 3.0),
    (date(2025, 9, 30), "06:26", "18:15", 3.0),
    (date(2025, 12, 1), "06:24", "16:15", 2.0),
    (date(2025, 12, 21), "06:39", "16:19", 2.0),
    (date(2025, 12, 31), "06:43", "16:25", 2.0),
)

#: NOAA gives its algorithm as good to about a minute at these latitudes,
#: and the table is rounded to the minute and may place Aleppo a few
#: kilometres from the reference point.
TOLERANCE_MINUTES = 2.0


def _local_midnight(day: date) -> float:
    return (day - date(1970, 1, 1)).days * DAY


def _crossing(lo: float, hi: float, offset: float) -> float:
    """The instant in [lo, hi] the sun crosses sunrise/sunset elevation."""

    def above(t: float) -> bool:
        return (
            solar_elevation(t, SYRIA_LATITUDE, SYRIA_LONGITUDE, offset)
            >= SUNRISE_ELEVATION
        )

    rising = not above(lo)
    for _ in range(50):
        mid = (lo + hi) / 2.0
        if above(mid) == rising:
            hi = mid
        else:
            lo = mid
    return lo


def _clock(text: str) -> float:
    hours, minutes = text.split(":")
    return int(hours) * HOUR + int(minutes) * 60.0


class TestTheSun(unittest.TestCase):
    def test_sunrise_and_sunset_at_aleppo_match_the_published_table(self):
        for day, sunrise, sunset, offset in PUBLISHED_ALEPPO:
            midnight = _local_midnight(day)
            with self.subTest(day=day.isoformat()):
                rise = _crossing(midnight + 3 * HOUR, midnight + 10 * HOUR, offset)
                fall = _crossing(midnight + 14 * HOUR, midnight + 22 * HOUR, offset)
                self.assertLessEqual(
                    abs(rise - midnight - _clock(sunrise)) / 60.0, TOLERANCE_MINUTES,
                    f"sunrise {(rise - midnight) / HOUR:.3f} h, published {sunrise}",
                )
                self.assertLessEqual(
                    abs(fall - midnight - _clock(sunset)) / 60.0, TOLERANCE_MINUTES,
                    f"sunset {(fall - midnight) / HOUR:.3f} h, published {sunset}",
                )

    def test_noon_is_day_and_midnight_is_night(self):
        midnight = _local_midnight(date(2025, 12, 21))
        args = (SYRIA_LATITUDE, SYRIA_LONGITUDE, SYRIA_UTC_OFFSET)
        self.assertTrue(is_daylight(midnight + 12.5 * HOUR, *args))
        self.assertFalse(is_daylight(midnight + 0.5 * HOUR, *args))
        # The winter sun at noon at 36 N: 90 - 36.2 - 23.4, about 30 deg.
        self.assertAlmostEqual(
            solar_elevation(midnight + 12.4 * HOUR, *args), 30.4, delta=1.0
        )

    def test_the_campaign_reads_the_sun_on_its_own_local_clock(self):
        campaign = Campaign(start=datetime(2025, 9, 22, 0, 0))
        self.assertFalse(campaign.in_daylight(0.0))
        self.assertFalse(campaign.in_daylight(6.0 * HOUR))
        self.assertTrue(campaign.in_daylight(6.5 * HOUR))
        self.assertTrue(campaign.in_daylight(18.25 * HOUR))
        self.assertFalse(campaign.in_daylight(18.75 * HOUR))
        self.assertEqual(campaign.local_day(0.0), campaign.local_day(DAY - 1.0))
        self.assertEqual(campaign.local_day(DAY), campaign.local_day(0.0) + 1)


# --------------------------------------------------------------------------
# A one-squadron war, where each rule is the only thing that can bind
# --------------------------------------------------------------------------


def _single_squadron_war(
    rate: SortieRate,
    *,
    airframes: int = 4,
    start: datetime = datetime(2025, 9, 22, 0, 0),
) -> Campaign:
    """Blue strikes a target that cannot be finished; red does not plan.

    One squadron, one target, no air defences: the slice's base and depot,
    with the depot made big enough to outlast any test. Red has a target of
    its own (so the war is a war) and no squadron, so it never plans.
    """
    slice_theater = build_slice_theater()
    incirlik = slice_theater.airbases["incirlik"]
    bassel = slice_theater.airbases["bassel_al_assad"]
    depot = slice_theater.targets["latakia_fuel_depot"]
    storage = slice_theater.targets["incirlik_munitions_storage"]
    theater = Theater(
        name="Syria",
        airbases={incirlik.id: incirlik, bassel.id: bassel},
        targets={
            depot.id: Target(**{**vars(depot), "units_initial": 10_000,
                                "units_alive": 10_000}),
            storage.id: storage,
        },
    )
    blue = SideInventory(coalition="blue")
    blue.add(
        Squadron(
            id="test_sqn",
            name="test squadron",
            coalition="blue",
            airframe="F-16C_50",
            template="F-16C_strike_jdam",
            home_base="incirlik",
            airframes_total=airframes,
            airframes_available=airframes,
            munitions_total={"GBU-38": 10_000},
            munitions_available={"GBU-38": 10_000},
            sortie_rate=rate,
        )
    )
    return Campaign(
        theater=theater,
        inventories={"blue": blue, "red": SideInventory(coalition="red")},
        start=start,
    )


def _run(campaign: Campaign, seconds: float) -> None:
    for _ in range(int(seconds / PAPER_STEP)):
        campaign.advance(PAPER_STEP)


def _by_creation(campaign: Campaign):
    return sorted(campaign.packages.values(), key=lambda p: p.id)


class TestTurnaround(unittest.TestCase):
    TURNAROUND = 2 * HOUR

    def test_a_squadron_whose_jets_are_all_turning_waits_for_them(self):
        # Two airframes: the whole squadron flies the first package, so the
        # second can only be the same jets, after they are turned round.
        campaign = _single_squadron_war(
            SortieRate(turnaround=self.TURNAROUND), airframes=2
        )
        _run(campaign, 4 * HOUR)
        first, second = _by_creation(campaign)[:2]
        landed = first.strike.t_rtb
        self.assertGreaterEqual(
            second.t_created, landed + self.TURNAROUND,
            "re-fragged before the turnaround was over",
        )
        self.assertLess(second.t_created, landed + self.TURNAROUND + 2 * PAPER_STEP,
                        "ready, and still not fragged")

    def test_while_turning_the_airframes_are_neither_ready_nor_reserved(self):
        campaign = _single_squadron_war(
            SortieRate(turnaround=self.TURNAROUND), airframes=2
        )
        squadron = campaign.inventories["blue"].squadrons["test_sqn"]
        first = None
        while first is None or first.is_open:
            campaign.advance(PAPER_STEP)
            first = first or next(iter(campaign.packages.values()), None)
        self.assertEqual(squadron.airframes_turning, 2)
        self.assertEqual(squadron.airframes_available, 0)
        self.assertEqual(squadron.open_reservations, {})
        squadron.check_invariant()

    def test_with_no_turnaround_the_same_jets_go_again_at_once(self):
        # The control: the same war, unconstrained, re-frags in the pulse the
        # jets land. Without it the test above could pass on a squadron that
        # waited for some other reason.
        campaign = _single_squadron_war(UNCONSTRAINED, airframes=2)
        _run(campaign, 2 * HOUR)
        first, second = _by_creation(campaign)[:2]
        self.assertGreaterEqual(second.t_created, first.strike.t_rtb)
        self.assertLess(second.t_created, first.strike.t_rtb + PAPER_STEP)

    def test_an_unflown_reservation_comes_back_ready(self):
        squadron = Squadron(
            id="s", name="s", coalition="blue", airframe="F-16C_50", template="t",
            home_base="b", airframes_total=4, airframes_available=4,
            munitions_total={"GBU-38": 8}, munitions_available={"GBU-38": 8},
            sortie_rate=SortieRate(turnaround=HOUR),
        )
        squadron.reserve("flew", 2, "GBU-38", 4)
        squadron.reserve("did_not", 2, "GBU-38", 4)
        squadron.release("flew", landed_at=100.0)
        squadron.release("did_not")
        self.assertEqual(squadron.airframes_available, 2)
        self.assertEqual(squadron.airframes_turning, 2)
        self.assertEqual(squadron.mature(100.0 + HOUR - 1.0), 0)
        self.assertEqual(squadron.mature(100.0 + HOUR), 2)
        self.assertEqual(squadron.airframes_available, 4)
        squadron.check_invariant()


class TestDailyLimit(unittest.TestCase):
    def test_a_squadron_flies_no_more_sorties_a_day_than_its_rate_allows(self):
        # Four airframes at 1.5 sorties a day: six aircraft sorties, three
        # two-ship packages. Short sorties and no turnaround, so without the
        # limit the squadron would fly dozens.
        rate = SortieRate(sorties_per_day=1.5)
        campaign = _single_squadron_war(rate)
        _run(campaign, 2 * DAY + HOUR)
        squadron = campaign.inventories["blue"].squadrons["test_sqn"]
        by_day: dict[int, int] = {}
        for package in campaign.packages.values():
            day = campaign.local_day(package.strike.t_takeoff)
            by_day[day] = by_day.get(day, 0) + package.strike.flight_size
        first = campaign.local_day(0.0)
        self.assertEqual(by_day.get(first), 6, by_day)
        self.assertEqual(by_day.get(first + 1), 6, by_day)
        self.assertEqual(squadron.sorties_by_day, by_day)

    def test_the_next_day_starts_a_fresh_count(self):
        campaign = _single_squadron_war(SortieRate(sorties_per_day=0.5))
        _run(campaign, DAY + HOUR)
        takeoffs = [p.strike.t_takeoff for p in _by_creation(campaign)]
        self.assertEqual(len(takeoffs), 2, "one two-ship a day from four airframes")
        self.assertEqual(campaign.local_day(takeoffs[1]), campaign.local_day(takeoffs[0]) + 1)

    def test_losses_shrink_the_limit(self):
        rate = SortieRate(sorties_per_day=1.35)
        self.assertEqual(rate.daily_limit(24), 32)
        self.assertEqual(rate.daily_limit(20), 27)
        self.assertEqual(rate.daily_limit(1), 1)
        self.assertIsNone(UNCONSTRAINED.daily_limit(24))


class TestDaylight(unittest.TestCase):
    def test_a_day_only_squadron_waits_for_the_sun(self):
        campaign = _single_squadron_war(SortieRate(day_only=True))
        _run(campaign, DAY)
        packages = _by_creation(campaign)
        self.assertTrue(packages)
        for package in packages:
            self.assertTrue(campaign.in_daylight(package.t_tot), package.id)
        # The first TOT is the first the sun allows, not the first the clock
        # does: the war starts at midnight.
        first_tot = packages[0].t_tot
        self.assertFalse(campaign.in_daylight(first_tot - 2 * PAPER_STEP))

    def test_a_night_start_tasks_nothing_and_says_nothing(self):
        # Waiting for the sun is the tempo of a war, not a side that has run
        # dry: no "Cannot task" for the side humans fly.
        campaign = _single_squadron_war(SortieRate(day_only=True))
        frames = campaign.advance(3 * HOUR)
        self.assertEqual(campaign.packages, {})
        self.assertEqual([f for f in frames if isinstance(f, Message)], [])


# --------------------------------------------------------------------------
# Syria: published figures, concurrency, whole wars
# --------------------------------------------------------------------------

WAR_LIMIT = 10 * DAY
SEEDS = (11, 12)


def new_syria(seed: int, **kwargs) -> Campaign:
    blue, red = build_syria_oob()
    return Campaign(
        theater=build_syria_theater(),
        inventories={blue.coalition: blue, red.coalition: red},
        seed=seed,
        **kwargs,
    )


def _deconfliction_problems(campaign: Campaign) -> list[str]:
    problems: list[str] = []
    for side in ("blue", "red"):
        open_packages = [
            p for p in sorted(campaign.packages.values(), key=lambda p: p.id)
            if p.is_open and p.coalition == side
        ]
        targets = [p.target_id for p in open_packages]
        if len(targets) != len(set(targets)):
            problems.append(f"{campaign.clock}: {side} has two open on one target {targets}")
        squadrons = [
            e.squadron_id for p in open_packages for e in p.elements if e.is_open
        ]
        if len(squadrons) != len(set(squadrons)):
            problems.append(f"{campaign.clock}: a {side} squadron is in two {squadrons}")
    return problems


def _conservation_problems(campaign: Campaign) -> list[str]:
    """Each squadron's books, bucket by bucket, counted here and not trusted."""
    problems: list[str] = []
    for side in sorted(campaign.inventories):
        for s in campaign.inventories[side].squadrons.values():
            reserved = sum(r.airframes for r in s.open_reservations.values())
            turning = sum(b.airframes for b in s.turning)
            held = s.airframes_available + reserved + turning + s.airframes_lost
            if held != s.airframes_total or min(s.airframes_available, turning) < 0:
                problems.append(f"{campaign.clock}: {s.id} airframes {held}")
            for munition, total in s.munitions_total.items():
                rounds = (
                    s.munitions_available.get(munition, 0)
                    + sum(r.rounds for r in s.open_reservations.values()
                          if r.munition == munition)
                    + s.munitions_expended.get(munition, 0)
                    + s.munitions_lost.get(munition, 0)
                )
                if rounds != total:
                    problems.append(f"{campaign.clock}: {s.id} {munition} {rounds}")
    return problems


class TestSyriaWars(unittest.TestCase):
    """Whole offline wars on the Syria map, checked at every step."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wars: dict[int, Campaign] = {}
        cls.deconfliction: dict[int, list[str]] = {}
        cls.conservation: dict[int, list[str]] = {}
        cls.most_open: dict[int, dict[str, int]] = {}
        cls.turned: dict[int, bool] = {}
        for seed in SEEDS:
            campaign = new_syria(seed)
            deconfliction: list[str] = []
            conservation: list[str] = []
            most = {"blue": 0, "red": 0}
            turned = False
            for _ in range(int(WAR_LIMIT / PAPER_STEP)):
                campaign.advance(PAPER_STEP)
                deconfliction += _deconfliction_problems(campaign)
                conservation += _conservation_problems(campaign)
                for side in most:
                    most[side] = max(most[side], sum(
                        1 for p in campaign.packages.values()
                        if p.is_open and p.coalition == side
                    ))
                turned = turned or any(
                    s.turning
                    for inv in campaign.inventories.values()
                    for s in inv.squadrons.values()
                )
                if campaign.war_result is not None and not any(
                    p.is_open for p in campaign.packages.values()
                ):
                    break
            cls.wars[seed] = campaign
            cls.deconfliction[seed] = deconfliction
            cls.conservation[seed] = conservation
            cls.most_open[seed] = most
            cls.turned[seed] = turned

    def test_the_wars_ended(self):
        for seed, campaign in self.wars.items():
            self.assertIsNotNone(campaign.war_result, seed)

    def test_a_side_flies_several_packages_at_once(self):
        # Not a cap of one any more: each side has more than one strike
        # squadron, and uses them together.
        for seed, most in self.most_open.items():
            self.assertGreaterEqual(most["blue"], 2, seed)
            self.assertGreaterEqual(most["red"], 2, seed)

    def test_never_two_open_packages_on_one_target_nor_a_squadron_in_two(self):
        for seed, problems in self.deconfliction.items():
            self.assertEqual(problems, [], f"seed {seed}")

    def test_every_squadron_conserves_every_airframe_and_round_throughout(self):
        for seed, problems in self.conservation.items():
            self.assertEqual(problems, [], f"seed {seed}")
            self.assertTrue(self.turned[seed], "no airframe was ever in turnaround")

    def test_no_time_on_target_ever_falls_in_darkness(self):
        for seed, campaign in self.wars.items():
            tots = [p.t_tot for p in campaign.packages.values()]
            dark = [t for t in tots if not campaign.in_daylight(t)]
            self.assertEqual(dark, [], f"seed {seed}")
            # A war long enough to have had a night in it, or the assertion
            # above proves nothing about the rule.
            days = {campaign.local_day(t) for t in tots}
            self.assertGreater(len(days), 1, seed)

    def test_no_squadron_exceeds_its_daily_limit(self):
        for seed, campaign in self.wars.items():
            for side in campaign.inventories.values():
                for squadron in side.squadrons.values():
                    limit = squadron.sortie_rate.daily_limit(squadron.airframes_total)
                    for day, flown in squadron.sorties_by_day.items():
                        self.assertLessEqual(flown, limit, (seed, squadron.id, day))

    def test_dawn_does_not_send_a_target_to_a_farther_field(self):
        # At 06:00 on the default date the nearest field's TOT on each side's
        # most important target falls before sunrise, and a farther field's
        # longer leg -- or a less important target's -- would land after it.
        # The target waits for the sun at its nearest field, and that field's
        # squadron waits with it rather than flying something else.
        campaign = new_syria(31)
        top = {
            side: targets_by_priority(campaign.theater, enemy_of(side))[0]
            for side in ("blue", "red")
        }

        def against_top() -> dict:
            found: dict = {}
            for package in sorted(campaign.packages.values(), key=lambda p: p.id):
                if package.target_id == top[package.coalition].id:
                    found.setdefault(package.coalition, package)
            return found

        while set(against_top()) != {"blue", "red"}:
            campaign.advance(PAPER_STEP)
        for side, package in against_top().items():
            target = top[side]
            nearest = campaign.theater.airbases_nearest(side, target.pos)[0]
            self.assertEqual(package.base_id, nearest.id, side)
            self.assertTrue(campaign.in_daylight(package.t_tot), side)
            # Not vacuous: at the start the nearest field could not have gone.
            self.assertFalse(
                campaign.in_daylight(package_schedule(nearest, target, 0.0)[1]), side
            )
            self.assertGreater(package.t_created, 0.0, side)

    def test_every_syria_squadron_carries_its_types_published_rate(self):
        blue, red = build_syria_oob()
        for side in (blue, red):
            for squadron in side.squadrons.values():
                self.assertEqual(
                    squadron.sortie_rate, SYRIA_SORTIE_RATES[squadron.airframe]
                )
                self.assertTrue(squadron.sortie_rate.day_only)
                self.assertGreater(squadron.sortie_rate.turnaround, 0.0)
                self.assertIsNotNone(squadron.sortie_rate.sorties_per_day)


class TestTheSliceIsUnconstrained(unittest.TestCase):
    """The slice goes through the same code with values that bind nothing.

    The proof that its behaviour did not move is tests/test_single_element.py
    replaying the recorded wars; this only pins how.
    """

    def test_every_slice_squadron_is_unconstrained(self):
        for side in build_slice_oob():
            for squadron in side.squadrons.values():
                self.assertEqual(squadron.sortie_rate, UNCONSTRAINED)
        for squadron in Campaign().inventories["blue"].squadrons.values():
            self.assertEqual(squadron.sortie_rate, UNCONSTRAINED)

    def test_a_slice_package_is_planned_in_the_dark(self):
        campaign = Campaign(start=datetime(2025, 9, 22, 0, 0))
        campaign.advance(PAPER_STEP)
        self.assertTrue(campaign.packages)
        for package in campaign.packages.values():
            self.assertFalse(campaign.in_daylight(package.t_tot))


# --------------------------------------------------------------------------
# Determinism and persistence
# --------------------------------------------------------------------------


class TestReplayAndSave(unittest.TestCase):
    def test_the_same_seed_is_the_same_war(self):
        first, second = new_syria(21), new_syria(21)
        _run(first, 1.5 * DAY)
        _run(second, 1.5 * DAY)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_a_save_carries_the_start_turnarounds_and_daily_counts(self):
        start = datetime(2025, 6, 1, 5, 30)
        original = new_syria(22, start=start)
        # Run to a moment with airframes in turnaround, so the save has
        # something there to lose.
        for _ in range(int(2 * DAY / PAPER_STEP)):
            original.advance(PAPER_STEP)
            if original.clock > 6 * HOUR and any(
                s.turning
                for inv in original.inventories.values()
                for s in inv.squadrons.values()
            ):
                break
        squadrons = [s for inv in original.inventories.values()
                     for s in inv.squadrons.values()]
        self.assertTrue(any(s.turning for s in squadrons))
        self.assertTrue(any(s.sorties_by_day for s in squadrons))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "war.json"
            original.save(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
            reloaded = Campaign.load(path)
        self.assertEqual(raw["save_version"], SAVE_VERSION)
        self.assertEqual(raw["start"], "2025-06-01T05:30:00")
        self.assertEqual(reloaded.start, start)
        self.assertEqual(reloaded.to_dict(), original.to_dict())
        _run(original, 0.5 * DAY)
        _run(reloaded, 0.5 * DAY)
        self.assertEqual(reloaded.to_dict(), original.to_dict())


# --------------------------------------------------------------------------
# The mission's clock against the campaign's
# --------------------------------------------------------------------------


def _hello(local: datetime, t: float = 0.0) -> Hello:
    start = local - datetime(1970, 1, 1)
    return Hello(
        seq=1, t=t, protocol=PROTOCOL_VERSION, theater="Syria",
        mission_start_epoch=int(start.total_seconds()),
    )


class TestMissionClock(unittest.TestCase):
    def test_a_mission_at_another_hour_is_logged(self):
        campaign = Campaign(start=datetime(2025, 9, 22, 6, 0))
        _run(campaign, HOUR)  # the campaign is at 07:00
        with self.assertLogs("campaign.campaign", level="WARNING") as logs:
            campaign.on_hello(_hello(datetime(2025, 9, 22, 9, 0)))
        text = "\n".join(logs.output)
        self.assertIn("2025-09-22 09:00:00", text)
        self.assertIn("2025-09-22 07:00:00", text)
        self.assertIn("+7200 s", text)

    def test_a_mission_at_the_campaigns_time_is_not(self):
        campaign = Campaign(start=datetime(2025, 9, 22, 6, 0))
        _run(campaign, HOUR)
        with self.assertNoLogs("campaign.campaign", level="WARNING"):
            # Mission started at 06:50 and has run ten minutes.
            campaign.on_hello(_hello(datetime(2025, 9, 22, 6, 50), t=600.0))

    def test_the_mismatch_changes_nothing_the_engine_does(self):
        a = Campaign(start=datetime(2025, 9, 22, 6, 0))
        b = Campaign(start=datetime(2025, 9, 22, 6, 0))
        with self.assertLogs("campaign.campaign", level="WARNING"):
            frames_a = a.on_hello(_hello(datetime(2025, 9, 22, 14, 0)))
        frames_b = b.on_hello(_hello(datetime(2025, 9, 22, 6, 0)))
        self.assertEqual(frames_a, frames_b)
        self.assertEqual(a.to_dict(), b.to_dict())

    def test_a_mission_with_no_start_time_cannot_be_compared(self):
        campaign = Campaign()
        with self.assertNoLogs("campaign.campaign", level="WARNING"):
            campaign.on_hello(
                Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
            )


# --------------------------------------------------------------------------
# Setting the start
# --------------------------------------------------------------------------


class TestTheStart(unittest.TestCase):
    def test_the_default_is_the_september_equinox_at_six(self):
        self.assertEqual(DEFAULT_START, datetime(2025, 9, 22, 6, 0))
        self.assertEqual(Campaign().start, DEFAULT_START)

    def test_start_is_settable_from_the_command_line(self):
        self.assertIsNone(parse_args([]).start)
        self.assertEqual(
            parse_args(["--start", "2025-06-01T05:00"]).start,
            datetime(2025, 6, 1, 5, 0),
        )
        with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            parse_args(["--start", "2025-06-01T05:00+03:00"])

    def test_a_new_war_takes_it_and_a_save_keeps_its_own(self):
        with tempfile.TemporaryDirectory() as tmp:
            save = Path(tmp) / "war.json"
            engine = build_engine(save, "syria", datetime(2025, 6, 1, 5, 0))
            self.assertEqual(engine.start, datetime(2025, 6, 1, 5, 0))
            engine.save(save)
            out, err = io.StringIO(), io.StringIO()
            with redirect_stdout(out), redirect_stderr(err), self.assertLogs(
                "campaign", level="WARNING"
            ) as logs:
                code = main(["--save", str(save), "--start", "2026-01-01T12:00",
                             "--simulate", "0", "--log-level", "WARNING"])
            self.assertEqual(code, 0, err.getvalue())
            self.assertIn("--start 2026-01-01T12:00:00 ignored", "\n".join(logs.output))
            self.assertEqual(Campaign.load(save).start, datetime(2025, 6, 1, 5, 0))

    def test_an_aware_start_is_refused(self):
        from datetime import timezone

        with self.assertRaises(ValueError):
            Campaign(start=datetime(2025, 9, 22, 6, 0, tzinfo=timezone.utc))


if __name__ == "__main__":
    unittest.main()
