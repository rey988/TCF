#!/usr/bin/env sh
set -eu

usage() {
  cat <<'EOF'
Install TCF as a standalone container for an existing V3-backoffice runtime.

Usage:
  ./install-v3b-tcf.sh \
    --tc-url http://tc:8023 \
    --tc-token <TC_API_TOKEN> \
    --service-code svc-v3-backoffice \
    [--backoffice-container <container_name>] \
    [--network <docker_network>] \
    [--feeder-id tcf-v3b-001] \
    [--container-name collector-tcf] \
    [--image-name collector-tcf:dev]

Notes:
- Provide either --network or --backoffice-container.
- If both are omitted, network defaults to bridge.
EOF
}

TC_URL=""
TC_TOKEN=""
SERVICE_CODE=""
BACKOFFICE_CONTAINER=""
NETWORK=""
FEEDER_ID=""
CONTAINER_NAME="collector-tcf"
IMAGE_NAME="collector-tcf:dev"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --tc-url)
      TC_URL="$2"
      shift 2
      ;;
    --tc-token)
      TC_TOKEN="$2"
      shift 2
      ;;
    --service-code)
      SERVICE_CODE="$2"
      shift 2
      ;;
    --backoffice-container)
      BACKOFFICE_CONTAINER="$2"
      shift 2
      ;;
    --network)
      NETWORK="$2"
      shift 2
      ;;
    --feeder-id)
      FEEDER_ID="$2"
      shift 2
      ;;
    --container-name)
      CONTAINER_NAME="$2"
      shift 2
      ;;
    --image-name)
      IMAGE_NAME="$2"
      shift 2
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [ -z "$TC_URL" ] || [ -z "$TC_TOKEN" ] || [ -z "$SERVICE_CODE" ]; then
  echo "Error: --tc-url, --tc-token, and --service-code are required." >&2
  usage
  exit 1
fi

if [ -n "$BACKOFFICE_CONTAINER" ] && [ -z "$NETWORK" ]; then
  NETWORK="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{println $k}}{{end}}' "$BACKOFFICE_CONTAINER" | head -n 1 | tr -d '\r')"
  if [ -z "$NETWORK" ]; then
    echo "Error: Failed to detect network from container $BACKOFFICE_CONTAINER" >&2
    exit 1
  fi
fi

if [ -z "$NETWORK" ]; then
  NETWORK="bridge"
fi

if [ -z "$FEEDER_ID" ]; then
  FEEDER_ID="tcf-$(date +%Y%m%d%H%M%S)"
fi

HOST_NAME="$(hostname)"
IP_ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
if [ -z "$IP_ADDRESS" ]; then
  IP_ADDRESS="127.0.0.1"
fi

mkdir -p state input

cat > tcf.config.v3b.json <<EOF
{
  "tc": {
    "base_url": "$TC_URL",
    "api_token": "$TC_TOKEN",
    "service_code": "$SERVICE_CODE"
  },
  "agent": {
    "feeder_identifier": "$FEEDER_ID",
    "host_name": "$HOST_NAME",
    "ip_address": "$IP_ADDRESS",
    "metadata": {
      "agent_version": "0.1.0",
      "os": "container-v3-backoffice"
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
EOF

echo "[1/3] Building TCF image: $IMAGE_NAME"
docker build -t "$IMAGE_NAME" .

if docker ps -a --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
  echo "[2/3] Replacing existing container: $CONTAINER_NAME"
  docker rm -f "$CONTAINER_NAME" >/dev/null
else
  echo "[2/3] Creating new container: $CONTAINER_NAME"
fi

echo "[3/3] Starting TCF container on network: $NETWORK"
docker run -d \
  --name "$CONTAINER_NAME" \
  --restart unless-stopped \
  --network "$NETWORK" \
  -v "$(pwd)/tcf.config.v3b.json:/app/tcf.config.v3b.json" \
  -v "$(pwd)/state:/app/state" \
  -v "$(pwd)/input:/app/input" \
  "$IMAGE_NAME" \
  python tcf.py --config tcf.config.v3b.json run >/dev/null

echo "TCF installed and started successfully."
echo "Container: $CONTAINER_NAME"
echo "Feeder ID: $FEEDER_ID"
echo ""
echo "Useful commands:"
echo "  docker logs -f $CONTAINER_NAME"
echo "  docker exec $CONTAINER_NAME python tcf.py --config tcf.config.v3b.json status"
