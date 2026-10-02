import json
import sqlite3
from datetime import datetime, timezone

from . import rules
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
                CREATE TABLE IF NOT EXISTS offline_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL,
                    client_record_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    expected_version INTEGER,
                    status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    original_json TEXT NOT NULL,
                    merged_json TEXT,
                    version_before INTEGER,
                    version_after INTEGER,
                    recorded_at TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    applied_at TEXT,
                    UNIQUE(item_id, client_record_id),
                    FOREIGN KEY(item_id) REFERENCES items(id)
                );
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

    def _apply_one_offline(self, conn, item_row, rec, actor, role):
        item_id = item_row["id"]
        client_id = rec.get("client_record_id")
        action = rec.get("action")
        payload = rec.get("payload") or {}
        expected_version = rec.get("expected_version")
        recorded_at = rec.get("recorded_at") or now_iso()
        original = {
            "client_record_id": client_id,
            "action": action,
            "payload": payload,
            "expected_version": expected_version,
            "recorded_at": recorded_at,
        }
        existing = conn.execute(
            "SELECT * FROM offline_records WHERE item_id=? AND client_record_id=?",
            (item_id, client_id),
        ).fetchone()
        if existing is not None and existing["status"] == "applied":
            return {"bucket": "skipped", "client_record_id": client_id, "id": existing["id"]}

        item = self._row_to_item(item_row)
        version_before = int(item["version"])
        try:
            new_status, new_payload, merged = rules.merge_action(item, action, payload, actor, role)
        except DomainError as exc:
            attempts = (existing["attempts"] if existing else 0) + 1
            if existing is None:
                conn.execute(
                    "INSERT INTO offline_records(item_id,client_record_id,action,payload,expected_version,status,attempts,last_error,original_json,recorded_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (item_id, client_id, action, canonical_json(payload), expected_version, "pending", attempts, exc.code, canonical_json(original), recorded_at, now_iso()),
                )
                rec_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
            else:
                conn.execute(
                    "UPDATE offline_records SET status='pending', attempts=?, last_error=? WHERE id=?",
                    (attempts, exc.code, existing["id"]),
                )
                rec_id = existing["id"]
            self.append_audit(
                conn,
                item_id,
                "offline_record_rejected",
                actor,
                role,
                {"client_record_id": client_id, "action": action, "original": original, "error": exc.code, "message": str(exc)},
            )
            return {"bucket": "pending", "client_record_id": client_id, "id": rec_id, "error": exc.code}

        version_after = version_before + 1
        conn.execute(
            "UPDATE items SET status=?,version=?,payload=?,updated_at=? WHERE id=?",
            (new_status, version_after, canonical_json(new_payload), now_iso(), item_id),
        )
        conn.execute(
            "INSERT INTO actions(item_id,action,actor,role,payload,created_at) VALUES(?,?,?,?,?,?)",
            (item_id, action, actor, role, canonical_json(merged), now_iso()),
        )
        if existing is None:
            conn.execute(
                "INSERT INTO offline_records(item_id,client_record_id,action,payload,expected_version,status,attempts,last_error,original_json,merged_json,version_before,version_after,recorded_at,created_at,applied_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (item_id, client_id, action, canonical_json(payload), expected_version, "applied", 1, None, canonical_json(original), canonical_json(merged), version_before, version_after, recorded_at, now_iso(), now_iso()),
            )
            rec_id = conn.execute("SELECT last_insert_rowid() AS id").fetchone()["id"]
        else:
            conn.execute(
                "UPDATE offline_records SET status='applied', attempts=?, last_error=NULL, merged_json=?, version_before=?, version_after=?, applied_at=? WHERE id=?",
                (existing["attempts"] + 1, canonical_json(merged), version_before, version_after, now_iso(), existing["id"]),
            )
            rec_id = existing["id"]
        self.append_audit(
            conn,
            item_id,
            "offline_record_applied",
            actor,
            role,
            {
                "client_record_id": client_id,
                "action": action,
                "original": original,
                "merged": merged,
                "version_before": version_before,
                "version_after": version_after,
            },
        )
        return {"bucket": "applied", "client_record_id": client_id, "id": rec_id, "version_after": version_after}

    def submit_offline_records(self, item_id, records, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            summary = {"applied": [], "pending": [], "skipped": []}
            for rec in records:
                result = self._apply_one_offline(conn, item_row, rec, actor, role)
                summary[result["bucket"]].append(result)
                if result["bucket"] == "applied":
                    item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            conn.execute("COMMIT")
            return summary
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()

    def list_offline_records(self, item_id, status=None):
        conn = self.connect()
        try:
            if status:
                rows = conn.execute(
                    "SELECT * FROM offline_records WHERE item_id=? AND status=? ORDER BY id",
                    (item_id, status),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM offline_records WHERE item_id=? ORDER BY id",
                    (item_id,),
                ).fetchall()
            result = []
            for row in rows:
                value = dict(row)
                value["payload"] = json.loads(value["payload"]) if value["payload"] else None
                value["original"] = json.loads(value["original_json"])
                value["merged"] = json.loads(value["merged_json"]) if value["merged_json"] else None
                result.append(value)
            return result
        finally:
            conn.close()

    def replay_offline_records(self, item_id, actor, role):
        conn = self.connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            if item_row is None:
                raise NotFoundError("item_not_found", "业务实体不存在")
            rows = conn.execute(
                "SELECT * FROM offline_records WHERE item_id=? AND status='pending' ORDER BY id",
                (item_id,),
            ).fetchall()
            summary = {"applied": [], "pending": [], "skipped": []}
            for row in rows:
                rec = json.loads(row["original_json"])
                result = self._apply_one_offline(conn, item_row, rec, actor, role)
                summary[result["bucket"]].append(result)
                if result["bucket"] == "applied":
                    item_row = conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
            conn.execute("COMMIT")
            return summary
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
        finally:
            conn.close()
