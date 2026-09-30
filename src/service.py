from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import ensure_role, normalize_severity, require_number, require_text
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, ENTITY, EQUIPMENT_ROLES, RECORD_ROLES,
                    VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    role_for_renewal_transition, validate_transition,
                    validate_renewal_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    # ---------- 项目 ----------
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
        return self._enrich_item(item)

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if blockers:
            from .domain import ConflictError
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self._enrich_item(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self._enrich_item(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self._enrich_item(item) for item in self.repository.list_items(status)]

    def _enrich_item(self, item: Dict[str, Any]) -> Dict[str, Any]:
        result = Service.enrich(item)
        snap = self.repository.get_current_snapshot(item["id"])
        if snap is not None:
            result["snapshot_version"] = snap["version"]
            result["snapshot_reason"] = snap["reason"]
            result["snapshot_source"] = snap["source"]
            result["frozen_quantity"] = snap["quantity"]
            result["frozen_equipment_version"] = snap["equipment_version"]
            result["frozen_open_records"] = snap["open_records"]
            result["compliant"] = bool(snap["compliant"])
        else:
            result["snapshot_version"] = 0
            result["snapshot_reason"] = None
            result["snapshot_source"] = None
            result["frozen_quantity"] = item["quantity"]
            result["frozen_equipment_version"] = 1
            result["frozen_open_records"] = self.repository.open_record_count(item["id"])
            result["compliant"] = True
        return result

    def get_current_snapshot(self, item_id: int) -> Optional[Dict[str, Any]]:
        return self.repository.get_current_snapshot(item_id)

    # ---------- 检查记录 ----------
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
        idempotency_key = payload.get("idempotency_key")
        if idempotency_key is not None:
            idempotency_key = require_text(idempotency_key, "idempotency_key", 100)
        expected_snapshot_version = payload.get("expected_snapshot_version")
        if expected_snapshot_version is not None:
            if not isinstance(expected_snapshot_version, int) or expected_snapshot_version < 1:
                raise ValueError("expected_snapshot_version必须是正整数")
        record, snap, idempotent = self.repository.add_record(
            item_id, kind, detail, status, external_ref, idempotency_key,
            expected_snapshot_version, actor)
        result = dict(record)
        result["snapshot_version"] = snap["version"] if snap else None
        result["idempotent_replay"] = bool(idempotent)
        return result

    def close_record(self, item_id: int, record_id: int, actor: str,
                      role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        record, snap, changed = self.repository.close_record(item_id, record_id, actor)
        result = dict(record)
        result["snapshot_version"] = snap["version"] if snap else None
        result["recalculated"] = bool(changed)
        return result

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    # ---------- 治理设备 ----------
    def list_equipment(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_equipment(item_id)

    def add_equipment(self, item_id: int, payload: Dict[str, Any], actor: str,
                       role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "active")
        if status not in ("active", "inactive"):
            raise ValueError("status必须是active或inactive")
        equipment, snap, returned = self.repository.add_equipment(
            item_id, name, detail, status, actor)
        result = dict(equipment)
        result["snapshot_version"] = snap["version"] if snap else None
        result["renewal_returned"] = returned["id"] if returned else None
        return result

    def update_equipment(self, item_id: int, equipment_id: int, payload: Dict[str, Any],
                         actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, EQUIPMENT_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "active")
        if status not in ("active", "inactive"):
            raise ValueError("status必须是active或inactive")
        equipment, snap, returned = self.repository.update_equipment(
            equipment_id, name, detail, status, actor)
        result = dict(equipment)
        result["snapshot_version"] = snap["version"] if snap else None
        result["renewal_returned"] = returned["id"] if returned else None
        return result

    # ---------- 许可快照 ----------
    def list_snapshots(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_snapshots(item_id)

    # ---------- 续期申请 ----------
    def list_renewals(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_renewals(item_id)

    def submit_renewal(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        idempotency_key = require_text(payload.get("idempotency_key"),
                                      "idempotency_key", 100)
        note = payload.get("note")
        if note is not None:
            note = require_text(note, "note", 500)
        renewal, snap, idempotent = self.repository.submit_renewal(
            item_id, idempotency_key, actor, note)
        result = self._enrich_renewal(renewal, snap)
        result["idempotent_replay"] = bool(idempotent)
        return result

    def approve_renewal(self, item_id: int, renewal_id: int, actor: str,
                         role: str, note: Optional[str] = None) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(item_id, renewal_id)
        validate_renewal_transition(renewal["status"], "approved")
        ensure_role(role, role_for_renewal_transition("approved"))
        if note is not None:
            note = require_text(note, "note", 500)
        updated = self.repository.approve_renewal(item_id, renewal_id, actor, note)
        snap = self.repository.get_current_snapshot(item_id)
        return self._enrich_renewal(updated, snap)

    def resubmit_renewal(self, item_id: int, renewal_id: int, actor: str,
                          role: str, note: Optional[str] = None) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        renewal = self.repository.get_renewal(item_id, renewal_id)
        validate_renewal_transition(renewal["status"], "pending")
        if note is not None:
            note = require_text(note, "note", 500)
        updated, snap = self.repository.resubmit_renewal(item_id, renewal_id, actor, note)
        return self._enrich_renewal(updated, snap)

    def _enrich_renewal(self, renewal: Dict[str, Any],
                         snap: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        result = dict(renewal)
        if snap is not None:
            result["snapshot_version"] = snap["version"]
            result["snapshot_reason"] = snap["reason"]
            result["snapshot_source"] = snap["source"]
            result["frozen_quantity"] = snap["quantity"]
            result["frozen_threshold"] = snap["threshold"]
            result["frozen_equipment_version"] = snap["equipment_version"]
            result["frozen_open_records"] = snap["open_records"]
            result["frozen_compliant"] = bool(snap["compliant"])
        return result

    # ---------- 审计 ----------
    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(item_id)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
