"""The Syria theater: a real map, sized so a war has an arc.

`theater.build_syria_theater` and `oob.build_syria_oob`, beside the slice
every other test is written against. Nothing here pins an outcome to a seed:
how many dice a TOT draws is not this file's to fix, so every assertion is
one that holds whatever the dice say.

What is pinned:

  * every target and site has a client template that can hold it, the
    offline harness accepts it as the client would, and the template
    validator has a case for it that agrees with the client;
  * every position is on the Syria map, the airbases are pydcs's numbers on
    the right axes, and known distances come out right;
  * the planner flies each target from the nearest base, never strikes a
    threat site, and both sides fly from more than one base;
  * every squadron's books balance through a whole offline war, for several
    seeds;
  * a seed is a war, and a save of one continues it;
  * `python -m campaign --theater syria|slice` starts, and `--simulate`
    works on each.
"""

from __future__ import annotations

import asyncio
import io
import re
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from campaign.__main__ import DEFAULT_THEATER, build_engine, main, parse_args
from campaign.api import PAPER_STEP
from campaign.campaign import Campaign
from campaign.oob import ANTI_RADIATION_MUNITIONS, build_syria_oob
from campaign.planner import FLIGHT_SIZE, SEAD_FLIGHT_SIZE, select_target
from campaign.server import CampaignServer
from campaign.theater import (
    SYRIA_BASSEL,
    SYRIA_INCIRLIK,
    Theater,
    build_slice_theater,
    build_syria_theater,
    enemy_of,
    ground_distance,
)
from tools.fake_dcs import TEMPLATE_CAPACITY, Config, FakeDCS

try:
    import lupa  # noqa: F401

    HAVE_LUPA = True
except ImportError:  # pragma: no cover - exercised on hosts without lupa
    HAVE_LUPA = False

if HAVE_LUPA:
    from tests.dcsmock import DCSMock, lua_to_py
    from tests.test_validation import ValidatorRun

requires_lua = unittest.skipUnless(
    HAVE_LUPA, "lupa is not installed; the Lua client cannot be executed"
)

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "mission" / "campaign_client.lua"
VALIDATOR = ROOT / "mission" / "validate_templates.lua"

DAY = 86_400.0
#: Long enough for any war on this map to end: they have been measured at
#: under two days.
WAR_LIMIT = 5 * DAY
#: Several seeds, none chosen for what it does: every assertion below has to
#: hold for each.
SEEDS = (1, 2, 3, 4)

#: The box spanned by the DCS Syria map's own airfields in pydcs's terrain
#: data -- Nevatim furthest south, Kahramanmaras furthest north, Gazipasa
#: furthest west, Deir ez-Zor furthest east -- with 30 km to spare. Vec3 x
#: north, z east.
MAP_X = (-450_000.0, 310_000.0)
MAP_Z = (-350_000.0, 420_000.0)


def new_syria(seed: int = 1) -> Campaign:
    blue, red = build_syria_oob()
    return Campaign(
        theater=build_syria_theater(),
        inventories={blue.coalition: blue, red.coalition: red},
        seed=seed,
    )


def fight(campaign: Campaign, limit: float = WAR_LIMIT) -> None:
    """Advance offline until the war is over and nothing is in the air."""
    for _ in range(int(limit / PAPER_STEP)):
        campaign.advance(PAPER_STEP)
        if campaign.war_result is not None and not any(
            p.is_open for p in campaign.packages.values()
        ):
            return


def entities(theater: Theater):
    return list(theater.targets.values()) + list(theater.threats.values())


def lua_table_keys(path: Path, table: str) -> set[str]:
    """The `["name"] = {` keys of a top-level Lua table, read as text.

    Enough to say a template is declared, without lupa. The executed checks
    below say what it declares.
    """
    text = path.read_text(encoding="utf-8")
    start = text.index(f"{table} = {{")
    depth = 0
    for index in range(start, len(text)):
        if text[index] == "{":
            depth += 1
        elif text[index] == "}":
            depth -= 1
            if depth == 0:
                body = text[start:index]
                break
    return set(re.findall(r'^\s{4,8}\["([^"]+)"\]\s*=\s*\{', body, re.MULTILINE))


def validator_template_order() -> list[str]:
    text = VALIDATOR.read_text(encoding="utf-8")
    block = re.search(r"template_order\s*=\s*\{(.*?)\}", text, re.DOTALL)
    assert block is not None, "validate_templates.lua has no template_order"
    return re.findall(r'"([^"]+)"', block.group(1))


# --------------------------------------------------------------------------
# Content: every entity can be built, by the client and by the harness
# --------------------------------------------------------------------------


class TestEveryEntityHasATemplate(unittest.TestCase):
    """Without lupa: what can be read from the files and the harness."""

    def setUp(self) -> None:
        self.theater = build_syria_theater()
        self.harness = FakeDCS(Config())

    def test_every_target_and_site_template_is_declared_by_the_client(self):
        declared = lua_table_keys(CLIENT, "local TEMPLATES")
        missing = sorted(
            {e.template for e in entities(self.theater)} - declared
        )
        self.assertEqual(missing, [], "spawns the client would refuse")

    def test_every_squadron_template_is_declared_by_the_client(self):
        declared = lua_table_keys(CLIENT, "local TEMPLATES")
        blue, red = build_syria_oob()
        used = {
            s.template
            for side in (blue, red)
            for s in side.squadrons.values()
        }
        self.assertEqual(sorted(used - declared), [])

    def test_the_validator_has_a_case_for_every_template(self):
        order = validator_template_order()
        missing = sorted({e.template for e in entities(self.theater)} - set(order))
        self.assertEqual(missing, [], "templates the validator never probes")
        self.assertEqual(len(order), len(set(order)), "a template probed twice")

    def test_the_harness_accepts_every_entity_whole(self):
        refused = [
            (e.id, e.template, e.units_initial)
            for e in entities(self.theater)
            if self.harness._capacity_for(e.template, e.category) < e.units_initial
        ]
        self.assertEqual(refused, [], "fake_dcs would refuse these spawns")

    def test_every_template_bigger_or_smaller_than_the_default_is_listed(self):
        # The harness falls back to four units for anything on the ground. A
        # site template that holds three would be accepted at four here and
        # refused by the client: the harness may be no more forgiving.
        for site in self.theater.threats.values():
            if site.units_initial != 4:
                self.assertIn(site.template, TEMPLATE_CAPACITY, site.id)


@requires_lua
class TestTheClientAndValidatorAgree(unittest.TestCase):
    """Executed: the client's own table, the harness and the validator."""

    @classmethod
    def setUpClass(cls) -> None:
        mock = DCSMock(port=1, tick=0.1, autostart=False)
        cls.addClassCleanup(mock.stop)
        mock.load_client()
        cls.templates = lua_to_py(mock.lua.globals().CampaignClient.TEMPLATES)
        cls.theater = build_syria_theater()

    def test_every_template_holds_every_entity_built_from_it(self):
        for entity in entities(self.theater):
            with self.subTest(entity=entity.id):
                template = self.templates.get(entity.template)
                self.assertIsNotNone(template, entity.template)
                self.assertGreaterEqual(template["count"], entity.units_initial)

    def test_targets_are_statics_and_sites_are_ground_groups(self):
        for target in self.theater.targets.values():
            self.assertTrue(self.templates[target.template].get("static"), target.id)
        for site in self.theater.threats.values():
            self.assertFalse(self.templates[site.template].get("static"), site.id)
            # A battery spawns whole; a template that held more would leave
            # the engine counting units the sim never built.
            self.assertEqual(self.templates[site.template]["count"], site.units_initial)

    def test_the_harness_capacity_is_the_clients_count(self):
        harness = FakeDCS(Config())
        for entity in entities(self.theater):
            with self.subTest(template=entity.template):
                self.assertEqual(
                    harness._capacity_for(entity.template, entity.category),
                    self.templates[entity.template]["count"],
                )

    def test_every_squadron_template_holds_its_element(self):
        blue, red = build_syria_oob()
        for side in (blue, red):
            for squadron in side.squadrons.values():
                arms = set(squadron.munitions_total) & ANTI_RADIATION_MUNITIONS
                size = SEAD_FLIGHT_SIZE if arms else FLIGHT_SIZE
                self.assertGreaterEqual(
                    self.templates[squadron.template]["count"], size, squadron.id
                )

    def test_the_validator_probes_every_syria_template_and_does_not_drift(self):
        run = ValidatorRun(with_client=True)
        self.addCleanup(run.close)
        results = {r["id"]: r for r in run.run_now()["results"]}
        self.assertEqual(results["drift.templates"]["status"], "OK",
                         results["drift.templates"].get("error"))
        for template in sorted({e.template for e in entities(self.theater)}):
            with self.subTest(template=template):
                case = results.get(f"template.{template}")
                self.assertIsNotNone(case, "no validator case")
                self.assertEqual(case["status"], "OK", case.get("error"))


# --------------------------------------------------------------------------
# Geography
# --------------------------------------------------------------------------


class TestTheMap(unittest.TestCase):
    def setUp(self) -> None:
        self.theater = build_syria_theater()

    def test_airbases_are_pydcs_points_with_x_north_and_y_east(self):
        # pydcs Incirlik: Point(221207.773438, -35240.347656); Bassel_Al_Assad:
        # Point(42236.566406, 5836.231689). Its y is the engine's z.
        self.assertEqual(SYRIA_INCIRLIK, (221207.773438, 0.0, -35240.347656))
        self.assertEqual(SYRIA_BASSEL, (42236.566406, 0.0, 5836.231689))

    def test_every_position_is_on_the_syria_map(self):
        positions = [(b.id, b.pos) for b in self.theater.airbases.values()]
        positions += [(e.id, e.pos) for e in entities(self.theater)]
        for name, pos in positions:
            with self.subTest(entity=name):
                self.assertTrue(MAP_X[0] <= pos[0] <= MAP_X[1], pos)
                self.assertTrue(MAP_Z[0] <= pos[2] <= MAP_Z[1], pos)
                self.assertEqual(pos[1], 0.0, "on the ground")

    def test_known_distances(self):
        bases = self.theater.airbases
        incirlik_to_bassel = ground_distance(SYRIA_INCIRLIK, SYRIA_BASSEL)
        self.assertAlmostEqual(incirlik_to_bassel / 1000.0, 183.6, delta=0.5)
        hatay_to_gaziantep = ground_distance(bases["hatay"].pos, bases["gaziantep"].pos)
        self.assertAlmostEqual(hatay_to_gaziantep / 1000.0, 124.8, delta=0.5)
        kuweires_to_abu = ground_distance(
            bases["kuweires"].pos, bases["abu_al_duhur"].pos
        )
        self.assertAlmostEqual(kuweires_to_abu / 1000.0, 66.4, delta=0.5)

    def test_the_axes_are_not_swapped(self):
        # Distances cannot tell a swapped axis; directions can. Incirlik is
        # north-west of Bassel al-Assad, Gaziantep east of Hatay.
        bases = self.theater.airbases
        self.assertGreater(SYRIA_INCIRLIK[0], SYRIA_BASSEL[0], "Incirlik is north")
        self.assertLess(SYRIA_INCIRLIK[2], SYRIA_BASSEL[2], "Incirlik is west")
        self.assertGreater(bases["gaziantep"].pos[2], bases["hatay"].pos[2])

    def test_blue_is_on_the_turkish_side_and_red_on_the_syrian(self):
        def norths(side):
            return [b.pos[0] for b in self.theater.airbases_of(side)] + [
                e.pos[0] for e in entities(self.theater) if e.coalition == side
            ]

        self.assertGreater(min(norths("blue")), max(norths("red")))

    def test_every_target_and_site_is_placed_off_a_dcs_airbase(self):
        import campaign.theater as theater_module

        anchors = [
            value
            for name, value in vars(theater_module).items()
            if name.startswith("SYRIA_") and isinstance(value, tuple) and len(value) == 3
        ]
        self.assertGreater(len(anchors), 6)
        for entity in entities(self.theater):
            nearest = min(ground_distance(entity.pos, a) for a in anchors)
            self.assertLessEqual(nearest, 10_000.0, entity.id)

    def test_each_side_has_enough_war_to_fight(self):
        for side in ("blue", "red"):
            targets = self.theater.targets_of(side)
            self.assertTrue(5 <= len(targets) <= 8, side)
            self.assertGreater(len({t.priority for t in targets}), 3, side)
            sites = [s for s in self.theater.threats.values() if s.coalition == side]
            self.assertGreaterEqual(len({s.template for s in sites}), 2, side)
            self.assertGreaterEqual(len({s.engagement_radius for s in sites}), 2, side)

    def test_deeper_targets_are_later_and_better_defended(self):
        # The arc: in the order the planner takes them (priority), each
        # target is at least as far from the nearest enemy base as the
        # shallowest one, and the last is under more envelopes than the first.
        for side in ("blue", "red"):
            enemy = enemy_of(side)
            ordered = sorted(
                self.theater.targets_of(side), key=lambda t: (-t.priority, t.id)
            )

            def depth(target):
                return ground_distance(
                    self.theater.airbases_nearest(enemy, target.pos)[0].pos, target.pos
                )

            def envelopes(target):
                base = self.theater.airbases_nearest(enemy, target.pos)[0]
                return len(self.theater.live_threats_along(side, base.pos, target.pos))

            self.assertGreater(depth(ordered[-1]), depth(ordered[0]), side)
            self.assertGreater(envelopes(ordered[-1]), envelopes(ordered[0]), side)
            self.assertEqual(envelopes(ordered[0]), 0, side)


# --------------------------------------------------------------------------
# Planning on this map
# --------------------------------------------------------------------------


class TestPlanning(unittest.TestCase):
    def test_airbases_nearest_orders_by_ground_distance(self):
        theater = build_syria_theater()
        target = theater.targets["gaziantep_fuel_storage"]
        ordered = theater.airbases_nearest("red", target.pos)
        distances = [ground_distance(b.pos, target.pos) for b in ordered]
        self.assertEqual(distances, sorted(distances))
        self.assertEqual({b.coalition for b in ordered}, {"red"})

    def test_each_side_flies_its_first_target_from_the_base_nearest_it(self):
        campaign = new_syria()
        campaign.advance(PAPER_STEP)
        by_side = {p.coalition: p for p in campaign.packages.values()}
        self.assertEqual(set(by_side), {"blue", "red"})
        for side, package in by_side.items():
            target = campaign.theater.targets[package.target_id]
            nearest = campaign.theater.airbases_nearest(side, target.pos)[0]
            self.assertEqual(package.base_id, nearest.id, side)
        # Red's first target is nearest Kuweires, the last of its fields by
        # id: a planner that tried bases in id order would send it from Abu
        # al-Duhur.
        self.assertEqual(by_side["red"].base_id, "kuweires")

    def test_the_planner_never_selects_a_threat_site(self):
        theater = build_syria_theater()
        for side in ("blue", "red"):
            for target in theater.targets_of(side):
                target.units_alive = 0
            self.assertIsNone(
                select_target(theater, side),
                "only threat sites are left standing, and none is a target",
            )


class TestLongWars(unittest.TestCase):
    """Whole offline wars, one per seed, read every way that holds for all."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.wars: dict[int, Campaign] = {}
        cls.problems: dict[int, list[str]] = {}
        for seed in SEEDS:
            campaign = new_syria(seed)
            problems: list[str] = []
            for step in range(int(WAR_LIMIT / PAPER_STEP)):
                campaign.advance(PAPER_STEP)
                if step % 12 == 0:
                    problems += _conservation_problems(campaign)
                if campaign.war_result is not None and not any(
                    p.is_open for p in campaign.packages.values()
                ):
                    break
            problems += _conservation_problems(campaign)
            cls.wars[seed] = campaign
            cls.problems[seed] = problems

    def test_every_squadron_on_both_sides_conserves_its_stock_throughout(self):
        for seed, problems in self.problems.items():
            self.assertEqual(problems, [], f"seed {seed}")

    def test_stock_is_reserved_exactly_for_the_elements_still_open(self):
        # Whether or how a war ends is the dice's business; that a closed
        # element leaves nothing reserved behind it is not.
        for seed, campaign in self.wars.items():
            open_elements = sorted(
                (e.squadron_id, e.reservation_id)
                for p in campaign.packages.values()
                for e in p.elements
                if e.is_open
            )
            reserved = sorted(
                (s.id, r)
                for side in campaign.inventories.values()
                for s in side.squadrons.values()
                for r in s.open_reservations
            )
            self.assertEqual(reserved, open_elements, f"seed {seed}")

    def test_the_planner_struck_only_strategic_targets(self):
        for seed, campaign in self.wars.items():
            struck = {p.target_id for p in campaign.packages.values()}
            self.assertTrue(struck <= set(campaign.theater.targets), seed)
            self.assertEqual(struck & set(campaign.theater.threats), set(), seed)

    def test_both_sides_fly_from_more_than_one_base(self):
        for seed, campaign in self.wars.items():
            for side in ("blue", "red"):
                bases = {
                    p.base_id for p in campaign.packages.values() if p.coalition == side
                }
                self.assertGreater(len(bases), 1, f"seed {seed}, {side}: {bases}")
                self.assertTrue(
                    bases <= {b.id for b in campaign.theater.airbases_of(side)}
                )

    def test_every_package_flies_from_its_own_side(self):
        for campaign in self.wars.values():
            for package in campaign.packages.values():
                base = campaign.theater.airbases[package.base_id]
                target = campaign.theater.targets[package.target_id]
                self.assertEqual(base.coalition, package.coalition)
                self.assertEqual(target.coalition, enemy_of(package.coalition))


def _conservation_problems(campaign: Campaign) -> list[str]:
    """Every squadron's airframes and rounds, counted bucket by bucket.

    Counted here rather than only through `check_invariant`, so that a check
    weakened there would not take this test with it.
    """
    problems: list[str] = []
    for side in sorted(campaign.inventories):
        for squadron in campaign.inventories[side].squadrons.values():
            reserved = sum(r.airframes for r in squadron.open_reservations.values())
            held = squadron.airframes_available + reserved + squadron.airframes_lost
            if held != squadron.airframes_total or squadron.airframes_available < 0:
                problems.append(f"{campaign.clock}: {squadron.id} airframes {held}")
            for munition, total in squadron.munitions_total.items():
                rounds = (
                    squadron.munitions_available.get(munition, 0)
                    + sum(
                        r.rounds
                        for r in squadron.open_reservations.values()
                        if r.munition == munition
                    )
                    + squadron.munitions_expended.get(munition, 0)
                    + squadron.munitions_lost.get(munition, 0)
                )
                if rounds != total or squadron.munitions_available.get(munition, 0) < 0:
                    problems.append(f"{campaign.clock}: {squadron.id} {munition} {rounds}")
    return problems


# --------------------------------------------------------------------------
# Determinism and persistence
# --------------------------------------------------------------------------


class TestReplay(unittest.TestCase):
    def test_the_same_seed_is_the_same_war(self):
        first, second = new_syria(7), new_syria(7)
        fight(first)
        fight(second)
        self.assertEqual(first.to_dict(), second.to_dict())

    def test_two_fresh_theaters_are_identical(self):
        self.assertEqual(build_syria_theater().to_dict(), build_syria_theater().to_dict())
        a, b = build_syria_oob(), build_syria_oob()
        self.assertEqual([s.to_dict() for s in a], [s.to_dict() for s in b])

    def test_a_save_mid_war_continues_the_same_war(self):
        original = new_syria(5)
        fight(original, limit=0.5 * DAY)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "war.json"
            original.save(path)
            reloaded = Campaign.load(path)
        self.assertEqual(reloaded.to_dict(), original.to_dict())
        self.assertEqual(reloaded.theater.to_dict(), original.theater.to_dict())
        fight(original, limit=0.25 * DAY)
        fight(reloaded, limit=0.25 * DAY)
        self.assertEqual(reloaded.to_dict(), original.to_dict())


# --------------------------------------------------------------------------
# The entry point
# --------------------------------------------------------------------------


def _simulate(save: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-m", "campaign", "--save", str(save), *args,
         "--log-level", "WARNING"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestTheEntryPoint(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.save = self.dir / "war.json"

    def test_syria_is_the_default(self):
        self.assertEqual(DEFAULT_THEATER, "syria")
        self.assertIsNone(parse_args([]).theater)
        self.assertEqual(parse_args(["--theater", "slice"]).theater, "slice")

    def test_simulate_runs_a_syria_war(self):
        run = _simulate(self.save, "--theater", "syria", "--simulate", "3600")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("simulated 720 paper step(s)", run.stdout)
        war = Campaign.load(self.save)
        self.assertEqual(set(war.theater.airbases), set(build_syria_theater().airbases))
        self.assertTrue(war.packages, "an hour passed and nobody flew")

    def test_simulate_runs_a_slice_war(self):
        run = _simulate(self.save, "--theater", "slice", "--simulate", "3600")
        self.assertEqual(run.returncode, 0, run.stderr)
        war = Campaign.load(self.save)
        self.assertEqual(war.theater.to_dict()["airbases"],
                         build_slice_theater().to_dict()["airbases"])
        self.assertEqual(set(war.theater.targets), set(build_slice_theater().targets))

    def test_with_no_theater_named_a_new_war_is_on_syria(self):
        run = _simulate(self.save, "--simulate", "0")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("hatay", Campaign.load(self.save).theater.airbases)

    def test_a_save_keeps_its_own_theater_whatever_is_asked(self):
        self.assertEqual(_simulate(self.save, "--theater", "syria",
                                   "--simulate", "0").returncode, 0)
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), self.assertLogs(
            "campaign", level="WARNING"
        ) as logs:
            code = main(["--save", str(self.save), "--theater", "slice",
                         "--simulate", "0", "--log-level", "WARNING"])
        self.assertEqual(code, 0, err.getvalue())
        self.assertIn("--theater slice ignored", "\n".join(logs.output))
        self.assertIn("hatay", Campaign.load(self.save).theater.airbases)

    def test_both_theaters_start_a_server(self):
        async def serve(engine) -> int:
            server = CampaignServer(engine, host="127.0.0.1", port=0, tick_period=0.05)
            await server.start()
            try:
                return server.port
            finally:
                await server.close()

        for theater in ("syria", "slice"):
            with self.subTest(theater=theater):
                engine = build_engine(self.dir / f"{theater}.json", theater)
                self.assertGreater(asyncio.run(serve(engine)), 0)
                self.assertEqual(
                    set(engine.theater.airbases),
                    set((build_syria_theater() if theater == "syria"
                         else build_slice_theater()).airbases),
                )


if __name__ == "__main__":
    unittest.main()
