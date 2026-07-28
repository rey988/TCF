# TCF (The Collector Feeder)

TCF is a stdlib-only Python feeder agent for TC.

## Requirements

- Python 3.11+
- No third-party Python dependencies

## Quick Start

1. Configure [tcf.config.json](tcf.config.json)

Interactive wizard:

```powershell
python tcf.py --createConfig
```

Or write to a custom file:

```powershell
python tcf.py --config tcf.config.v3b.json --createConfig
```

2. Set tc.api_token and tc.service_code or tc.service_id
3. Run one cycle:

```powershell
python tcf.py --config tcf.config.json sync-once
```

4. Start detached service:

```powershell
python tcf.py --config tcf.config.json start
```

## Fast Install for Existing V3-backoffice Container

From TCF folder, run one command:

PowerShell:

```powershell
.\install-v3b-tcf.ps1 -TcUrl http://host.docker.internal:8023 -TcToken <TC_API_TOKEN> -ServiceCode svc-v3-backoffice -BackofficeContainer <V3_BACKOFFICE_CONTAINER_NAME>
```

Shell:

```bash
./install-v3b-tcf.sh --tc-url http://host.docker.internal:8023 --tc-token <TC_API_TOKEN> --service-code svc-v3-backoffice --backoffice-container <V3_BACKOFFICE_CONTAINER_NAME>
```

## Commands

- `python tcf.py --config tcf.config.json run`
- `python tcf.py --config tcf.config.json start`
- `python tcf.py --config tcf.config.json stop`
- `python tcf.py --config tcf.config.json status`
- `python tcf.py --config tcf.config.json watch`
- `python tcf.py --config tcf.config.json queue`
- `python tcf.py --config tcf.config.json sync-once`
- `python tcf.py --createConfig`

## Container Run

Build image:

```powershell
docker build -t collector-tcf:dev .
```

Run status in container:

```powershell
docker run --rm -v ${PWD}/tcf.config.json:/app/tcf.config.json -v ${PWD}/state:/app/state collector-tcf:dev python tcf.py --config tcf.config.json status
```

Run with compose:

```powershell
docker compose run --rm tcf
```

Notes:

- For host TC access from container, set `tc.base_url` in `tcf.config.json` to `http://host.docker.internal:8023`.
- `state/` is mounted for persistent queue and runtime state.

## State Files

Created under `state/`:

- `tcf_state.db`: sqlite queue, offsets, runtime state
- `identity.json`: immutable feeder identifier
- `tasks_snapshot.json`: active task snapshot
- `tcf.pid`: running process id
- `tcf.log`: runtime logs

## Runtime Notes

- Registration sends feeder identifier, service mapping, host name, and IP address.
- Task sync uses feeder task version endpoint and snapshot pull by version.
- Collectors currently support file-based `log_collecting` and `audit_collecting` tasks using `config.path` (or `file_path`/`log_path`).
- Queue is persistent and at-least-once. Failed deliveries retry with backoff+jitter.
