"""What the sim fired: ammunition in the snapshot, and the paper firing only the rest.

docs/design.md, section 5, and docs/protocol.md, "Changes from v2". A SEAD
element's missiles must never be resolved twice -- once by the sim and again
on paper. Events cannot say what was fired, because events are attribution
only; so protocol v3 puts the ammunition aboard each flight in the `state`
snapshot, beside the spawn-time reading the client took when it built the
group. The engine counts what has left the sim since that baseline
(`Element.sim_spent`), books it when the snapshot shows it, and lets the
paper fire at most what it reserved less that.

What is pinned here, below the frame-level tests in tests/test_sead.py:

  * the protocol: v3 or nothing, and a v2 hello refused before anything
    changes;
  * the baseline: an element whose pylons are empty reports zero from its
    first snapshot and has fired nothing; one that fired before its first
    snapshot is still counted from the spawn-time reading; each
    instantiation is its own baseline, because DCS re-arms what it re-builds;
  * the edges: a missile and an aircraft gone in the same interval, the
    snapshot sent just before a despawn, a count that rises, a count the
    client could not vouch for, and a strike element's ammunition, which is
    not read at all;
  * conservation through a long war in and out of the bubble, with the sim
    firing and losing SEAD jets: every round is debited exactly once, and no
    paper missile is ever one the sim already fired.
"""

from __future__ import annotations

import unittest
from collections import defaultdict
from unittest import mock

from campaign.api import PAPER_STEP
from campaign.campaign import Campaign
from campaign.oob import Squadron
from campaign.protocol import (
    PROTOCOL_VERSION,
    Ack,
    Event,
    GroupSnapshot,
    Hello,
    ProtocolError,
    StateReport,
)
from tests.test_domain import STATE_PERIOD, FakeDCS
from tests.test_sead import (
    ALL_EMITTERS,
    HITS,
    SA6,
    SEAD_SQN,
    SURVIVES,
    blue_package,
    conserved_everywhere,
    hold,
    resolve_blue_tot,
    sa6_in_reach,
    to_the_brink,
)

HARM = "AGM-88C"


def snapshot(campaign: Campaign, element, *, units=2, ammo, initial=4, seq=50):
    """One state frame reporting `element` alone, at the campaign's clock."""
    return StateReport(
        seq=seq,
        t=campaign.clock,
        groups=[
            GroupSnapshot(
                spawn_id=element.spawn_id,
                alive=units > 0,
                units=units,
                units_initial=element.flight_size,
                ammo=ammo,
                ammo_initial=None if initial is None else ({HARM: initial} if initial else {}),
            )
        ],
    )


def harms(count: int) -> dict[str, int]:
    return {HARM: count} if count else {}


def blue_sead_squadron(campaign: Campaign):
    return campaign.inventories["blue"].squadron(SEAD_SQN)


def held_reservation(campaign: Campaign, element) -> int:
    return blue_sead_squadron(campaign).open_reservations[element.reservation_id].rounds


class TestTheProtocolIsVersion3(unittest.TestCase):
    """Version 3 brought ammunition; version 4 (typed site units) keeps it
    unchanged, so this file's frames are v4 frames and a v2 peer is still
    refused. The v3 refusal is tests/test_typed_sites.py's."""

    def test_the_engine_speaks_4(self):
        self.assertEqual(PROTOCOL_VERSION, 4)

    def test_a_version_2_hello_is_refused_and_changes_nothing(self):
        campaign = Campaign()
        campaign.advance(PAPER_STEP)
        before = campaign.to_dict()
        with self.assertRaises(ProtocolError) as caught:
            campaign.on_hello(Hello(seq=1, t=0.0, protocol=2, theater="Syria"))
        self.assertIn("client protocol 2 != engine 4", str(caught.exception))
        self.assertFalse(campaign.connected)
        self.assertEqual(campaign.to_dict(), before)


class TestTheBaseline(unittest.TestCase):
    """The first reading of an instantiation is the client's spawn-time count."""

    def setUp(self) -> None:
        self.campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(self.campaign)
        self.package = blue_package(self.campaign)
        self.sead = self.package.sead
        hold(self.campaign, self.sead.spawn_id)

    def test_an_element_never_instantiated_has_spent_nothing(self):
        campaign = Campaign()
        for _ in range(int(20_000 / PAPER_STEP)):
            campaign.advance(PAPER_STEP)
        seads = [p.sead for p in campaign.packages.values() if p.sead is not None]
        self.assertTrue(seads, "no SEAD element flew; vacuous")
        self.assertEqual({e.sim_spent for e in seads}, {0})

    def test_an_unarmed_element_reads_zero_from_the_start_and_has_fired_nothing(self):
        """DCS today: empty pylons. Zero read as an absolute count would say
        every missile was fired; read against its baseline it says none was."""
        for _ in range(3):
            self.campaign.on_state(snapshot(self.campaign, self.sead, ammo={}, initial=0))
        self.assertEqual(self.sead.sim_spent, 0)
        self.assertEqual(held_reservation(self.campaign, self.sead), 4)
        self.assertEqual(blue_sead_squadron(self.campaign).munitions_expended.get(HARM, 0), 0)

    def test_a_shot_before_the_first_snapshot_is_counted_from_the_spawn_reading(self):
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(3)))
        self.assertEqual(self.sead.sim_spent, 1)
        self.assertEqual(held_reservation(self.campaign, self.sead), 3)

    def test_every_drop_is_counted_once_however_many_snapshots_repeat_it(self):
        for left in (4, 3, 3, 1, 1, 1):
            self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(left)))
        self.assertEqual(self.sead.sim_spent, 3)
        self.assertEqual(blue_sead_squadron(self.campaign).munitions_expended[HARM], 3)
        self.assertEqual(held_reservation(self.campaign, self.sead), 1)
        conserved_everywhere(self, self.campaign)

    def test_each_instantiation_is_its_own_baseline(self):
        """The client re-arms whatever it re-builds, so a re-spawned element
        reads full again; what it fired the first time stays spent."""
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(2)))
        self.campaign.tracker.mark_removed(self.sead.spawn_id)
        self.campaign.pending[77] = ("spawn", self.sead.spawn_id)
        self.campaign.on_ack(Ack(seq=3, t=self.campaign.clock, ref=77, ok=True))
        self.assertIsNone(self.sead.ammo_seen)
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(4)))
        self.assertEqual(self.sead.sim_spent, 2, "a re-armed jet is not a missile un-fired")
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(3)))
        self.assertEqual(self.sead.sim_spent, 3)
        self.assertEqual(held_reservation(self.campaign, self.sead), 1)

    def test_the_snapshot_before_a_despawn_is_read_though_the_group_is_let_go(self):
        """The engine stops holding a group when it sends the despawn; the
        client's last census, sent just before it obeys, still counts."""
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(4)))
        self.campaign.tracker.mark_removed(self.sead.spawn_id)
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(2)))
        self.assertEqual(self.sead.sim_spent, 2)

    def test_a_count_that_rises_is_the_new_reading(self):
        """A jet in flight cannot gain a missile; if a reading says it did,
        the drops after it are counted from there rather than lost."""
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(1), initial=1))
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(3), initial=1))
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(1), initial=1))
        self.assertEqual(self.sead.sim_spent, 2)

    def test_a_count_nobody_could_read_is_everything_spent(self):
        """Unvouched is not zero: the paper fires nothing rather than risk a
        missile the sim may have fired."""
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=None))
        self.assertEqual(self.sead.sim_spent, 4)
        self.assertEqual(held_reservation(self.campaign, self.sead), 0)
        self.assertEqual(blue_sead_squadron(self.campaign).munitions_expended[HARM], 4)
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        element = blue_package(campaign).sead
        hold(campaign, element.spawn_id)
        campaign.on_state(snapshot(campaign, element, ammo=harms(4), initial=None))
        self.assertEqual(element.sim_spent, 4)

    def test_a_strike_elements_ammunition_is_not_read(self):
        """Its bombs are whoever holds it at the TOT's to resolve."""
        strike = self.package.strike
        hold(self.campaign, strike.spawn_id)
        report = StateReport(seq=51, t=self.campaign.clock, groups=[GroupSnapshot(
            spawn_id=strike.spawn_id, alive=True, units=2, units_initial=2,
            ammo={}, ammo_initial={"GBU-38": 4})])
        self.campaign.on_state(report)
        self.assertEqual(strike.sim_spent, 0)
        squadron = self.campaign.inventories["blue"].squadron(strike.squadron_id)
        self.assertEqual(squadron.open_reservations[strike.reservation_id].rounds, 4)

    def test_events_say_nothing_about_ammunition(self):
        before = self.campaign.to_dict()
        self.campaign.on_event(Event(seq=9, t=self.campaign.clock, kind="shot",
                                     initiator=f"cmp_{self.sead.spawn_id}", weapon=HARM))
        self.assertEqual(self.campaign.to_dict()["packages"], before["packages"])
        self.assertEqual(self.sead.sim_spent, 0)


class TestAMissileAndAnAircraftGoneTogether(unittest.TestCase):
    """A summed count cannot split rounds fired from rounds carried down.

    The engine books the dead aircraft's share as lost and the rest as
    expended, which is exact whenever each jet carried its full load, and
    in every case leaves the paper only what the survivors still carry.

    The SA-6 is `ALL_EMITTERS`, so the one paper missile that hits takes a
    unit and leaves a battery that still fires at the strike, as before
    units were typed; against the real SA-6 it takes the radar and the strike
    throws no exposure dice (docs/design.md, section 6).
    """

    def setUp(self) -> None:
        self.campaign = Campaign(theater=sa6_in_reach(**ALL_EMITTERS))
        to_the_brink(self.campaign)
        self.package = blue_package(self.campaign)
        self.sead = self.package.sead
        hold(self.campaign, self.sead.spawn_id)
        self.squadron = blue_sead_squadron(self.campaign)

    def test_lead_fires_one_as_his_wingman_goes_down_with_two(self):
        self.campaign.on_state(snapshot(self.campaign, self.sead, units=1, ammo=harms(1)))
        self.assertEqual(self.squadron.munitions_lost, {HARM: 2})
        self.assertEqual(self.squadron.munitions_expended, {HARM: 1})
        self.assertEqual(held_reservation(self.campaign, self.sead), 1)
        self.assertEqual(self.squadron.airframes_lost, 1)
        conserved_everywhere(self, self.campaign)

    def test_a_lead_who_fired_one_earlier_fires_his_last_as_his_wingman_goes_down(self):
        """Read before the losses are booked: the interval's whole excess is
        judged at once, so the wingman's two are lost and the lead's shot is
        expended, as happened."""
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(3)))
        self.campaign.on_state(snapshot(self.campaign, self.sead, units=1, ammo={}))
        self.assertEqual(self.squadron.munitions_lost, {HARM: 2})
        self.assertEqual(self.squadron.munitions_expended, {HARM: 2})
        self.assertEqual(held_reservation(self.campaign, self.sead), 0)

    def test_a_wingman_who_fired_both_and_then_died_took_nothing_down(self):
        self.campaign.on_state(snapshot(self.campaign, self.sead, ammo=harms(2)))
        self.campaign.on_state(snapshot(self.campaign, self.sead, units=1, ammo=harms(2)))
        self.assertEqual(self.squadron.munitions_expended, {HARM: 2})
        self.assertEqual(self.squadron.munitions_lost.get(HARM, 0), 0)
        # The lead still carries both of his: the paper may fire them.
        self.assertEqual(held_reservation(self.campaign, self.sead), 2)

    def test_an_unarmed_element_losing_a_jet_books_what_the_one_flight_package_did(self):
        self.campaign.on_state(snapshot(self.campaign, self.sead, units=1, ammo={}, initial=0))
        self.assertEqual(self.squadron.munitions_lost, {HARM: 2})
        self.assertEqual(self.squadron.munitions_expended.get(HARM, 0), 0)
        self.assertEqual(held_reservation(self.campaign, self.sead), 2)

    def test_then_the_paper_fires_only_what_the_survivor_still_carries(self):
        self.campaign.on_state(snapshot(self.campaign, self.sead, units=1, ammo=harms(1)))
        self.campaign.tracker.mark_removed(self.sead.spawn_id)
        # On paper at the TOT: the survivor's exposure (the SA-6 here can
        # reach as far as the HARM), its one missile, the strike's exposure
        # and four bombs.
        _, dice = resolve_blue_tot(self.campaign, [SURVIVES] + [HITS] + [SURVIVES] * 6)
        self.assertEqual(dice.script, [])
        self.assertEqual(self.campaign.theater.threats[SA6].units_alive, 4)
        self.assertEqual(self.squadron.munitions_expended, {HARM: 2})
        self.assertEqual(self.squadron.munitions_lost, {HARM: 2})
        conserved_everywhere(self, self.campaign)


class TestOnlyTheAircraftThatFireOnPaperSuppress(unittest.TestCase):
    """docs/design.md, section 5: a jet that spent its missiles in the sim
    has nothing to fire at the TOT, and buys no paper suppression.

    A strike die of 0.05 kills at the SA-6's Pk halved once (0.075) and
    spares at it halved twice (0.0375), so the strike element's losses say
    how many SEAD aircraft suppressed.
    """

    KILLS_UNLESS_TWO_SUPPRESS = 0.05

    def _strikers_lost(self, spent: int) -> int:
        campaign = Campaign(theater=sa6_in_reach())
        to_the_brink(campaign)
        package = blue_package(campaign)
        hold(campaign, package.sead.spawn_id)
        campaign.on_state(snapshot(campaign, package.sead, ammo=harms(4 - spent)))
        campaign.tracker.mark_removed(package.sead.spawn_id)
        paper = 4 - spent
        lost_if_one = spent >= 2
        survivors = 0 if lost_if_one else 2
        _, dice = resolve_blue_tot(
            campaign,
            [SURVIVES, SURVIVES]                       # the SEAD element's exposure
            + [SURVIVES] * paper                       # its missiles, all missing
            + [self.KILLS_UNLESS_TWO_SUPPRESS] * 2     # the strike's exposure
            + [SURVIVES] * (2 * survivors),            # the strikers' bombs
        )
        self.assertEqual(dice.script, [])
        conserved_everywhere(self, campaign)
        return len(campaign.tracker.losses_for(package.strike.spawn_id))

    def test_two_left_on_a_two_ship_is_one_aircraft_suppressing(self):
        self.assertEqual(self._strikers_lost(spent=0), 0, "two shooters suppress twice")
        self.assertEqual(self._strikers_lost(spent=1), 0, "three missiles need both jets")
        self.assertEqual(self._strikers_lost(spent=2), 2, "two missiles are one jet's")
        self.assertEqual(self._strikers_lost(spent=3), 2, "one missile is one jet's")


# ---------------------------------------------------------------------------
# A long war, in and out of the bubble
# ---------------------------------------------------------------------------

#: Where the observer sits, phase by phase: on blue's target and the SA-6,
#: nowhere, on red's target and the Patriot, nowhere. Each SEAD element is
#: therefore held for part of its sortie and let go for part, and meets its
#: TOT on either side of the bubble depending on the phase.
_SA6_SIDE = (16_000.0, 100.0, 25_000.0)
_PATRIOT_SIDE = (126_000.0, 100.0, -22_000.0)
_NOWHERE = (900_000.0, 100.0, 900_000.0)
PHASES = (_SA6_SIDE, _NOWHERE, _PATRIOT_SIDE, _NOWHERE)
#: DCS restarts once, mid-war: everything is re-built, re-armed, and starts a
#: new baseline.
RESTART_AT = 9_000
LOADOUTS = {"F-16C_sead_harm": {"AGM-88C": 2}, "Su-24M_sead_kh58": {"Kh-58U": 2}}


class _Ledger:
    """Every munition debit and return, by reservation, spied off the squadrons."""

    def __init__(self) -> None:
        self.expended: dict[str, int] = defaultdict(int)
        self.lost: dict[str, int] = defaultdict(int)
        self.returned: dict[str, int] = defaultdict(int)
        debit, release = Squadron.debit_munitions, Squadron.release
        ledger = self

        def spy_debit(squadron, reservation_id, count, *, lost):
            taken = debit(squadron, reservation_id, count, lost=lost)
            (ledger.lost if lost else ledger.expended)[reservation_id] += taken
            return taken

        def spy_release(squadron, reservation_id):
            held = squadron.open_reservations.get(reservation_id)
            ledger.returned[reservation_id] += 0 if held is None else held.rounds
            return release(squadron, reservation_id)

        self.patches = [
            mock.patch.object(Squadron, "debit_munitions", spy_debit),
            mock.patch.object(Squadron, "release", spy_release),
        ]

    def __enter__(self):
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in reversed(self.patches):
            patch.stop()


def mixed_war(seed: int, duration: int, phase_length: int, check=None):
    """A war driven through the in-process client with the observer moving.

    Every state period, before the snapshot is taken, each SEAD group DCS
    holds fires one missile if it has one, and every third period one of
    its jets is shot down. All of it happens right before a snapshot, so
    every round that leaves the sim is reported: `left_sim` is what the
    engine must have counted, per element, up to its TOT.
    """
    campaign = Campaign(seed=seed)
    dcs = FakeDCS(campaign=campaign, deliver_events=False, loadouts=dict(LOADOUTS))
    dcs.connect(0.0)
    left_sim: dict[str, int] = defaultdict(int)
    paper: dict[str, int] = {}
    deaths = 0

    original = campaign._suppress

    def spy(package, sead, survivors, rounds, suppression):
        if not campaign.tracker.is_instantiated(sead.spawn_id):
            paper[sead.spawn_id] = rounds
        return original(package, sead, survivors, rounds, suppression)

    campaign._suppress = spy
    for t in range(0, duration + 1, 5):
        if t == RESTART_AT:
            campaign.on_disconnect()
            dcs.groups.clear()
            dcs.connect(float(t))
        dcs.t = float(t)
        dcs.pump(campaign.tick(float(t)))
        phase = PHASES[(t // phase_length) % len(PHASES)]
        dcs.observers(float(t), [phase])
        if t % STATE_PERIOD == 0:
            for group in list(dcs.groups.values()):
                found = campaign._find_element(group.spawn_id)
                if group.kind != "sead" or found is None or found[1].weapons_released:
                    continue
                for load in group.ammo[: group.units]:
                    weapon = next((w for w, n in sorted(load.items()) if n > 0), None)
                    if weapon is not None:
                        load[weapon] -= 1
                        left_sim[group.spawn_id] += 1
                        break
                if (t // STATE_PERIOD) % 3 == 0 and group.units > 0:
                    gone = group.ammo[group.units - 1]
                    left_sim[group.spawn_id] += sum(gone.values())
                    group.units -= 1
                    group.alive = group.units > 0
                    deaths += 1
            dcs.state(t)
        if check is not None:
            check(campaign)
    return campaign, left_sim, paper, deaths


class TestConservationThroughAMixedWar(unittest.TestCase):
    """Every round debited exactly once, and none fired twice, in and out of DCS."""

    #: (seed, seconds per observer phase). How long the observer dwells
    #: decides which elements meet their TOT on paper after being held, and
    #: between them these fly every case: an element that spent nothing in
    #: DCS, part of its load, and all of it, before a paper TOT.
    CASES = ((0, 690), (2, 450), (2, 300), (0, 900))
    DURATION = 20_000

    def _invariant(self, campaign: Campaign) -> None:
        for inventory in campaign.inventories.values():
            inventory.check_invariant()

    def test_every_round_is_booked_once_and_no_paper_missile_was_fired_in_the_sim(self):
        partial = spent_out = paper_fired = deaths_seen = 0
        for seed, phase_length in self.CASES:
            with self.subTest(seed=seed, phase_length=phase_length), _Ledger() as ledger:
                campaign, left_sim, paper, deaths = mixed_war(
                    seed, self.DURATION, phase_length, check=self._invariant
                )
                deaths_seen += deaths
                seads = [
                    (p, p.sead) for p in sorted(campaign.packages.values(), key=lambda p: p.id)
                    if p.sead is not None
                ]
                self.assertTrue(seads, "no SEAD element flew; vacuous")
                for package, sead in seads:
                    res = sead.reservation_id
                    if not package.is_open:
                        # Exactly once: every round of the reservation is in
                        # one bucket, and none was debited twice.
                        self.assertEqual(
                            ledger.expended[res] + ledger.lost[res] + ledger.returned[res],
                            sead.rounds, res,
                        )
                    if not sead.weapons_released:
                        continue
                    # The snapshots are the whole truth about what left the sim.
                    self.assertEqual(sead.sim_spent, left_sim[sead.spawn_id], res)
                    if sead.spawn_id in paper:
                        fired = paper[sead.spawn_id]
                        self.assertLessEqual(
                            fired, max(0, sead.rounds - left_sim[sead.spawn_id]),
                            f"{res}: the paper fired a missile the sim had already spent",
                        )
                        paper_fired += fired
                        if left_sim[sead.spawn_id] >= sead.rounds:
                            spent_out += 1
                        elif left_sim[sead.spawn_id] > 0 and fired > 0:
                            partial += 1
                conserved_everywhere(self, campaign)
        self.assertGreater(partial, 0, "no element fired part of its load in DCS and the rest on paper")
        self.assertGreater(spent_out, 0, "no element spent everything in DCS before a paper TOT")
        self.assertGreater(paper_fired, 0, "nothing was fired on paper; vacuous")
        self.assertGreater(deaths_seen, 0, "no SEAD jet died in DCS; vacuous")


if __name__ == "__main__":
    unittest.main()
