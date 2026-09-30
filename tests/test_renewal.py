import tempfile
import unittest
from pathlib import Path

from src.domain import ConflictError
from src.repository import Repository
from src.service import Service


class RenewalSnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def _item(self, quantity=5, threshold=10, severity="high", ref="IT-1"):
        return self.service.create_item(
            {"title": "permit", "description": "air source", "severity": severity,
             "quantity": quantity, "threshold": threshold, "external_ref": ref},
            "creator", "applicant")

    # ---------- 提交冻结 ----------
    def test_submit_freezes_emissions_and_equipment_version(self):
        item = self._item(quantity=5, threshold=10)
        self.service.add_equipment(item["id"], {"name": "scrubber", "detail": "v1"},
                                  "insp", "inspector")
        renewal = self.service.submit_renewal(
            item["id"], {"idempotency_key": "RN-1", "note": "first"}, "app", "applicant")
        self.assertEqual(renewal["status"], "pending")
        self.assertEqual(renewal["frozen_quantity"], 5)
        self.assertEqual(renewal["frozen_threshold"], 10)
        self.assertEqual(renewal["frozen_equipment_version"], 2)
        self.assertTrue(renewal["frozen_compliant"])
        self.assertEqual(renewal["snapshot_reason"], "renewal_submitted")
        self.assertTrue(renewal["snapshot_source"].startswith("renewal:"))
        # 冻结后改动设备版本，快照排放量不变、设备版本递增
        self.service.add_equipment(item["id"], {"name": "scrubber", "detail": "v2"},
                                   "insp", "inspector")
        snapshots = self.service.list_snapshots(item["id"], "viewer")
        self.assertGreaterEqual(len(snapshots), 3)
        latest = snapshots[0]
        self.assertEqual(latest["quantity"], 5)
        self.assertEqual(latest["equipment_version"], 3)
        self.assertEqual(latest["reason"], "equipment_changed")

    # ---------- 改动触发重算 ----------
    def test_equipment_change_triggers_recalculation(self):
        item = self._item()
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        snapshots = self.service.list_snapshots(item["id"], "viewer")
        self.assertEqual(snapshots[0]["reason"], "equipment_changed")
        self.assertEqual(snapshots[0]["source"], "equipment:1")
        self.assertEqual(snapshots[0]["equipment_version"], 2)

    def test_record_change_and_close_trigger_recalculation(self):
        item = self._item()
        rec = self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "found issue",
                         "status": "open", "external_ref": "REC-1"},
            "insp", "inspector")
        snapshots = self.service.list_snapshots(item["id"], "viewer")
        self.assertEqual(snapshots[0]["reason"], "record_changed")
        self.assertEqual(snapshots[0]["source"], f"record:{rec['id']}")
        # 关闭记录再次触发重算
        self.service.close_record(item["id"], rec["id"], "insp", "inspector")
        snapshots = self.service.list_snapshots(item["id"], "viewer")
        self.assertEqual(snapshots[0]["reason"], "record_closed")
        self.assertEqual(snapshots[0]["open_records"], 0)

    # ---------- 待批申请退回复核 ----------
    def test_pending_renewal_returned_on_open_records(self):
        item = self._item(quantity=5, threshold=10)
        self.service.submit_renewal(item["id"], {"idempotency_key": "RN-1"},
                                    "app", "applicant")
        # 产生未关闭事项 -> 重算 -> 待批申请退回
        self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "issue", "status": "open",
                         "external_ref": "REC-1"}, "insp", "inspector")
        renewals = self.service.list_renewals(item["id"], "viewer")
        self.assertEqual(renewals[0]["status"], "returned")
        self.assertIn("仍有未关闭事项", renewals[0]["review_note"])

    def test_pending_renewal_returned_on_exceeded_emissions(self):
        item = self._item(quantity=12, threshold=10)  # 超标
        self.service.submit_renewal(item["id"], {"idempotency_key": "RN-1"},
                                    "app", "applicant")
        # 任何改动触发重算 -> 超标待批申请退回
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        renewals = self.service.list_renewals(item["id"], "viewer")
        self.assertEqual(renewals[0]["status"], "returned")
        self.assertIn("排放量超过许可限值", renewals[0]["review_note"])

    def test_returned_renewal_can_resubmit(self):
        item = self._item(quantity=12, threshold=10)
        self.service.submit_renewal(item["id"], {"idempotency_key": "RN-1"},
                                    "app", "applicant")
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        renewal = self.service.list_renewals(item["id"], "viewer")[0]
        self.assertEqual(renewal["status"], "returned")
        resubmitted = self.service.resubmit_renewal(
            item["id"], renewal["id"], "app", "applicant", "fixed")
        self.assertEqual(resubmitted["status"], "pending")
        self.assertEqual(resubmitted["snapshot_reason"], "renewal_resubmitted")

    # ---------- 已批许可生成修订版 ----------
    def test_approved_permit_generates_revision(self):
        item = self._item()
        # 走到已批状态
        current = item
        for target in ["submitted", "inspection", "correction", "approved"]:
            role = {"submitted": "applicant", "inspection": "inspector",
                    "correction": "inspector", "approved": "compliance_manager"}[target]
            current = self.service.transition(current["id"], target, current["version"],
                                              "reviewer", role)
        self.assertEqual(current["status"], "approved")
        # 已批许可改动设备 -> 生成修订版（新快照），状态不变
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        updated = self.service.get_item(item["id"], "viewer")
        self.assertEqual(updated["status"], "approved")
        self.assertGreater(updated["snapshot_version"], item["snapshot_version"])
        events = self.service.audit("viewer", item["id"])
        self.assertTrue(any(e["action"] == "revision" for e in events))
        self.assertTrue(self.repo.verify_audit_chain())

    # ---------- 并发控制 ----------
    def test_concurrent_submit_accepts_only_current_snapshot(self):
        item = self._item()
        v1 = self.service.get_item(item["id"], "viewer")["snapshot_version"]
        # 检查员 A 提交（不带版本，直接生成新快照）
        self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "a", "status": "open",
                         "external_ref": "A", "idempotency_key": "key-A"},
            "insp", "inspector")
        v2 = self.service.get_item(item["id"], "viewer")["snapshot_version"]
        self.assertEqual(v2, v1 + 1)
        # 检查员 B 用过期版本提交 -> 409
        with self.assertRaises(ConflictError):
            self.service.add_record(
                item["id"], {"kind": "inspection", "detail": "b", "status": "open",
                             "external_ref": "B", "idempotency_key": "key-B",
                             "expected_snapshot_version": v1},
                "insp", "inspector")
        # B 刷新后用新版本 + 幂等键重试 -> 成功
        v3 = self.service.get_item(item["id"], "viewer")["snapshot_version"]
        self.assertEqual(v3, v2)
        result = self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "b", "status": "open",
                         "external_ref": "B", "idempotency_key": "key-B",
                         "expected_snapshot_version": v2},
            "insp", "inspector")
        self.assertFalse(result["idempotent_replay"])
        v4 = self.service.get_item(item["id"], "viewer")["snapshot_version"]
        self.assertEqual(v4, v3 + 1)
        # B 重复提交（网络重试）-> 幂等回放，不产生新修订
        replay = self.service.add_record(
            item["id"], {"kind": "inspection", "detail": "b", "status": "open",
                         "external_ref": "B", "idempotency_key": "key-B",
                         "expected_snapshot_version": v4},
            "insp", "inspector")
        self.assertTrue(replay["idempotent_replay"])
        self.assertEqual(replay["id"], result["id"])
        self.assertEqual(
            self.service.get_item(item["id"], "viewer")["snapshot_version"], v4)
        # 记录不重复
        records = self.service.list_records(item["id"], "viewer")
        self.assertEqual(len(records), 2)

    def test_renewal_idempotent_replay_no_duplicate(self):
        item = self._item()
        first = self.service.submit_renewal(
            item["id"], {"idempotency_key": "RN-1"}, "app", "applicant")
        second = self.service.submit_renewal(
            item["id"], {"idempotency_key": "RN-1"}, "app", "applicant")
        self.assertTrue(second["idempotent_replay"])
        self.assertEqual(second["id"], first["id"])
        renewals = self.service.list_renewals(item["id"], "viewer")
        self.assertEqual(len(renewals), 1)

    # ---------- 历史数据补齐 ----------
    def test_backfill_creates_initial_snapshot_for_legacy_items(self):
        # 直接插一条没有快照的历史项目
        with self.repo._lock, self.repo.conn:
            cur = self.repo.conn.execute(
                """INSERT INTO items(title, description, severity, quantity, threshold,
                   status, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                ("legacy", "old", "low", 1, 10, "draft", 1, "sys", "2024-01-01T00:00:00",
                 "2024-01-01T00:00:00"))
            legacy_id = int(cur.lastrowid)
        # 新仓储启动时自动补齐
        repo2 = Repository(str(Path(self.tmp.name) / "test.db"))
        try:
            snap = repo2.get_current_snapshot(legacy_id)
            self.assertIsNotNone(snap)
            self.assertEqual(snap["version"], 1)
            self.assertEqual(snap["reason"], "initial")
            self.assertEqual(snap["source"], "backfill")
            events = repo2.list_audit(legacy_id)
            self.assertTrue(any(e["action"] == "snapshot"
                                and e["detail"]["source"] == "backfill" for e in events))
            self.assertTrue(repo2.verify_audit_chain())
        finally:
            repo2.close()

    # ---------- 列表与审计显示原因/来源 ----------
    def test_list_and_audit_show_reason_and_source(self):
        item = self._item()
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        items = self.service.list_items("viewer")
        entry = next(i for i in items if i["id"] == item["id"])
        self.assertEqual(entry["snapshot_reason"], "equipment_changed")
        self.assertTrue(entry["snapshot_source"].startswith("equipment:"))
        events = self.service.audit("viewer", item["id"])
        snapshot_events = [e for e in events if e["action"] == "snapshot"]
        self.assertTrue(any(e["detail"]["reason"] == "equipment_changed"
                            and e["detail"]["source"].startswith("equipment:")
                            for e in snapshot_events))
        self.assertTrue(self.repo.verify_audit_chain())

    # ---------- 角色权限 ----------
    def test_renewal_approval_requires_manager(self):
        item = self._item()
        renewal = self.service.submit_renewal(
            item["id"], {"idempotency_key": "RN-1"}, "app", "applicant")
        from src.domain import PermissionDenied
        with self.assertRaises(PermissionDenied):
            self.service.approve_renewal(item["id"], renewal["id"], "attacker",
                                         "viewer")
        approved = self.service.approve_renewal(item["id"], renewal["id"], "mgr",
                                                "compliance_manager", "ok")
        self.assertEqual(approved["status"], "approved")

    def test_approved_renewal_generates_revision_on_change(self):
        # 续期申请批准后，即使项目未走工作流 approved，改动也生成修订版
        item = self._item()
        renewal = self.service.submit_renewal(
            item["id"], {"idempotency_key": "RN-1"}, "app", "applicant")
        self.service.approve_renewal(item["id"], renewal["id"], "mgr",
                                     "compliance_manager")
        self.service.add_equipment(item["id"], {"name": "filter", "detail": "v1"},
                                   "insp", "inspector")
        events = self.service.audit("viewer", item["id"])
        revision_events = [e for e in events if e["action"] == "revision"]
        self.assertEqual(len(revision_events), 1)
        self.assertEqual(revision_events[0]["detail"]["reason"], "equipment_changed")
        self.assertTrue(self.repo.verify_audit_chain())


if __name__ == "__main__":
    unittest.main()
