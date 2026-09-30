import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path

from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.rules import STATES
from src.service import Service


def approve(service, repo, item_id, applicant="u", inspector="insp", manager="mgr"):
    item = service.get_item(item_id, "viewer")
    if item["status"] == "draft":
        item = service.transition(item_id, "submitted", item["version"], applicant, "applicant")
    if item["status"] == "submitted":
        item = service.transition(item_id, "inspection", item["version"], inspector, "inspector")
    if item["status"] == "inspection":
        item = service.transition(item_id, "correction", item["version"], inspector, "inspector")
    return service.transition(item_id, "approved", item["version"], manager, "compliance_manager")


class SnapshotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.item = self.service.create_item(
            {"title": "snap item", "description": "freeze on submit", "severity": "medium",
             "quantity": 4, "threshold": 10, "external_ref": "SNAP-1"},
            "u", "applicant")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def test_submit_freezes_quantity_and_equipment_version(self):
        self.service.upsert_equipment(
            self.item["id"], {"name": "baghouse", "spec": "BH-100", "external_ref": "EQ-1"},
            "u", "applicant")
        submitted = self.service.transition(
            self.item["id"], "submitted", 1, "u", "applicant")
        self.assertEqual(submitted["snapshot_version"], 1)
        snapshot = self.service.snapshots(self.item["id"], "viewer")[0]
        self.assertEqual(snapshot["source"], "submit")
        self.assertEqual(snapshot["effect"], "frozen")
        self.assertEqual(snapshot["frozen_quantity"], 4)
        self.assertEqual(snapshot["frozen_threshold"], 10)
        self.assertEqual(snapshot["frozen_equipment_version"], 1)
        self.assertEqual(snapshot["trigger"], "submit")

    def test_open_finding_on_pending_application_returns_for_review(self):
        submitted = self.service.transition(
            self.item["id"], "submitted", 1, "u", "applicant")
        self.service.transition(
            self.item["id"], "inspection", submitted["version"], "insp", "inspector")
        result = self.service.add_inspection(
            self.item["id"],
            {"kind": "finding", "detail": "leak", "idempotency_key": "F-1",
             "expected_snapshot_version": 1},
            "insp", "inspector")
        self.assertEqual(result["effect"], "returned")
        self.assertEqual(result["item"]["status"], "submitted")
        self.assertTrue(result["item"]["returned_for_review"])
        self.assertEqual(result["snapshot"]["snapshot_version"], 2)
        self.assertTrue(any("未关闭" in r for r in result["recompute_reasons"]))
        snapshots = self.service.snapshots(self.item["id"], "viewer")
        self.assertEqual([s["snapshot_version"] for s in snapshots], [1, 2])
        self.assertEqual(snapshots[-1]["source"], "recalculate")
        # 关闭整改后重算，申请保持在待批阶段且不再标记退回
        refreshed = self.service.close_record(
            self.item["id"], result["record"]["id"],
            {"expected_snapshot_version": 2}, "insp", "inspector")
        self.assertEqual(refreshed["effect"], "refreshed")
        self.assertEqual(refreshed["item"]["status"], "submitted")
        self.assertFalse(refreshed["item"]["returned_for_review"])

    def test_exceedance_record_returns_pending_and_records_measured_reason(self):
        submitted = self.service.transition(
            self.item["id"], "submitted", 1, "u", "applicant")
        result = self.service.add_inspection(
            self.item["id"],
            {"kind": "finding", "detail": "smoke", "measured": 25, "status": "closed",
             "idempotency_key": "F-2", "expected_snapshot_version": 1},
            "insp", "inspector")
        self.assertEqual(result["effect"], "returned")
        self.assertTrue(any("25" in r and "10" in r for r in result["recompute_reasons"]))
        self.assertTrue(result["snapshot"]["exceedance"])
        # 即使事项关闭，超标记录仍阻止批准
        cur = result["item"]
        cur = self.service.transition(cur["id"], "inspection", cur["version"], "insp", "inspector")
        cur = self.service.transition(cur["id"], "correction", cur["version"], "insp", "inspector")
        with self.assertRaises(ConflictError):
            self.service.transition(cur["id"], "approved", cur["version"], "mgr",
                                    "compliance_manager")

    def test_equipment_change_on_approved_permit_creates_revision(self):
        approved = approve(self.service, self.repo, self.item["id"])
        self.assertEqual(approved["revision_no"], 0)
        result = self.service.upsert_equipment(
            self.item["id"],
            {"name": "baghouse", "spec": "BH-200", "external_ref": "EQ-2"},
            "u", "applicant")
        self.assertEqual(result["effect"], "revision")
        self.assertEqual(result["item"]["status"], "approved")
        self.assertEqual(result["snapshot"]["revision_no"], 1)
        self.assertEqual(result["snapshot"]["revision_of"], 1)
        self.assertEqual(result["snapshot"]["source"], "revision")
        self.assertTrue(any("治理设备版本" in r for r in result["recompute_reasons"]))
        # 再次设备升级 -> 第2个修订版，沿修订链向前追溯
        result2 = self.service.upsert_equipment(
            self.item["id"],
            {"name": "baghouse", "spec": "BH-300", "external_ref": "EQ-2"},
            "u", "applicant")
        self.assertEqual(result2["snapshot"]["revision_no"], 2)
        self.assertEqual(result2["snapshot"]["revision_of"], 2)

    def test_inspection_record_requires_current_snapshot_version(self):
        self.service.transition(self.item["id"], "submitted", 1, "u", "applicant")
        with self.assertRaises(ConflictError):
            self.service.add_inspection(
                self.item["id"],
                {"kind": "finding", "detail": "stale", "idempotency_key": "S-1",
                 "expected_snapshot_version": 7},
                "insp", "inspector")
        with self.assertRaises(ValidationError):
            self.service.add_inspection(
                self.item["id"], {"kind": "finding", "detail": "no version"},
                "insp", "inspector")
        # 申请人记录不强制快照版本（向后兼容）
        record = self.service.add_record(
            self.item["id"], {"kind": "note", "detail": "applicant note"},
            "u", "applicant")
        self.assertIn("id", record)

    def test_concurrent_inspectors_only_one_accepted_and_retry_is_idempotent(self):
        self.service.transition(self.item["id"], "submitted", 1, "u", "applicant")
        barrier = threading.Barrier(2)
        outcomes = []

        def submit(key, detail):
            barrier.wait()
            try:
                outcomes.append(self.service.add_inspection(
                    self.item["id"],
                    {"kind": "finding", "detail": detail, "idempotency_key": key,
                     "expected_snapshot_version": 1},
                    "insp", "inspector"))
            except ConflictError as exc:
                outcomes.append(exc)

        t1 = threading.Thread(target=submit, args=("C-1", "first"))
        t2 = threading.Thread(target=submit, args=("C-2", "second"))
        t1.start(); t2.start(); t1.join(); t2.join()
        accepted = [o for o in outcomes if not isinstance(o, ConflictError)]
        rejected = [o for o in outcomes if isinstance(o, ConflictError)]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(len(rejected), 1)
        # 只有一个检查记录落库，且只产生一次退回重算（v1 -> v2）
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 1)
        versions = [s["snapshot_version"] for s in
                    self.service.snapshots(self.item["id"], "viewer")]
        self.assertEqual(versions, [1, 2])

    def test_same_idempotency_key_retry_does_not_duplicate_revision(self):
        approved = approve(self.service, self.repo, self.item["id"])
        base = approved["snapshot_version"]
        payload = {"kind": "finding", "detail": "over limit", "measured": 99,
                   "idempotency_key": "IDEM-1", "expected_snapshot_version": base}
        first = self.service.add_inspection(self.item["id"], payload, "insp", "inspector")
        self.assertEqual(first["effect"], "revision")
        # 失败方持旧快照版本凭同一幂等键重试：返回首次结果，不重复修订
        retry = self.service.add_inspection(self.item["id"], payload, "insp", "inspector")
        self.assertTrue(retry["idempotent_replay"])
        self.assertEqual(retry["record"]["id"], first["record"]["id"])
        self.assertEqual(retry["snapshot"]["snapshot_version"],
                         first["snapshot"]["snapshot_version"])
        records = self.service.list_records(self.item["id"], "viewer")
        self.assertEqual(len(records), 1)
        revisions = [s for s in self.service.snapshots(self.item["id"], "viewer")
                     if s["effect"] == "revision"]
        self.assertEqual(len(revisions), 1)
        self.assertEqual(revisions[0]["revision_no"], 1)

    def test_benign_change_on_approved_permit_does_not_revise(self):
        approve(self.service, self.repo, self.item["id"])
        base = self.service.get_item(self.item["id"], "viewer")["snapshot_version"]
        result = self.service.add_inspection(
            self.item["id"],
            {"kind": "note", "detail": "routine", "measured": 1, "status": "closed",
             "idempotency_key": "B-1", "expected_snapshot_version": base},
            "insp", "inspector")
        self.assertEqual(result["effect"], "refreshed")
        self.assertEqual(result["snapshot"]["revision_no"], 0)
        self.assertIsNone(result["snapshot"]["revision_of"])

    def test_list_and_audit_show_recompute_reason_and_revision_source(self):
        approve(self.service, self.repo, self.item["id"])
        self.service.upsert_equipment(
            self.item["id"], {"name": "baghouse", "spec": "BH-200", "external_ref": "EQ-3"},
            "u", "applicant")
        listed = self.service.list_items("viewer")[0]
        self.assertEqual(listed["latest_snapshot"]["source"], "revision")
        self.assertEqual(listed["revision_no"], 1)
        self.assertTrue(any("治理设备版本" in r for r in listed["recompute_reasons"]))
        events = self.service.audit("viewer", self.item["id"])
        revision_events = [e for e in events if e["action"] == "revision"]
        self.assertEqual(len(revision_events), 1)
        detail = revision_events[0]["detail"]
        self.assertEqual(detail["effect"], "revision")
        self.assertEqual(detail["trigger"], "equipment_change")
        self.assertTrue(detail["reasons"])
        self.assertEqual(detail["revision_no"], 1)
        self.assertEqual(detail["revision_of"], 1)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_only_applicant_can_change_equipment(self):
        with self.assertRaises(PermissionDenied):
            self.service.upsert_equipment(
                self.item["id"], {"name": "x", "spec": "y"}, "insp", "inspector")


class BackfillTest(unittest.TestCase):
    """历史数据缺快照：旧版数据库文件迁移后按初始版本补齐。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "legacy.db")
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript("""
            CREATE TABLE items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT NOT NULL,
                description TEXT NOT NULL, severity TEXT NOT NULL,
                quantity REAL NOT NULL, threshold REAL NOT NULL, status TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1, external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            CREATE TABLE records (
                id INTEGER PRIMARY KEY AUTOINCREMENT, item_id INTEGER NOT NULL,
                kind TEXT NOT NULL, detail TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open', external_ref TEXT,
                created_by TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, action TEXT NOT NULL,
                entity_type TEXT NOT NULL, entity_id INTEGER NOT NULL, actor TEXT NOT NULL,
                detail TEXT NOT NULL, previous_hash TEXT NOT NULL,
                entry_hash TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        conn.execute(
            """INSERT INTO items(title,description,severity,quantity,threshold,status,
               version,external_ref,created_by,created_at,updated_at)
               VALUES('legacy','old permit','high',7,10,'approved',3,'OLD-1',
               'legacy','2026-01-01T00:00:00+00:00','2026-01-02T00:00:00+00:00')""")
        conn.execute(
            """INSERT INTO records(item_id,kind,detail,status,external_ref,created_by,created_at)
               VALUES(1,'finding','open item','open','R-1','insp','2026-01-01T01:00:00+00:00')""")
        conn.commit(); conn.close()

    def tearDown(self):
        self.tmp.cleanup()

    def test_legacy_rows_get_initial_snapshot(self):
        repo = Repository(self.db_path)
        try:
            service = Service(repo)
            item = service.get_item(1, "viewer")
            self.assertEqual(item["snapshot_version"], 1)
            self.assertEqual(item["equipment_version"], 1)
            snapshots = service.snapshots(1, "viewer")
            self.assertEqual(len(snapshots), 1)
            snap = snapshots[0]
            self.assertEqual(snap["source"], "initial")
            self.assertEqual(snap["effect"], "initial")
            self.assertEqual(snap["trigger"], "backfill")
            self.assertEqual(snap["frozen_quantity"], 7)
            self.assertEqual(snap["open_records"], 1)
            self.assertTrue(any("初始版本" in r for r in snap["recompute_reasons"]))
            events = service.audit("viewer", 1)
            self.assertTrue(any(e["action"] == "snapshot_backfill" for e in events))
            self.assertTrue(repo.verify_audit_chain())
            # 幂等：再次打开不应重复补齐
            service.list_items("viewer")
        finally:
            repo.close()
        repo2 = Repository(self.db_path)
        try:
            self.assertEqual(len(Service(repo2).snapshots(1, "viewer")), 1)
        finally:
            repo2.close()

    def test_legacy_draft_can_still_submit_without_snapshot_collision(self):
        # 补一条历史草稿（无快照），迁移补齐v1后提交应追加v2而不是冲突
        conn = sqlite3.connect(self.db_path)
        conn.execute(
            """INSERT INTO items(title,description,severity,quantity,threshold,status,
               version,external_ref,created_by,created_at,updated_at)
               VALUES('legacy draft','old draft','low',1,10,'draft',1,'OLD-2',
               'legacy','2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')""")
        conn.commit(); conn.close()
        repo = Repository(self.db_path)
        try:
            service = Service(repo)
            submitted = service.transition(2, "submitted", 1, "u", "applicant")
            self.assertEqual(submitted["snapshot_version"], 2)
            versions = [s["snapshot_version"] for s in service.snapshots(2, "viewer")]
            self.assertEqual(versions, [1, 2])
            self.assertEqual(service.snapshots(2, "viewer")[1]["source"], "submit")
        finally:
            repo.close()


if __name__ == "__main__":
    unittest.main()
