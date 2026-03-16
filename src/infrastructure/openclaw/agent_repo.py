from __future__ import annotations
import json
from pathlib import Path

_AGENTS_DIR = Path.home() / ".openclaw" / "agents"


class AgentRepo:
    """Manages per-agent config files in ~/.openclaw/agents/{name}/agent/."""

    def _auth_profiles_path(self, agent_name: str) -> Path:
        return _AGENTS_DIR / agent_name / "agent" / "auth-profiles.json"

    def _read_auth_profiles(self, agent_name: str) -> dict:
        p = self._auth_profiles_path(agent_name)
        if not p.exists():
            return {"version": 1, "profiles": {}, "lastGood": {}}
        try:
            return json.loads(p.read_text())
        except Exception:
            return {"version": 1, "profiles": {}, "lastGood": {}}

    def _write_auth_profiles(self, agent_name: str, data: dict) -> None:
        import tempfile, os
        p = self._auth_profiles_path(agent_name)
        p.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile("w", dir=p.parent, delete=False, suffix=".tmp") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            tmp = f.name
        os.replace(tmp, p)

    def get_auth_profiles(self, agent_name: str) -> dict:
        """Return dict: { provider: [profile_key, ...] } grouped by provider."""
        data = self._read_auth_profiles(agent_name)
        profiles = data.get("profiles", {})
        last_good = data.get("lastGood", {})
        grouped: dict[str, list[str]] = {}
        for key, pdata in profiles.items():
            provider = pdata.get("provider", key.split(":")[0])
            grouped.setdefault(provider, []).append(key)
        return {"grouped": grouped, "lastGood": last_good}

    def set_last_good(self, agent_name: str, provider: str, profile_key: str) -> None:
        """Set which profile is used for a provider."""
        data = self._read_auth_profiles(agent_name)
        if profile_key not in data.get("profiles", {}):
            raise ValueError(f"Profile '{profile_key}' not found")
        data.setdefault("lastGood", {})[provider] = profile_key
        self._write_auth_profiles(agent_name, data)
