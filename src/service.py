from __future__ import annotations

import json
from typing import Any, Dict, Optional

from .domain import (ConflictError, ensure_role, normalize_severity,
                     require_int, require_number, require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EQUIPMENT_ROLES,
                    RECORD_ROLES, VIEW_ROLES, completion_blockers,
                    escalation_required, priority_score, response_deadline_hours,
                    role_for_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def _parse_inspection_payload(self, payload, role):
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        measured = payload.get("measured")
        if measured is not None:
            measured = require_number(measured, "measured")
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = require_text(idempotency_key, "idempotency_key", 100)
        expected = payload.get("expected_snapshot_version")
        # 检查员并发提交：必须基于当前快照版本；其他角色可选
        if role == "inspector":
            expected = require_int(expected, "expected_snapshot_version")
        elif expected is not None:
            expected = require_int(expected, "expected_snapshot_version")
        return kind, detail, status, external_ref, measured, idempotency_key, expected

    def add_inspection(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        (kind, detail, status, external_ref, measured, idempotency_key,
         expected) = self._parse_inspection_payload(payload, role)
        result = self.repository.add_inspection_record(
            item_id, kind, detail, status, external_ref, measured,
            idempotency_key, expected, actor)
        return self._present_recompute(item_id, result)

    def close_record(self, item_id: int, record_id: int, payload: Dict[str, Any],
                     actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        expected = payload.get("expected_snapshot_version")
        if role == "inspector":
            expected = require_int(expected, "expected_snapshot_version")
        elif expected is not None:
            expected = require_int(expected, "expected_snapshot_version")
        result = self.repository.close_record(item_id, record_id, expected, actor)
        return self._present_recompute(item_id, result)

    def upsert_equipment(self, item_id: int, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        spec = payload.get("spec", "") or ""
        if not isinstance(spec, str) or len(spec) > 2000:
            raise ValueError("spec必须是不超过2000字符的字符串")
        status = payload.get("status", "active")
        if status not in ("active", "retired"):
            raise ValueError("equipment.status必须是active或retired")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        result = self.repository.upsert_equipment(
            item_id, name, spec.strip(), status, external_ref, actor)
        presented = self._present_recompute(item_id, result)
        presented.pop("record", None)
        presented["equipment"] = result["equipment"]
        return presented

    def list_equipment(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_equipment(item_id)

    def snapshots(self, item_id: int, role: str) -> list:
        self._view(role)
        return [self._present_snapshot(s)
                for s in self.repository.list_snapshots(item_id)]

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(
            target,
            self.repository.open_record_count(item_id),
            exceedance=self.repository.exceedance_count(
                item_id, item["threshold"]) > 0)
        if blockers:
            raise ConflictError("；".join(blockers))
        if target == "submitted" and item["status"] == "draft":
            # 续期申请提交：冻结排放量与设备版本（快照在仓储事务内生成）
            updated = self.repository.submit_item(item_id, expected_version, actor)
        else:
            updated = self.repository.transition_item(item_id, target,
                                                      expected_version, actor)
            self.repository.append_audit("transition", ENTITY, item_id, actor, {
                "from": item["status"], "to": target,
                "snapshot_version": updated["snapshot_version"],
                "escalation_required": escalation_required(
                    item["severity"], item["quantity"], item["threshold"]),
            })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    # ------------------------------------------------------------------
    def _present_recompute(self, item_id: int, result: dict) -> Dict[str, Any]:
        snap = self._present_snapshot(result["snapshot"]) if result.get("snapshot") else None
        reasons = result.get("reasons")
        if reasons is None and snap is not None and result.get("effect") in (
                "returned", "revision", "refreshed"):
            reasons = snap["recompute_reasons"]
        return {
            "record": result.get("record"),
            "item": self.enrich(self.repository.get_item(item_id)),
            "snapshot": snap,
            "effect": result.get("effect"),
            "recompute_reasons": reasons,
            "idempotent_replay": bool(result.get("idempotent_replay")),
        }

    @staticmethod
    def _decode_reason(raw: str):
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return [raw] if raw else []
        return value if isinstance(value, list) else [raw]

    def _present_snapshot(self, snap: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "snapshot_version": snap["snapshot_version"],
            "source": snap["source"],
            "effect": snap["effect"],
            "trigger": snap["trigger"],
            "frozen_quantity": snap["quantity"],
            "frozen_threshold": snap["threshold"],
            "frozen_equipment_version": snap["equipment_version"],
            "open_records": snap["open_records"],
            "exceedance": bool(snap["exceedance"]),
            "revision_no": snap["revision_no"],
            "revision_of": snap["revision_of"],
            "recompute_reasons": self._decode_reason(snap["reason"] or ""),
            "created_by": snap["created_by"],
            "created_at": snap["created_at"],
        }

    def enrich(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        snapshot = self.repository.latest_snapshot(item["id"])
        if snapshot is None:
            result["latest_snapshot"] = None
            result["returned_for_review"] = False
            result["revision_no"] = 0
            result["recompute_reasons"] = []
            return result
        presented = self._present_snapshot(snapshot)
        result["latest_snapshot"] = presented
        result["recompute_reasons"] = presented["recompute_reasons"]
        result["revision_no"] = presented["revision_no"]
        result["returned_for_review"] = (
            presented["effect"] == "returned"
            and item["status"] in ("submitted", "inspection", "correction"))
        return result
