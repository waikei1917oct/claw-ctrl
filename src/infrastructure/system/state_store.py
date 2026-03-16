from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_DIR = Path.home() / ".claw-ctrl"
STATE_FILE = STATE_DIR / "state.json"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    """Persists last-known system state to ~/.claw-ctrl/state.json.

    Written by the daemon after each poll cycle.
    Read by the TUI as a fast initial load before the live refresh completes.
    """

    def __init__(self, path: Path = STATE_FILE) -> None:
        self._path = path

    def save(self, openclaw: dict[str, Any], llm: list[dict[str, Any]]) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": _utc_now(),
            "openclaw": openclaw,
            "llm": llm,
        }
        self._path.write_text(json.dumps(payload, indent=2, ensure_ascii=False))

    def load(self) -> dict[str, Any] | None:
        if not self._path.exists():
            return None
        try:
            return json.loads(self._path.read_text())
        except Exception:
            return None

    def updated_at(self) -> str | None:
        data = self.load()
        return data.get("updated_at") if data else None
