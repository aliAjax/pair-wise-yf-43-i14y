from datetime import date, datetime, timedelta

from .domain import (
    ConflictError,
    InvalidTransition,
    PermissionDenied,
    ValidationError,
)


def _validate_calibration(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")


def _validate_perform(actor, entity, data, lookup):
    if data.get("result") not in ("passed", "failed"):
        raise ValidationError("calibration result must be passed or failed")
    if data.get("result") == "passed" and not data.get("due_at"):
        raise ValidationError("passed calibration requires due_at")


def calibration_current(due_at, as_of):
    return str(due_at) >= str(as_of)


def _validate_result_release(actor, entity, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    method = _find_one(lookup, "method", "id", data.get("method_id"))
    if not instrument or instrument["status"] != "active":
        raise ValidationError("result requires an active instrument")
    if not calibration_current(instrument["data"].get("due_at", ""), date.today().isoformat()):
        raise ValidationError("instrument calibration is not current")
    if not method or method["status"] != "validated":
        raise ValidationError("result requires a validated method")
    if data.get("instrument_id") not in method["data"].get("instrument_ids", []):
        raise ValidationError("method is not validated for this instrument")
    return {"released_by": actor.user_id}


def _validate_instrument_calibrate(actor, entity, data, lookup):
    if not data.get("passed"):
        raise ValidationError("instrument can only return to service after a passed calibration")


def _validate_check(actor, data, lookup):
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not instrument:
        raise ValidationError("instrument does not exist")
    for field in ("standard_value", "measured_value", "allowed_deviation"):
        try:
            float(data.get(field))
        except (TypeError, ValueError):
            raise ValidationError(field + " must be numeric")
    if float(data.get("allowed_deviation")) < 0:
        raise ValidationError("allowed_deviation must be non-negative")


def _validate_evaluate(actor, entity, data, lookup):
    record = entity["data"]
    deviation = abs(float(record.get("measured_value")) - float(record.get("standard_value")))
    allowed = float(record.get("allowed_deviation"))
    expired = not calibration_current(record.get("standard_due_at", ""), record.get("checked_at", ""))
    reasons = []
    if expired:
        reasons.append("standard expired")
    if deviation > allowed:
        reasons.append("deviation exceeds limit")
    return {
        "next_status": "failed" if reasons else "passed",
        "deviation": deviation,
        "standard_expired": expired,
        "failure_reasons": reasons,
        "evaluated_by": actor.user_id,
    }


CUSTOM_CREATE = {'calibration': _validate_calibration, 'check': _validate_check}
CUSTOM_TRANSITIONS = {('calibration', 'perform'): _validate_perform, ('result', 'release'): _validate_result_release, ('instrument', 'calibrate'): _validate_instrument_calibrate, ('check', 'evaluate'): _validate_evaluate}


class RuleEngine:
    ALIASES = {'instruments': 'instrument', 'calibrations': 'calibration', 'methods': 'method', 'results': 'result', 'checks': 'check'}
    INITIAL_STATUS = {'instrument': 'active', 'calibration': 'requested', 'method': 'draft', 'result': 'pending', 'check': 'recorded'}
    TRANSITIONS = {'instrument': {'send_calibration': (('active', 'out_of_service'), 'calibrating'), 'calibrate': (('calibrating',), 'active'), 'quarantine': (('active',), 'quarantined'), 'restore': (('quarantined',), 'active'), 'disable': (('active',), 'out_of_service')}, 'calibration': {'perform': (('requested', 'failed'), 'passed'), 'approve': (('passed',), 'approved'), 'reject': (('failed',), 'rejected')}, 'method': {'validate_method': (('draft',), 'validated'), 'revoke_method': (('validated',), 'revoked')}, 'result': {'release': (('pending',), 'released'), 'block': (('pending',), 'blocked'), 'reanalyze': (('blocked',), 'pending'), 'flag_review': (('released',), 'under_review'), 'rerelease': (('under_review',), 'released'), 'withdraw': (('under_review',), 'withdrawn')}, 'check': {'evaluate': (('recorded',), 'passed')}}
    CREATE_REQUIRED = {'instrument': ('name', 'serial'), 'calibration': ('instrument_id', 'requested_at'), 'method': ('name', 'version'), 'result': ('sample_id', 'measurement'), 'check': ('instrument_id', 'standard_id', 'standard_value', 'measured_value', 'allowed_deviation', 'standard_due_at', 'checked_at')}
    ACTION_REQUIRED = {('instrument', 'calibrate'): ('due_at', 'passed'), ('instrument', 'quarantine'): ('reason',), ('instrument', 'disable'): ('reason', 'check_id'), ('calibration', 'perform'): ('result', 'performed_at', 'uncertainty'), ('calibration', 'approve'): ('authorized_by',), ('calibration', 'reject'): ('reason',), ('method', 'validate_method'): ('parameters', 'instrument_ids'), ('method', 'revoke_method'): ('reason',), ('result', 'release'): ('instrument_id', 'method_id', 'value', 'unit'), ('result', 'block'): ('reason',), ('result', 'reanalyze'): ('reason',), ('result', 'flag_review'): ('reason', 'check_id'), ('result', 'rerelease'): ('review_note',), ('result', 'withdraw'): ('reason',)}
    CREATE_ROLES = {'instrument': ('admin', 'technician'), 'calibration': ('admin', 'metrology'), 'method': ('admin', 'authorizer'), 'result': ('admin', 'analyst'), 'check': ('admin', 'metrology')}
    ROLE_ACTIONS = {'send_calibration': ('admin', 'technician'), 'calibrate': ('admin', 'metrology'), 'quarantine': ('admin', 'metrology'), 'restore': ('admin', 'metrology'), 'disable': ('admin', 'metrology'), 'perform': ('admin', 'metrology'), 'approve': ('admin', 'authorizer'), 'reject': ('admin', 'authorizer'), 'validate_method': ('admin', 'authorizer'), 'revoke_method': ('admin', 'authorizer'), 'release': ('admin', 'analyst'), 'block': ('admin', 'analyst', 'metrology'), 'reanalyze': ('admin', 'analyst'), 'evaluate': ('admin', 'metrology'), 'flag_review': ('admin', 'metrology'), 'rerelease': ('admin', 'authorizer'), 'withdraw': ('admin', 'authorizer')}

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
        # A custom validator may override the default target status by
        # returning a "next_status" key (e.g. check evaluation pass/fail).
        if extra and "next_status" in extra:
            next_status = extra["next_status"]
        patch = dict(data)
        if extra:
            patch.update({key: value for key, value in extra.items() if key != "next_status"})
        return next_status, patch


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def _date_ordinal(value):
    return datetime.fromisoformat(str(value)[:10]).date().toordinal()
