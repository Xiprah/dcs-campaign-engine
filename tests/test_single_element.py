"""A package with only a strike element is the one-flight package, exactly.

docs/design.md, section 5 turned a package into a set of elements. Keeping
that one mechanism rather than two is only honest if the case the engine
always had -- one strike flight, no escort -- comes out of it unchanged: the
same spawn ids, the same dice, the same frames, the same losses at the same
seconds, the same words to the players.

`fixtures/pre_sead_wars.json` was recorded from the engine as it stood before
elements existed (commit 42fdcee), by the same fingerprint as below written
against that engine's one-flight `Package`. It holds eleven wars: eight
fought entirely on paper for 20,000 s, and three driven through the
in-process client with the observer parked on the depot, so the strike is
spawned, tasked, damaged by snapshots and despawned in DCS while red's raid
is rolled on paper. Every one ends, or runs, well past the first sortie.

These wars are fought with the pre-SEAD order of battle: the slice's
squadrons less the two that carry anti-radiation missiles. With no missiles
a side cannot attach a SEAD element, so every package is its strike element
alone, and anything that differs from the recording is the new mechanism
leaking into the old case.

Two differences are deliberate, and are applied to the recording rather
than hidden from the comparison: a flight spawned after its TOT is now sent
home with no attack task (`with_the_egress_fix` says why), and the `sync`
frame names the current wire protocol (`with_the_current_protocol`). Nothing
else moved: not one die, spawn id, loss, message or other frame field.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import unittest
from pathlib import Path

from campaign.api import PAPER_STEP
from campaign.campaign import Campaign
from campaign.oob import ANTI_RADIATION_MUNITIONS, SideInventory, build_slice_oob
from campaign.protocol import PROTOCOL_VERSION
from tests.test_domain import OBSERVER_AT_TARGET, SCENARIO, drive

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "pre_sead_wars.json"


def strike_only_oob() -> dict[str, SideInventory]:
    """The slice's inventories without a single anti-radiation missile.

    Removes the squadrons whose only stores are ARMs, which is exactly the
    order of battle the pre-SEAD engine shipped: the strike squadrons, in
    the same declaration order.
    """
    blue, red = build_slice_oob()
    for inventory in (blue, red):
        for squadron_id in [
            s.id
            for s in inventory.squadrons.values()
            if set(s.munitions_total) <= ANTI_RADIATION_MUNITIONS
        ]:
            del inventory.squadrons[squadron_id]
    return {blue.coalition: blue, red.coalition: red}


def _rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in sorted(value.items())}
    return value


def _frame(frame) -> dict:
    raw = dataclasses.asdict(frame)
    raw["type"] = frame.type
    return _rounded(raw)


def _package(package) -> dict:
    """The one-flight `Package`'s fields, read off a one-element package."""
    (element,) = package.elements
    return _rounded(
        {
            "id": package.id,
            "coalition": package.coalition,
            "callsign": package.callsign,
            "base_id": package.base_id,
            "target_id": package.target_id,
            "state": package.state,
            "t_created": package.t_created,
            "t_takeoff": element.t_takeoff,
            "t_tot": package.t_tot,
            "t_rtb": element.t_rtb,
            "weapons_released": package.weapons_released,
            "spawn_id": element.spawn_id,
            "squadron_id": element.squadron_id,
            "flight_size": element.flight_size,
            "munition": element.munition,
            "rounds": element.rounds,
            "rounds_per_aircraft": element.rounds_per_aircraft,
        }
    )


def _state(campaign: Campaign) -> dict:
    version, internal, gauss = campaign.rng.getstate()
    return _rounded(
        {
            "clock": campaign.clock,
            "mission_epoch": campaign.mission_epoch,
            "spawn_counter": campaign._spawn_counter,
            "package_counter": campaign._package_counter,
            "out_seq": campaign._out_seq,
            "rng": hashlib.sha256(
                json.dumps([version, list(internal), gauss]).encode()
            ).hexdigest(),
            "war_result": campaign.war_result,
            "live": sorted(campaign.live),
            "blocked": sorted(campaign.blocked),
            "packages": [_package(p) for _, p in sorted(campaign.packages.items())],
            "losses": [dataclasses.asdict(x) for x in campaign.tracker.losses],
            "targets": {
                k: v.units_alive for k, v in sorted(campaign.theater.targets.items())
            },
            "threats": {
                k: v.units_alive for k, v in sorted(campaign.theater.threats.items())
            },
            "squadrons": {
                s.id: {
                    "available": s.airframes_available,
                    "lost": s.airframes_lost,
                    "m_available": dict(s.munitions_available),
                    "m_expended": dict(s.munitions_expended),
                    "m_lost": dict(s.munitions_lost),
                    "open": len(s.open_reservations),
                }
                for inventory in (
                    campaign.inventories[k] for k in sorted(campaign.inventories)
                )
                for s in inventory.squadrons.values()
            },
        }
    )


def fingerprint_paper_war(seed: int) -> dict:
    campaign = Campaign(seed=seed, inventories=strike_only_oob())
    frames = []
    for _ in range(int(20_000 / PAPER_STEP)):
        frames.extend(campaign.advance(PAPER_STEP))
    return {"frames": [_frame(f) for f in frames], "state": _state(campaign)}


def fingerprint_watched_war(seed: int) -> dict:
    campaign = Campaign(seed=seed, inventories=strike_only_oob())
    dcs = drive(
        campaign,
        observer_positions=OBSERVER_AT_TARGET,
        damages=SCENARIO,
        deliver_events=True,
        start=0,
        duration=6000,
    )
    return {"frames": [_frame(f) for f in dcs.downlink], "state": _state(campaign)}


def _normalised(fingerprint: dict) -> dict:
    return json.loads(json.dumps(fingerprint, sort_keys=True))


def with_the_egress_fix(recorded: dict) -> tuple[dict, int]:
    """The recording, with the one change made on purpose applied to it.

    The pre-SEAD engine spawned a flight that re-entered the bubble after its
    TOT with its strike tasking intact. Its route has no attack waypoint left
    by then, so the client hung the attack on the landing waypoint, and a
    strike already resolved on paper could bomb its target a second time in
    DCS. That is a double resolution, and it is fixed: such a flight is now
    spawned with `{"kind": "egress", "callsign": ...}` and no task
    (campaign.Campaign._tasking). In the recorded wars it happens to red's
    raids flying home past the observer parked on the depot, near Bassel.

    Only the `tasking` of those spawn frames changes. Returns the adjusted
    recording and how many frames it touched.
    """
    adjusted = json.loads(json.dumps(recorded))
    epoch = adjusted["state"]["mission_epoch"]
    tots = {p["spawn_id"]: p["t_tot"] for p in adjusted["state"]["packages"]}
    touched = 0
    for frame in adjusted["frames"]:
        if frame["type"] != "spawn" or frame["spawn_id"] not in tots:
            continue
        if frame["t"] + epoch >= tots[frame["spawn_id"]]:
            frame["tasking"] = {
                "kind": "egress",
                "callsign": frame["tasking"]["callsign"],
            }
            touched += 1
    return adjusted, touched


def with_the_current_protocol(recorded: dict) -> tuple[dict, int]:
    """The recording, with the wire version it was made under brought up to date.

    The recording was made under protocol 2. Protocol v3 (docs/protocol.md,
    "Changes from v2") adds ammunition to the uplink snapshot, and v4
    ("Changes from v3") the surviving units of a ground group by type. v4
    also lets an air-defence spawn carry a `composition`, but only for a
    battery the client's radar-first build would get wrong -- one that has
    lost its radar -- and in these strike-only wars no site loses anything.
    So the engine's `sync`, which says which version it speaks, is the only
    thing either bump can change in a downlink recording: one integer in one
    frame. Returns the adjusted recording and how many frames it touched.
    """
    adjusted = json.loads(json.dumps(recorded))
    touched = 0
    for frame in adjusted["frames"]:
        if frame["type"] == "sync" and frame["protocol"] == 2:
            frame["protocol"] = PROTOCOL_VERSION
            touched += 1
    return adjusted, touched


class TestAStrikeOnlyPackageIsTheOneFlightPackage(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.recorded = json.loads(FIXTURE.read_text(encoding="utf-8"))

    def test_the_recording_is_not_vacuous(self):
        paper = self.recorded["paper"]
        self.assertEqual(len(paper), 8)
        self.assertEqual(len(self.recorded["watched"]), 3)
        wars = list(paper.values()) + list(self.recorded["watched"].values())
        self.assertTrue(all(war["state"]["losses"] for war in wars))
        self.assertTrue(all(len(war["state"]["packages"]) >= 2 for war in wars))
        self.assertTrue(
            any(x["cause"] == "unobserved" and x["entity_kind"] == "flight"
                for war in wars for x in war["state"]["losses"]),
            "no paper flight loss was recorded, so exposure is not covered",
        )
        self.assertTrue(
            any(frame["type"] == "spawn" for war in self.recorded["watched"].values()
                for frame in war["frames"]),
            "no spawn in the watched wars, so the wire is not covered",
        )

    def test_no_package_in_these_wars_has_a_sead_element(self):
        campaign = Campaign(seed=0, inventories=strike_only_oob())
        campaign.advance(20_000.0)
        self.assertTrue(campaign.packages)
        for package in campaign.packages.values():
            self.assertEqual([e.role for e in package.elements], ["strike"])

    def test_every_paper_war_is_the_recorded_one(self):
        for seed, recorded in sorted(self.recorded["paper"].items()):
            with self.subTest(seed=seed):
                self.assertEqual(
                    _normalised(fingerprint_paper_war(int(seed))), recorded
                )

    def test_every_watched_war_is_the_recorded_one(self):
        """Frame for frame, but for the flights sent home without a task and
        the protocol version the sync frame names."""
        for seed, recorded in sorted(self.recorded["watched"].items()):
            with self.subTest(seed=seed):
                current, synced = with_the_current_protocol(recorded)
                self.assertEqual(synced, 1, "one sync per watched war")
                expected, touched = with_the_egress_fix(current)
                self.assertGreater(touched, 0, "no post-TOT spawn; the fix is untested")
                self.assertEqual(
                    _normalised(fingerprint_watched_war(int(seed))), expected
                )

    def test_the_egress_fix_changes_nothing_but_those_taskings(self):
        """The adjustment above cannot hide anything else that moved."""
        for seed, recorded in sorted(self.recorded["watched"].items()):
            expected, _ = with_the_egress_fix(recorded)
            self.assertEqual(expected["state"], recorded["state"])
            self.assertEqual(len(expected["frames"]), len(recorded["frames"]))
            for new, old in zip(expected["frames"], recorded["frames"]):
                self.assertEqual(
                    {k: v for k, v in new.items() if k != "tasking"},
                    {k: v for k, v in old.items() if k != "tasking"},
                )
                if new != old:
                    self.assertEqual(old["tasking"]["kind"], "strike")
                    self.assertEqual(new["tasking"]["kind"], "egress")

    def test_the_version_bump_changes_nothing_but_the_sync_protocol(self):
        """Nor can the other adjustment: one integer, in the one sync frame."""
        for seed, recorded in sorted(self.recorded["watched"].items()):
            expected, _ = with_the_current_protocol(recorded)
            self.assertEqual(expected["state"], recorded["state"])
            self.assertEqual(len(expected["frames"]), len(recorded["frames"]))
            changed = [
                (new, old)
                for new, old in zip(expected["frames"], recorded["frames"])
                if new != old
            ]
            self.assertEqual(len(changed), 1, seed)
            new, old = changed[0]
            self.assertEqual(old["type"], "sync")
            self.assertEqual((old["protocol"], new["protocol"]), (2, 4))
            self.assertEqual(
                {k: v for k, v in new.items() if k != "protocol"},
                {k: v for k, v in old.items() if k != "protocol"},
            )


if __name__ == "__main__":
    unittest.main()
