from uuid import uuid4

from .audit import AuditTrail
from .domain import ConflictError, InvalidTransition, NotFoundError
from .rules import RuleEngine


class DomainService:
    def __init__(self, repository, rules=None):
        self.repository = repository
        self.rules = rules or RuleEngine()
        self.audit = AuditTrail(repository)

    def _lookup(self, kind, field, value):
        return self.repository.find_entities(self.rules.normalize_kind(kind), field, value)

    def health(self):
        return {"status": "ok" if self.repository.ping() else "error"}

    def create(self, actor, kind, data, idempotency_key=None):
        kind = self.rules.normalize_kind(kind)
        payload = dict(data or {})
        if idempotency_key:
            existing = self.repository.get_idempotency(actor.user_id, idempotency_key)
            if existing:
                entity = self.repository.get_entity(existing)
                if entity:
                    return entity
        self.rules.validate_create(actor, kind, payload, self._lookup)
        entity_id = str(payload.pop("id", "") or uuid4())
        if self.repository.get_entity(entity_id):
            raise ConflictError("entity already exists: " + entity_id)
        status = self.rules.initial_status(kind)
        entity = self.repository.create_entity(entity_id, kind, status, payload, actor.user_id)
        self.audit.record(entity_id, actor, "create", None, status, {"kind": kind})
        if idempotency_key:
            self.repository.save_idempotency(actor.user_id, idempotency_key, entity_id)
        return entity

    def transition(self, actor, entity_id, action, data=None, expected_version=None):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        kind = self.rules.normalize_kind(entity["kind"])
        if kind == "check" and action == "perform":
            return self.perform_check(
                actor, entity, dict(data or {}), expected_version
            )
        expected = int(expected_version) if expected_version is not None else entity["version"]
        next_status, patch = self.rules.validate_transition(
            actor, entity, action, dict(data or {}), self._lookup
        )
        merged = dict(entity["data"])
        merged.update(patch)
        updated = self.repository.update_entity(entity_id, expected, next_status, merged)
        self.audit.record(
            entity_id,
            actor,
            action,
            entity["status"],
            updated["status"],
            {"patch": patch},
        )
        return updated

    def perform_check(self, actor, check, data, expected_version=None):
        """期间核查：登记标准器/标准值/实测值/允许偏差，失败时级联追溯结果。"""
        if check["status"] != "pending":
            raise InvalidTransition(
                "cannot perform check from status %s" % check["status"]
            )
        self.rules._ensure_role(actor, self.rules.ROLE_ACTIONS[("check", "perform")])
        self.rules._require(data, self.rules.ACTION_REQUIRED[("check", "perform")])
        evaluation = self.rules.evaluate_check(check, data, self._lookup)
        instrument = evaluation["instrument"]
        expected = (
            int(expected_version) if expected_version is not None else check["version"]
        )

        check_data = dict(check["data"])
        check_data.update(
            {
                "checked_at": evaluation["checked_at"],
                "standard_name": evaluation["standard"]["data"].get("name"),
                "standard_due": evaluation["standard_due"],
                "standard_value": evaluation["standard_value"],
                "measured_value": evaluation["measured_value"],
                "tolerance": evaluation["tolerance"],
                "deviation": evaluation["deviation"],
                "performed_by": actor.user_id,
                "reasons": evaluation["reasons"],
                "affected": {
                    "blocked_results": [r["id"] for r in evaluation["pending_results"]],
                    "review_results": [r["id"] for r in evaluation["released_results"]],
                },
            }
        )
        next_status = "passed" if evaluation["passed"] else "failed"
        check_data["result"] = next_status

        changes = [
            {
                "entity_id": check["id"],
                "expected_version": expected,
                "status": next_status,
                "data": check_data,
                "actor": actor,
                "action": "perform",
                "from_status": check["status"],
                "detail": {
                    "standard_value": evaluation["standard_value"],
                    "measured_value": evaluation["measured_value"],
                    "tolerance": evaluation["tolerance"],
                    "deviation": evaluation["deviation"],
                    "standard_due": evaluation["standard_due"],
                    "reasons": evaluation["reasons"],
                },
            }
        ]

        instrument_data = dict(instrument["data"])
        if evaluation["passed"]:
            instrument_data["last_successful_check_id"] = check["id"]
            instrument_data["last_successful_check_at"] = evaluation["checked_at"]
            changes.append(
                {
                    "entity_id": instrument["id"],
                    "expected_version": instrument["version"],
                    "status": instrument["status"],
                    "data": instrument_data,
                    "actor": actor,
                    "action": "check_passed",
                    "from_status": instrument["status"],
                    "detail": {"check_id": check["id"]},
                }
            )
        else:
            reasons = evaluation["reasons"]
            stop_reason = "; ".join(reasons)
            instrument_data["stopped_by_check"] = check["id"]
            instrument_data["stopped_at"] = evaluation["checked_at"]
            instrument_data["stop_reason"] = stop_reason
            changes.append(
                {
                    "entity_id": instrument["id"],
                    "expected_version": instrument["version"],
                    "status": "stopped",
                    "data": instrument_data,
                    "actor": actor,
                    "action": "check_failed_stop",
                    "from_status": instrument["status"],
                    "detail": {"check_id": check["id"], "reasons": reasons},
                }
            )
            # 未放行结果：给出阻塞原因。
            block_reason = (
                "blocked by failed intermediate check %s: %s"
                % (check["id"], stop_reason)
            )
            for result in evaluation["pending_results"]:
                result_data = dict(result["data"])
                result_data["block_reason"] = block_reason
                result_data["blocked_by_check"] = check["id"]
                result_data["blocked_at"] = evaluation["checked_at"]
                changes.append(
                    {
                        "entity_id": result["id"],
                        "expected_version": result["version"],
                        "status": "blocked",
                        "data": result_data,
                        "actor": actor,
                        "action": "check_failed_block",
                        "from_status": result["status"],
                        "detail": {"check_id": check["id"], "reason": block_reason},
                    }
                )
            # 自上次成功核查以来已放行结果：转入待复核。
            review_reason = (
                "under review after failed intermediate check %s: %s"
                % (check["id"], stop_reason)
            )
            for result in evaluation["released_results"]:
                result_data = dict(result["data"])
                result_data["review_reason"] = review_reason
                result_data["reviewed_by_check"] = check["id"]
                result_data["reviewed_at"] = evaluation["checked_at"]
                changes.append(
                    {
                        "entity_id": result["id"],
                        "expected_version": result["version"],
                        "status": "review",
                        "data": result_data,
                        "actor": actor,
                        "action": "check_failed_hold_for_review",
                        "from_status": result["status"],
                        "detail": {"check_id": check["id"], "reason": review_reason},
                    }
                )

        self.repository.apply_changes(changes)
        return self.repository.get_entity(check["id"])


    def get(self, entity_id):
        entity = self.repository.get_entity(entity_id)
        if not entity:
            raise NotFoundError("entity not found: " + entity_id)
        return entity

    def list(self, kind=None, status=None):
        if kind:
            kind = self.rules.normalize_kind(kind)
        return self.repository.list_entities(kind=kind, status=status)

    def audit_log(self, entity_id=None):
        return self.repository.list_audit(entity_id=entity_id)
