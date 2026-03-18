from __future__ import annotations

import json
from pathlib import Path

_CONFIG_PATH = Path.home() / ".claw-ctrl" / "llm_dirs.json"


class ModelDirRepo:
    """Persists model directory and port configuration for LLM frameworks.

    File format (new):
        {"dirs": {"ollama": "...", "llama.cpp": "..."}, "ports": {"llama.cpp": 32102}}

    Backward-compatible: old flat format {"ollama": "...", "llama.cpp": "..."} is
    treated as dirs-only and migrated on next write.
    """

    def _load_all(self) -> dict:
        if not _CONFIG_PATH.exists():
            return {"dirs": {}, "ports": {}}
        try:
            raw = json.loads(_CONFIG_PATH.read_text())
        except Exception:
            return {"dirs": {}, "ports": {}}
        # Migrate old flat format
        if "dirs" not in raw:
            return {"dirs": raw, "ports": {}}
        return raw

    def _save_all(self, data: dict) -> None:
        _CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _CONFIG_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(_CONFIG_PATH)

    # ------------------------------------------------------------------
    # Directory methods (backward-compatible API)
    # ------------------------------------------------------------------

    def load(self) -> dict[str, str]:
        """Returns {framework_name: model_dir} mapping."""
        return self._load_all().get("dirs", {})

    def save(self, dirs: dict[str, str]) -> None:
        data = self._load_all()
        data["dirs"] = dirs
        self._save_all(data)

    def get(self, framework_name: str) -> str | None:
        return self.load().get(framework_name)

    def set(self, framework_name: str, model_dir: str) -> None:
        dirs = self.load()
        dirs[framework_name] = model_dir
        self.save(dirs)

    def remove(self, framework_name: str) -> None:
        dirs = self.load()
        dirs.pop(framework_name, None)
        self.save(dirs)

    # ------------------------------------------------------------------
    # Port methods
    # ------------------------------------------------------------------

    def get_port(self, framework_name: str) -> int | None:
        return self._load_all().get("ports", {}).get(framework_name)

    def set_port(self, framework_name: str, port: int) -> None:
        data = self._load_all()
        data.setdefault("ports", {})[framework_name] = port
        self._save_all(data)

    # ------------------------------------------------------------------
    # Default model / profile
    # ------------------------------------------------------------------

    def get_default(self, framework_name: str) -> dict:
        """Return {"model": ..., "profile": ...} or {} if not set."""
        return self._load_all().get("defaults", {}).get(framework_name, {})

    def set_default(self, framework_name: str, model: str, profile: str) -> None:
        data = self._load_all()
        data.setdefault("defaults", {})[framework_name] = {
            "model": model,
            "profile": profile,
        }
        self._save_all(data)
