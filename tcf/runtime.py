from __future__ import annotations

import ipaddress
import json
import logging
import random
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import TCFConfig
from .state_db import QueueItem, StateDB
from .tc_client import TCClient, TCClientError


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_iso(dt: datetime | None = None) -> str:
    value = dt or utc_now()
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@dataclass
class RuntimeSnapshot:
    current_version_md5: str | None
    tasks: list[dict[str, Any]]


class TCFService:
    MAX_READ_BYTES_PER_TASK = 262144

    def __init__(self, config: TCFConfig, db: StateDB, client: TCClient, feeder_identifier: str):
        self.config = config
        self.db = db
        self.client = client
        self.feeder_identifier = feeder_identifier
        self.service_id = config.tc.service_id
        loaded = self._load_snapshot()
        self.current_version_md5 = loaded.current_version_md5
        self.tasks = loaded.tasks

    def bootstrap(self) -> None:
        self.register_feeder()
        self.sync_tasks(force=True)
        self.heartbeat_once()

    def run_once(self) -> None:
        self.sync_tasks(force=False)
        self.collect_once()
        self.flush_once()
        self.heartbeat_once()

    def run_forever(self, stop_requested) -> None:
        self.bootstrap()
        self._set_state("running_since", utc_iso())

        next_sync = 0.0
        next_collect = 0.0
        next_flush = 0.0
        next_heartbeat = 0.0

        while not stop_requested():
            now_monotonic = time.monotonic()

            if now_monotonic >= next_sync:
                self._safe_step(self.sync_tasks, "task_sync")
                next_sync = now_monotonic + self.config.runtime.task_sync_interval_seconds

            if now_monotonic >= next_collect:
                self._safe_step(self.collect_once, "collect")
                next_collect = now_monotonic + self.config.runtime.collect_interval_seconds

            if now_monotonic >= next_flush:
                self._safe_step(self.flush_once, "flush")
                next_flush = now_monotonic + self.config.runtime.flush_interval_seconds

            if now_monotonic >= next_heartbeat:
                self._safe_step(self.heartbeat_once, "heartbeat")
                next_heartbeat = now_monotonic + self.config.runtime.heartbeat_interval_seconds

            time.sleep(0.25)

    def register_feeder(self) -> None:
        payload_metadata = dict(self.config.agent.metadata)
        payload_metadata["agent_runtime"] = "python-stdlib"

        response = self.client.register_feeder(
            identifier=self.feeder_identifier,
            service_code=self.config.tc.service_code,
            service_id=self.service_id,
            host_name=self.config.agent.host_name,
            ip_address=self.config.agent.ip_address,
            metadata=payload_metadata,
        )

        if response.get("service_id"):
            self.service_id = int(response["service_id"])

        self._set_state("feeder_identifier", self.feeder_identifier)
        self._set_state("service_id", str(self.service_id or ""))
        self._set_state("last_register_at", utc_iso())

    def sync_tasks(self, force: bool = False) -> None:
        version_info = self.client.get_feeder_task_version(
            identifier=self.feeder_identifier,
            current_version_md5=self.current_version_md5,
        )

        service_id = int(version_info.get("service_id") or self.service_id or 0)
        if service_id <= 0:
            raise RuntimeError("Missing service_id from feeder task version response")

        self.service_id = service_id
        latest_version = version_info.get("latest_version_md5")
        has_update = bool(version_info.get("has_update"))

        if force or has_update:
            if latest_version:
                snapshot_data = self.client.get_snapshot_by_version(service_id, str(latest_version))
            else:
                snapshot_data = self.client.get_latest_snapshot(service_id)

            self.current_version_md5 = str(snapshot_data.get("version_md5") or latest_version or "") or None
            self.tasks = [
                task for task in list(snapshot_data.get("snapshot") or []) if bool(task.get("is_active", True))
            ]
            self._persist_snapshot(snapshot_data)
            self._set_state("current_task_version_md5", self.current_version_md5 or "")

        watch_rows = []
        for task in self.tasks:
            path = self._task_path(task)
            if path:
                watch_rows.append({"task_id": task.get("id"), "task_type": task.get("task_type"), "path": path})

        self.db.set_state_json("active_watch_paths", {"items": watch_rows}, utc_iso())
        self._set_state("last_task_sync_at", utc_iso())

    def collect_once(self) -> None:
        for task in self.tasks:
            task_type = str(task.get("task_type") or "")
            path = self._task_path(task)
            if not path:
                continue

            if task_type == "log_collecting":
                self._collect_log_task(task, Path(path))
            elif task_type == "audit_collecting":
                self._collect_audit_task(task, Path(path))

    def flush_once(self) -> None:
        self._flush_event_type("log")
        self._flush_event_type("audit")

    def heartbeat_once(self) -> None:
        counts = self.db.queue_counts()
        metadata = dict(self.config.agent.metadata)
        metadata.update(
            {
                "polling_interval_seconds": self.config.runtime.task_sync_interval_seconds,
                "queue_pending": counts.get("pending", 0),
                "queue_dead_letter": counts.get("dead_letter", 0),
                "queue_bytes": self.db.queue_bytes(),
                "current_task_version_md5": self.current_version_md5,
            }
        )

        self.client.heartbeat(self.feeder_identifier, metadata)
        self._set_state("last_heartbeat_at", utc_iso())

    def _safe_step(self, action, action_name: str) -> None:
        try:
            action()
        except Exception as exc:  # noqa: BLE001
            logging.exception("Step failed: %s", action_name)
            self._set_state("last_error_summary", f"{action_name}: {exc}")

    def _task_path(self, task: dict[str, Any]) -> str | None:
        config = task.get("config") or {}
        if not isinstance(config, dict):
            return None
        path = config.get("path") or config.get("file_path") or config.get("log_path")
        if not path:
            return None
        return str(path)

    def _source_fingerprint(self, task_id: int, file_path: Path) -> str:
        base = f"{self.feeder_identifier}:{task_id}:{str(file_path.resolve())}"
        if len(base) <= 255:
            return base
        compact = uuid.uuid5(uuid.NAMESPACE_URL, base).hex
        return f"{self.feeder_identifier}:{task_id}:{compact}"[:255]

    def _read_new_lines(self, source_fingerprint: str, file_path: Path) -> list[str]:
        if not file_path.exists() or not file_path.is_file():
            return []

        stat = file_path.stat()
        inode_key = f"{stat.st_dev}:{stat.st_ino}"
        saved = self.db.get_offset(source_fingerprint)

        offset = 0
        if saved:
            saved_inode, saved_offset = saved
            if saved_inode == inode_key:
                offset = max(0, saved_offset)

        if stat.st_size < offset:
            offset = 0

        with file_path.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(offset)
            data = handle.read(self.MAX_READ_BYTES_PER_TASK)
            new_offset = handle.tell()

        self.db.set_offset(source_fingerprint, str(file_path), inode_key, new_offset, utc_iso())

        if not data:
            return []

        return [line for line in data.splitlines() if line.strip()]

    def _collect_log_task(self, task: dict[str, Any], file_path: Path) -> None:
        task_id = int(task.get("id") or 0)
        if task_id <= 0:
            return

        source_fingerprint = self._source_fingerprint(task_id, file_path)
        lines = self._read_new_lines(source_fingerprint, file_path)

        for line in lines:
            if not self._queue_available():
                break
            payload = {
                "event_id": str(uuid.uuid4()),
                "schema_version": "log_collecting.v1",
                "task_id": task_id,
                "source_fingerprint": source_fingerprint,
                "log_level": self._detect_log_level(line),
                "log_category": str(task.get("log_category") or "plain_log"),
                "message": line[:10000],
                "context": {
                    "file_path": str(file_path),
                },
                "occurred_at": utc_iso(),
            }
            self.db.enqueue_event("log", payload, utc_iso())

    def _collect_audit_task(self, task: dict[str, Any], file_path: Path) -> None:
        task_id = int(task.get("id") or 0)
        if task_id <= 0:
            return

        source_fingerprint = self._source_fingerprint(task_id, file_path)
        lines = self._read_new_lines(source_fingerprint, file_path)

        for line in lines:
            if not self._queue_available():
                break

            record = self._parse_json_line(line)
            user_snapshot = record.get("user_snapshot")
            if not isinstance(user_snapshot, dict):
                user_snapshot = {
                    "actor": str(record.get("actor") or "unknown")
                }

            payload: dict[str, Any] = {
                "event_id": str(uuid.uuid4()),
                "schema_version": "audit_collecting.v1",
                "task_id": task_id,
                "user_snapshot": user_snapshot,
                "action_label": str(record.get("action_label") or record.get("action") or "UNKNOWN_ACTION")[:255],
                "occurred_at": self._coerce_iso(record.get("occurred_at")),
            }

            action_code = record.get("action_code")
            if isinstance(action_code, str) and action_code:
                payload["action_code"] = action_code[:255]

            target_snapshot = record.get("target_snapshot")
            if isinstance(target_snapshot, dict):
                payload["target_snapshot"] = target_snapshot

            before_state = record.get("before_state")
            if isinstance(before_state, dict):
                payload["before_state"] = before_state

            after_state = record.get("after_state")
            if isinstance(after_state, dict):
                payload["after_state"] = after_state

            ip_raw = record.get("ip_address")
            if isinstance(ip_raw, str) and self._is_valid_ip(ip_raw):
                payload["ip_address"] = ip_raw

            user_agent = record.get("user_agent")
            if isinstance(user_agent, str) and user_agent:
                payload["user_agent"] = user_agent[:2000]

            self.db.enqueue_event("audit", payload, utc_iso())

    def _queue_available(self) -> bool:
        if self.db.is_over_capacity(self.config.runtime.queue_max_bytes):
            self._set_state("last_error_summary", "Queue storage limit reached, dropping new events")
            return False
        return True

    def _flush_event_type(self, event_type: str) -> None:
        due_items = self.db.fetch_due_events(
            event_type=event_type,
            now_iso=utc_iso(),
            limit=self.config.runtime.max_batch_events,
            max_bytes=self.config.runtime.max_batch_bytes,
        )

        if not due_items:
            return

        batch_payload = {
            "schema_version": "log_collecting.batch.v1" if event_type == "log" else "audit_collecting.batch.v1",
            "batch_uuid": str(uuid.uuid4()),
            "feeder_identifier": self.feeder_identifier,
            "events": [item.payload for item in due_items],
        }

        ids = [item.id for item in due_items]

        try:
            if event_type == "log":
                self.client.ingest_logs(batch_payload)
            else:
                self.client.ingest_audits(batch_payload)
            self.db.mark_sent(ids)
            self._set_state("last_ingest_success_at", utc_iso())
        except TCClientError as exc:
            retry_ids: list[int] = []
            dead_ids: list[int] = []

            for item in due_items:
                if item.attempts + 1 >= self.config.runtime.max_retries:
                    dead_ids.append(item.id)
                else:
                    retry_ids.append(item.id)

            if retry_ids:
                backoff_seconds = self._backoff_seconds(max(item.attempts for item in due_items) + 1)
                retry_at = utc_iso(utc_now() + timedelta(seconds=backoff_seconds))
                self.db.mark_retry(retry_ids, retry_at, str(exc), utc_iso())

            if dead_ids:
                self.db.move_to_dead_letter(dead_ids, str(exc), utc_iso())

            self._set_state("last_error_summary", f"flush_{event_type}: {exc}")

    def _backoff_seconds(self, attempt: int) -> int:
        base = self.config.runtime.retry_base_seconds
        cap = self.config.runtime.retry_max_seconds
        jitter = self.config.runtime.retry_jitter_seconds
        delay = min(base * (2 ** max(0, attempt - 1)), cap)
        if jitter > 0:
            delay += random.randint(0, jitter)
        return min(delay, cap)

    def _persist_snapshot(self, snapshot_data: dict[str, Any]) -> None:
        payload = {
            "version_md5": snapshot_data.get("version_md5"),
            "published_at": snapshot_data.get("published_at"),
            "snapshot": snapshot_data.get("snapshot") or [],
        }
        self.config.paths.snapshot_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _load_snapshot(self) -> RuntimeSnapshot:
        if not self.config.paths.snapshot_path.exists():
            return RuntimeSnapshot(current_version_md5=None, tasks=[])

        raw = json.loads(self.config.paths.snapshot_path.read_text(encoding="utf-8"))
        version = raw.get("version_md5")
        tasks = list(raw.get("snapshot") or [])
        return RuntimeSnapshot(
            current_version_md5=str(version) if version else None,
            tasks=tasks,
        )

    def _parse_json_line(self, line: str) -> dict[str, Any]:
        try:
            parsed = json.loads(line)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
        return {"raw_line": line}

    def _detect_log_level(self, message: str) -> str:
        text = message.upper()
        if "EMERGENCY" in text:
            return "EMERGENCY"
        if "ALERT" in text:
            return "ALERT"
        if "CRITICAL" in text:
            return "CRITICAL"
        if "ERROR" in text:
            return "ERROR"
        if "WARNING" in text or "WARN" in text:
            return "WARNING"
        if "NOTICE" in text:
            return "NOTICE"
        if "DEBUG" in text:
            return "DEBUG"
        return "INFO"

    def _is_valid_ip(self, value: str) -> bool:
        try:
            ipaddress.ip_address(value)
            return True
        except ValueError:
            return False

    def _coerce_iso(self, value: Any) -> str:
        if isinstance(value, str) and value:
            return value
        return utc_iso()

    def _set_state(self, key: str, value: str) -> None:
        self.db.set_state(key, value, utc_iso())
