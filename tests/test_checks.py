import tempfile
import unittest
from pathlib import Path

from src.domain import Actor, PermissionDenied, ValidationError
from src.repository import SQLiteRepository
from src.rules import RuleEngine
from src.service import DomainService


class IntermediateCheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = SQLiteRepository(Path(self.tmp.name) / "test.db")
        self.service = DomainService(self.repo, RuleEngine())
        self.admin = Actor("admin", "admin")
        self.metrology = Actor("met-1", "metrology")
        self.analyst = Actor("ana-1", "analyst")
        self.authorizer = Actor("auth-1", "authorizer")

    def tearDown(self):
        self.tmp.cleanup()

    def _active_instrument(self):
        instrument = self.service.create(
            self.admin, "instrument", {"name": "Analyzer", "serial": "A-1"}
        )
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        return self.service.transition(
            self.admin, instrument["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )

    def _validated_method(self, instrument_id):
        method = self.service.create(
            self.admin, "method", {"name": "Assay-A", "version": "v1"}
        )
        return self.service.transition(
            self.admin, method["id"], "validate_method",
            {"parameters": {"range": [0, 10]}, "instrument_ids": [instrument_id]},
        )

    def _released_result(self, instrument_id, method_id, sample="S-1"):
        result = self.service.create(
            self.analyst, "result", {"sample_id": sample, "measurement": "m"}
        )
        return self.service.transition(
            self.analyst, result["id"], "release",
            {"instrument_id": instrument_id, "method_id": method_id,
             "value": 1.0, "unit": "mg/L"},
        )

    def _create_check(self, instrument_id, **overrides):
        data = {
            "instrument_id": instrument_id,
            "standard_id": "STD-1",
            "standard_value": 10.0,
            "measured_value": 10.25,
            "allowed_deviation": 0.5,
            "standard_due_at": "2099-01-01",
            "checked_at": "2026-09-26",
        }
        data.update(overrides)
        return self.service.create(self.metrology, "check", data)

    def _fail_instrument(self, instrument_id, **overrides):
        overrides.setdefault("measured_value", 11.0)
        overrides.setdefault("allowed_deviation", 0.5)
        check = self._create_check(instrument_id, **overrides)
        return self.service.transition(self.metrology, check["id"], "evaluate", {})

    def test_passed_check_keeps_instrument_active(self):
        instrument = self._active_instrument()
        check = self._create_check(instrument["id"])
        evaluated = self.service.transition(self.metrology, check["id"], "evaluate", {})
        self.assertEqual(evaluated["status"], "passed")
        self.assertEqual(evaluated["data"]["deviation"], 0.25)
        self.assertFalse(evaluated["data"]["standard_expired"])
        self.assertEqual(evaluated["data"]["failure_reasons"], [])
        self.assertEqual(self.service.get(instrument["id"])["status"], "active")

    def test_deviation_exceeded_suspends_and_traces(self):
        instrument = self._active_instrument()
        method = self._validated_method(instrument["id"])
        released = self._released_result(instrument["id"], method["id"])
        pending = self.service.create(
            self.analyst, "result",
            {"sample_id": "S-2", "measurement": "m", "instrument_id": instrument["id"]},
        )
        evaluated = self._fail_instrument(instrument["id"])
        self.assertEqual(evaluated["status"], "failed")
        self.assertEqual(evaluated["data"]["failure_reasons"], ["deviation exceeds limit"])

        instrument = self.service.get(instrument["id"])
        self.assertEqual(instrument["status"], "out_of_service")
        self.assertEqual(instrument["data"]["check_id"], evaluated["id"])

        blocked = self.service.get(pending["id"])
        self.assertEqual(blocked["status"], "blocked")
        self.assertIn(evaluated["id"], blocked["data"]["reason"])

        flagged = self.service.get(released["id"])
        self.assertEqual(flagged["status"], "under_review")
        self.assertEqual(flagged["data"]["check_id"], evaluated["id"])

        another = self.service.create(
            self.analyst, "result", {"sample_id": "S-3", "measurement": "m"}
        )
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.analyst, another["id"], "release",
                {"instrument_id": instrument["id"], "method_id": method["id"],
                 "value": 1.0, "unit": "mg/L"},
            )

    def test_expired_standard_fails_check(self):
        instrument = self._active_instrument()
        check = self._create_check(
            instrument["id"],
            measured_value=10.0,
            standard_due_at="2026-09-01",
            checked_at="2026-09-26",
        )
        evaluated = self.service.transition(self.metrology, check["id"], "evaluate", {})
        self.assertEqual(evaluated["status"], "failed")
        self.assertEqual(evaluated["data"]["failure_reasons"], ["standard expired"])
        self.assertTrue(evaluated["data"]["standard_expired"])
        self.assertEqual(self.service.get(instrument["id"])["status"], "out_of_service")

    def test_only_results_since_last_passed_check_are_flagged(self):
        instrument = self._active_instrument()
        method = self._validated_method(instrument["id"])
        old = self._released_result(instrument["id"], method["id"], "S-old")
        good = self._create_check(instrument["id"])
        self.service.transition(self.metrology, good["id"], "evaluate", {})
        recent = self._released_result(instrument["id"], method["id"], "S-new")
        self._fail_instrument(instrument["id"])
        self.assertEqual(self.service.get(old["id"])["status"], "released")
        self.assertEqual(self.service.get(recent["id"])["status"], "under_review")

    def test_rerelease_and_withdraw_after_review(self):
        instrument = self._active_instrument()
        method = self._validated_method(instrument["id"])
        ok = self._released_result(instrument["id"], method["id"], "S-ok")
        bad = self._released_result(instrument["id"], method["id"], "S-bad")
        failed = self._fail_instrument(instrument["id"])

        rereleased = self.service.transition(
            self.authorizer, ok["id"], "rerelease",
            {"review_note": "no impact confirmed", "check_id": failed["id"]},
        )
        self.assertEqual(rereleased["status"], "released")

        withdrawn = self.service.transition(
            self.authorizer, bad["id"], "withdraw",
            {"reason": "bias confirmed", "check_id": failed["id"]},
        )
        self.assertEqual(withdrawn["status"], "withdrawn")

        timeline = self.service.audit_log(entity_id=ok["id"])
        self.assertEqual(
            [entry["action"] for entry in timeline],
            ["create", "release", "flag_review", "rerelease"],
        )
        flag_entry = timeline[2]
        self.assertEqual(flag_entry["detail"]["patch"]["check_id"], failed["id"])

    def test_restore_after_new_passed_calibration(self):
        instrument = self._active_instrument()
        self._fail_instrument(instrument["id"])
        self.assertEqual(self.service.get(instrument["id"])["status"], "out_of_service")
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        restored = self.service.transition(
            self.metrology, instrument["id"], "calibrate",
            {"due_at": "2099-01-01", "passed": True},
        )
        self.assertEqual(restored["status"], "active")

    def test_failed_calibration_does_not_restore(self):
        instrument = self._active_instrument()
        self._fail_instrument(instrument["id"])
        self.service.transition(self.admin, instrument["id"], "send_calibration", {})
        with self.assertRaises(ValidationError):
            self.service.transition(
                self.metrology, instrument["id"], "calibrate",
                {"due_at": "2099-01-01", "passed": False},
            )

    def test_check_validation_and_permissions(self):
        instrument = self._active_instrument()
        with self.assertRaises(ValidationError):
            self._create_check(instrument["id"], measured_value="not-a-number")
        with self.assertRaises(ValidationError):
            self._create_check(instrument["id"], allowed_deviation=-1)
        with self.assertRaises(ValidationError):
            self._create_check("missing-instrument")
        check = self._create_check(instrument["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(self.analyst, check["id"], "evaluate", {})

    def test_review_actions_require_authorizer(self):
        instrument = self._active_instrument()
        method = self._validated_method(instrument["id"])
        released = self._released_result(instrument["id"], method["id"])
        self._fail_instrument(instrument["id"])
        with self.assertRaises(PermissionDenied):
            self.service.transition(
                self.analyst, released["id"], "rerelease",
                {"review_note": "looks fine"},
            )


if __name__ == "__main__":
    unittest.main()
