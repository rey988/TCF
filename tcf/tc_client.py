from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request


class TCClientError(RuntimeError):
    pass


@dataclass
class TCClient:
    base_url: str
    api_token: str
    timeout_seconds: int = 15

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        query: dict[str, Any] | None = None,
        auth: bool = True,
    ) -> dict[str, Any]:
        url = f"{self.base_url}{path}"
        if query:
            query_clean = {k: v for k, v in query.items() if v is not None and v != ""}
            if query_clean:
                url = f"{url}?{parse.urlencode(query_clean)}"

        body_bytes = None
        headers = {"Accept": "application/json"}

        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"

        if auth:
            headers["Authorization"] = f"Bearer {self.api_token}"

        req = request.Request(url=url, data=body_bytes, headers=headers, method=method)

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                raw = resp.read().decode("utf-8")
                if not raw:
                    return {}
                return json.loads(raw)
        except error.HTTPError as exc:
            body = exc.read().decode("utf-8") if exc.fp else ""
            message = f"HTTP {exc.code} calling {path}"
            if body:
                message = f"{message}: {body}"
            raise TCClientError(message) from exc
        except error.URLError as exc:
            raise TCClientError(f"Network error calling {path}: {exc}") from exc

    def register_feeder(
        self,
        identifier: str,
        service_code: str | None,
        service_id: int | None,
        host_name: str,
        ip_address: str,
        metadata: dict[str, Any],
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "identifier": identifier,
            "host_name": host_name,
            "ip_address": ip_address,
            "metadata": metadata,
        }
        if service_code:
            payload["service_code"] = service_code
        if service_id:
            payload["service_id"] = service_id

        result = self._request("POST", "/api/registry/feeders", payload=payload, auth=True)
        return result.get("data", {})

    def get_feeder_task_version(self, identifier: str, current_version_md5: str | None) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/api/registry/feeders/{parse.quote(identifier, safe='')}/task-version",
            query={"current_version_md5": current_version_md5},
            auth=False,
        )
        return result.get("data", {})

    def get_latest_snapshot(self, service_id: int) -> dict[str, Any]:
        result = self._request("GET", f"/api/task/services/{service_id}/snapshots/latest", auth=False)
        return result.get("data", {})

    def get_snapshot_by_version(self, service_id: int, version_md5: str) -> dict[str, Any]:
        result = self._request(
            "GET",
            f"/api/task/services/{service_id}/snapshots/{parse.quote(version_md5, safe='')}",
            auth=False,
        )
        return result.get("data", {})

    def ingest_logs(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/api/ingestion/log-batches", payload=payload, auth=True)
        return result.get("data", {})

    def ingest_audits(self, payload: dict[str, Any]) -> dict[str, Any]:
        result = self._request("POST", "/api/ingestion/audit-batches", payload=payload, auth=True)
        return result.get("data", {})

    def heartbeat(self, feeder_identifier: str, metadata: dict[str, Any]) -> dict[str, Any]:
        result = self._request(
            "POST",
            "/api/ingestion/feeders/heartbeat",
            payload={
                "feeder_identifier": feeder_identifier,
                "metadata": metadata,
            },
            auth=True,
        )
        return result.get("data", {})
