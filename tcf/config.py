from __future__ import annotations

import json
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def detect_default_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return str(sock.getsockname()[0])
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


@dataclass
class PathsConfig:
    root_dir: Path
    state_dir: Path
    db_path: Path
    snapshot_path: Path
    pid_path: Path
    log_path: Path

    @classmethod
    def from_root(cls, root_dir: Path) -> "PathsConfig":
        state_dir = root_dir / "state"
        return cls(
            root_dir=root_dir,
            state_dir=state_dir,
            db_path=state_dir / "tcf_state.db",
            snapshot_path=state_dir / "tasks_snapshot.json",
            pid_path=state_dir / "tcf.pid",
            log_path=state_dir / "tcf.log",
        )


@dataclass
class TCConfig:
    base_url: str
    api_token: str
    service_code: str | None = None
    service_id: int | None = None


@dataclass
class RuntimeConfig:
    task_sync_interval_seconds: int = 30
    collect_interval_seconds: int = 2
    flush_interval_seconds: int = 5
    heartbeat_interval_seconds: int = 30
    request_timeout_seconds: int = 15
    max_batch_events: int = 200
    max_batch_bytes: int = 262144
    queue_max_bytes: int = 2147483648
    max_retries: int = 12
    retry_base_seconds: int = 2
    retry_max_seconds: int = 300
    retry_jitter_seconds: int = 3


@dataclass
class AgentConfig:
    feeder_identifier: str | None = None
    host_name: str = field(default_factory=socket.gethostname)
    ip_address: str = field(default_factory=detect_default_ip)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TCFConfig:
    tc: TCConfig
    agent: AgentConfig
    runtime: RuntimeConfig
    paths: PathsConfig

    @classmethod
    def load(cls, config_path: Path, require_tc_auth: bool = True) -> "TCFConfig":
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        root_dir = config_path.parent.resolve()

        tc_raw = raw.get("tc", {})
        agent_raw = raw.get("agent", {})
        runtime_raw = raw.get("runtime", {})

        tc_cfg = TCConfig(
            base_url=str(tc_raw.get("base_url", "")).rstrip("/"),
            api_token=str(tc_raw.get("api_token", "")),
            service_code=tc_raw.get("service_code"),
            service_id=tc_raw.get("service_id"),
        )
        agent_cfg = AgentConfig(
            feeder_identifier=agent_raw.get("feeder_identifier"),
            host_name=str(agent_raw.get("host_name") or socket.gethostname()),
            ip_address=str(agent_raw.get("ip_address") or detect_default_ip()),
            metadata=dict(agent_raw.get("metadata") or {}),
        )
        runtime_cfg = RuntimeConfig(
            task_sync_interval_seconds=int(runtime_raw.get("task_sync_interval_seconds", 30)),
            collect_interval_seconds=int(runtime_raw.get("collect_interval_seconds", 2)),
            flush_interval_seconds=int(runtime_raw.get("flush_interval_seconds", 5)),
            heartbeat_interval_seconds=int(runtime_raw.get("heartbeat_interval_seconds", 30)),
            request_timeout_seconds=int(runtime_raw.get("request_timeout_seconds", 15)),
            max_batch_events=int(runtime_raw.get("max_batch_events", 200)),
            max_batch_bytes=int(runtime_raw.get("max_batch_bytes", 262144)),
            queue_max_bytes=int(runtime_raw.get("queue_max_bytes", 2147483648)),
            max_retries=int(runtime_raw.get("max_retries", 12)),
            retry_base_seconds=int(runtime_raw.get("retry_base_seconds", 2)),
            retry_max_seconds=int(runtime_raw.get("retry_max_seconds", 300)),
            retry_jitter_seconds=int(runtime_raw.get("retry_jitter_seconds", 3)),
        )
        cfg = cls(tc=tc_cfg, agent=agent_cfg, runtime=runtime_cfg, paths=PathsConfig.from_root(root_dir))
        cfg.validate(require_tc_auth=require_tc_auth)
        return cfg

    def validate(self, require_tc_auth: bool = True) -> None:
        if not self.tc.base_url:
            raise ValueError("tc.base_url is required")
        if require_tc_auth:
            if not self.tc.api_token:
                raise ValueError("tc.api_token is required")
            if not self.tc.service_code and not self.tc.service_id:
                raise ValueError("Either tc.service_code or tc.service_id is required")
        if self.runtime.max_batch_events < 1:
            raise ValueError("runtime.max_batch_events must be >= 1")
        if self.runtime.queue_max_bytes < 1048576:
            raise ValueError("runtime.queue_max_bytes must be >= 1048576")

    def ensure_paths(self) -> None:
        self.paths.state_dir.mkdir(parents=True, exist_ok=True)


def write_example_config(file_path: Path) -> None:
    sample = {
        "tc": {
            "base_url": "http://localhost:8000",
            "api_token": "",
            "service_code": "svc-core-flow"
        },
        "agent": {
            "feeder_identifier": None,
            "host_name": socket.gethostname(),
            "ip_address": detect_default_ip(),
            "metadata": {
                "agent_version": "0.1.0",
                "os": "windows"
            }
        },
        "runtime": {
            "task_sync_interval_seconds": 30,
            "collect_interval_seconds": 2,
            "flush_interval_seconds": 5,
            "heartbeat_interval_seconds": 30,
            "request_timeout_seconds": 15,
            "max_batch_events": 200,
            "max_batch_bytes": 262144,
            "queue_max_bytes": 2147483648,
            "max_retries": 12,
            "retry_base_seconds": 2,
            "retry_max_seconds": 300,
            "retry_jitter_seconds": 3
        }
    }
    file_path.write_text(json.dumps(sample, indent=2), encoding="utf-8")
