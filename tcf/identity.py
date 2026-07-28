from __future__ import annotations

import json
import uuid
from pathlib import Path


class IdentityStore:
    def __init__(self, identity_path: Path):
        self.identity_path = identity_path

    def get_or_create(self, configured_identifier: str | None) -> str:
        if configured_identifier:
            self._write(configured_identifier)
            return configured_identifier

        if self.identity_path.exists():
            raw = json.loads(self.identity_path.read_text(encoding="utf-8"))
            existing = raw.get("feeder_identifier")
            if isinstance(existing, str) and existing:
                return existing

        generated = f"tcf-{uuid.uuid4()}"
        self._write(generated)
        return generated

    def _write(self, identifier: str) -> None:
        self.identity_path.parent.mkdir(parents=True, exist_ok=True)
        self.identity_path.write_text(
            json.dumps({"feeder_identifier": identifier}, indent=2),
            encoding="utf-8",
        )
