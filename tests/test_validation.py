"""The template validator, executed -- and its report, parsed.

`mission/validate_templates.lua` exists to answer one question inside DCS:
which of the content strings `mission/campaign_client.lua` names does DCS
actually accept? Nothing in this file can answer that question -- the mock
accepts any well-formed table, which is precisely why the validator had to be
written. What this file proves is that the *instrument* works:

* it runs to completion under the mock, spreading its work across scheduled
  ticks rather than stalling one;
* a template DCS refuses comes back REJECTED;
* a template DCS accepts and then creates nothing -- the silent failure the
  client guards against -- comes back ORPHAN, not OK;
* the pylon probe can tell a loaded pylon from an empty one, and says
  UNKNOWN rather than guessing when it cannot look;
* it leaves the mission exactly as it found it, including after a run where
  every spawn failed;
* the values it tests match `CampaignClient.TEMPLATES`, so the two cannot
  drift apart without a test going red;
* `tools/parse_validation.py` reads both of the validator's output channels,
  agrees with itself across them, and exits non-zero when something required
  failed.

The Lua half requires `lupa` and skips without it, exactly as
tests/test_mission_client.py does. The parser half is standard library and
always runs.
"""

from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools import parse_validation

try:
    import lupa  # noqa: F401

    HAVE_LUPA = True
except ImportError:  # pragma: no cover - exercised on hosts without lupa
    HAVE_LUPA = False

if HAVE_LUPA:
    from tests.dcsmock import DCSMock, MISSION, lua_to_py

requires_lua = unittest.skipUnless(
    HAVE_LUPA, "lupa is not installed; the Lua validator cannot be executed"
)

VALIDATOR = "validate_templates.lua"

#: Every case the validator must report on. Losing one silently would make a
#: green run mean less than it looks like it means.
EXPECTED_IDS = {
    "drift.templates",
    "template.F-16C_strike_jdam",
    "template.F-16C_cap",
    "template.fuel_depot_medium",
    "country.USA",
    "country.RUSSIA",
    "country.SWITZERLAND",
    "waypoint.turning_point",
    "waypoint.fly_over_point",
    "waypoint.landing",
    "alt_type.BARO",
    "grouptask.Ground_Attack",
    "grouptask.CAP",
    "grouptask.Nothing",
    "task.Bombing",
    "task.AttackGroup",
    "pylon.baseline_empty",
}


# --------------------------------------------------------------------------
# Harness
# --------------------------------------------------------------------------

#: Wraps the mock's coalition.addGroup so spawned units answer getAmmo. The
#: mock has no weapons model and this file must not change it, so the
#: ammunition a jet reports is decided here: the gun always (category 0,
#: which the probe must ignore) and one bomb per pylon whose CLSID is in
#: `loaded`. With `always` set, every jet reports a bomb whatever is on its
#: pylons -- which is how a broken probe would behave.
_AMMO_SHIM = """
local loaded, always = ...
local original = coalition.addGroup
coalition.addGroup = function(cid, cat, data)
    local group = original(cid, cat, data)
    if group and type(data) == "table" and type(data.units) == "table" then
        local units = group:getUnits()
        for i = 1, #units do
            local ammo = {}
            -- The internal gun. `gun = 100` puts it on every one of these
            -- jets whether or not a pylon loaded, so a probe that counts it
            -- would call an unarmed strike package armed.
            ammo[#ammo + 1] = {count = 100,
                               desc = {category = 0, typeName = "M61A1"}}
            local payload = data.units[i] and data.units[i].payload
            if payload and type(payload.pylons) == "table" then
                for _, pylon in pairs(payload.pylons) do
                    local name = loaded[pylon.CLSID]
                    if name then
                        ammo[#ammo + 1] = {count = 2,
                                           desc = {category = 3, typeName = name}}
                    end
                end
            end
            if always then
                ammo[#ammo + 1] = {count = 2,
                                   desc = {category = 3, typeName = "PHANTOM"}}
            end
            units[i].getAmmo = function() return ammo end
        end
    end
    return group
end
"""


class ValidatorRun:
    """One mock DCS with mission/validate_templates.lua loaded into it."""

    def __init__(
        self,
        *,
        config: dict | None = None,
        with_client: bool = False,
        ammo: dict[str, str] | None = None,
        ammo_always: bool = False,
        autoload: bool = True,
    ) -> None:
        # Port 1 has nothing listening. Only the `with_client` runs open a
        # socket at all, and a refused connect is a path the client already
        # has tests for; nothing here waits on it.
        self.mock = DCSMock(port=1, tick=0.1, autostart=False)
        self.tmp = tempfile.TemporaryDirectory()
        self.json_path = Path(self.tmp.name) / "campaign_validation.json"

        if ammo is not None or ammo_always:
            shim = self.mock.lua.eval(f"function(...) {_AMMO_SHIM} end")
            shim(self.mock.lua.table_from(ammo or {}), ammo_always)

        if with_client:
            self.mock.load_client()

        settings = {
            "auto_start": False,
            "tick": 0.1,
            "out_path": str(self.json_path).replace("\\", "/"),
        }
        settings.update(config or {})
        # table_from is shallow, so any nested list of dicts is converted by
        # hand -- a Python dict handed to Lua does not answer `t.clsid`.
        if isinstance(settings.get("clsids"), list):
            settings["clsids"] = self.mock.lua.table_from(
                [self.mock.lua.table_from(entry) for entry in settings["clsids"]]
            )
        self.mock.lua.globals()["CAMPAIGN_VALIDATE_CONFIG"] = (
            self.mock.lua.table_from(settings)
        )

        self.validator = None
        if autoload:
            self.load()

    def load(self):
        self.validator = self.mock._dofile(MISSION / VALIDATOR)
        return self.validator

    def close(self) -> None:
        self.mock.stop()
        self.tmp.cleanup()

    # -- driving -----------------------------------------------------------

    def start(self) -> None:
        self.validator.start()

    def run_by_ticks(self, limit: int = 2000) -> int:
        """Start, then advance the clock until the run reports it is done."""
        self.start()
        ticks = 0
        while ticks < limit and not self.validator.finished():
            self.mock.advance(0.1)
            ticks += 1
        return ticks

    def run_now(self):
        self.start()
        self.validator.run_to_completion()
        return self.report()

    # -- reading -----------------------------------------------------------

    def report(self) -> dict:
        return lua_to_py(self.validator.report())

    def results(self) -> dict[str, dict]:
        report = self.report()
        return {r["id"]: r for r in report["results"]}

    def log_text(self) -> str:
        return "\n".join(
            f"2024-09-22 12:00:00.000 INFO    SCRIPTING: {line}"
            for line in self.mock.logs()
        )

    def leftovers(self) -> dict[str, list[str]]:
        return {
            "groups": self.mock.group_names(),
            "statics": self.mock.static_names(),
            "units": self.mock.unit_names(),
        }


def parse_text(text: str) -> parse_validation.Report:
    """Run text through the parser the way the CLI does, via a temp file."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report"
        path.write_text(text, encoding="utf-8")
        return parse_validation.read_report(path)


def run_cli(text: str, *args: str) -> tuple[int, str]:
    """`python tools/parse_validation.py <file> <args>`, in process."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "report"
        path.write_text(text, encoding="utf-8")
        return call_main(str(path), *args)


def call_main(*args: str) -> tuple[int, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = parse_validation.main(list(args))
    return code, out.getvalue() + err.getvalue()


# --------------------------------------------------------------------------
# 1. It runs, end to end, on the scheduler
# --------------------------------------------------------------------------


@requires_lua
class TestTheValidatorRuns(unittest.TestCase):
    """The whole instrument, driven by scheduled ticks as it is in DCS."""

    @classmethod
    def setUpClass(cls) -> None:
        runner = ValidatorRun()
        cls.runner = runner
        cls.addClassCleanup(runner.close)
        cls.ticks = runner.run_by_ticks()
        cls.report = runner.report()
        cls.by_id = {r["id"]: r for r in cls.report["results"]}

    def test_it_finished_and_the_lua_never_raised(self):
        self.assertTrue(self.report["complete"])
        self.assertEqual(self.runner.mock.scheduler_errors(), [])

    def test_every_case_the_client_depends_on_was_attempted(self):
        missing = EXPECTED_IDS - set(self.by_id)
        self.assertEqual(missing, set(), "cases silently dropped from the run")

    def test_nothing_required_failed_against_a_mock_that_accepts_everything(self):
        # Not a statement about DCS: the mock accepts any well-formed table.
        # It is a statement about the validator -- if a case fails HERE, the
        # case itself is malformed, and a real run would blame DCS for it.
        bad = [
            r["id"]
            for r in self.report["results"]
            if r["status"] in ("REJECTED", "ORPHAN", "ERROR", "DRIFT")
        ]
        self.assertEqual(bad, [], "a case failed against a permissive mock")

    def test_a_group_that_spawned_is_reported_retrievable_and_counted(self):
        strike = self.by_id["template.F-16C_strike_jdam"]
        self.assertEqual(strike["status"], "OK")
        self.assertTrue(strike["detail"]["retrievable"])
        self.assertTrue(strike["detail"]["retrievable_next"])
        self.assertEqual(strike["detail"]["units"], 2)
        self.assertEqual(strike["detail"]["wanted"], 2)

    def test_the_static_template_reports_the_object_was_retrievable(self):
        depot = self.by_id["template.fuel_depot_medium"]
        self.assertEqual(depot["status"], "OK")
        self.assertTrue(depot["detail"]["retrievable"])

    def test_it_left_the_mission_exactly_as_it_found_it(self):
        self.assertEqual(
            self.runner.leftovers(),
            {"groups": [], "statics": [], "units": []},
            "the validator leaked DCS objects into the mission",
        )
        self.assertEqual(self.report["leaked"], 0)

    def test_the_work_was_spread_over_ticks_rather_than_one_frame(self):
        # One case per tick, two ticks per case that created something. A
        # run that finished in one tick would be a frame hitch in DCS.
        self.assertGreater(self.ticks, len(self.report["results"]))

    def test_the_log_carries_a_begin_a_result_per_case_and_a_summary(self):
        lines = [
            line for line in self.runner.mock.logs()
            if line.startswith(parse_validation.PREFIX)
        ]
        self.assertTrue(any(" BEGIN " in line for line in lines))
        self.assertTrue(any(" SUMMARY " in line for line in lines))
        results = [line for line in lines if " RESULT " in line]
        self.assertEqual(len(results), len(self.report["results"]))

    def test_the_json_report_was_written_and_parses(self):
        data = json.loads(self.runner.json_path.read_text(encoding="utf-8"))
        self.assertTrue(data["complete"])
        self.assertEqual(len(data["results"]), len(self.report["results"]))


# --------------------------------------------------------------------------
# 2. The two ways DCS says no
# --------------------------------------------------------------------------


@requires_lua
class TestARejectedTemplateIsReportedRejected(unittest.TestCase):
    """DCS raising out of addGroup / addStaticObject."""

    def setUp(self) -> None:
        self.runner = ValidatorRun()
        self.addCleanup(self.runner.close)
        self.runner.mock.fail_next_group_spawn(silently=False)
        self.runner.mock.env.fail_add_static = True
        self.runner.mock.env.fail_add_static_silently = False

    def test_a_refused_group_template_is_rejected_with_the_error_text(self):
        results = self.runner.run_now()
        by_id = {r["id"]: r for r in results["results"]}
        case = by_id["template.F-16C_strike_jdam"]
        self.assertEqual(case["status"], "REJECTED")
        self.assertIn("injected failure", case["error"])
        self.assertFalse(case["detail"]["created"])

    def test_a_refused_static_template_is_rejected(self):
        by_id = {r["id"]: r for r in self.runner.run_now()["results"]}
        case = by_id["template.fuel_depot_medium"]
        self.assertEqual(case["status"], "REJECTED")
        self.assertIn("injected failure", case["error"])

    def test_a_run_where_everything_failed_still_finishes_and_leaves_nothing(self):
        report = self.runner.run_now()
        self.assertTrue(report["complete"])
        self.assertEqual(self.runner.mock.scheduler_errors(), [])
        self.assertEqual(
            self.runner.leftovers(), {"groups": [], "statics": [], "units": []}
        )

    def test_the_parser_exits_non_zero_on_that_run(self):
        self.runner.run_now()
        code, output = run_cli(self.runner.json_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 1)
        self.assertIn("required case(s) failed", output)


@requires_lua
class TestAnAcceptedButUnusableTemplateIsReportedOrphan(unittest.TestCase):
    """The failure the client itself guards against: DCS takes the table,
    raises nothing, and creates no object. An instrument that called that a
    pass would be worse than no instrument."""

    def setUp(self) -> None:
        self.runner = ValidatorRun()
        self.addCleanup(self.runner.close)
        self.runner.mock.fail_next_group_spawn(silently=True)
        self.runner.mock.env.fail_add_static = True
        self.runner.mock.env.fail_add_static_silently = True

    def test_a_silently_failed_group_is_orphan_not_ok(self):
        by_id = {r["id"]: r for r in self.runner.run_now()["results"]}
        case = by_id["template.F-16C_cap"]
        self.assertEqual(case["status"], "ORPHAN")
        self.assertTrue(case["detail"]["created"])
        self.assertFalse(case["detail"]["retrievable"])
        self.assertIn("getByName", case["error"])

    def test_a_silently_failed_static_is_orphan_not_ok(self):
        by_id = {r["id"]: r for r in self.runner.run_now()["results"]}
        case = by_id["template.fuel_depot_medium"]
        self.assertEqual(case["status"], "ORPHAN")
        self.assertFalse(case["detail"]["retrievable"])

    def test_nothing_is_left_behind_by_an_orphan_run(self):
        self.runner.run_now()
        self.assertEqual(
            self.runner.leftovers(), {"groups": [], "statics": [], "units": []}
        )


@requires_lua
class TestOneCaseFailingDoesNotStopTheRun(unittest.TestCase):
    """The whole point is one run instead of one crash at a time."""

    def test_a_static_that_fails_does_not_prevent_the_group_cases(self):
        run = ValidatorRun()
        self.addCleanup(run.close)
        run.mock.fail_static_named("cmpval_fuel_depot_medium", silently=False)
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        self.assertEqual(by_id["template.fuel_depot_medium"]["status"], "REJECTED")
        self.assertEqual(by_id["template.F-16C_cap"]["status"], "OK")
        self.assertEqual(by_id["task.AttackGroup"]["status"], "OK")
        self.assertEqual(run.mock.scheduler_errors(), [])


# --------------------------------------------------------------------------
# 3. Pylons -- the probe that has to distinguish armed from unarmed
# --------------------------------------------------------------------------


@requires_lua
class TestThePylonProbe(unittest.TestCase):

    CLSIDS = [
        {"clsid": "{NOT_REAL}", "pylon": 3, "label": "control", "expect": "empty"},
        {"clsid": "{REAL}", "pylon": 3, "label": "a CLSID this build knows"},
    ]

    def make(self, **kwargs):
        lua_clsids = [dict(entry) for entry in self.CLSIDS]
        run = ValidatorRun(config={"clsids": lua_clsids}, **kwargs)
        self.addCleanup(run.close)
        return run

    def test_a_known_clsid_reports_the_munition_it_loaded(self):
        run = self.make(ammo={"{REAL}": "GBU-38"})
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        case = by_id["pylon.2"]
        self.assertEqual(case["status"], "OK")
        self.assertEqual(case["detail"]["ammo"], 2)
        self.assertIn("GBU-38", case["detail"]["ammo_types"])

    def test_an_unknown_clsid_leaves_the_pylon_empty_and_says_so(self):
        run = self.make(ammo={"{REAL}": "GBU-38"})
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        control = by_id["pylon.1"]
        # The control expected an empty pylon and got one, so the probe can
        # tell the difference: that is what makes pylon.2 above mean anything.
        self.assertEqual(control["status"], "OK")
        self.assertEqual(control["detail"]["ammo"], 0)

    def test_the_clients_empty_payload_is_reported_as_carrying_nothing(self):
        run = self.make(ammo={"{REAL}": "GBU-38"})
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        baseline = by_id["pylon.baseline_empty"]
        self.assertEqual(baseline["status"], "OK")
        # This is the finding the whole probe exists for: the strike package
        # spawns with the gun and nothing else.
        self.assertEqual(baseline["detail"]["ammo"], 0)

    def test_the_gun_is_not_counted_as_a_munition(self):
        run = self.make(ammo={})
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        self.assertEqual(by_id["pylon.2"]["detail"]["ammo"], 0)
        self.assertEqual(by_id["pylon.2"]["status"], "UNKNOWN")
        self.assertIn("does not know that CLSID", by_id["pylon.2"]["error"])

    def test_a_probe_that_cannot_tell_loaded_from_empty_fails_its_control(self):
        run = self.make(ammo={}, ammo_always=True)
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        control = by_id["pylon.1"]
        self.assertEqual(control["status"], "REJECTED")
        self.assertTrue(control["required"])
        self.assertIn("cannot tell loaded from unarmed", control["error"])

    def test_without_getammo_it_says_unknown_rather_than_guessing(self):
        run = ValidatorRun()  # the plain mock has no weapons model at all
        self.addCleanup(run.close)
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        case = by_id["pylon.baseline_empty"]
        self.assertEqual(case["status"], "UNKNOWN")
        self.assertIn("getAmmo", case["error"])


# --------------------------------------------------------------------------
# 4. Drift -- the validator must test what the client actually asks for
# --------------------------------------------------------------------------


@requires_lua
class TestItValidatesWhatTheClientActuallyUses(unittest.TestCase):

    def test_the_mirrored_templates_match_the_clients_own_table(self):
        run = ValidatorRun(with_client=True)
        self.addCleanup(run.close)
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        drift = by_id["drift.templates"]
        self.assertEqual(
            drift["status"],
            "OK",
            "mission/validate_templates.lua is validating values "
            "mission/campaign_client.lua no longer uses: " + drift["error"],
        )
        self.assertEqual(drift["detail"]["mismatches"], 0)

    def test_a_template_changed_in_the_client_is_reported_as_drift(self):
        run = ValidatorRun(with_client=True, autoload=False)
        self.addCleanup(run.close)
        templates = run.mock.lua.globals().CampaignClient.TEMPLATES
        templates["F-16C_cap"]["unit_type"] = "F-15ESE"
        run.load()
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        drift = by_id["drift.templates"]
        self.assertEqual(drift["status"], "DRIFT")
        self.assertTrue(drift["required"])
        self.assertIn("F-16C_cap.unit_type", drift["error"])

    def test_a_template_added_to_the_client_is_reported_as_drift(self):
        run = ValidatorRun(with_client=True, autoload=False)
        self.addCleanup(run.close)
        templates = run.mock.lua.globals().CampaignClient.TEMPLATES
        templates["sa6_site"] = run.mock.lua.table_from(
            {"unit_type": "Kub 2P25 ln", "count": 4, "task": "Ground Nothing"}
        )
        run.load()
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        self.assertEqual(by_id["drift.templates"]["status"], "DRIFT")
        self.assertIn("sa6_site", by_id["drift.templates"]["error"])

    def test_without_the_client_loaded_the_cross_check_skips_rather_than_lying(self):
        run = ValidatorRun()
        self.addCleanup(run.close)
        by_id = {r["id"]: r for r in run.run_now()["results"]}
        drift = by_id["drift.templates"]
        self.assertEqual(drift["status"], "SKIP")
        self.assertFalse(drift["required"])

    def test_the_validator_needs_no_client_and_no_engine(self):
        # It loaded and ran in every test above with no campaign_client.lua
        # and nothing listening on the socket. Assert it explicitly.
        run = ValidatorRun()
        self.addCleanup(run.close)
        self.assertIsNone(run.mock.client)
        report = run.run_now()
        self.assertTrue(report["complete"])
        self.assertEqual(run.mock.sockets.opened, 0)


# --------------------------------------------------------------------------
# 5. It survives the environment it will actually meet
# --------------------------------------------------------------------------


@requires_lua
class TestItSurvivesASanitisedEnvironment(unittest.TestCase):
    """mission/README.md tells the owner to leave `io` sanitised, so the JSON
    report is a convenience and the log is the result. Losing the file must
    not lose the run."""

    def test_no_io_means_no_json_and_nothing_else_lost(self):
        runner = ValidatorRun(autoload=False)
        self.addCleanup(runner.close)
        runner.mock.lua.globals()["io"] = None
        runner.load()
        report = runner.run_now()

        self.assertTrue(report["complete"])
        self.assertFalse(runner.json_path.exists())
        self.assertEqual(runner.mock.scheduler_errors(), [])
        summary = [l for l in runner.mock.logs() if " SUMMARY " in l]
        self.assertEqual(len(summary), 1)
        self.assertIn("json_written=0", summary[0])
        self.assertIn("sanitised", summary[0])
        # The log is still a complete, parseable result.
        parsed = parse_text(runner.log_text())
        self.assertTrue(parsed.complete)
        self.assertIn("template.F-16C_cap", {r["id"] for r in parsed.results})


@requires_lua
class TestStoppingHalfwayLeavesNothingBehind(unittest.TestCase):
    """A mission that ends, or an owner who calls stop(), mid-run."""

    def test_a_run_stopped_in_flight_destroys_what_it_had_created(self):
        runner = ValidatorRun()
        self.addCleanup(runner.close)
        runner.start()
        for _ in range(7):
            runner.mock.advance(0.1)
        self.assertFalse(runner.validator.finished())
        # Something is live at this point; that is what makes the stop mean
        # anything.
        live = runner.leftovers()
        self.assertTrue(live["groups"] or live["statics"])

        runner.validator.stop()
        self.assertTrue(runner.validator.finished())
        self.assertEqual(
            runner.leftovers(), {"groups": [], "statics": [], "units": []}
        )
        report = runner.report()
        self.assertEqual(report["leaked"], 0)
        # A partial run must never look like a complete pass.
        parsed = parse_text(runner.json_path.read_text(encoding="utf-8"))
        self.assertLess(len(parsed.results), len(EXPECTED_IDS))


# --------------------------------------------------------------------------
# 6. The two output channels have to agree
# --------------------------------------------------------------------------


@requires_lua
class TestTheLogAndTheJsonSayTheSameThing(unittest.TestCase):

    def test_parsing_the_log_gives_the_same_verdicts_as_the_json(self):
        run = ValidatorRun()
        self.addCleanup(run.close)
        run.mock.fail_next_group_spawn(silently=True)
        run.run_now()

        from_log = parse_text(run.log_text())
        from_json = parse_text(run.json_path.read_text(encoding="utf-8"))

        self.assertEqual(from_log.source, "log")
        self.assertEqual(from_json.source, "json")
        self.assertTrue(from_log.complete)
        self.assertTrue(from_json.complete)
        self.assertEqual(
            [(r["id"], r["status"], r["required"]) for r in from_log.results],
            [(r["id"], r["status"], r["required"]) for r in from_json.results],
        )

    def test_the_log_survives_dcs_log_decoration(self):
        run = ValidatorRun()
        self.addCleanup(run.close)
        run.run_now()
        report = parse_text(run.log_text())
        self.assertIn(
            "template.F-16C_strike_jdam", {r["id"] for r in report.results}
        )
        errors = [r for r in report.results if r["error"]]
        # Quoted error text must survive the trip through the log format.
        self.assertTrue(all("\n" not in r["error"] for r in errors))


# --------------------------------------------------------------------------
# 7. The parser, on its own. No lupa required.
# --------------------------------------------------------------------------


def make_result(id_, status, required=True, error="", **detail):
    return {
        "id": id_,
        "kind": "group",
        "status": status,
        "required": required,
        "label": id_ + " label",
        "error": error,
        "detail": detail,
    }


def make_json(results, complete=True):
    return json.dumps(
        {
            "version": 1,
            "complete": complete,
            "leaked": 0,
            "theatre": "Syria",
            "template_source": "mirror",
            "results": results,
        }
    )


def make_log(results, complete=True):
    lines = [parse_validation.PREFIX + "BEGIN version=1 cases=%d" % len(results)]
    for r in results:
        lines.append(
            parse_validation.PREFIX
            + 'RESULT status=%s required=%d id=%s kind=%s label="%s" error="%s"'
            % (
                r["status"],
                1 if r["required"] else 0,
                r["id"],
                r["kind"],
                r["label"],
                r["error"],
            )
        )
    if complete:
        lines.append(parse_validation.PREFIX + "SUMMARY total=%d leaked=0" % len(results))
    return "\n".join("INFO SCRIPTING: " + line for line in lines)


class TestTheParser(unittest.TestCase):

    def test_a_clean_run_exits_zero(self):
        text = make_json([make_result("template.a", "OK")])
        code, output = run_cli(text)
        self.assertEqual(code, 0)
        self.assertIn("every required case passed", output)

    def test_a_required_rejection_exits_one(self):
        text = make_json(
            [
                make_result("template.a", "OK"),
                make_result("template.b", "REJECTED", error="unknown unit type"),
            ]
        )
        code, output = run_cli(text)
        self.assertEqual(code, 1)
        self.assertIn("unknown unit type", output)

    def test_an_orphan_is_a_failure_because_the_object_never_existed(self):
        text = make_json([make_result("template.b", "ORPHAN")])
        self.assertEqual(run_cli(text)[0], 1)

    def test_drift_is_a_failure(self):
        text = make_json([make_result("drift.templates", "DRIFT")])
        self.assertEqual(run_cli(text)[0], 1)

    def test_an_optional_failure_does_not_fail_the_run(self):
        text = make_json(
            [
                make_result("template.a", "OK"),
                make_result("static.alt", "REJECTED", required=False),
            ]
        )
        code, output = run_cli(text)
        self.assertEqual(code, 0)
        self.assertIn("REJECTED", output)

    def test_unknown_is_not_a_failure_until_strict(self):
        text = make_json([make_result("pylon.1", "UNKNOWN", error="no getAmmo")])
        self.assertEqual(run_cli(text)[0], 0)
        self.assertEqual(run_cli(text, "--strict")[0], 1)

    def test_an_unfinished_run_is_never_reported_as_a_pass(self):
        text = make_json([make_result("template.a", "OK")], complete=False)
        code, output = run_cli(text)
        self.assertEqual(code, 2)
        self.assertIn("did not finish", output)

    def test_a_log_with_no_validator_lines_exits_two(self):
        code, output = run_cli("nothing to see here\nanother line\n")
        self.assertEqual(code, 2)
        self.assertIn("no validator results", output)

    def test_a_missing_file_exits_two(self):
        code, output = call_main("no/such/file.json")
        self.assertEqual(code, 2)
        self.assertIn("no such file", output)

    def test_malformed_json_exits_two(self):
        code, _ = run_cli('{"not": "a report"}')
        self.assertEqual(code, 2)

    def test_the_log_and_json_forms_produce_the_same_verdict(self):
        results = [
            make_result("template.a", "OK"),
            make_result("template.b", "REJECTED", error="bad type"),
            make_result("pylon.1", "UNKNOWN", required=False, error="no ammo"),
        ]
        from_json = parse_text(make_json(results))
        from_log = parse_text(make_log(results))
        self.assertEqual(
            [(r["id"], r["status"], r["required"]) for r in from_json.results],
            [(r["id"], r["status"], r["required"]) for r in from_log.results],
        )
        self.assertEqual(
            len(from_json.failures(strict=False)),
            len(from_log.failures(strict=False)),
        )

    def test_quoted_error_text_with_spaces_survives_the_log_format(self):
        report = parse_text(
            parse_validation.PREFIX
            + 'RESULT status=REJECTED required=1 id=t.a kind=group '
            'label="a label with spaces" error="DCS said: no such unit type"\n'
            + parse_validation.PREFIX
            + "SUMMARY total=1\n"
        )
        self.assertEqual(len(report.results), 1)
        self.assertEqual(
            report.results[0]["error"], "DCS said: no such unit type"
        )
        self.assertEqual(report.results[0]["label"], "a label with spaces")

    def test_detail_fields_reach_the_rendered_table(self):
        text = make_json(
            [make_result("template.a", "OK", units=2, wanted=2, retrievable=True)]
        )
        _, output = run_cli(text)
        self.assertIn("units=2", output)
        self.assertIn("retrievable=yes", output)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
