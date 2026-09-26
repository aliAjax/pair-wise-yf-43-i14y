import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, InvalidTransition, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class IntermediateCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.met = Actor("m1", "metrology")
        self.auth = Actor("q1", "authorizer")
        self.analyst = Actor("a1", "analyst")
        self.tech = Actor("t1", "technician")

    def tearDown(self):
        self.tmp.cleanup()

    def _setup(self, standard_due="2099-01-01"):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(
            self.admin, instrument["id"], "send_calibration", {}
        )
        self.service.transition(
            self.admin, instrument["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        standard = self.service.create(
            self.met,
            "standard",
            {"name": "Ref-Gauge", "serial": "RG-1", "due_at": standard_due},
        )
        method = self.service.create(
            self.admin, "method", {"name": "Assay", "version": "v1"}
        )
        self.service.transition(
            self.auth, method["id"], "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument["id"]]},
        )
        return instrument, standard, method

    def _release(self, instrument, method, sample, at, value=4.2):
        result = self.service.create(
            self.analyst, "result", {"sample_id": sample, "measurement": "x"}
        )
        return self.service.transition(
            self.analyst,
            result["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": value,
                "unit": "mg/L",
                "released_at": at,
            },
        )

    def _check(self, instrument, standard, checked_at, standard_value=10.0,
               measured=10.0, tolerance=0.1):
        check = self.service.create(
            self.tech,
            "check",
            {"instrument_id": instrument["id"], "standard_id": standard["id"]},
        )
        return self.service.transition(
            self.met,
            check["id"],
            "perform",
            {
                "standard_value": standard_value,
                "measured_value": measured,
                "tolerance": tolerance,
                "checked_at": checked_at,
            },
        )

    def test_successful_check_registers_evidence(self):
        instrument, standard, method = self._setup()
        check = self._check(instrument, standard, "2026-03-01")
        self.assertEqual(check["status"], "passed")
        self.assertEqual(check["data"]["deviation"], 0.0)
        self.assertEqual(check["data"]["standard_id"], standard["id"])
        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertEqual(
            instrument["data"]["last_successful_check_at"], "2026-03-01"
        )

    def _fail_scenario(self):
        instrument, standard, method = self._setup()
        # 核查通过基线之前放行的结果不受追溯。
        old = self._release(instrument, method, "S-OLD", "2026-02-01")
        self._check(instrument, standard, "2026-03-01")
        # 基线之后放行的结果进入追溯窗口。
        recent = self._release(instrument, method, "S-NEW", "2026-04-01")
        # 尚未放行的结果应被阻塞。
        pending = self.service.create(
            self.analyst, "result", {"sample_id": "S-PEND", "measurement": "x"}
        )

        expired_standard = self.service.create(
            self.met,
            "standard",
            {"name": "Old-Gauge", "serial": "RG-OLD", "due_at": "2026-01-01"},
        )
        failed = self._check(
            instrument, expired_standard, "2026-05-01", measured=10.0
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("expired", failed["data"]["reasons"][0])

        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "stopped")
        self.assertIn("expired", instrument["data"]["stop_reason"])
        self.assertEqual(instrument["data"]["stopped_by_check"], failed["id"])

        old = self.service.get(old["id"])
        recent = self.service.get(recent["id"])
        pending = self.service.get(pending["id"])
        self.assertEqual(old["status"], "released")  # 基线前放行不受影响
        self.assertEqual(recent["status"], "review")
        self.assertIn(failed["id"], recent["data"]["review_reason"])
        self.assertEqual(pending["status"], "blocked")
        self.assertIn("blocked by failed intermediate check",
                      pending["data"]["block_reason"])
        self.assertEqual(failed["data"]["affected"]["review_results"], [recent["id"]])
        self.assertEqual(failed["data"]["affected"]["blocked_results"], [pending["id"]])

        # 停用期间不得放行新结果，错误信息给出阻塞原因。
        with self.assertRaises(ValidationError) as ctx:
            self._release(instrument, method, "S-X", "2026-05-02")
        self.assertIn("instrument stopped", str(ctx.exception))

        return failed, instrument, recent, pending, method

    def test_deviation_out_of_tolerance_stops_instrument(self):
        instrument, standard, method = self._setup()
        failed = self._check(
            instrument, standard, "2026-03-01",
            standard_value=10.0, measured=10.5, tolerance=0.1,
        )
        self.assertEqual(failed["status"], "failed")
        self.assertIn("exceeds tolerance", failed["data"]["reasons"][0])
        self.assertEqual(self.service.get(instrument["id"])["status"], "stopped")

    def test_recalibrate_then_republish_keeps_timeline(self):
        failed, instrument, recent, pending, method = self._fail_scenario()
        original_release = [
            item for item in self.service.audit_log(recent["id"])
            if item["action"] == "release"
        ]
        self.assertEqual(len(original_release), 1)

        # 新校准合格后仪器恢复使用。
        self.service.transition(
            self.admin, instrument["id"], "send_calibration", {}
        )
        self.service.transition(
            self.admin, instrument["id"], "calibrate",
            {"due_at": "2099-12-31", "passed": True},
        )
        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "active")
        self.assertIsNone(instrument["data"]["stop_reason"])

        # 未确认无影响时不能重新发布。
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.auth, recent["id"], "republish",
                {"impact_assessment": "records verified", "no_impact": False},
            )
        republished = self.service.transition(
            self.auth,
            recent["id"],
            "republish",
            {"impact_assessment": "records verified against reference lab",
             "no_impact": True},
        )
        self.assertEqual(republished["status"], "released")
        # 原放行记录保留，复核结论补在同一实体上。
        self.assertEqual(republished["data"]["released_by"], "a1")
        self.assertEqual(republished["data"]["republished_by"], "q1")
        actions = [item["action"] for item in self.service.audit_log(recent["id"])]
        self.assertEqual(
            actions, ["create", "release", "check_failed_hold_for_review", "republish"]
        )
        # 重新发布后再次核查失败，时间锚点为重新发布时间，该结果再次进入复核。
        standard = self.service.list("standard")[0]
        failed2 = self._check(instrument, standard, "2026-06-01", measured=10.9)
        self.assertEqual(self.service.get(recent["id"])["status"], "review")
        self.assertEqual(failed2["data"]["affected"]["review_results"], [recent["id"]])

    def test_expired_standard_stops_instrument_and_traces_results(self):
        failed, instrument, recent, pending, method = self._fail_scenario()

    def test_recalibrate_then_withdraw(self):
        failed, instrument, recent, pending, method = self._fail_scenario()
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        self.service.transition(
            self.admin, instrument["id"], "calibrate",
            {"due_at": "2099-12-31", "passed": True},
        )
        withdrawn = self.service.transition(
            self.auth, recent["id"], "withdraw",
            {"reason": "bias confirmed, customer notified"},
        )
        self.assertEqual(withdrawn["status"], "withdrawn")
        self.assertEqual(withdrawn["data"]["withdrawn_by"], "q1")
        # 撤回为终态，不能重新发布或放行。
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.auth, withdrawn["id"], "republish",
                {"impact_assessment": "x", "no_impact": True},
            )

        # 被阻塞的待出结果在仪器恢复后可重新分析并放行。
        self.service.transition(
            self.analyst, pending["id"], "reanalyze", {"reason": "re-measure"}
        )
        re_released = self.service.transition(
            self.analyst,
            pending["id"],
            "release",
            {
                "instrument_id": instrument["id"],
                "method_id": method["id"],
                "value": 4.3,
                "unit": "mg/L",
            },
        )
        self.assertEqual(re_released["status"], "released")

    def test_review_requires_authorizer_and_only_from_review(self):
        instrument, standard, method = self._setup()
        result = self._release(instrument, method, "S-1", "2026-02-01")
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.auth, result["id"], "withdraw", {"reason": "nope"}
            )
        with self.assertRaises(InvalidTransition):
            self.service.transition(
                self.auth, result["id"], "republish",
                {"impact_assessment": "x", "no_impact": True},
            )

    def test_check_evidence_lives_in_audit_timeline(self):
        instrument, standard, method = self._setup()
        check = self._check(instrument, standard, "2026-03-01",
                            standard_value=5.0, measured=5.02, tolerance=0.1)
        entries = self.service.audit_log(check["id"])
        self.assertEqual([e["action"] for e in entries], ["create", "perform"])
        detail = entries[1]["detail"]
        self.assertEqual(detail["standard_value"], 5.0)
        self.assertEqual(detail["measured_value"], 5.02)
        self.assertEqual(detail["tolerance"], 0.1)
        self.assertAlmostEqual(detail["deviation"], 0.02)


if __name__ == "__main__":
    unittest.main()
