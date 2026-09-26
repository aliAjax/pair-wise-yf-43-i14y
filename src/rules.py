from datetime import datetime, timezone

from .domain import (
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


# 演示环境固定的“今天”，用于校准有效期判断，保证流程可重复。
REFERENCE_DATE = "2026-09-24"


def _today():
    return datetime.now(timezone.utc).date().isoformat()


def _day(value):
    return str(value)[:10]


def _iso_day(value, field):
    try:
        return datetime.fromisoformat(_day(value)).date().isoformat()
    except ValueError:
        raise ValidationError("%s must be an ISO date (YYYY-MM-DD)" % field)


def _number(value, field):
    if isinstance(value, bool):
        raise ValidationError("%s must be a number" % field)
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ValidationError("%s must be a number" % field)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_standard_create(actor, data, lookup):
    _iso_day(data.get("due_at"), "due_at")


def _validate_check_create(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    standard = _find_one(lookup, "standard", "id", data.get("standard_id"))
    if not standard:
        raise ValidationError("standard does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def _validate_instrument_calibrate(actor, entity, data, lookup):
    # 只有新校准合格才能把仪器（含因核查失败停用的仪器）恢复为可用。
    if data.get("passed") is not True:
        raise ValidationError("only a passed calibration can restore the instrument")
    return {
        "stop_reason": None,
        "stopped_by_check": None,
        "stopped_at": None,
    }


def calibration_current(due_at, as_of):
    return _day(due_at) >= _day(as_of)


def _usable_instrument(instrument):
    """放行/重新发布前对仪器状态的统一闸门，返回阻塞原因。"""
    if not instrument:
        return "result requires an active instrument"
    if instrument["status"] == "stopped":
        reason = instrument["data"].get("stop_reason") or "instrument is stopped"
        return "result blocked, instrument stopped: %s" % reason
    if instrument["status"] != "active":
        return "result requires an active instrument"
    if not calibration_current(instrument["data"].get("due_at", ""), REFERENCE_DATE):
        return "instrument calibration is not current"
    return None


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    blocked = _usable_instrument(instrument)
    if blocked:
        raise ValidationError(blocked)
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    return {
        "released_by": actor.user_id,
        "released_at": data.get("released_at") or _today(),
    }


def _validate_result_republish(actor, entity, data, lookup):
    if data.get("no_impact") is not True:
        raise ValidationError("republish requires no_impact=true confirmation")
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    blocked = _usable_instrument(instrument)
    if blocked:
        raise ValidationError(blocked)
    return {
        "republished_by": actor.user_id,
        "republished_at": data.get("republished_at") or _today(),
    }


def _validate_result_withdraw(actor, entity, data, lookup):
    return {"withdrawn_by": actor.user_id}


CUSTOM_CREATE = {
    "standard": _validate_standard_create,
    "check": _validate_check_create,
    "calibration": _validate_calibration,
}
CUSTOM_TRANSITIONS = {
    ("instrument", "calibrate"): _validate_instrument_calibrate,
    ("calibration", "perform"): _validate_perform,
    ("result", "release"): _validate_result_release,
    ("result", "republish"): _validate_result_republish,
    ("result", "withdraw"): _validate_result_withdraw,
}

CHECK_PERFORM_ROLES = ("admin", "metrology", "technician")
CHECK_PERFORM_REQUIRED = (
    "standard_value",
    "measured_value",
    "tolerance",
    "checked_at",
)


class RuleEngine:
    ALIASES = {
        "instruments": "instrument",
        "calibrations": "calibration",
        "methods": "method",
        "results": "result",
        "standards": "standard",
        "checks": "check",
    }
    INITIAL_STATUS = {
        "instrument": "active",
        "calibration": "requested",
        "method": "draft",
        "result": "pending",
        "standard": "active",
        "check": "pending",
    }
    TRANSITIONS = {
        "instrument": {
            "send_calibration": (("active", "quarantined", "stopped"), "calibrating"),
            "calibrate": (("calibrating",), "active"),
            "quarantine": (("active",), "quarantined"),
            "restore": (("quarantined",), "active"),
        },
        "calibration": {
            "perform": (("requested", "failed"), "passed"),
            "approve": (("passed",), "approved"),
            "reject": (("failed",), "rejected"),
        },
        "method": {
            "validate_method": (("draft",), "validated"),
            "revoke_method": (("validated",), "revoked"),
        },
        "result": {
            "release": (("pending",), "released"),
            "block": (("pending",), "blocked"),
            "reanalyze": (("blocked",), "pending"),
            "republish": (("review",), "released"),
            "withdraw": (("review",), "withdrawn"),
        },
        # check.perform 的目标状态由规则评估（passed/failed），在服务层编排。
        "check": {"perform": (("pending",), "passed")},
    }
    CREATE_REQUIRED = {
        "instrument": ("name", "serial"),
        "calibration": ("instrument_id", "requested_at"),
        "method": ("name", "version"),
        "result": ("sample_id", "measurement"),
        "standard": ("name", "serial", "due_at"),
        "check": ("instrument_id", "standard_id"),
    }
    ACTION_REQUIRED = {
        ("instrument", "calibrate"): ("due_at", "passed"),
        ("instrument", "quarantine"): ("reason",),
        ("calibration", "perform"): ("result", "performed_at", "uncertainty"),
        ("calibration", "approve"): ("authorized_by",),
        ("calibration", "reject"): ("reason",),
        ("method", "validate_method"): ("parameters", "instrument_ids"),
        ("method", "revoke_method"): ("reason",),
        ("result", "release"): ("instrument_id", "method_id", "value", "unit"),
        ("result", "block"): ("reason",),
        ("result", "reanalyze"): ("reason",),
        ("result", "republish"): ("impact_assessment", "no_impact"),
        ("result", "withdraw"): ("reason",),
        ("check", "perform"): CHECK_PERFORM_REQUIRED,
    }
    CREATE_ROLES = {
        "instrument": ("admin", "technician"),
        "calibration": ("admin", "metrology"),
        "method": ("admin", "authorizer"),
        "result": ("admin", "analyst"),
        "standard": ("admin", "metrology"),
        "check": ("admin", "metrology", "technician"),
    }
    ROLE_ACTIONS = {
        "send_calibration": ("admin", "technician"),
        "calibrate": ("admin", "metrology"),
        "quarantine": ("admin", "metrology"),
        "restore": ("admin", "metrology"),
        "perform": ("admin", "metrology"),
        "approve": ("admin", "authorizer"),
        "reject": ("admin", "authorizer"),
        "validate_method": ("admin", "authorizer"),
        "revoke_method": ("admin", "authorizer"),
        "release": ("admin", "analyst"),
        "block": ("admin", "analyst"),
        "reanalyze": ("admin", "analyst"),
        ("check", "perform"): CHECK_PERFORM_ROLES,
        ("result", "republish"): ("admin", "authorizer"),
        ("result", "withdraw"): ("admin", "authorizer"),
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if "*" not in allowed and actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = CUSTOM_CREATE.get(kind)
        if custom:
            custom(actor, data, lookup)
        return dict(data)

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition(
                "cannot %s from status %s" % (action, entity["status"])
            )
        allowed_roles = self.ROLE_ACTIONS.get(
            (kind, action), self.ROLE_ACTIONS.get(action, ("admin",))
        )
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch

    def evaluate_check(self, check, data, lookup):
        """评估一次期间核查：标准器是否在有效期、偏差是否超限，并圈定追溯结果。

        追溯范围：自上次成功核查（含重新发布的再确认时间）之后放行的结果；
        若从无成功核查，则该仪器全部已放行结果都纳入待复核。
        """
        instrument = _find_one(lookup, "instrument", "id", check["data"].get("instrument_id"))
        if not instrument:
            raise ValidationError("instrument does not exist")
        if instrument["status"] != "active":
            raise InvalidTransition(
                "cannot perform check on instrument in status %s" % instrument["status"]
            )
        standard = _find_one(lookup, "standard", "id", check["data"].get("standard_id"))
        if not standard:
            raise ValidationError("standard does not exist")

        checked_at = _iso_day(data.get("checked_at"), "checked_at")
        standard_due = _iso_day(standard["data"].get("due_at"), "standard due_at")
        standard_value = _number(data.get("standard_value"), "standard_value")
        measured_value = _number(data.get("measured_value"), "measured_value")
        tolerance = _number(data.get("tolerance"), "tolerance")
        if tolerance <= 0:
            raise ValidationError("tolerance must be greater than 0")
        deviation = abs(measured_value - standard_value)

        reasons = []
        if standard_due < checked_at:
            reasons.append(
                "standard %s expired on %s (check at %s)"
                % (standard["id"], standard_due, checked_at)
            )
        if deviation > tolerance:
            reasons.append(
                "deviation %s exceeds tolerance %s (standard %s, measured %s)"
                % (
                    _format_number(deviation),
                    _format_number(tolerance),
                    _format_number(standard_value),
                    _format_number(measured_value),
                )
            )
        passed = not reasons

        last_at = instrument["data"].get("last_successful_check_at")
        pending_results = []
        released_results = []
        # 未放行结果在放行登记前不绑定仪器；仪器停用后所有待出结果一律阻塞，
        # 已放行结果则按 instrument_id 追溯。
        for result in lookup("result", None, None) if lookup else []:
            if result["status"] == "pending":
                pending_results.append(result)
            elif result["status"] == "released" and result["data"].get(
                "instrument_id"
            ) == instrument["id"]:
                assured_at = (
                    result["data"].get("republished_at")
                    or result["data"].get("released_at")
                )
                if last_at is None or not assured_at or _day(assured_at) > _day(last_at):
                    released_results.append(result)

        return {
            "instrument": instrument,
            "standard": standard,
            "checked_at": checked_at,
            "standard_due": standard_due,
            "standard_value": standard_value,
            "measured_value": measured_value,
            "tolerance": tolerance,
            "deviation": deviation,
            "passed": passed,
            "reasons": reasons,
            "pending_results": pending_results,
            "released_results": released_results,
            "last_successful_check_at": last_at,
        }


def _format_number(value):
    return ("%f" % value).rstrip("0").rstrip(".")


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
