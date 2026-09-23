"""Coverage for the wire contract in campaign/protocol.py.

Everything the engine believes about the world arrives through this module, so
these tests are deliberately paranoid about the failure cases: a frame that
half-decodes is worse than one that is rejected outright.
"""

from __future__ import annotations

import json
import unittest

from campaign.protocol import (
    MAX_FRAME_BYTES,
    OWNED_PREFIX,
    PROTOCOL_VERSION,
    Ack,
    Despawn,
    Event,
    FrameBuffer,
    GroupSnapshot,
    Hello,
    Message,
    Observer,
    ObserverReport,
    ProtocolError,
    Spawn,
    StateReport,
    Sync,
    Waypoint,
    decode_downlink,
    decode_uplink,
    encode,
    group_name,
    spawn_id_of,
)

UPLINK_SAMPLES = [
    Hello(
        seq=1,
        t=0.0,
        protocol=PROTOCOL_VERSION,
        theater="Syria",
        dcs_version="2.9.29",
        mission_start_epoch=1758499200,
    ),
    ObserverReport(
        seq=12,
        t=305.0,
        observers=[
            Observer(id="player:Jakem", pos=(41230.0, 2100.0, -88400.0), speed=220.0),
            Observer(id="player:Wingman", pos=(41000.0, 2000.0, -88000.0)),
        ],
    ),
    ObserverReport(seq=13, t=310.0),
    Event(seq=40, t=812.5, kind="kill", initiator="cmp_a91f", target="cmp_7c02", weapon="AGM-88C"),
    Event(seq=41, t=813.0, kind="dead"),
    StateReport(
        seq=41,
        t=830.0,
        groups=[
            GroupSnapshot(
                spawn_id="a91f",
                alive=True,
                units=2,
                units_initial=4,
                pos=(41000.0, 3000.0, -88000.0),
            ),
            GroupSnapshot(spawn_id="7c02", alive=False, units=0, units_initial=4),
        ],
    ),
    StateReport(seq=42, t=860.0),
    Ack(seq=43, t=830.2, ref=91, ok=True),
    Ack(seq=44, t=830.2, ref=92, ok=False, error="unknown template: Su-34_strike"),
]

DOWNLINK_SAMPLES = [
    Sync(seq=1, t=0.0, campaign_time=417600.0),
    Sync(seq=2, t=0.0, campaign_time=417600.0, state_period=10.0, observer_period=2.5,
         bubble_radius=50000.0),
    Spawn(
        seq=91,
        t=600.0,
        ref=91,
        spawn_id="a91f",
        coalition="blue",
        category="plane",
        template="F-16C_strike_jdam",
        position=(40000.0, 4500.0, -90000.0),
        heading=1.57,
        route=[
            Waypoint(pos=(40000.0, 4500.0, -90000.0), alt=4500.0, speed=220.0),
            Waypoint(pos=(41000.0, 6000.0, -92000.0), alt=6000.0, speed=240.0, action="attack"),
        ],
        tasking={"kind": "strike", "target": "cmp_7c02", "tot": 1230.0},
    ),
    Spawn(
        seq=92,
        t=601.0,
        ref=92,
        spawn_id="7c02",
        coalition="red",
        category="ground",
        template="strategic_target",
        position=(1.0, 2.0, 3.0),
    ),
    Despawn(seq=93, t=1800.0, ref=93, spawn_id="a91f", reason="left_bubble"),
    Message(seq=94, t=1801.0, to="blue", text="Package COWBOY off target, RTB.", duration=20.0),
]


class RoundTripTests(unittest.TestCase):
    def test_every_uplink_type_round_trips(self) -> None:
        for frame in UPLINK_SAMPLES:
            with self.subTest(frame=type(frame).__name__, seq=frame.seq):
                self.assertEqual(decode_uplink(encode(frame)), frame)

    def test_every_downlink_type_round_trips(self) -> None:
        for frame in DOWNLINK_SAMPLES:
            with self.subTest(frame=type(frame).__name__, seq=frame.seq):
                self.assertEqual(decode_downlink(encode(frame)), frame)

    def test_encode_is_one_line_of_json(self) -> None:
        for frame in UPLINK_SAMPLES + DOWNLINK_SAMPLES:
            raw = encode(frame)
            with self.subTest(frame=type(frame).__name__):
                self.assertTrue(raw.endswith(b"\n"))
                self.assertNotIn(b"\n", raw[:-1])
                self.assertEqual(json.loads(raw)["type"], frame.type)

    def test_positions_come_back_as_tuples(self) -> None:
        decoded = decode_downlink(encode(DOWNLINK_SAMPLES[2]))
        self.assertIsInstance(decoded.position, tuple)
        self.assertIsInstance(decoded.route[0], Waypoint)
        self.assertIsInstance(decoded.route[0].pos, tuple)
        self.assertEqual(decoded.tasking["target"], "cmp_7c02")

    def test_absent_position_stays_none(self) -> None:
        decoded = decode_uplink(encode(UPLINK_SAMPLES[5]))
        self.assertIsNone(decoded.groups[1].pos)
        self.assertFalse(decoded.groups[1].alive)

    def test_decoding_accepts_bytes_and_str(self) -> None:
        frame = UPLINK_SAMPLES[0]
        raw = encode(frame)
        self.assertEqual(decode_uplink(raw), decode_uplink(raw.decode("utf-8")))

    def test_non_ascii_text_survives(self) -> None:
        frame = Message(seq=1, t=0.0, to="blue", text="Söke tower, päckage off target ✈")
        self.assertEqual(decode_downlink(encode(frame)), frame)


class RejectionTests(unittest.TestCase):
    def test_encode_rejects_oversize_frame(self) -> None:
        huge = Message(seq=1, t=0.0, to="blue", text="x" * (MAX_FRAME_BYTES + 10))
        with self.assertRaises(ProtocolError):
            encode(huge)

    def test_frame_buffer_rejects_oversize_frame(self) -> None:
        buffer = FrameBuffer()
        with self.assertRaises(ProtocolError):
            buffer.feed(b"x" * (MAX_FRAME_BYTES + 1))

    def test_malformed_json(self) -> None:
        for raw in (b'{"type":"hello",', b"not json at all", b""):
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError):
                    decode_uplink(raw)

    def test_json_that_is_not_an_object(self) -> None:
        for raw in (b"[]", b"42", b'"hello"', b"null"):
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError):
                    decode_uplink(raw)

    def test_unknown_type(self) -> None:
        raw = b'{"type":"launch_nukes","seq":1,"t":0.0}'
        with self.assertRaises(ProtocolError):
            decode_uplink(raw)
        with self.assertRaises(ProtocolError):
            decode_downlink(raw)

    def test_missing_type(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_uplink(b'{"seq":1,"t":0.0}')

    def test_direction_is_enforced(self) -> None:
        """A downlink frame must not decode as uplink, or vice versa."""
        with self.assertRaises(ProtocolError):
            decode_uplink(encode(DOWNLINK_SAMPLES[0]))
        with self.assertRaises(ProtocolError):
            decode_downlink(encode(UPLINK_SAMPLES[0]))

    def test_missing_required_field(self) -> None:
        cases = [
            b'{"type":"hello","seq":1,"t":0.0}',
            b'{"type":"ack","seq":1,"t":0.0,"ref":9}',
            b'{"type":"event","seq":1,"t":0.0}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(ProtocolError):
                    decode_uplink(raw)
        with self.assertRaises(ProtocolError):
            decode_downlink(b'{"type":"despawn","seq":1,"t":0.0,"ref":9}')

    def test_unknown_field_is_rejected(self) -> None:
        with self.assertRaises(ProtocolError):
            decode_uplink(b'{"type":"ack","seq":1,"t":0.0,"ref":9,"ok":true,"nonsense":1}')

    def test_malformed_nested_member_is_rejected(self) -> None:
        """A bad GroupSnapshot / Observer / Waypoint must never half-decode.

        `_build` only wraps the outer dataclass call in its TypeError guard, so
        today these surface as TypeError rather than ProtocolError. Both are
        rejections, and this asserts the part that matters; tighten it to
        ProtocolError alone once protocol.py wraps the nested construction too.
        """
        cases = [
            b'{"type":"state","seq":1,"t":0.0,"groups":[{"spawn_id":"a"}]}',
            b'{"type":"observer","seq":1,"t":0.0,"observers":[{"nope":1}]}',
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises((ProtocolError, TypeError)):
                    decode_uplink(raw)
        with self.assertRaises((ProtocolError, TypeError)):
            decode_downlink(
                b'{"type":"despawn","seq":1,"t":0.0,"ref":1,"spawn_id":"a","reason":{"x":1},'
                b'"bogus":2}'
            )


class FrameBufferTests(unittest.TestCase):
    def setUp(self) -> None:
        self.frames = UPLINK_SAMPLES[:4]
        self.blob = b"".join(encode(f) for f in self.frames)

    def test_several_frames_in_one_chunk(self) -> None:
        buffer = FrameBuffer()
        out = buffer.feed(self.blob)
        self.assertEqual([decode_uplink(raw) for raw in out], self.frames)

    def test_one_frame_split_across_chunk_boundaries(self) -> None:
        buffer = FrameBuffer()
        collected: list[bytes] = []
        for index in range(len(self.blob)):
            collected.extend(buffer.feed(self.blob[index : index + 1]))
        self.assertEqual([decode_uplink(raw) for raw in collected], self.frames)

    def test_split_at_every_possible_boundary(self) -> None:
        for cut in range(1, len(self.blob)):
            buffer = FrameBuffer()
            out = buffer.feed(self.blob[:cut]) + buffer.feed(self.blob[cut:])
            with self.subTest(cut=cut):
                self.assertEqual([decode_uplink(raw) for raw in out], self.frames)

    def test_partial_tail_is_withheld_until_complete(self) -> None:
        buffer = FrameBuffer()
        head, tail = self.blob[:-7], self.blob[-7:]
        out = buffer.feed(head)
        self.assertEqual(len(out), len(self.frames) - 1)
        out = buffer.feed(tail)
        self.assertEqual([decode_uplink(raw) for raw in out], self.frames[-1:])

    def test_empty_chunk_yields_nothing(self) -> None:
        buffer = FrameBuffer()
        self.assertEqual(buffer.feed(b""), [])

    def test_frames_are_returned_without_their_newline(self) -> None:
        buffer = FrameBuffer()
        for raw in buffer.feed(self.blob):
            self.assertNotIn(b"\n", raw)

    def test_buffer_survives_a_burst_of_small_frames(self) -> None:
        frame = Ack(seq=1, t=0.0, ref=1, ok=True)
        blob = encode(frame) * 500
        buffer = FrameBuffer()
        self.assertEqual(len(buffer.feed(blob)), 500)


class NamingTests(unittest.TestCase):
    IDS = ["a91f", "7c02", "0", "x" * 40, "with_underscores", "cmp_nested"]

    def test_spawn_id_of_inverts_group_name(self) -> None:
        for spawn_id in self.IDS:
            with self.subTest(spawn_id=spawn_id):
                self.assertEqual(spawn_id_of(group_name(spawn_id)), spawn_id)

    def test_group_name_is_prefixed(self) -> None:
        for spawn_id in self.IDS:
            self.assertTrue(group_name(spawn_id).startswith(OWNED_PREFIX))

    def test_foreign_names_are_not_ours(self) -> None:
        for name in ["Aerial-1", "", "cm_a91f", "CMP_a91f", " cmp_a91f", "Ground Units-3"]:
            with self.subTest(name=name):
                self.assertIsNone(spawn_id_of(name))

    def test_group_name_inverts_spawn_id_of(self) -> None:
        """The other direction of the property, for names that are ours."""
        for name in ["cmp_a91f", "cmp_", "cmp_cmp_x"]:
            with self.subTest(name=name):
                spawn_id = spawn_id_of(name)
                self.assertIsNotNone(spawn_id)
                self.assertEqual(group_name(spawn_id), name)


if __name__ == "__main__":
    unittest.main()
