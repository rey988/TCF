from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class QueueItem:
    id: int
    payload: dict[str, Any]
    attempts: int


class StateDB:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._conn = sqlite3.connect(str(db_path))
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA synchronous=NORMAL;")
        self._init_schema()

    def _init_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS kv_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS outbound_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                payload_size INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                next_attempt_at TEXT NOT NULL,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_outbound_status_next_attempt
                ON outbound_events(status, next_attempt_at, id);

            CREATE TABLE IF NOT EXISTS file_offsets (
                source_fingerprint TEXT PRIMARY KEY,
                file_path TEXT NOT NULL,
                inode_key TEXT NOT NULL,
                offset INTEGER NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    def set_state(self, key: str, value: str, now_iso: str) -> None:
        self._conn.execute(
            """
            INSERT INTO kv_state (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, value, now_iso),
        )
        self._conn.commit()

    def get_state(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM kv_state WHERE key = ?", (key,)).fetchone()
        if not row:
            return None
        return str(row["value"])

    def set_state_json(self, key: str, value: dict[str, Any], now_iso: str) -> None:
        self.set_state(key, json.dumps(value, separators=(",", ":")), now_iso)

    def get_state_json(self, key: str) -> dict[str, Any] | None:
        raw = self.get_state(key)
        if raw is None:
            return None
        return json.loads(raw)

    def enqueue_event(self, event_type: str, payload: dict[str, Any], now_iso: str) -> int:
        payload_json = json.dumps(payload, separators=(",", ":"))
        payload_size = len(payload_json.encode("utf-8"))
        cursor = self._conn.execute(
            """
            INSERT INTO outbound_events (
                event_type, payload_json, payload_size, status, attempts, next_attempt_at, created_at, updated_at
            ) VALUES (?, ?, ?, 'pending', 0, ?, ?, ?)
            """,
            (event_type, payload_json, payload_size, now_iso, now_iso, now_iso),
        )
        self._conn.commit()
        return int(cursor.lastrowid)

    def fetch_due_events(self, event_type: str, now_iso: str, limit: int, max_bytes: int) -> list[QueueItem]:
        rows = self._conn.execute(
            """
            SELECT id, payload_json, payload_size, attempts
            FROM outbound_events
            WHERE event_type = ?
              AND status = 'pending'
              AND next_attempt_at <= ?
            ORDER BY id ASC
            LIMIT ?
            """,
            (event_type, now_iso, limit),
        ).fetchall()

        items: list[QueueItem] = []
        used_bytes = 0

        for row in rows:
            size = int(row["payload_size"])
            if items and used_bytes + size > max_bytes:
                break
            used_bytes += size
            items.append(
                QueueItem(
                    id=int(row["id"]),
                    payload=json.loads(str(row["payload_json"])),
                    attempts=int(row["attempts"]),
                )
            )

        return items

    def mark_sent(self, ids: list[int]) -> None:
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        self._conn.execute(f"DELETE FROM outbound_events WHERE id IN ({placeholders})", ids)
        self._conn.commit()

    def mark_retry(self, ids: list[int], next_attempt_at: str, error: str, now_iso: str) -> None:
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        params = [next_attempt_at, error[:1000], now_iso, *ids]
        self._conn.execute(
            f"""
            UPDATE outbound_events
            SET attempts = attempts + 1,
                next_attempt_at = ?,
                last_error = ?,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            params,
        )
        self._conn.commit()

    def move_to_dead_letter(self, ids: list[int], error: str, now_iso: str) -> None:
        if not ids:
            return
        placeholders = ",".join(["?"] * len(ids))
        params = [error[:1000], now_iso, *ids]
        self._conn.execute(
            f"""
            UPDATE outbound_events
            SET status = 'dead_letter',
                last_error = ?,
                updated_at = ?
            WHERE id IN ({placeholders})
            """,
            params,
        )
        self._conn.commit()

    def queue_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            """
            SELECT status, COUNT(*) AS c
            FROM outbound_events
            GROUP BY status
            """
        ).fetchall()
        counts = {"pending": 0, "dead_letter": 0}
        for row in rows:
            counts[str(row["status"])] = int(row["c"])
        counts["total"] = sum(counts.values())
        return counts

    def queue_bytes(self) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(SUM(payload_size), 0) AS b FROM outbound_events WHERE status = 'pending'"
        ).fetchone()
        return int(row["b"]) if row else 0

    def set_offset(self, source_fingerprint: str, file_path: str, inode_key: str, offset: int, now_iso: str) -> None:
        self._conn.execute(
            """
            INSERT INTO file_offsets (source_fingerprint, file_path, inode_key, offset, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(source_fingerprint)
            DO UPDATE SET file_path=excluded.file_path,
                          inode_key=excluded.inode_key,
                          offset=excluded.offset,
                          updated_at=excluded.updated_at
            """,
            (source_fingerprint, file_path, inode_key, offset, now_iso),
        )
        self._conn.commit()

    def get_offset(self, source_fingerprint: str) -> tuple[str, int] | None:
        row = self._conn.execute(
            "SELECT inode_key, offset FROM file_offsets WHERE source_fingerprint = ?",
            (source_fingerprint,),
        ).fetchone()
        if not row:
            return None
        return str(row["inode_key"]), int(row["offset"])

    def is_over_capacity(self, max_bytes: int) -> bool:
        return self.queue_bytes() >= max_bytes
