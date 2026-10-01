"""The offline clock: the war carries on while DCS is closed.

docs/design.md, section 2. The first slice advanced only on frames, so with no
mission client the campaign clock sat at zero however long the engine ran --
an audit left it idle for ten thousand transport ticks and it never moved.
Yet driving the clock by hand showed the paper war already worked. What was
missing was something to advance it, under three constraints pinned here:

  * the war moves in fixed paper steps, so its state after N steps is a pure
    function of where it started and N, however the wall clock chunks them;
  * it does not move itself while a client is connected, because mission time
    is authoritative then;
  * a client connecting after an offline stretch joins the war where it is,
    neither rewinding it nor jumping it.
"""

from __future__ import annotations

import asyncio
import random
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from campaign.api import PAPER_STEP
from campaign.attrition import CAUSE_UNOBSERVED, KIND_FLIGHT
from campaign.campaign import Campaign
from campaign.planner import COMPLETE, OPEN_STATES
from campaign.protocol import (
    PROTOCOL_VERSION,
    Hello,
    Observer,
    ObserverReport,
    Spawn,
    Sync,
)
from campaign.server import MAX_PAPER_STEPS_PER_TICK, CampaignServer, PaperPacer
from tests.test_domain import OBSERVER_AT_TARGET, FakeDCS, drive
from tests.test_transport import Client, StubEngine

ROOT = Path(__file__).resolve().parents[1]
DEPOT = "latakia_fuel_depot"
SQUADRON = "vfa_incirlik_f16"


def say_hello(campaign: Campaign, t: float = 0.0) -> list:
    return campaign.on_hello(
        Hello(seq=1, t=t, protocol=PROTOCOL_VERSION, theater="Syria")
    )


def run_steps(campaign: Campaign, n: int) -> None:
    for _ in range(n):
        campaign.advance(PAPER_STEP)


def observe(campaign: Campaign, t: float, positions: list) -> list:
    return campaign.on_observer(
        ObserverReport(
            seq=2,
            t=t,
            observers=[Observer(id=f"player:{i}", pos=p) for i, p in enumerate(positions)],
        )
    )


class _CountingCampaign(Campaign):
    """A real campaign that also counts the transport's ticks."""

    ticks = 0

    def tick(self, now: float):
        self.ticks += 1
        return super().tick(now)


# ---------------------------------------------------------------------------
# The war moves
# ---------------------------------------------------------------------------


class TestTheAuditScenario(unittest.IsolatedAsyncioTestCase):
    async def test_ten_thousand_transport_ticks_with_no_client_move_the_war(self):
        """The audit's exact experiment, through the real tick loop.

        The transport's own clock is replaced by one that reads one wall second
        per tick -- the default tick period -- and stops after ten thousand, so
        where the war ends up is exact rather than whatever the host managed.
        Before the offline clock this finished at campaign time 0.0.
        """
        ticks = 10_000
        campaign = _CountingCampaign()
        server = CampaignServer(
            campaign,
            host="127.0.0.1",
            port=0,
            tick_period=0.0,
            clock=lambda: float(min(campaign.ticks, ticks)),
        )
        await server.start()
        try:
            deadline = asyncio.get_running_loop().time() + 60.0
            while campaign.ticks < ticks + 3:
                self.assertLess(asyncio.get_running_loop().time(), deadline, "tick loop stalled")
                await asyncio.sleep(0.01)
        finally:
            await server.close()

        self.assertEqual(campaign.clock, float(ticks), "the war did not move with DCS closed")
        self.assertTrue(
            any(p.state == COMPLETE for p in campaign.packages.values()),
            "no package flew",
        )
        depot = campaign.theater.targets[DEPOT]
        self.assertLess(depot.units_alive, depot.units_initial, "nothing was hit")

        # And the transport's ten thousand irregular pulses and 2,000 steps
        # are exactly the war that 2,000 bare steps make.
        reference = Campaign()
        run_steps(reference, int(ticks / PAPER_STEP))
        self.assertEqual(campaign.to_dict(), reference.to_dict())


class TestAdvance(unittest.TestCase):
    def test_the_war_moves_with_no_client_connected(self):
        campaign = Campaign()
        run_steps(campaign, 2_000)
        self.assertEqual(campaign.clock, 2_000 * PAPER_STEP)
        self.assertTrue(any(p.state == COMPLETE for p in campaign.packages.values()))
        sqn = campaign.inventories["blue"].squadron(SQUADRON)
        self.assertGreater(sqn.munitions_expended.get("GBU-38", 0), 0)
        sqn.check_invariant()

    def test_only_whole_paper_steps_are_accepted(self):
        campaign = Campaign()
        for bad in (PAPER_STEP / 2, PAPER_STEP * 1.5, -PAPER_STEP):
            with self.subTest(dt=bad), self.assertRaises(ValueError):
                campaign.advance(bad)
        self.assertEqual(campaign.clock, 0.0, "a refused advance moved the clock")
        self.assertEqual(campaign.advance(0.0), [])
        self.assertEqual(campaign.clock, 0.0)


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestChunkingDeterminism(unittest.TestCase):
    STEPS = 3_000

    def test_the_same_steps_in_any_chunking_are_the_same_war(self):
        one_at_a_time = Campaign()
        run_steps(one_at_a_time, self.STEPS)

        all_at_once = Campaign()
        all_at_once.advance(self.STEPS * PAPER_STEP)

        jittered = Campaign()
        chunks = random.Random(7)
        left = self.STEPS
        while left:
            n = min(left, chunks.randint(1, 400))
            jittered.advance(n * PAPER_STEP)
            left -= n

        self.assertTrue(
            [x for x in one_at_a_time.tracker.losses if x.cause == CAUSE_UNOBSERVED],
            "nothing happened in the war, so agreeing about it proves nothing",
        )
        reference = one_at_a_time.to_dict()
        self.assertEqual(all_at_once.to_dict(), reference)
        self.assertEqual(jittered.to_dict(), reference)

    def test_ticks_landing_between_steps_change_nothing(self):
        """The transport pulses on its own cadence as well as stepping.

        Whether a tick lands before the first step, between two steps or not
        at all is scheduling. It must not decide when the first package is
        planned, or which war the steps produce.
        """
        stepped = Campaign()
        run_steps(stepped, 600)

        ticked = Campaign()
        pulses = random.Random(3)
        ticked.tick(0.0)
        for _ in range(600):
            for _ in range(pulses.randint(0, 3)):
                ticked.tick(0.0)
            ticked.advance(PAPER_STEP)

        self.assertEqual(ticked.to_dict(), stepped.to_dict())

    def test_the_pacer_turns_any_chunking_of_the_same_wall_time_into_the_same_steps(self):
        """The wall clock decides how many steps, never what a step is.

        Chunk sizes are multiples of 1/64 s so every chunking sums to exactly
        the same wall time; what is under test is the pacer, not float sums.
        """
        compression = 3.0
        total_64ths = 79_008  # 1234.5 wall seconds

        def paced(chunks: list[int]) -> tuple[int, float]:
            pacer = PaperPacer(compression)
            taken = sum(pacer.feed(c / 64) for c in chunks)
            while True:  # drain whatever the per-tick cap carried over
                more = pacer.feed(0.0)
                if not more:
                    return taken, pacer.backlog
                taken += more

        one_chunk = paced([total_64ths])
        even = paced([48] * (total_64ths // 48))
        rng = random.Random(11)
        jitter: list[int] = []
        left = total_64ths
        while left:
            jitter.append(min(left, rng.randint(1, 200)))
            left -= jitter[-1]
        jittered = paced(jitter)

        expected_steps = int(1234.5 * compression // PAPER_STEP)
        self.assertEqual(one_chunk[0], expected_steps)
        self.assertGreater(expected_steps, MAX_PAPER_STEPS_PER_TICK, "the cap was never exercised")
        self.assertEqual(even, one_chunk)
        self.assertEqual(jittered, one_chunk)

    def test_the_per_tick_cap_carries_the_rest_rather_than_dropping_it(self):
        pacer = PaperPacer(1.0)
        self.assertEqual(pacer.feed(10 * MAX_PAPER_STEPS_PER_TICK * PAPER_STEP), MAX_PAPER_STEPS_PER_TICK)
        self.assertEqual(pacer.backlog, 9 * MAX_PAPER_STEPS_PER_TICK * PAPER_STEP)


# ---------------------------------------------------------------------------
# Connected means mission time
# ---------------------------------------------------------------------------


class TestAdvanceIsRefusedWhileConnected(unittest.TestCase):
    def test_a_connected_campaign_does_not_move_itself(self):
        campaign = Campaign()
        say_hello(campaign)
        observe(campaign, 5.0, OBSERVER_AT_TARGET)
        before = campaign.to_dict()

        self.assertEqual(campaign.advance(100 * PAPER_STEP), [])
        self.assertEqual(campaign.clock, 5.0)
        self.assertEqual(campaign.to_dict(), before, "advance changed a connected campaign")

    def test_it_moves_again_once_the_client_is_gone(self):
        campaign = Campaign()
        say_hello(campaign)
        campaign.on_disconnect()
        campaign.advance(10 * PAPER_STEP)
        self.assertEqual(campaign.clock, 10 * PAPER_STEP)


class TestTheTransportOnlyStepsWithNobodySynced(unittest.IsolatedAsyncioTestCase):
    """The transport's half of the rule, against an engine that would obey.

    The stub never refuses a step, so anything it records is a step the
    transport chose to deliver.
    """

    async def asyncSetUp(self) -> None:
        self.engine = StubEngine()
        self.server = CampaignServer(
            self.engine, host="127.0.0.1", port=0, tick_period=3600.0, time_compression=1.0
        )
        await self.server.start()
        self.client = Client()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        await self.server.close()

    def steps(self) -> list:
        return [dt for name, dt in self.engine.calls if name == "advance"]

    async def test_steps_are_fixed_size_and_stop_at_hello(self):
        self.assertEqual(self.server.advance_offline(100.0), 20)
        self.assertEqual(self.steps(), [PAPER_STEP] * 20)

        # A socket that has not said hello is not a client yet.
        await self.client.connect(self.server.port)
        self.assertEqual(self.server.advance_offline(10.0), 2)

        await self.client.send(
            Hello(seq=1, t=0.0, protocol=PROTOCOL_VERSION, theater="Syria")
        )
        self.assertIsInstance(await self.client.recv(), Sync)
        self.assertEqual(self.server.advance_offline(1000.0), 0)
        self.assertEqual(len(self.steps()), 22, "the transport stepped a connected war")

        await self.client.close()
        deadline = asyncio.get_running_loop().time() + 5.0
        while self.engine.disconnects == 0:
            self.assertLess(asyncio.get_running_loop().time(), deadline)
            await asyncio.sleep(0.01)
        # Nothing owed from while the client was here.
        self.assertEqual(self.server.advance_offline(4.0), 0)
        self.assertEqual(self.server.advance_offline(1.0), 1)


# ---------------------------------------------------------------------------
# Reconnecting after an offline stretch
# ---------------------------------------------------------------------------


class TestReconnectAfterAnOfflineStretch(unittest.TestCase):
    def test_a_client_at_mission_zero_joins_the_war_where_it_is(self):
        campaign = Campaign()
        run_steps(campaign, 600)
        offline = campaign.clock
        self.assertEqual(offline, 3_000.0)

        frames = say_hello(campaign, t=0.0)
        sync = frames[0]
        self.assertIsInstance(sync, Sync)
        self.assertEqual(sync.campaign_time, offline, "the client was told another time")
        self.assertEqual(sync.t, 0.0, "the sync is not in the client's own clock")
        self.assertEqual(campaign.clock, offline, "the hello moved the clock")
        self.assertEqual(campaign.mission_epoch, offline)

        observe(campaign, 5.0, OBSERVER_AT_TARGET)
        self.assertEqual(campaign.clock, offline + 5.0, "mission time was not rebased")

    def test_offline_then_online_then_offline_again_never_rewinds(self):
        campaign = Campaign()
        dcs = FakeDCS(campaign=campaign, deliver_events=False)
        run_steps(campaign, 20)
        dcs.connect(0.0)
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=False,
            start=5,
            duration=300,
            dcs=dcs,
        )
        self.assertEqual(campaign.clock, 100.0 + 305.0)
        campaign.on_disconnect()
        run_steps(campaign, 100)
        self.assertEqual(campaign.clock, 905.0)

        # A DCS restart: the new mission's clock starts at zero again.
        frames = say_hello(campaign, t=0.0)
        self.assertEqual(frames[0].campaign_time, 905.0)
        package = next(p for p in campaign.packages.values() if p.state in OPEN_STATES)
        self.assertFalse(package.weapons_released, "nothing still inbound; vacuous")
        # The briefing speaks the new mission's clock: a TOT still ahead of
        # the war is still ahead of the client, by exactly as much.
        tot = package.t_tot - 905.0
        self.assertGreater(tot, 0.0)
        self.assertIn(f"TOT {tot:.0f}.", " ".join(getattr(f, "text", "") for f in frames))
        observe(campaign, 5.0, OBSERVER_AT_TARGET)
        self.assertEqual(campaign.clock, 910.0)


class TestObserversDoNotOutliveTheWarMovingOn(unittest.TestCase):
    """When DCS goes away, the players go with it.

    The last observer frame says where the players were. Kept through a
    disconnect it bridges a quick restart, but once the war moves on paper it
    describes nobody: a bubble built from it instantiates things for no one,
    and the next hello re-issues that stale picture to a mission whose players
    are somewhere else entirely.
    """

    def _observed_then_abandoned(self) -> Campaign:
        campaign = Campaign()
        dcs = FakeDCS(campaign=campaign, deliver_events=False)
        drive(
            campaign,
            observer_positions=OBSERVER_AT_TARGET,
            damages=[],
            deliver_events=False,
            start=0,
            duration=60,
            dcs=dcs,
        )
        self.assertIn(
            campaign.theater.targets[DEPOT].spawn_id,
            campaign.live,
            "nothing was in the bubble, so this proves nothing",
        )
        campaign.on_disconnect()
        return campaign

    def test_a_quick_restart_with_no_paper_time_keeps_the_picture(self):
        campaign = self._observed_then_abandoned()
        spawned = [f for f in say_hello(campaign) if isinstance(f, Spawn)]
        self.assertIn(campaign.theater.targets[DEPOT].spawn_id, {f.spawn_id for f in spawned})

    def test_after_paper_time_nothing_is_instantiated_for_nobody(self):
        campaign = self._observed_then_abandoned()
        run_steps(campaign, 1)
        self.assertEqual(campaign.observers, [])
        self.assertEqual(campaign.live, set(), "the bubble outlived its players")

        frames = say_hello(campaign)
        self.assertEqual(
            [f for f in frames if isinstance(f, Spawn)],
            [],
            "the hello re-issued a bubble built around where the players used to be",
        )
        # The next observer frame builds the bubble around where they are now.
        spawned = [f for f in observe(campaign, 5.0, OBSERVER_AT_TARGET) if isinstance(f, Spawn)]
        self.assertIn(campaign.theater.targets[DEPOT].spawn_id, {f.spawn_id for f in spawned})


# ---------------------------------------------------------------------------
# python -m campaign --simulate
# ---------------------------------------------------------------------------


def _mid_sortie_save(path: Path, *, disconnect: bool) -> Campaign:
    """A real save, written with the flight and the depot instantiated.

    Seed 10, a war blue wins at 1475 s with the paper strike that follows
    this save, because what the tests below read off the result is the depot
    destroyed: proof that paper strikes reached it. The default seed was such
    a war until SEAD elements changed every seed's dice, and seed 3 until
    they fired from standoff: an escorted TOT now draws no exposure dice for
    a SEAD element that out-ranges its site, and at 3 red now wins at 6435 s
    with the depot standing. Seed 10 is the first, in order, that blue wins
    whether or not the save was detached; at 4, 8 and 9 the depot falls
    too, but only after red has won.
    """
    campaign = Campaign(seed=10)
    drive(
        campaign,
        observer_positions=OBSERVER_AT_TARGET,
        damages=[],
        deliver_events=False,
        start=0,
        duration=1_200,
    )
    assert any(p.state in OPEN_STATES for p in campaign.packages.values())
    if disconnect:
        campaign.on_disconnect()
    campaign.save(path)
    return campaign


def _simulate(path: Path, seconds: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-B", "-m", "campaign", "--save", str(path), "--simulate", seconds,
         "--log-level", "WARNING"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )


class TestSimulate(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.save = self.dir / "war.json"

    def test_it_fast_forwards_a_save_end_to_end(self):
        _mid_sortie_save(self.save, disconnect=True)
        before = Campaign.load(self.save)

        run = _simulate(self.save, "40002.5")
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("simulated 8000 paper step(s) of 5s", run.stdout)

        after = Campaign.load(self.save)
        self.assertEqual(after.clock, before.clock + 40_000.0)
        self.assertTrue(after.theater.targets[DEPOT].destroyed, "the war did not go on")
        after.inventories["blue"].check_invariant()

        # The same war an in-process fast-forward makes: a subprocess is no
        # excuse for a different one.
        run_steps(before, 8_000)
        self.assertEqual(after.to_dict(), before.to_dict())

    def test_a_save_from_a_killed_process_is_not_still_attached_to_its_dcs(self):
        """The save was written with the depot instantiated, and never detached.

        Loaded as-is, the depot is "held by DCS" in a process with no DCS. If
        it stayed that way no paper strike could touch it, every sortie would
        bomb nothing, and the offline war could never be won. The first paper
        step is what lets go: it empties the bubble, and a despawn marks the
        group as no longer held.
        """
        _mid_sortie_save(self.save, disconnect=False)
        run = _simulate(self.save, "40000")
        self.assertEqual(run.returncode, 0, run.stderr)
        after = Campaign.load(self.save)
        self.assertTrue(after.theater.targets[DEPOT].destroyed)

    def test_a_negative_duration_is_refused(self):
        run = _simulate(self.save, "-5")
        self.assertEqual(run.returncode, 2)
        self.assertIn("--simulate", run.stderr)
        self.assertFalse(self.save.exists())


class TestConnectedIsNotSaved(unittest.TestCase):
    def test_a_campaign_saved_while_connected_loads_disconnected(self):
        campaign = Campaign()
        say_hello(campaign)
        self.assertNotIn("connected", campaign.to_dict())
        reloaded = Campaign.from_dict(campaign.to_dict())
        self.assertFalse(reloaded.connected)
        reloaded.advance(PAPER_STEP)
        self.assertEqual(reloaded.clock, PAPER_STEP)


if __name__ == "__main__":
    unittest.main()
