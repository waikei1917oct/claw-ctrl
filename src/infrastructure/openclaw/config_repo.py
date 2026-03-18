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
    """Return (cpu_percent, mem_mb) for the given PID."""
    # Try psutil first
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

    # Fallback: read from /proc and ps (Linux only, no extra deps)
    cpu: float | None = None
    mem_mb: float | None = None

    try:
        result = subprocess.run(
            ["ps", "--no-headers", "-p", str(pid), "-o", "%cpu"],
            capture_output=True, text=True, check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            cpu = float(result.stdout.strip())
    except Exception:
        pass

    try:
        status = Path(f"/proc/{pid}/status").read_text()
        for line in status.splitlines():
            if line.startswith("VmRSS:"):
                mem_mb = int(line.split()[1]) / 1024
                break
    except Exception:
        pass

    return cpu, mem_mb


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
        """
        Return agent names from two sources, merged and deduplicated:
        1. openclaw.json agents.list  (authoritative for configured agents)
        2. ~/.openclaw/agents/ filesystem dirs  (catches stale dirs after bad deletes)
        Stale dirs (in filesystem but not in JSON) are included so the user can delete them.
        """
        names: list[str] = []
        seen: set[str] = set()

        # Primary: openclaw.json
        try:
            raw = self._read_raw()
            for entry in raw.get("agents", {}).get("list", []):
                n = entry.get("id")
                if n and n not in seen:
                    names.append(n)
                    seen.add(n)
        except Exception:
            pass

        # Secondary: filesystem dirs not already in the list
        if _AGENTS_DIR.exists():
            for p in sorted(_AGENTS_DIR.iterdir()):
                if p.is_dir() and p.name not in seen:
                    names.append(p.name)
                    seen.add(p.name)

        return names

    def get_agent_workspace(self, name: str) -> Path:
        """Return the workspace path for an agent from openclaw.json, or the default workspace."""
        try:
            raw = self._read_raw()
            for entry in raw.get("agents", {}).get("list", []):
                if entry.get("id") == name:
                    ws = entry.get("workspace")
                    if ws:
                        return Path(ws)
        except Exception:
            pass
        return Path.home() / ".openclaw" / "workspace"

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
        """Return global channel info list. Each dict: name, enabled, account_count, accounts."""
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

    def get_agent_channel_bindings(self, agent_name: str) -> list[dict]:
        """
        Return channel bindings for a specific agent.
        Reads the global `bindings` array and filters by agentId == agent_name.
        Each returned dict: {channel, accountId, match (full match dict)}.
        Returns [] if agent has no bindings.
        """
        raw = self._read_raw()
        result = []
        for binding in raw.get("bindings", []):
            if binding.get("agentId") != agent_name:
                continue
            match = binding.get("match", {})
            result.append({
                "channel": match.get("channel", ""),
                "accountId": match.get("accountId", ""),
                "match": match,
            })
        return result

    def create_agent(self, name: str, model_id: str) -> None:
        """
        Create a fully isolated agent:
        1. Add entry to openclaw.json with a dedicated workspace path.
        2. Create ~/.openclaw/workspace-{name}/ and copy persona templates from main workspace.
        3. Create ~/.openclaw/agents/{name}/agent/ with empty models.json + auth-profiles.json.
        """
        raw = self._read_raw()
        agent_list = raw.setdefault("agents", {}).setdefault("list", [])
        for entry in agent_list:
            if entry.get("id") == name:
                raise ValueError(f"Agent '{name}' already exists")

        # Isolated workspace path — never share with other agents
        new_workspace = Path.home() / ".openclaw" / f"workspace-{name}"
        new_workspace.mkdir(parents=True, exist_ok=True)

        # Copy persona template files from the main workspace
        main_workspace = Path(
            raw.get("agents", {}).get("defaults", {}).get("workspace",
            str(Path.home() / ".openclaw" / "workspace"))
        )
        _PERSONA_FILES = (
            "AGENTS.md", "HEARTBEAT.md", "IDENTITY.md",
            "SOUL.md", "TOOLS.md", "USER.md", "MEMORY.md",
        )
        for fname in _PERSONA_FILES:
            src = main_workspace / fname
            dst = new_workspace / fname
            if src.exists() and not dst.exists():
                shutil.copy2(str(src), str(dst))

        # Register in openclaw.json
        agent_list.append({
            "id": name,
            "workspace": str(new_workspace),
            "model": model_id,
        })
        self._write_raw(raw)

        # Create agentDir structure
        agent_dir = _AGENTS_DIR / name / "agent"
        agent_dir.mkdir(parents=True, exist_ok=True)
        models_file = agent_dir / "models.json"
        if not models_file.exists():
            models_file.write_text(json.dumps({"providers": {}}, indent=2))
        auth_file = agent_dir / "auth-profiles.json"
        if not auth_file.exists():
            auth_file.write_text(json.dumps({"version": 1, "profiles": {}, "lastGood": {}}, indent=2))

    def get_llamacpp_provider_id(self) -> str | None:
        """
        Find the provider ID for the local llama.cpp server by looking for
        a provider with api=openai-completions and a local baseUrl.
        Returns the provider key (e.g. 'llamaLocal'), or None if not found.
        """
        raw = self._read_raw()
        for provider_id, pdata in raw.get("models", {}).get("providers", {}).items():
            if pdata.get("api") == "openai-completions":
                base_url = pdata.get("baseUrl", "")
                if "127.0.0.1" in base_url or "localhost" in base_url:
                    return provider_id
        return None

    def add_model_to_provider(self, provider_id: str, model_entry: dict) -> None:
        """
        Add or update a model entry in models.providers[provider_id].models.
        If a model with the same id already exists it is replaced.
        """
        raw = self._read_raw()
        provider = raw.get("models", {}).get("providers", {}).get(provider_id)
        if provider is None:
            raise ValueError(f"Provider '{provider_id}' not found in openclaw.json")
        models_list: list = provider.setdefault("models", [])
        model_id = model_entry.get("id")
        replaced = False
        for i, m in enumerate(models_list):
            if m.get("id") == model_id:
                models_list[i] = model_entry
                replaced = True
                break
        if not replaced:
            models_list.append(model_entry)
        self._write_raw(raw)

    def remove_model_from_provider(self, provider_id: str, model_id: str) -> bool:
        """
        Remove a model from models.providers[provider_id].models by model id.
        Also removes the corresponding key from agents.defaults.models if present.
        Returns True if the model was found and removed, False if it wasn't there.
        """
        raw = self._read_raw()
        provider = raw.get("models", {}).get("providers", {}).get(provider_id)
        if provider is None:
            return False
        before = provider.get("models", [])
        after = [m for m in before if m.get("id") != model_id]
        if len(after) == len(before):
            return False
        provider["models"] = after
        # Also clean up agents.defaults.models entry
        model_key = f"{provider_id}/{model_id}"
        defaults_models = raw.get("agents", {}).get("defaults", {}).get("models", {})
        defaults_models.pop(model_key, None)
        self._write_raw(raw)
        return True

    def add_model_to_defaults(self, model_key: str, params: dict, alias: str | None = None) -> None:
        """Add or update a model entry in agents.defaults.models with the given params."""
        raw = self._read_raw()
        models = raw.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
        entry = models.get(model_key, {})
        entry["params"] = {k: v for k, v in params.items()}
        if alias is not None:
            entry["alias"] = alias
        models[model_key] = entry
        self._write_raw(raw)

    def set_agent_model_params(self, agent_name: str, model_key: str, params: dict) -> None:
        """
        Apply profile params for an agent by writing to agents.defaults.models[model_key].params.
        openclaw does NOT support agents.list[].params (unknown key → crash), so params
        can only be set at the model level in defaults.models.
        The agent's model pointer is also updated to model_key.
        """
        raw = self._read_raw()
        # 1. Write params into agents.defaults.models[model_key]
        models = raw.setdefault("agents", {}).setdefault("defaults", {}).setdefault("models", {})
        entry = models.get(model_key, {})
        entry["params"] = params
        models[model_key] = entry
        # 2. Point the agent to this model key
        found = False
        for agent_entry in raw.get("agents", {}).get("list", []):
            if agent_entry.get("id") == agent_name:
                agent_entry["model"] = model_key
                found = True
                break
        if not found:
            raise ValueError(f"Agent '{agent_name}' not found in openclaw.json")
        self._write_raw(raw)

    def get_agent_model_params(self, agent_name: str) -> dict:
        """Return the params currently set for the agent's model in agents.defaults.models."""
        raw = self._read_raw()
        model_key = None
        for entry in raw.get("agents", {}).get("list", []):
            if entry.get("id") == agent_name:
                model_key = entry.get("model")
                break
        if not model_key:
            return {}
        return raw.get("agents", {}).get("defaults", {}).get("models", {}).get(model_key, {}).get("params", {})

    def delete_agent(self, name: str) -> None:
        """
        Fully delete an agent:
        1. Remove from openclaw.json agents.list (if present — silently skip if not found).
        2. Delete ~/.openclaw/agents/{name}/ directory tree.
        3. Delete ~/.openclaw/workspace-{name}/ directory tree (isolated workspace).
        Does NOT touch the shared main workspace or any other agent's files.
        """
        # 1. Remove from JSON (tolerate missing entry — handles stale-dir case)
        raw = self._read_raw()
        agent_list = raw.get("agents", {}).get("list", [])
        new_list = [e for e in agent_list if e.get("id") != name]
        if len(new_list) != len(agent_list):
            raw["agents"]["list"] = new_list
            self._write_raw(raw)

        # 2. Delete agentDir
        agent_dir = _AGENTS_DIR / name
        if agent_dir.exists():
            shutil.rmtree(str(agent_dir))

        # 3. Delete isolated workspace (only workspace-{name}, never plain workspace)
        isolated_ws = Path.home() / ".openclaw" / f"workspace-{name}"
        if isolated_ws.exists():
            shutil.rmtree(str(isolated_ws))
