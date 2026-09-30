from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (STATES, is_compliant, renewal_blockers)


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()
        self.backfill_snapshots("system")

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    idempotency_key TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_records_idempotency
                    ON records(item_id, idempotency_key) WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active'
                        CHECK(status IN ('active','inactive')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_equipment_item ON equipment(item_id);
                CREATE TABLE IF NOT EXISTS permit_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    version INTEGER NOT NULL,
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    equipment_version INTEGER NOT NULL DEFAULT 1,
                    open_records INTEGER NOT NULL DEFAULT 0,
                    compliant INTEGER NOT NULL DEFAULT 1,
                    reason TEXT NOT NULL,
                    source TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, version)
                );
                CREATE INDEX IF NOT EXISTS idx_snapshots_item
                    ON permit_snapshots(item_id, version);
                CREATE TABLE IF NOT EXISTS renewal_applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    snapshot_id INTEGER REFERENCES permit_snapshots(id),
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK(status IN ('pending','approved','returned')),
                    idempotency_key TEXT NOT NULL UNIQUE,
                    note TEXT,
                    submitted_by TEXT NOT NULL,
                    submitted_at TEXT NOT NULL,
                    reviewed_by TEXT,
                    reviewed_at TEXT,
                    review_note TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_renewals_item ON renewal_applications(item_id);
            """)
        self._migrate()

    def _migrate(self) -> None:
        """为旧库补齐 records.idempotency_key 列。"""
        with self._lock, self.conn:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(records)").fetchall()]
            if "idempotency_key" not in cols:
                self.conn.execute("ALTER TABLE records ADD COLUMN idempotency_key TEXT")
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ux_records_idempotency "
                "ON records(item_id, idempotency_key) WHERE idempotency_key IS NOT NULL"
            )

    # ---------- 基础读取 ----------
    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
                snap = self._create_snapshot(item_id, "initial", "system", actor, self.conn)
                self._append_snapshot_audit(snap, actor, self.conn)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    # ---------- 设备 ----------
    def list_equipment(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM equipment WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def add_equipment(self, item_id: int, name: str, detail: str, status: str,
                      actor: str) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
        with self._lock, self.conn:
            self.get_item(item_id)
            now = utc_now()
            row = self.conn.execute(
                "SELECT COALESCE(MAX(version),1) AS v FROM equipment WHERE item_id=?",
                (item_id,),
            ).fetchone()
            version = int(row["v"]) + 1
            cur = self.conn.execute(
                """INSERT INTO equipment(item_id, name, detail, status, version,
                   created_by, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)""",
                (item_id, name, detail, status, version, actor, now, now),
            )
            equipment = dict(self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (int(cur.lastrowid),)
            ).fetchone())
            snap, returned = self._recalculate(
                item_id, "equipment_changed", f"equipment:{equipment['id']}", actor, self.conn)
            return equipment, snap, returned

    def update_equipment(self, equipment_id: int, name: str, detail: str, status: str,
                         actor: str) -> Tuple[Dict[str, Any], Dict[str, Any], Optional[Dict[str, Any]]]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("治理设备不存在")
            now = utc_now()
            version = int(row["version"]) + 1
            self.conn.execute(
                "UPDATE equipment SET name=?, detail=?, status=?, version=?, updated_at=? WHERE id=?",
                (name, detail, status, version, now, equipment_id),
            )
            equipment = dict(self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)
            ).fetchone())
            snap, returned = self._recalculate(
                equipment["item_id"], "equipment_changed", f"equipment:{equipment_id}",
                actor, self.conn)
            return equipment, snap, returned

    # ---------- 检查记录 ----------
    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], idempotency_key: Optional[str],
                   expected_snapshot_version: Optional[int], actor: str
                   ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]], bool]:
        with self._lock, self.conn:
            self.get_item(item_id)
            # 幂等：同 key 重试直接回放，不产生新修订
            if idempotency_key:
                existing = self.conn.execute(
                    "SELECT * FROM records WHERE item_id=? AND idempotency_key=?",
                    (item_id, idempotency_key),
                ).fetchone()
                if existing:
                    snap = self._current_snapshot(item_id, self.conn)
                    return dict(existing), snap, True
            # 乐观并发：只接受当前快照版本
            if expected_snapshot_version is not None:
                current = self._current_snapshot_version(item_id, self.conn)
                if int(expected_snapshot_version) != current:
                    raise ConflictError("快照版本冲突，请刷新后重试")
            try:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       idempotency_key, created_by, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, idempotency_key,
                     actor, utc_now()),
                )
                record_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                raise ConflictError("记录唯一标识已存在") from exc
            record = dict(self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            snap, returned = self._recalculate(
                item_id, "record_changed", f"record:{record_id}", actor, self.conn)
            self._append_audit(
                "record", "item", item_id, actor,
                {"record_id": record_id, "kind": kind, "status": status,
                 "snapshot_version": snap["version"], "reason": snap["reason"],
                 "source": snap["source"]},
                self.conn)
            return record, snap, False

    def close_record(self, item_id: int, record_id: int, actor: str
                     ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        with self._lock, self.conn:
            record = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?", (record_id, item_id)
            ).fetchone()
            if record is None:
                raise NotFoundError("记录不存在")
            if record["status"] == "closed":
                snap = self._current_snapshot(item_id, self.conn)
                return dict(record), snap, False
            self.conn.execute(
                "UPDATE records SET status='closed' WHERE id=?", (record_id,))
            record = dict(self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            snap, returned = self._recalculate(
                item_id, "record_closed", f"record:{record_id}", actor, self.conn)
            self._append_audit(
                "record_closed", "item", item_id, actor,
                {"record_id": record_id, "snapshot_version": snap["version"],
                 "reason": snap["reason"], "source": snap["source"]},
                self.conn)
            return record, snap, True

    # ---------- 许可快照 ----------
    def _current_snapshot_version(self, item_id: int, conn: sqlite3.Connection) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(version),0) AS v FROM permit_snapshots WHERE item_id=?",
            (item_id,),
        ).fetchone()
        return int(row["v"])

    def _current_snapshot(self, item_id: int, conn: sqlite3.Connection) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM permit_snapshots WHERE item_id=? ORDER BY version DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return dict(row) if row else None

    def get_current_snapshot(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._current_snapshot(item_id, self.conn)

    def _create_snapshot(self, item_id: int, reason: str, source: str,
                         actor: str, conn: sqlite3.Connection) -> Dict[str, Any]:
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if item is None:
            raise NotFoundError("项目不存在")
        version = self._current_snapshot_version(item_id, conn) + 1
        eq = conn.execute(
            "SELECT COALESCE(MAX(version),1) AS v FROM equipment WHERE item_id=?",
            (item_id,),
        ).fetchone()
        equipment_version = int(eq["v"])
        open_row = conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
            (item_id,),
        ).fetchone()
        open_records = int(open_row["n"])
        compliant = 1 if is_compliant(item["quantity"], item["threshold"], open_records) else 0
        now = utc_now()
        cur = conn.execute(
            """INSERT INTO permit_snapshots(item_id, version, quantity, threshold,
               equipment_version, open_records, compliant, reason, source,
               created_by, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, version, item["quantity"], item["threshold"], equipment_version,
             open_records, compliant, reason, source, actor, now),
        )
        return dict(conn.execute(
            "SELECT * FROM permit_snapshots WHERE id=?", (int(cur.lastrowid),)
        ).fetchone())

    def _append_snapshot_audit(self, snap: Dict[str, Any], actor: str,
                               conn: sqlite3.Connection) -> None:
        self._append_audit(
            "snapshot", "permit", snap["item_id"], actor,
            {"snapshot_id": snap["id"], "version": snap["version"],
             "reason": snap["reason"], "source": snap["source"],
             "quantity": snap["quantity"], "threshold": snap["threshold"],
             "equipment_version": snap["equipment_version"],
             "open_records": snap["open_records"],
             "compliant": bool(snap["compliant"])},
            conn)

    def _recalculate(self, item_id: int, reason: str, source: str, actor: str,
                     conn: sqlite3.Connection
                     ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        """改动触发重算：生成新快照；待批申请不合规则退回；已批许可生成修订版。"""
        snap = self._create_snapshot(item_id, reason, source, actor, conn)
        self._append_snapshot_audit(snap, actor, conn)
        item = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        # 已批许可：生成修订版（新快照即修订版），记录修订来源。
        # “已批”既包括工作流状态 approved，也包括续期申请已批准。
        approved_renewal = conn.execute(
            "SELECT 1 FROM renewal_applications WHERE item_id=? AND status='approved' LIMIT 1",
            (item_id,),
        ).fetchone()
        is_approved = (item is not None and item["status"] == "approved") or approved_renewal is not None
        if is_approved:
            self._append_audit(
                "revision", "permit", item_id, actor,
                {"snapshot_id": snap["id"], "version": snap["version"],
                 "reason": reason, "source": source},
                conn)
        # 待批申请：超标或未关闭事项 -> 退回复核
        returned: Optional[Dict[str, Any]] = None
        renewal = conn.execute(
            """SELECT * FROM renewal_applications
               WHERE item_id=? AND status='pending' ORDER BY id DESC LIMIT 1""",
            (item_id,),
        ).fetchone()
        if renewal is not None and not snap["compliant"]:
            blockers = renewal_blockers(snap["quantity"], snap["threshold"],
                                         snap["open_records"])
            note = "；".join(blockers)
            now = utc_now()
            conn.execute(
                """UPDATE renewal_applications
                   SET status='returned', review_note=?, reviewed_by=?, reviewed_at=?,
                       updated_at=? WHERE id=?""",
                (note, actor, now, now, renewal["id"]),
            )
            returned = dict(renewal)
            returned.update({"status": "returned", "review_note": note})
            self._append_audit(
                "renewal_returned", "renewal", renewal["id"], actor,
                {"item_id": item_id, "snapshot_id": snap["id"],
                 "snapshot_version": snap["version"], "reason": reason,
                 "source": source, "blockers": blockers},
                conn)
        return snap, returned

    def recalculate(self, item_id: int, reason: str, source: str, actor: str
                    ) -> Tuple[Dict[str, Any], Optional[Dict[str, Any]]]:
        with self._lock, self.conn:
            return self._recalculate(item_id, reason, source, actor, self.conn)

    def ensure_initial_snapshot(self, item_id: int, reason: str, source: str,
                                actor: str) -> Optional[Dict[str, Any]]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM permit_snapshots WHERE item_id=?",
                (item_id,),
            ).fetchone()
            if int(row["n"]) > 0:
                return None
            snap = self._create_snapshot(item_id, reason, source, actor, self.conn)
            self._append_snapshot_audit(snap, actor, self.conn)
            return snap

    def backfill_snapshots(self, actor: str = "system") -> int:
        """历史数据缺快照按初始版本补齐。"""
        count = 0
        with self._lock, self.conn:
            rows = self.conn.execute(
                """SELECT i.id FROM items i
                   WHERE NOT EXISTS (
                       SELECT 1 FROM permit_snapshots p WHERE p.item_id=i.id)"""
            ).fetchall()
            for row in rows:
                snap = self._create_snapshot(int(row["id"]), "initial", "backfill",
                                            actor, self.conn)
                self._append_snapshot_audit(snap, actor, self.conn)
                count += 1
        return count

    def list_snapshots(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM permit_snapshots WHERE item_id=? ORDER BY version DESC",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    # ---------- 续期申请 ----------
    def list_renewals(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                """SELECT r.*, s.version AS snapshot_version,
                          s.quantity AS frozen_quantity, s.threshold AS frozen_threshold,
                          s.equipment_version AS frozen_equipment_version,
                          s.open_records AS frozen_open_records,
                          s.compliant AS frozen_compliant,
                          s.reason AS snapshot_reason, s.source AS snapshot_source
                   FROM renewal_applications r
                   LEFT JOIN permit_snapshots s ON s.id = r.snapshot_id
                   WHERE r.item_id=? ORDER BY r.id DESC""",
                (item_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def get_renewal(self, item_id: int, renewal_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM renewal_applications WHERE id=? AND item_id=?",
                (renewal_id, item_id),
            ).fetchone()
        if row is None:
            raise NotFoundError("续期申请不存在")
        return dict(row)

    def submit_renewal(self, item_id: int, idempotency_key: str, actor: str,
                       note: Optional[str]
                       ) -> Tuple[Dict[str, Any], Dict[str, Any], bool]:
        with self._lock, self.conn:
            self.get_item(item_id)
            existing = self.conn.execute(
                "SELECT * FROM renewal_applications WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                snap = self._current_snapshot(item_id, self.conn)
                return dict(existing), snap, True
            now = utc_now()
            cur = self.conn.execute(
                """INSERT INTO renewal_applications(item_id, snapshot_id, status,
                   idempotency_key, note, submitted_by, submitted_at, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (item_id, None, "pending", idempotency_key, note, actor, now, now, now),
            )
            renewal_id = int(cur.lastrowid)
            snap = self._create_snapshot(
                item_id, "renewal_submitted", f"renewal:{renewal_id}", actor, self.conn)
            self.conn.execute(
                "UPDATE renewal_applications SET snapshot_id=? WHERE id=?",
                (snap["id"], renewal_id),
            )
            renewal = dict(self.conn.execute(
                "SELECT * FROM renewal_applications WHERE id=?", (renewal_id,)
            ).fetchone())
            self._append_audit(
                "renewal_submitted", "renewal", renewal_id, actor,
                {"item_id": item_id, "snapshot_id": snap["id"],
                 "snapshot_version": snap["version"],
                 "quantity": snap["quantity"], "threshold": snap["threshold"],
                 "equipment_version": snap["equipment_version"],
                 "open_records": snap["open_records"],
                 "compliant": bool(snap["compliant"]),
                 "idempotency_key": idempotency_key},
                self.conn)
            self._append_snapshot_audit(snap, actor, self.conn)
            return renewal, snap, False

    def approve_renewal(self, item_id: int, renewal_id: int, actor: str,
                        note: Optional[str]) -> Dict[str, Any]:
        with self._lock, self.conn:
            renewal = self.get_renewal(item_id, renewal_id)
            if renewal["status"] != "pending":
                raise ConflictError("只有待批申请可以批准")
            now = utc_now()
            self.conn.execute(
                """UPDATE renewal_applications
                   SET status='approved', reviewed_by=?, reviewed_at=?, review_note=?,
                       updated_at=? WHERE id=?""",
                (actor, now, note, now, renewal_id),
            )
            self._append_audit(
                "renewal_approved", "renewal", renewal_id, actor,
                {"item_id": item_id, "note": note}, self.conn)
            return self.get_renewal(item_id, renewal_id)

    def resubmit_renewal(self, item_id: int, renewal_id: int, actor: str,
                         note: Optional[str]
                         ) -> Tuple[Dict[str, Any], Dict[str, Any]]:
        with self._lock, self.conn:
            renewal = self.get_renewal(item_id, renewal_id)
            if renewal["status"] != "returned":
                raise ConflictError("只有退回的申请可以重新提交")
            snap = self._create_snapshot(
                item_id, "renewal_resubmitted", f"renewal:{renewal_id}", actor, self.conn)
            now = utc_now()
            self.conn.execute(
                """UPDATE renewal_applications
                   SET status='pending', snapshot_id=?, note=?, submitted_by=?,
                       submitted_at=?, updated_at=? WHERE id=?""",
                (snap["id"], note, actor, now, now, renewal_id),
            )
            self._append_audit(
                "renewal_resubmitted", "renewal", renewal_id, actor,
                {"item_id": item_id, "snapshot_id": snap["id"],
                 "snapshot_version": snap["version"], "note": note},
                self.conn)
            self._append_snapshot_audit(snap, actor, self.conn)
            return self.get_renewal(item_id, renewal_id), snap

    # ---------- 审计链 ----------
    def _append_audit(self, action: str, entity_type: str, entity_id: int,
                      actor: str, detail: dict,
                      conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
        conn = conn or self.conn
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
        ).fetchone()
        previous = row["entry_hash"] if row else "GENESIS"
        event = make_entry(action, entity_type, entity_id, actor, detail, previous)
        cur = conn.execute(
            """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
               previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
            (event["action"], event["entity_type"], event["entity_id"], event["actor"],
             json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
             event["previous_hash"], event["entry_hash"], event["created_at"]),
        )
        event["id"] = int(cur.lastrowid)
        return event

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            return self._append_audit(action, entity_type, entity_id, actor, detail, self.conn)

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    def close(self) -> None:
        with self._lock:
            self.conn.close()
