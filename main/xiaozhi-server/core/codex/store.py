"""SQLite persistence for the Codex voice bridge.

The cloud server and the Mac bridge communicate through leased jobs.  SQLite is
enough for a single-user deployment and gives us durable delivery without
introducing another service.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any

SESSION_DEFAULTS = {
    "active": 0,
    "stage": "inactive",
    "bridge_id": None,
    "selected_thread_id": None,
    "selected_thread_title": None,
    "selected_thread_host_id": None,
    "recent_threads": [],
    "task_status": "unknown",
    "task_progress": None,
    "current_turn_id": None,
    "pending_request": None,
    "pending_send": None,
    "announcements_enabled": 0,
    "announcement_interval": 300,
    "next_announcement_at": None,
    "last_announced_progress_revision": None,
    "announcement_prompted": 0,
    "expires_at": None,
}

MAX_TASK_MEMORY_MESSAGES = 20
MAX_TASK_MEMORY_CHARS = 30_000


class CodexStore:
    """Small transactional repository around the bridge database."""

    def __init__(self, database_path: str | Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 30000")
        return connection

    @staticmethod
    def _loads(value: str | None, default: Any = None) -> Any:
        if not value:
            return default
        try:
            return json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return default

    @staticmethod
    def _job(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["payload"] = CodexStore._loads(item.pop("payload_json", None), {})
        item["response"] = CodexStore._loads(item.pop("response_json", None))
        return item

    @staticmethod
    def _session(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["recent_threads"] = CodexStore._loads(
            item.pop("recent_threads_json", None), []
        )
        item["pending_request"] = CodexStore._loads(
            item.pop("pending_request_json", None)
        )
        item["pending_send"] = CodexStore._loads(item.pop("pending_send_json", None))
        item["task_progress"] = CodexStore._loads(item.pop("task_progress_json", None))
        item["active"] = bool(item["active"])
        item["announcements_enabled"] = bool(item["announcements_enabled"])
        item["announcement_prompted"] = bool(item["announcement_prompted"])
        return item

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS bridges (
                    bridge_id TEXT PRIMARY KEY,
                    hostname TEXT NOT NULL,
                    version TEXT,
                    capabilities_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    device_id TEXT PRIMARY KEY,
                    active INTEGER NOT NULL DEFAULT 0,
                    stage TEXT NOT NULL DEFAULT 'inactive',
                    bridge_id TEXT,
                    selected_thread_id TEXT,
                    selected_thread_title TEXT,
                    selected_thread_host_id TEXT,
                    recent_threads_json TEXT NOT NULL DEFAULT '[]',
                    task_status TEXT NOT NULL DEFAULT 'unknown',
                    task_progress_json TEXT,
                    current_turn_id TEXT,
                    pending_request_json TEXT,
                    pending_send_json TEXT,
                    announcements_enabled INTEGER NOT NULL DEFAULT 0,
                    announcement_interval INTEGER NOT NULL DEFAULT 300,
                    next_announcement_at REAL,
                    last_announced_progress_revision TEXT,
                    announcement_prompted INTEGER NOT NULL DEFAULT 0,
                    expires_at REAL,
                    updated_at REAL NOT NULL
                );

                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    bridge_id TEXT,
                    kind TEXT NOT NULL,
                    payload_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    available_at REAL NOT NULL,
                    leased_by TEXT,
                    lease_until REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    response_json TEXT,
                    error TEXT,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_codex_jobs_ready
                    ON jobs(status, available_at, created_at);
                CREATE INDEX IF NOT EXISTS idx_codex_jobs_thread
                    ON jobs(device_id, status, kind);

                CREATE TABLE IF NOT EXISTS notifications (
                    notification_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    available_at REAL NOT NULL,
                    expires_at REAL NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT,
                    created_at REAL NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_codex_notifications_due
                    ON notifications(status, available_at, created_at);

                CREATE TABLE IF NOT EXISTS codex_task_memories (
                    thread_id TEXT PRIMARY KEY,
                    recent_messages_json TEXT NOT NULL DEFAULT '[]',
                    updated_at REAL NOT NULL
                );
                """
            )
            session_columns = {
                row["name"]
                for row in connection.execute("PRAGMA table_info(sessions)").fetchall()
            }
            if "selected_thread_host_id" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN selected_thread_host_id TEXT"
                )
            if "task_progress_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN task_progress_json TEXT"
                )
            if "last_announced_progress_revision" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN last_announced_progress_revision TEXT"
                )
            if "pending_send_json" not in session_columns:
                connection.execute(
                    "ALTER TABLE sessions ADD COLUMN pending_send_json TEXT"
                )

    def heartbeat(
        self,
        bridge_id: str,
        hostname: str,
        version: str | None,
        capabilities: dict[str, Any] | None,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO bridges(
                    bridge_id, hostname, version, capabilities_json, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(bridge_id) DO UPDATE SET
                    hostname=excluded.hostname,
                    version=excluded.version,
                    capabilities_json=excluded.capabilities_json,
                    last_seen_at=excluded.last_seen_at
                """,
                (
                    bridge_id,
                    hostname,
                    version,
                    json.dumps(capabilities or {}, ensure_ascii=False),
                    now,
                ),
            )

    def latest_bridge(self, online_after: float) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM bridges
                WHERE last_seen_at >= ?
                ORDER BY last_seen_at DESC LIMIT 1
                """,
                (online_after,),
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["capabilities"] = self._loads(result.pop("capabilities_json", None), {})
        return result

    def get_session(self, device_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM sessions WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._session(row) or {"device_id": device_id, **SESSION_DEFAULTS}

    def patch_session(self, device_id: str, **changes: Any) -> dict[str, Any]:
        allowed = set(SESSION_DEFAULTS)
        unknown = set(changes) - allowed
        if unknown:
            raise ValueError(f"未知会话字段: {', '.join(sorted(unknown))}")

        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sessions(device_id, updated_at)
                VALUES (?, ?)
                ON CONFLICT(device_id) DO NOTHING
                """,
                (device_id, now),
            )
            if changes:
                encoded = {}
                for key, value in changes.items():
                    column = key
                    if key in {
                        "recent_threads",
                        "pending_request",
                        "pending_send",
                        "task_progress",
                    }:
                        column = f"{key}_json"
                        value = (
                            None
                            if value is None
                            else json.dumps(value, ensure_ascii=False)
                        )
                    elif key in {
                        "active",
                        "announcements_enabled",
                        "announcement_prompted",
                    }:
                        value = int(bool(value))
                    encoded[column] = value
                encoded["updated_at"] = now
                assignments = ", ".join(f"{key} = ?" for key in encoded)
                connection.execute(
                    f"UPDATE sessions SET {assignments} WHERE device_id = ?",
                    (*encoded.values(), device_id),
                )
            row = connection.execute(
                "SELECT * FROM sessions WHERE device_id = ?", (device_id,)
            ).fetchone()
        return self._session(row)

    def sessions_for_thread(self, thread_id: str) -> list[dict[str, Any]]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE active = 1 AND selected_thread_id = ?
                """,
                (thread_id,),
            ).fetchall()
        return [self._session(row) for row in rows]

    def remember_task_message(
        self, thread_id: str, message: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Keep a small, deduplicated assistant-message history for one task."""
        message_id = str(message.get("message_id") or "").strip()[:180]
        phase = str(message.get("phase") or "").strip()
        text = str(message.get("text") or "").strip()[:4000]
        if (
            not thread_id
            or not message_id
            or phase
            not in {
                "commentary",
                "final_answer",
            }
            or not text
        ):
            return self.get_task_memory(thread_id)

        try:
            updated_at = float(message.get("updated_at") or time.time())
        except (TypeError, ValueError):
            updated_at = time.time()
        item = {
            "message_id": message_id,
            "phase": phase,
            "text": text,
            "updated_at": updated_at,
        }

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT recent_messages_json FROM codex_task_memories WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
            messages = self._loads(row["recent_messages_json"] if row else None, [])
            if not isinstance(messages, list):
                messages = []
            messages = [
                current
                for current in messages
                if isinstance(current, dict) and current.get("message_id") != message_id
            ]
            messages.append(item)
            messages = messages[-MAX_TASK_MEMORY_MESSAGES:]
            total_chars = sum(
                len(str(current.get("text") or "")) for current in messages
            )
            while messages and total_chars > MAX_TASK_MEMORY_CHARS:
                removed = messages.pop(0)
                total_chars -= len(str(removed.get("text") or ""))

            now = time.time()
            connection.execute(
                """
                INSERT INTO codex_task_memories(
                    thread_id, recent_messages_json, updated_at
                ) VALUES (?, ?, ?)
                ON CONFLICT(thread_id) DO UPDATE SET
                    recent_messages_json=excluded.recent_messages_json,
                    updated_at=excluded.updated_at
                """,
                (thread_id, json.dumps(messages, ensure_ascii=False), now),
            )
            connection.commit()
            return messages
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_task_memory(self, thread_id: str) -> list[dict[str, Any]]:
        if not thread_id:
            return []
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recent_messages_json FROM codex_task_memories WHERE thread_id=?",
                (thread_id,),
            ).fetchone()
        messages = self._loads(row["recent_messages_json"] if row else None, [])
        return messages if isinstance(messages, list) else []

    def create_job(
        self,
        device_id: str,
        kind: str,
        payload: dict[str, Any],
        bridge_id: str | None = None,
        blocked: bool = False,
        max_attempts: int = 3,
    ) -> str:
        job_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, device_id, bridge_id, kind, payload_json, status,
                    available_at, max_attempts, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    device_id,
                    bridge_id,
                    kind,
                    json.dumps(payload, ensure_ascii=False),
                    "blocked" if blocked else "queued",
                    now,
                    max_attempts,
                    now,
                    now,
                ),
            )
        return job_id

    def lease_job(self, bridge_id: str, lease_seconds: int) -> dict[str, Any] | None:
        now = time.time()
        lease_until = now + max(5, lease_seconds)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                """
                UPDATE jobs SET
                    status = CASE WHEN attempts >= max_attempts
                                  THEN 'failed' ELSE 'queued' END,
                    leased_by = NULL,
                    lease_until = NULL,
                    error = CASE WHEN attempts >= max_attempts
                                 THEN 'bridge lease expired' ELSE error END,
                    updated_at = ?
                WHERE status = 'leased' AND lease_until < ?
                """,
                (now, now),
            )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status = 'queued' AND available_at <= ?
                  AND (bridge_id IS NULL OR bridge_id = ?)
                ORDER BY created_at ASC LIMIT 1
                """,
                (now, bridge_id),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE jobs SET status='leased', leased_by=?, lease_until=?,
                    attempts=attempts + 1, updated_at=?
                WHERE job_id=?
                """,
                (bridge_id, lease_until, now, row["job_id"]),
            )
            leased = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (row["job_id"],)
            ).fetchone()
            connection.commit()
            return self._job(leased)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def complete_job(
        self,
        bridge_id: str,
        job_id: str,
        response: dict[str, Any] | None,
        error: str | None,
    ) -> dict[str, Any] | None:
        now = time.time()
        with self._connect() as connection:
            cursor = connection.execute(
                """
                UPDATE jobs SET status=?, response_json=?, error=?,
                    lease_until=NULL, updated_at=?
                WHERE job_id=? AND status='leased' AND leased_by=?
                """,
                (
                    "failed" if error else "completed",
                    (
                        None
                        if response is None
                        else json.dumps(response, ensure_ascii=False)
                    ),
                    error,
                    now,
                    job_id,
                    bridge_id,
                ),
            )
            if cursor.rowcount != 1:
                return None
            row = connection.execute(
                "SELECT * FROM jobs WHERE job_id = ?", (job_id,)
            ).fetchone()
        return self._job(row)

    def unblock_jobs_for_thread(self, thread_id: str) -> int:
        now = time.time()
        changed = 0
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT job_id, payload_json FROM jobs WHERE status='blocked'"
            ).fetchall()
            matching = [
                row["job_id"]
                for row in rows
                if self._loads(row["payload_json"], {}).get("thread_id") == thread_id
            ]
            if matching:
                placeholders = ",".join("?" for _ in matching)
                cursor = connection.execute(
                    f"""
                    UPDATE jobs SET status='queued', available_at=?, updated_at=?
                    WHERE job_id IN ({placeholders})
                    """,
                    (now, now, *matching),
                )
                changed = cursor.rowcount
        return changed

    def queue_notification(
        self,
        device_id: str,
        text: str,
        ttl_seconds: int = 3600,
        available_at: float | None = None,
    ) -> str:
        notification_id = uuid.uuid4().hex
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO notifications(
                    notification_id, device_id, text, available_at,
                    expires_at, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    notification_id,
                    device_id,
                    text,
                    available_at or now,
                    now + ttl_seconds,
                    now,
                ),
            )
        return notification_id

    def claim_notification(self) -> dict[str, Any] | None:
        now = time.time()
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM notifications WHERE expires_at < ?", (now,))
            row = connection.execute(
                """
                SELECT * FROM notifications
                WHERE status='pending' AND available_at <= ?
                ORDER BY created_at ASC LIMIT 1
                """,
                (now,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            connection.execute(
                """
                UPDATE notifications SET status='delivering', attempts=attempts+1
                WHERE notification_id=?
                """,
                (row["notification_id"],),
            )
            claimed = connection.execute(
                "SELECT * FROM notifications WHERE notification_id=?",
                (row["notification_id"],),
            ).fetchone()
            connection.commit()
            return dict(claimed)
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def finish_notification(
        self, notification_id: str, delivered: bool, error: str | None = None
    ) -> None:
        with self._connect() as connection:
            if delivered:
                connection.execute(
                    "DELETE FROM notifications WHERE notification_id=?",
                    (notification_id,),
                )
            else:
                connection.execute(
                    """
                    UPDATE notifications SET status='pending', available_at=?,
                        last_error=? WHERE notification_id=?
                    """,
                    (time.time() + 15, error, notification_id),
                )

    def due_periodic_sessions(self) -> list[dict[str, Any]]:
        now = time.time()
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM sessions
                WHERE active=1 AND announcements_enabled=1
                  AND selected_thread_id IS NOT NULL
                  AND next_announcement_at IS NOT NULL
                  AND next_announcement_at <= ?
                """,
                (now,),
            ).fetchall()
        return [self._session(row) for row in rows]

    def advance_periodic_announcement(self, device_id: str) -> None:
        session = self.get_session(device_id)
        interval = max(60, int(session.get("announcement_interval") or 300))
        self.patch_session(
            device_id,
            next_announcement_at=time.time() + interval,
            last_announced_progress_revision=(session.get("task_progress") or {}).get(
                "revision"
            ),
        )

    def cleanup(self, retention_hours: int = 72) -> None:
        """Remove terminal jobs and stale inactive sessions after their audit window."""
        now = time.time()
        cutoff = now - max(1, retention_hours) * 3600
        with self._connect() as connection:
            connection.execute(
                """
                DELETE FROM jobs
                WHERE status IN ('completed', 'failed') AND updated_at < ?
                """,
                (cutoff,),
            )
            connection.execute("DELETE FROM notifications WHERE expires_at < ?", (now,))
            connection.execute(
                """
                DELETE FROM sessions
                WHERE active=0 AND updated_at < ?
                """,
                (cutoff,),
            )
