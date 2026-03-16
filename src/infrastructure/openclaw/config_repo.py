from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

from entities.openclaw import GatewayEntity, OpenClawEntity, WorkspaceEntity

_CONFIG_PATHS = [
    Path.home() / ".openclaw" / "openclaw.json",
    Path("/etc/openclaw/openclaw.json"),
]

_AGENTS_DIR = Path.home() / ".openclaw" / "agents"


def _find_config() -> Path | None:
    for p in _CONFIG_PATHS:
        if p.exists():
            return p
    return None


def _detect_pid() -> int | None:
    """Detect the openclaw gateway PID using pgrep."""
    try:
        result = subprocess.run(
            ["pgrep", "-f", "openclaw-gateway|openclaw gateway"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            lines = result.stdout.strip().splitlines()
            if lines:
                return int(lines[0])
    except Exception:
        pass
    return None


def get_resource_usage(pid: int) -> tuple[float | None, float | None]:
    """Return (cpu_percent, mem_mb) for the given PID using psutil if available."""
    try:
        import psutil  # type: ignore

        proc = psutil.Process(pid)
        cpu = proc.cpu_percent(interval=0.2)
        mem = proc.memory_info().rss / (1024 * 1024)
        return cpu, mem
    except ImportError:
        pass
    except Exception:
        pass
    return None, None


class OpenClawConfigRepo:
    """Reads openclaw configuration from disk and builds an OpenClawEntity."""

    def load(self) -> OpenClawEntity:
        binary = shutil.which("openclaw")
        install_path = Path(binary).parent if binary else Path("/usr/local/bin")

        config_file = _find_config()
        config_path = str(config_file) if config_file else str(_CONFIG_PATHS[0])

        gateway = GatewayEntity()
        workspaces: list[WorkspaceEntity] = []
        version = "unknown"

        if config_file and config_file.exists():
            try:
                raw = json.loads(config_file.read_text())

                # version
                meta = raw.get("meta", {})
                version = meta.get("lastTouchedVersion", raw.get("version", "unknown"))

                # gateway
                gw = raw.get("gateway", {})
                gateway = GatewayEntity(
                    host=gw.get("host", "127.0.0.1"),
                    port=gw.get("port"),
                    mode=gw.get("mode"),
                )

                # workspaces
                for ws in raw.get("workspaces", []):
                    if isinstance(ws, dict):
                        name = ws.get("name", "")
                        path_str = ws.get("path", "")
                        if name and path_str:
                            workspaces.append(WorkspaceEntity(name=name, path=Path(path_str)))
                    elif isinstance(ws, str):
                        workspaces.append(WorkspaceEntity(name=Path(ws).name, path=Path(ws)))

            except Exception:
                pass

        entity = OpenClawEntity(
            name="openclaw",
            version=version,
            install_path=install_path,
            config_path=config_path,
            gateway=gateway,
            workspaces=workspaces,
        )
        return entity

    def list_agents(self) -> list[str]:
        """Return agent names found in ~/.openclaw/agents/."""
        if not _AGENTS_DIR.exists():
            return []
        return [p.name for p in _AGENTS_DIR.iterdir() if p.is_dir()]

    def _read_raw(self) -> dict:
        """Read raw openclaw.json. Returns empty dict on failure."""
        config_file = _find_config()
        if not config_file or not config_file.exists():
            return {}
        try:
            return json.loads(config_file.read_text())
        except Exception:
            return {}

    def _write_raw(self, data: dict) -> None:
        """Write back to openclaw.json atomically (write to tmp then rename)."""
        config_file = _find_config() or _CONFIG_PATHS[0]
        import tempfile, os
        with tempfile.NamedTemporaryFile("w", dir=config_file.parent, delete=False, suffix=".tmp") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            tmp_path = f.name
        os.replace(tmp_path, config_file)

    def list_available_models(self) -> list[str]:
        """Return all model IDs from agents.defaults.models."""
        raw = self._read_raw()
        models_dict = raw.get("agents", {}).get("defaults", {}).get("models", {})
        return list(models_dict.keys())

    def get_agent_current_model(self, agent_name: str) -> str | None:
        """Return current model for agent from agents.list."""
        raw = self._read_raw()
        for entry in raw.get("agents", {}).get("list", []):
            if entry.get("id") == agent_name:
                return entry.get("model")
        return None

    def set_agent_model(self, agent_name: str, model_id: str) -> None:
        """Update the model for agent_name in openclaw.json, then restart openclaw."""
        raw = self._read_raw()
        found = False
        for entry in raw.get("agents", {}).get("list", []):
            if entry.get("id") == agent_name:
                entry["model"] = model_id
                found = True
                break
        if not found:
            raise ValueError(f"Agent '{agent_name}' not found in openclaw.json")
        self._write_raw(raw)

    def list_channels(self) -> list[dict]:
        """Return channel info list. Each dict: name, enabled, account_count."""
        raw = self._read_raw()
        channels = raw.get("channels", {})
        result = []
        for ch_name, ch_data in channels.items():
            accounts = ch_data.get("accounts", {})
            result.append({
                "name": ch_name,
                "enabled": ch_data.get("enabled", False),
                "account_count": len(accounts),
                "accounts": list(accounts.keys()),
            })
        return result

    def create_agent(self, name: str, model_id: str) -> None:
        """Add agent to openclaw.json and create ~/.openclaw/agents/{name}/agent/ dirs."""
        raw = self._read_raw()
        agent_list = raw.setdefault("agents", {}).setdefault("list", [])
        for entry in agent_list:
            if entry.get("id") == name:
                raise ValueError(f"Agent '{name}' already exists")
        default_workspace = raw.get("agents", {}).get("defaults", {}).get("workspace", str(Path.home() / ".openclaw" / "workspace"))
        agent_list.append({"id": name, "workspace": default_workspace, "model": model_id})
        self._write_raw(raw)
        # Create directory structure
        agent_dir = _AGENTS_DIR / name / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        # Write empty models.json and auth-profiles.json
        models_file = agent_dir / "models.json"
        if not models_file.exists():
            models_file.write_text(json.dumps({"providers": {}}, indent=2))
        auth_file = agent_dir / "auth-profiles.json"
        if not auth_file.exists():
            auth_file.write_text(json.dumps({"version": 1, "profiles": {}, "lastGood": {}}, indent=2))

    def delete_agent(self, name: str) -> None:
        """Remove agent from openclaw.json (does NOT delete files)."""
        raw = self._read_raw()
        agent_list = raw.get("agents", {}).get("list", [])
        new_list = [e for e in agent_list if e.get("id") != name]
        if len(new_list) == len(agent_list):
            raise ValueError(f"Agent '{name}' not found in openclaw.json")
        raw["agents"]["list"] = new_list
        self._write_raw(raw)
