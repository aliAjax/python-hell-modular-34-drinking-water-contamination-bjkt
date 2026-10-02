import json
import sqlite3
from datetime import datetime, timezone

from .audit import audit_hash, canonical_json
from .domain import ConflictError, NotFoundError, DomainError


def now_iso():
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, path):
        self.path = path

    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def initialize(self):
        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    entity_type TEXT NOT NULL,
                    stable_key TEXT NOT NULL,
                    status TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    created_role TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    UNIQUE(entity_type, stable_key)
                );
                CREATE TABLE IF NOT EXISTS sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    source_type TEXT NOT NULL,
                    external_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, source_type, external_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS actions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    role TEXT,
                    payload TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    event_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS version_snapshots (
                    item_id INTEGER NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY(item_id, version)
                );
                CREATE TABLE IF NOT EXISTS offline_batches (
                    batch_id TEXT PRIMARY KEY,
                    item_id INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    base_version INTEGER NOT NULL,
                    seq INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    role TEXT NOT NULL,
                    original_payload TEXT NOT NULL,
                    status TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    applied_at TEXT,
                    applied_version INTEGER,
                    result_payload TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_offline_pending
                    ON offline_batches(status, item_id, seq);
                """
            )
        finally:
            conn.close()

    def _row_to_item(self, row):
        if row is None:
            return None
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        return result

    def _last_hash(self, conn, item_id):
        row = conn.execute(
            "SELECT event_hash FROM audit_events WHERE item_id IS ? ORDER BY id DESC LIMIT 1",
            (item_id,),
        ).fetchone()
        return row["event_hash"] if row else "GENESIS"

    def append_audit(self, conn, item_id, event_type, actor, role, payload):
        previous = self._last_hash(conn, item_id)
        event = {
            "item_id": item_id,
            "event_type": event_type,
            "actor": actor,
            "role": role,
            "payload": payload,
            "created_at": now_iso(),
        }
        event_hash = audit_hash(previous, event)
        conn.execute(
            "INSERT INTO audit_events(item_id,event_type,actor,role,payload,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (item_id, event_type, actor, role, canonical_json(payload), previous, event_hash, event["created_at"]),
        )

    def create_item(self, entity_type, stable_key, initial_status, payload, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT INTO items(entity_type,stable_key,status,version,payload,created_by,created_role,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_type,
                        stable_key,
                        initial_status,
                        1,
                        canonical_json(payload),
                        actor,
                        role,
                        now_iso(),
                        now_iso(),
                    ),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_item", "同一业务实体已经存在")
            item_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(conn, item_id, "created", actor, role, {"stable_key": stable_key})
            conn.execute(
                "INSERT INTO version_snapshots(item_id,version,status,payload,created_at) VALUES(?,?,?,?,?)",
                (item_id, 1, initial_status, canonical_json(payload), now_iso()),
            )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_item(self, item_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            return self._row_to_item(row)
        finally:
            conn.close()

    def list_items(self, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute("SELECT * FROM items WHERE status=? ORDER BY id DESC", (status,)).fetchall()
            else:
                rows = conn.execute("SELECT * FROM items ORDER BY id DESC").fetchall()
            return [self._row_to_item(row) for row in rows]
        finally:
            conn.close()

    def add_source(self, item_id, source_type, external_id, payload, observed_at, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            try:
                conn.execute(
                    "INSERT INTO sources(item_id,source_type,external_id,payload,observed_at,created_at) VALUES(?,?,?,?,?,?)",
                    (item_id, source_type, external_id, canonical_json(payload), observed_at, now_iso()),
                )
            except sqlite3.IntegrityError:
                raise ConflictError("duplicate_source", "同一来源记录已经提交")
            source_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            self.append_audit(
                conn,
                item_id,
                "source_recorded",
                actor,
                role,
                {"source_id": source_id, "source_type": source_type, "external_id": external_id},
            )
            conn.execute("COMMIT")
            return {"id": source_id, "item_id": item_id, "source_type": source_type, "external_id": external_id, "payload": payload, "observed_at": observed_at}
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_sources(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM sources WHERE item_id=? ORDER BY id DESC", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def apply_action(self, item_id, action, actor, role, new_status, new_payload, event_payload, expected_version=None):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            if expected_version is not None and int(expected_version) != int(row["version"]):
                raise ConflictError("version_conflict", "记录已被其他操作更新，请重新读取")
            version = int(row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (item_id, action, actor, role, canonical_json(event_payload), now_iso()),
            )
            self.append_audit(conn, item_id, action, actor, role, event_payload)
            conn.execute(
                "INSERT INTO version_snapshots(item_id,version,status,payload,created_at) VALUES(?,?,?,?,?)",
                (item_id, version, new_status, canonical_json(new_payload), now_iso()),
            )
            conn.execute("COMMIT")
            return self.get_item(item_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def get_snapshot(self, conn, item_id, version):
        row = conn.execute(
            "SELECT * FROM version_snapshots WHERE item_id=? AND version=?",
            (item_id, int(version)),
        ).fetchone()
        if row is None:
            return None
        return {"version": int(row["version"]), "status": row["status"], "payload": json.loads(row["payload"])}

    def enqueue_offline_batch(self, batch_id, item_id, kind, base_version, actor, role, original_payload):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item = conn.execute("SELECT id FROM items WHERE id=?", (item_id,)).fetchone()
            if item is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            existing = conn.execute(
                "SELECT * FROM offline_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if existing is not None:
                conn.execute("COMMIT")
                return self._offline_row_to_dict(existing)
            row = conn.execute(
                "SELECT COALESCE(MAX(seq), 0) + 1 AS next_seq FROM offline_batches WHERE item_id=?",
                (item_id,),
            ).fetchone()
            seq = int(row["next_seq"])
            timestamp = now_iso()
            conn.execute(
                "INSERT INTO offline_batches(batch_id,item_id,kind,base_version,seq,actor,role,"
                "original_payload,status,attempts,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    batch_id,
                    item_id,
                    kind,
                    int(base_version),
                    seq,
                    actor,
                    role,
                    canonical_json(original_payload),
                    "pending",
                    0,
                    timestamp,
                    timestamp,
                ),
            )
            self.append_audit(
                conn,
                item_id,
                "offline_registered",
                actor,
                role,
                {
                    "batch_id": batch_id,
                    "kind": kind,
                    "base_version": int(base_version),
                    "seq": seq,
                    "original_payload": original_payload,
                },
            )
            conn.execute("COMMIT")
            return self.get_offline_batch(batch_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _offline_row_to_dict(self, row):
        value = dict(row)
        value["original_payload"] = json.loads(value["original_payload"])
        if value.get("result_payload"):
            value["result_payload"] = json.loads(value["result_payload"])
        value["base_version"] = int(value["base_version"])
        value["seq"] = int(value["seq"])
        value["attempts"] = int(value["attempts"])
        if value.get("applied_version") is not None:
            value["applied_version"] = int(value["applied_version"])
        return value

    def get_offline_batch(self, batch_id):
        conn = self.connect()
        try:
            row = conn.execute("SELECT * FROM offline_batches WHERE batch_id=?", (batch_id,)).fetchone()
            if row is None:
                raise NotFoundError("offline_batch_not_found", "离线补录批次不存在")
            return self._offline_row_to_dict(row)
        finally:
            conn.close()

    def list_offline_batches(self, item_id=None, status=None):
        conn = self.connect()
        try:
            sql = "SELECT * FROM offline_batches"
            clauses = []
            params = []
            if item_id is not None:
                clauses.append("item_id=?")
                params.append(item_id)
            if status:
                clauses.append("status=?")
                params.append(status)
            if clauses:
                sql += " WHERE " + " AND ".join(clauses)
            sql += " ORDER BY item_id, seq"
            return [self._offline_row_to_dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()

    def pending_offline_batches(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute(
                "SELECT * FROM offline_batches WHERE item_id=? AND status='pending' ORDER BY seq",
                (item_id,),
            ).fetchall()
            return [self._offline_row_to_dict(row) for row in rows]
        finally:
            conn.close()

    def replay_offline_batch(self, batch_id, plan, is_retryable):
        """在单个事务里重放一个离线补录批次。

        plan(item_row, base_snapshot, current_payload, batch) 在事务内执行规则校验并
        返回 (new_status, new_payload, event_payload, result_summary)；
        is_retryable(exc) 返回真时批次保留为 pending 待重试，否则标记为 rejected。
        """
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            batch_row = conn.execute(
                "SELECT * FROM offline_batches WHERE batch_id=?", (batch_id,)
            ).fetchone()
            if batch_row is None:
                raise NotFoundError("offline_batch_not_found", "离线补录批次不存在")
            batch = self._offline_row_to_dict(batch_row)
            item_id = int(batch["item_id"])
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            attempts = int(batch_row["attempts"]) + 1
            if item_row is None:
                self._mark_batch(conn, batch_row, "rejected", attempts, "item_not_found")
                conn.execute("COMMIT")
                return self.get_offline_batch(batch_id)

            base_snapshot = self.get_snapshot(conn, item_id, batch["base_version"])
            if base_snapshot is None:
                self._mark_batch(
                    conn,
                    batch_row,
                    "rejected",
                    attempts,
                    "base_version_missing: 原始版本 %s 的快照不存在" % batch["base_version"],
                )
                conn.execute("COMMIT")
                return self.get_offline_batch(batch_id)

            current_payload = json.loads(item_row["payload"])
            try:
                new_status, new_payload, event_payload, event_detail = plan(
                    dict(item_row), base_snapshot, current_payload, batch
                )
            except Exception as exc:
                new_status = None
                error_code = getattr(exc, "code", "invalid_record")
                if is_retryable(exc):
                    mark_status = "pending"
                else:
                    mark_status = "rejected"
                self._mark_batch(conn, batch_row, mark_status, attempts, "%s: %s" % (error_code, exc))
                self.append_audit(
                    conn,
                    item_id,
                    "offline_rejected",
                    batch["actor"],
                    batch["role"],
                    {
                        "batch_id": batch_id,
                        "kind": batch["kind"],
                        "base_version": batch["base_version"],
                        "reason": error_code,
                        "message": str(exc),
                        "will_retry": mark_status == "pending",
                        "original_payload": batch["original_payload"],
                    },
                )
                conn.execute("COMMIT")
                return self.get_offline_batch(batch_id)

            version = int(item_row["version"]) + 1
            conn.execute(
                "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
                (new_status, version, canonical_json(new_payload), now_iso(), item_id),
            )
            conn.execute(
                "INSERT INTO version_snapshots(item_id,version,status,payload,created_at) VALUES(?,?,?,?,?)",
                (item_id, version, new_status, canonical_json(new_payload), now_iso()),
            )
            conn.execute(
                "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
                (
                    item_id,
                    "offline_%s" % batch["kind"],
                    batch["actor"],
                    batch["role"],
                    canonical_json(event_payload),
                    now_iso(),
                ),
            )
            self.append_audit(
                conn,
                item_id,
                "offline_%s_merged" % batch["kind"],
                batch["actor"],
                batch["role"],
                event_payload,
            )
            applied_at = now_iso()
            conn.execute(
                "UPDATE offline_batches SET status='applied',attempts=?,last_error=NULL,"
                "applied_at=?,applied_version=?,result_payload=?,updated_at=? WHERE batch_id=?",
                (
                    attempts,
                    applied_at,
                    version,
                    canonical_json(event_detail),
                    applied_at,
                    batch_id,
                ),
            )
            conn.execute("COMMIT")
            return self.get_offline_batch(batch_id)
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def _mark_batch(self, conn, batch_row, status, attempts, error):
        conn.execute(
            "UPDATE offline_batches SET status=?,attempts=?,last_error=?,updated_at=? WHERE batch_id=?",
            (status, attempts, error, now_iso(), batch_row["batch_id"]),
        )

    def audit_trail(self, item_id):
        conn = self.connect()
        try:
            rows = conn.execute("SELECT * FROM audit_events WHERE item_id=? ORDER BY id", (item_id,)).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"])
                result.append(value)
            return result
        finally:
            conn.close()

    def state_summary(self):
        conn = self.connect()
        try:
            counts = {}
            for row in conn.execute("SELECT status, COUNT(*) AS total FROM items GROUP BY status").fetchall():
                counts[row["status"]] = row["total"]
            return {"counts": counts, "items": self.list_items()}
        finally:
            conn.close()
