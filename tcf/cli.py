from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from .config import TCFConfig, write_example_config
from .identity import IdentityStore
from .runtime import TCFService
from .state_db import StateDB
from .tc_client import TCClient


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "init-config":
        return cmd_init_config(Path(args.config))

    requires_tc_auth = args.command in {"run", "start", "sync-once"}

    try:
        cfg = TCFConfig.load(Path(args.config).resolve(), require_tc_auth=requires_tc_auth)
    except FileNotFoundError:
        print(f"Config not found: {args.config}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"Config validation error: {exc}", file=sys.stderr)
        return 2

    cfg.ensure_paths()

    if args.command == "run":
        return cmd_run(cfg)
    if args.command == "start":
        return cmd_start(cfg, args.config)
    if args.command == "stop":
        return cmd_stop(cfg)
    if args.command == "status":
        return cmd_status(cfg)
    if args.command == "watch":
        return cmd_watch(cfg)
    if args.command == "queue":
        return cmd_queue(cfg)
    if args.command == "sync-once":
        return cmd_sync_once(cfg)

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tcf", description="The Collector Feeder")
    parser.add_argument("--config", default="tcf.config.json", help="Path to TCF config JSON")

    sub = parser.add_subparsers(dest="command")
    sub.required = True

    sub.add_parser("init-config", help="Create example config file")
    sub.add_parser("run", help="Run foreground service loop")
    sub.add_parser("start", help="Start service in detached mode")
    sub.add_parser("stop", help="Stop service")
    sub.add_parser("status", help="Show runtime status")
    sub.add_parser("watch", help="Show active watch paths and task mapping")
    sub.add_parser("queue", help="Show queue stats")
    sub.add_parser("sync-once", help="Run one sync/collect/flush/heartbeat cycle")

    return parser


def cmd_init_config(config_path: Path) -> int:
    if config_path.exists():
        print(f"Config already exists: {config_path}")
        return 1
    write_example_config(config_path)
    print(f"Created {config_path}")
    return 0


def cmd_run(cfg: TCFConfig) -> int:
    _setup_logging(cfg.paths.log_path)

    db = StateDB(cfg.paths.db_path)
    identity = IdentityStore(cfg.paths.state_dir / "identity.json")
    feeder_identifier = identity.get_or_create(cfg.agent.feeder_identifier)
    client = TCClient(cfg.tc.base_url, cfg.tc.api_token, cfg.runtime.request_timeout_seconds)
    service = TCFService(cfg, db, client, feeder_identifier)

    stop = {"requested": False}

    def _request_stop(_signum, _frame) -> None:
        stop["requested"] = True

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    _write_pid(cfg.paths.pid_path, os.getpid())

    try:
        service.run_forever(lambda: stop["requested"])
    finally:
        _remove_pid(cfg.paths.pid_path)
        db.close()

    return 0


def cmd_start(cfg: TCFConfig, config_arg: str) -> int:
    pid = _read_pid(cfg.paths.pid_path)
    if pid and _is_running(pid):
        print(f"TCF already running with PID {pid}")
        return 0

    cfg.paths.state_dir.mkdir(parents=True, exist_ok=True)
    log_handle = cfg.paths.log_path.open("a", encoding="utf-8")

    command = [sys.executable, "-m", "tcf", "--config", config_arg, "run"]

    kwargs = {
        "stdout": log_handle,
        "stderr": log_handle,
        "cwd": str(cfg.paths.root_dir),
        "close_fds": True,
    }

    if os.name == "nt":
        kwargs["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP

    process = subprocess.Popen(command, **kwargs)
    time.sleep(0.4)

    print(f"TCF start requested. Spawned PID {process.pid}")
    return 0


def cmd_stop(cfg: TCFConfig) -> int:
    pid = _read_pid(cfg.paths.pid_path)
    if not pid:
        print("TCF is not running")
        return 0

    if not _is_running(pid):
        _remove_pid(cfg.paths.pid_path)
        print("TCF was not running. Cleaned stale pid file.")
        return 0

    os.kill(pid, signal.SIGTERM)

    for _ in range(40):
        if not _is_running(pid):
            _remove_pid(cfg.paths.pid_path)
            print(f"TCF stopped (PID {pid})")
            return 0
        time.sleep(0.25)

    print(f"Stop signal sent to PID {pid}, process still appears alive")
    return 1


def cmd_status(cfg: TCFConfig) -> int:
    db = StateDB(cfg.paths.db_path)
    try:
        pid = _read_pid(cfg.paths.pid_path)
        running = bool(pid and _is_running(pid))
        counts = db.queue_counts()

        print(json.dumps(
            {
                "running": running,
                "pid": pid,
                "feeder_identifier": db.get_state("feeder_identifier"),
                "service_id": db.get_state("service_id"),
                "running_since": db.get_state("running_since"),
                "current_task_version_md5": db.get_state("current_task_version_md5"),
                "last_task_sync_at": db.get_state("last_task_sync_at"),
                "last_heartbeat_at": db.get_state("last_heartbeat_at"),
                "last_ingest_success_at": db.get_state("last_ingest_success_at"),
                "last_error_summary": db.get_state("last_error_summary"),
                "queue": {
                    "pending": counts.get("pending", 0),
                    "dead_letter": counts.get("dead_letter", 0),
                    "total": counts.get("total", 0),
                    "pending_bytes": db.queue_bytes(),
                },
            },
            indent=2,
        ))
    finally:
        db.close()

    return 0


def cmd_watch(cfg: TCFConfig) -> int:
    db = StateDB(cfg.paths.db_path)
    try:
        watch = db.get_state_json("active_watch_paths") or {"items": []}
        current_version = db.get_state("current_task_version_md5")
        print(json.dumps({"current_task_version_md5": current_version, "watch": watch.get("items", [])}, indent=2))
    finally:
        db.close()
    return 0


def cmd_queue(cfg: TCFConfig) -> int:
    db = StateDB(cfg.paths.db_path)
    try:
        counts = db.queue_counts()
        print(json.dumps({"counts": counts, "pending_bytes": db.queue_bytes()}, indent=2))
    finally:
        db.close()
    return 0


def cmd_sync_once(cfg: TCFConfig) -> int:
    _setup_logging(cfg.paths.log_path)
    db = StateDB(cfg.paths.db_path)
    try:
        identity = IdentityStore(cfg.paths.state_dir / "identity.json")
        feeder_identifier = identity.get_or_create(cfg.agent.feeder_identifier)
        client = TCClient(cfg.tc.base_url, cfg.tc.api_token, cfg.runtime.request_timeout_seconds)
        service = TCFService(cfg, db, client, feeder_identifier)
        service.bootstrap()
        service.run_once()
    finally:
        db.close()

    print("TCF sync-once completed")
    return 0


def _setup_logging(log_path: Path) -> None:
    logging.basicConfig(
        filename=str(log_path),
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )


def _read_pid(pid_path: Path) -> int | None:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        return None


def _write_pid(pid_path: Path, pid: int) -> None:
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    pid_path.write_text(str(pid), encoding="utf-8")


def _remove_pid(pid_path: Path) -> None:
    try:
        pid_path.unlink(missing_ok=True)
    except OSError:
        pass


def _is_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False
