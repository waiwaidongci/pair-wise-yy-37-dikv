from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError
from .rules import (EFFECT_FROZEN, EFFECT_INITIAL, ENTITY, SNAP_INITIAL,
                    SNAP_RECALC, SNAP_REVISION, SNAP_SUBMIT, STATES,
                    TRIGGER_BACKFILL, TRIGGER_EQUIPMENT, TRIGGER_INSPECTION,
                    TRIGGER_RECORD_CLOSE, TRIGGER_SUBMIT, build_reasons,
                    effect_source, recompute_effect)


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
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
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
            """)
            # 旧库迁移：治理设备版本与快照版本挂到许可主表
            self._ensure_column("items", "equipment_version",
                                "INTEGER NOT NULL DEFAULT 1")
            self._ensure_column("items", "snapshot_version",
                                "INTEGER NOT NULL DEFAULT 0")
            self._ensure_column("records", "measured", "REAL")
            self._ensure_column("records", "idempotency_key", "TEXT")
            self.conn.executescript("""
                CREATE UNIQUE INDEX IF NOT EXISTS ux_records_idempotency
                    ON records(item_id, idempotency_key)
                    WHERE idempotency_key IS NOT NULL;
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    spec TEXT NOT NULL DEFAULT '',
                    version INTEGER NOT NULL DEFAULT 1,
                    status TEXT NOT NULL DEFAULT 'active',
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS permit_snapshots (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    snapshot_version INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    effect TEXT NOT NULL,
                    trigger TEXT NOT NULL DEFAULT '',
                    quantity REAL NOT NULL,
                    threshold REAL NOT NULL,
                    equipment_version INTEGER NOT NULL,
                    open_records INTEGER NOT NULL DEFAULT 0,
                    exceedance INTEGER NOT NULL DEFAULT 0,
                    revision_of INTEGER,
                    revision_no INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT '',
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, snapshot_version)
                );
                CREATE INDEX IF NOT EXISTS ix_snapshots_item
                    ON permit_snapshots(item_id, snapshot_version);
            """)
            self._backfill_initial_snapshots()

    def _ensure_column(self, table: str, column: str, decl: str) -> None:
        cols = {row["name"] for row in self.conn.execute(
            f"PRAGMA table_info({table})")}
        if column not in cols:
            self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def _backfill_initial_snapshots(self) -> None:
        """历史数据缺快照：按初始版本（v1）补齐，来源标记为initial。"""
        rows = self.conn.execute(
            """SELECT i.* FROM items i
               WHERE NOT EXISTS (
                   SELECT 1 FROM permit_snapshots s WHERE s.item_id=i.id)""").fetchall()
        now = utc_now()
        for row in rows:
            item = dict(row)
            open_records = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item["id"],)).fetchone()["n"]
            max_measured = self.conn.execute(
                "SELECT MAX(measured) AS m FROM records WHERE item_id=? AND measured IS NOT NULL",
                (item["id"],)).fetchone()["m"]
            exceedance = max_measured is not None and max_measured > item["threshold"]
            reason = "历史许可缺少快照，按初始版本v1补齐排放量与治理设备版本"
            self.conn.execute(
                """INSERT INTO permit_snapshots(item_id, snapshot_version, source, effect,
                   trigger, quantity, threshold, equipment_version, open_records, exceedance,
                   revision_of, revision_no, reason, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item["id"], 1, SNAP_INITIAL, EFFECT_INITIAL, TRIGGER_BACKFILL,
                 item["quantity"], item["threshold"], item["equipment_version"],
                 int(open_records), int(exceedance), None, 0, reason, "system", now))
            self.conn.execute(
                "UPDATE items SET snapshot_version=1 WHERE id=?", (item["id"],))
            self._insert_audit_locked(
                self.conn, "snapshot_backfill", ENTITY, item["id"], "system", {
                    "snapshot_version": 1, "source": SNAP_INITIAL,
                    "trigger": TRIGGER_BACKFILL, "reason": reason,
                    "quantity": item["quantity"], "threshold": item["threshold"],
                    "equipment_version": item["equipment_version"],
                    "open_records": int(open_records), "exceedance": bool(exceedance),
                })

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at,
                       equipment_version, snapshot_version)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now, 1, 0),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

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

    def submit_item(self, item_id: int, expected_version: int, actor: str
                    ) -> Dict[str, Any]:
        """续期申请提交：冻结排放量、许可限值与治理设备版本，生成提交快照。"""
        now = utc_now()
        with self._lock, self.conn:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("项目不存在")
            item = dict(row)
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=? AND status='draft'""",
                (STATES[1], now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                raise ConflictError("版本冲突或申请已提交，请刷新后重试")
            latest = self._latest_snapshot_locked(item_id)
            # 正常新建许可从v1开始；被补齐过初始快照的历史草稿则在其后追加
            snapshot_version = (int(latest["snapshot_version"]) + 1) if latest else 1
            open_records = self._open_count_locked(item_id)
            max_measured = self._max_measured_locked(item_id)
            exceedance = max_measured is not None and max_measured > item["threshold"]
            self.conn.execute(
                """INSERT INTO permit_snapshots(item_id, snapshot_version, source, effect,
                   trigger, quantity, threshold, equipment_version, open_records, exceedance,
                   revision_of, revision_no, reason, created_by, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (item_id, snapshot_version, SNAP_SUBMIT, EFFECT_FROZEN, TRIGGER_SUBMIT,
                 item["quantity"], item["threshold"], item["equipment_version"],
                 open_records, int(exceedance), None,
                 int(latest["revision_no"] or 0) if latest else 0,
                 "续期申请提交，冻结排放量与治理设备版本", actor, now))
            self.conn.execute(
                "UPDATE items SET snapshot_version=? WHERE id=?",
                (snapshot_version, item_id))
            self._insert_audit_locked(self.conn, "submit", ENTITY, item_id, actor, {
                "snapshot_version": snapshot_version, "source": SNAP_SUBMIT,
                "quantity": item["quantity"], "threshold": item["threshold"],
                "equipment_version": item["equipment_version"],
                "open_records": open_records, "exceedance": bool(exceedance),
            })
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def _find_idempotent_record(self, conn, item_id: int, key: str
                                ) -> Optional[Dict[str, Any]]:
        row = conn.execute(
            "SELECT * FROM records WHERE item_id=? AND idempotency_key=?",
            (item_id, key)).fetchone()
        return dict(row) if row else None

    def add_inspection_record(self, item_id: int, kind: str, detail: str, status: str,
                              external_ref: Optional[str], measured: Optional[float],
                              idempotency_key: Optional[str],
                              expected_snapshot_version: Optional[int], actor: str
                              ) -> Dict[str, Any]:
        """检查员提交检查记录：只接受当前快照版本；幂等键保证重试不重复修订。"""
        now = utc_now()
        with self._lock, self.conn:
            item = self.get_item(item_id)
            # 幂等重放优先于版本检查：失败方持旧版本号重试也必须返回首次结果
            if idempotency_key is not None:
                existing = self._find_idempotent_record(self.conn, item_id, idempotency_key)
                if existing is not None:
                    snap = self._latest_snapshot_locked(item_id)
                    return {"record": existing, "snapshot": snap,
                            "effect": snap.get("effect") if snap else None,
                            "reasons": None, "idempotent_replay": True}
            if (expected_snapshot_version is not None
                    and int(expected_snapshot_version) != int(item["snapshot_version"])):
                raise ConflictError(
                    f"快照版本已过期：客户端v{expected_snapshot_version}，当前v{item['snapshot_version']}，请刷新后凭幂等键重试")
            try:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       measured, idempotency_key, created_by, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, measured,
                     idempotency_key, actor, now),
                )
                record_id = int(cur.lastrowid)
            except sqlite3.IntegrityError as exc:
                existing = (self._find_idempotent_record(self.conn, item_id, idempotency_key)
                            if idempotency_key is not None else None)
                if existing is not None:
                    snap = self._latest_snapshot_locked(item_id)
                    return {"record": existing, "snapshot": snap,
                            "effect": snap.get("effect") if snap else None,
                            "reasons": None, "idempotent_replay": True}
                raise ConflictError("记录唯一标识已存在") from exc
            record = dict(self.conn.execute(
                "SELECT * FROM records WHERE id=?", (record_id,)).fetchone())
            result = {"record": record, "snapshot": None, "effect": None,
                      "reasons": None, "idempotent_replay": False}
            if item["status"] != "draft":
                result.update(self._recompute_locked(
                    item, TRIGGER_INSPECTION, actor))
        return result

    def close_record(self, item_id: int, record_id: int,
                     expected_snapshot_version: Optional[int], actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            record_row = self.conn.execute(
                "SELECT * FROM records WHERE id=? AND item_id=?",
                (record_id, item_id)).fetchone()
            if record_row is None:
                raise NotFoundError("检查记录不存在")
            record = dict(record_row)
            item = self.get_item(item_id)
            if record["status"] == "closed":
                return {"record": record, "snapshot": self._latest_snapshot_locked(item_id),
                        "effect": None, "reasons": None, "idempotent_replay": True}
            if (expected_snapshot_version is not None
                    and int(expected_snapshot_version) != int(item["snapshot_version"])):
                raise ConflictError(
                    f"快照版本已过期：客户端v{expected_snapshot_version}，当前v{item['snapshot_version']}，请刷新后重试")
            self.conn.execute("UPDATE records SET status='closed' WHERE id=?", (record_id,))
            record["status"] = "closed"
            result = {"record": record, "snapshot": None, "effect": None,
                      "reasons": None, "idempotent_replay": False}
            if item["status"] != "draft":
                result.update(self._recompute_locked(
                    item, TRIGGER_RECORD_CLOSE, actor))
        return result

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            return self._open_count_locked(item_id)

    def _open_count_locked(self, item_id: int) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
            (item_id,)).fetchone()
        return int(row["n"])

    def _max_measured_locked(self, item_id: int) -> Optional[float]:
        row = self.conn.execute(
            "SELECT MAX(measured) AS m FROM records WHERE item_id=? AND measured IS NOT NULL",
            (item_id,)).fetchone()
        m = row["m"]
        return float(m) if m is not None else None

    def exceedance_count(self, item_id: int, threshold: float) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND measured IS NOT NULL AND measured>?",
                (item_id, threshold)).fetchone()
        return int(row["n"])

    # ------------------------------------------------------------------
    # 治理设备
    # ------------------------------------------------------------------
    def upsert_equipment(self, item_id: int, name: str, spec: str, status: str,
                         external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        """登记或升级治理设备；设备版本变化在快照冻结之后即触发重算。"""
        now = utc_now()
        with self._lock, self.conn:
            item = self.get_item(item_id)
            existing_row = None
            if external_ref is not None:
                existing_row = self.conn.execute(
                    "SELECT * FROM equipment WHERE item_id=? AND external_ref=?",
                    (item_id, external_ref)).fetchone()
            first_device = self.conn.execute(
                "SELECT COUNT(*) AS n FROM equipment WHERE item_id=?", (item_id,)).fetchone()["n"] == 0
            if existing_row is not None:
                existing = dict(existing_row)
                new_version = int(existing["version"]) + 1
                self.conn.execute(
                    """UPDATE equipment SET name=?, spec=?, version=?, status=?,
                       updated_at=? WHERE id=?""",
                    (name, spec, new_version, status, now, existing["id"]))
                equip_id = existing["id"]
            else:
                new_version = 1
                cur = self.conn.execute(
                    """INSERT INTO equipment(item_id, name, spec, version, status,
                       external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?)""",
                    (item_id, name, spec, new_version, status, external_ref, actor, now, now))
                equip_id = int(cur.lastrowid)
            # 许可级设备版本：任何设备变动都递增；仅首次在提交前登记设备时保持v1基线
            new_equipment_version = int(item["equipment_version"])
            if not (first_device and int(item["snapshot_version"]) == 0):
                new_equipment_version += 1
            self.conn.execute(
                "UPDATE items SET equipment_version=?, updated_at=? WHERE id=?",
                (new_equipment_version, now, item_id))
            item["equipment_version"] = new_equipment_version
            equipment = dict(self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equip_id,)).fetchone())
            result = {"equipment": equipment, "snapshot": None, "effect": None,
                      "reasons": None, "idempotent_replay": False}
            if item["status"] != "draft":
                result.update(self._recompute_locked(item, TRIGGER_EQUIPMENT, actor))
        return result

    def list_equipment(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM equipment WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
        return [dict(row) for row in rows]

    # ------------------------------------------------------------------
    # 许可快照与重算
    # ------------------------------------------------------------------
    def _latest_snapshot_locked(self, item_id: int) -> Optional[Dict[str, Any]]:
        row = self.conn.execute(
            "SELECT * FROM permit_snapshots WHERE item_id=? ORDER BY snapshot_version DESC LIMIT 1",
            (item_id,)).fetchone()
        return dict(row) if row else None

    def latest_snapshot(self, item_id: int) -> Optional[Dict[str, Any]]:
        with self._lock:
            return self._latest_snapshot_locked(item_id)

    def list_snapshots(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM permit_snapshots WHERE item_id=? ORDER BY snapshot_version",
                (item_id,)).fetchall()
        return [dict(row) for row in rows]

    def _recompute_locked(self, item: Dict[str, Any], trigger: str, actor: str
                          ) -> Dict[str, Any]:
        """依据冻结快照重算：待批且超标/有未关闭事项则退回复核；已批则生成修订版。

        调用方必须已持有 self._lock 与写事务。
        """
        item_id = int(item["id"])
        now = utc_now()
        frozen = self._latest_snapshot_locked(item_id)
        if frozen is None:
            # 极端兼容：缺失快照时先按初始版本补齐
            frozen = self._emergency_initial_snapshot(item, now)
        open_records = self._open_count_locked(item_id)
        max_measured = self._max_measured_locked(item_id)
        frozen_threshold = float(frozen["threshold"])
        exceedance = max_measured is not None and max_measured > frozen_threshold
        equipment_version = int(item["equipment_version"])
        frozen_equipment_version = int(frozen["equipment_version"])
        equipment_drift = equipment_version != frozen_equipment_version
        reasons = build_reasons(
            equipment_version=equipment_version,
            frozen_equipment_version=frozen_equipment_version,
            open_records=open_records, exceedance=exceedance,
            max_measured=max_measured, frozen_threshold=frozen_threshold,
            trigger=trigger, changed=True)
        effect = recompute_effect(item["status"], open_records, exceedance,
                                  equipment_drift)
        if effect is None:
            return {"snapshot": frozen, "effect": None, "reasons": reasons}
        new_version = int(frozen["snapshot_version"]) + 1
        revision_no = int(frozen["revision_no"] or 0)
        revision_of = None
        new_status = item["status"]
        if effect == "revision":
            revision_no += 1
            revision_of = int(frozen["snapshot_version"])
        elif effect == "returned" and item["status"] != "submitted":
            # 待批申请退回申请人复核
            new_status = "submitted"
        self.conn.execute(
            """INSERT INTO permit_snapshots(item_id, snapshot_version, source, effect,
               trigger, quantity, threshold, equipment_version, open_records, exceedance,
               revision_of, revision_no, reason, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item_id, new_version, effect_source(effect), effect, trigger,
             float(frozen["quantity"]), frozen_threshold, equipment_version,
             open_records, int(exceedance), revision_of, revision_no,
             json.dumps(reasons, ensure_ascii=False), actor, now))
        if new_status == "submitted" and item["status"] != "submitted":
            self.conn.execute(
                """UPDATE items SET status='submitted', version=version+1,
                   snapshot_version=?, updated_at=? WHERE id=?""",
                (new_version, now, item_id))
        else:
            self.conn.execute(
                "UPDATE items SET version=version+1, snapshot_version=?, updated_at=? WHERE id=?",
                (new_version, now, item_id))
        snapshot = self._latest_snapshot_locked(item_id)
        detail = {
            "snapshot_version": new_version, "source": effect_source(effect),
            "effect": effect, "trigger": trigger, "reasons": reasons,
            "from_status": item["status"], "to_status": new_status,
            "equipment_version": equipment_version,
            "frozen_equipment_version": frozen_equipment_version,
            "open_records": open_records, "exceedance": bool(exceedance),
            "revision_no": revision_no, "revision_of": revision_of,
        }
        self._insert_audit_locked(
            self.conn,
            "revision" if effect == "revision" else "recompute",
            ENTITY, item_id, actor, detail)
        return {"snapshot": snapshot, "effect": effect, "reasons": reasons}

    def _emergency_initial_snapshot(self, item: Dict[str, Any], now: str
                                    ) -> Dict[str, Any]:
        open_records = self._open_count_locked(item["id"])
        max_measured = self._max_measured_locked(item["id"])
        exceedance = max_measured is not None and max_measured > item["threshold"]
        reason = "历史许可缺少快照，按初始版本v1补齐排放量与治理设备版本"
        self.conn.execute(
            """INSERT INTO permit_snapshots(item_id, snapshot_version, source, effect,
               trigger, quantity, threshold, equipment_version, open_records, exceedance,
               revision_of, revision_no, reason, created_by, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (item["id"], 1, SNAP_INITIAL, EFFECT_INITIAL, TRIGGER_BACKFILL,
             item["quantity"], item["threshold"], item["equipment_version"],
             open_records, int(exceedance), None, 0, reason, "system", now))
        self.conn.execute("UPDATE items SET snapshot_version=1 WHERE id=?", (item["id"],))
        return self._latest_snapshot_locked(item["id"])

    # ------------------------------------------------------------------
    # 审计链
    # ------------------------------------------------------------------
    def _insert_audit_locked(self, conn, action: str, entity_type: str, entity_id: int,
                             actor: str, detail: dict) -> Dict[str, Any]:
        row = conn.execute(
            "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1").fetchone()
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
            return self._insert_audit_locked(
                self.conn, action, entity_type, entity_id, actor, detail)

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
