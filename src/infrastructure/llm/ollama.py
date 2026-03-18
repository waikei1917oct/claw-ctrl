from __future__ import annotations

import os
import shutil
import socket
import subprocess
from pathlib import Path
from typing import Callable

_SYSTEMD_OVERRIDE_DIR = Path("/etc/systemd/system/ollama.service.d")
_SYSTEMD_OVERRIDE_FILE = _SYSTEMD_OVERRIDE_DIR / "override.conf"

_FALLBACK_MODEL_DIRS = [
    Path("/usr/share/ollama/.ollama/models"),
    Path.home() / ".ollama" / "models",
]


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


def _probe_running_process_model_dir() -> Path | None:
    """
    Find where the running ollama server stores models by reading its
    process environment from /proc/{pid}/environ.
    Returns the model dir path, or None if no running process is found.
    """
    try:
        result = subprocess.run(
            ["pgrep", "-f", "ollama serve"],
            capture_output=True, text=True, check=False,
        )
        for pid_str in result.stdout.strip().splitlines():
            pid_str = pid_str.strip()
            if not pid_str:
                continue
            try:
                env_raw = Path(f"/proc/{pid_str}/environ").read_bytes()
                env_vars: dict[str, str] = {}
                for entry in env_raw.split(b"\x00"):
                    if b"=" in entry:
                        k, _, v = entry.partition(b"=")
                        env_vars[k.decode(errors="replace")] = v.decode(errors="replace")

                # OLLAMA_MODELS takes priority
                if "OLLAMA_MODELS" in env_vars:
                    p = Path(env_vars["OLLAMA_MODELS"])
                    if p.exists():
                        return p

                # Fall back to HOME-based path
                if "HOME" in env_vars:
                    candidate = Path(env_vars["HOME"]) / ".ollama" / "models"
                    if candidate.exists():
                        return candidate
            except Exception:
                continue
    except Exception:
        pass
    return None


def get_live_storage_dir() -> Path:
    """
    Return the directory where the running ollama server currently stores
    its models. Probes the live process; falls back to known defaults.
    """
    live = _probe_running_process_model_dir()
    if live:
        return live
    for d in _FALLBACK_MODEL_DIRS:
        if d.exists():
            return d
    return _FALLBACK_MODEL_DIRS[0]  # best-guess even if it doesn't exist


class OllamaAdapter:
    """Adapter for managing the Ollama LLM framework."""

    # ------------------------------------------------------------------
    # Systemd helpers
    # ------------------------------------------------------------------

    def _is_systemd_managed(self) -> bool:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "ollama"],
                capture_output=True, text=True, check=False,
            )
            return result.stdout.strip() == "active"
        except Exception:
            return False

    def _update_systemd_env(self, key: str, value: str) -> None:
        """
        Add or replace an Environment= line in the ollama systemd override conf.
        Preserves all other existing lines.
        """
        _SYSTEMD_OVERRIDE_DIR.mkdir(parents=True, exist_ok=True)

        existing_lines: list[str] = []
        if _SYSTEMD_OVERRIDE_FILE.exists():
            existing_lines = _SYSTEMD_OVERRIDE_FILE.read_text().splitlines()

        # Remove the existing line for this key (handles both quoted and unquoted)
        filtered = [
            ln for ln in existing_lines
            if not (
                ln.strip().startswith(f'Environment="{key}=')
                or ln.strip().startswith(f"Environment={key}=")
            )
        ]

        # Ensure [Service] section header exists
        if not any(ln.strip() == "[Service]" for ln in filtered):
            filtered.insert(0, "[Service]")

        filtered.append(f'Environment="{key}={value}"')

        tmp = _SYSTEMD_OVERRIDE_FILE.with_suffix(".conf.tmp")
        tmp.write_text("\n".join(filtered) + "\n")
        tmp.replace(_SYSTEMD_OVERRIDE_FILE)

        subprocess.run(["systemctl", "daemon-reload"], capture_output=True, check=False)

    # ------------------------------------------------------------------
    # Core adapter interface
    # ------------------------------------------------------------------

    def is_installed(self) -> bool:
        return shutil.which("ollama") is not None

    def is_running(self, port: int) -> bool:
        return _check_port("127.0.0.1", port)

    def get_effective_model_dir(self, entity) -> Path:
        """
        Return the directory to USE for this entity (configured destination).
        entity.model_dir overrides the live-detected default.
        """
        if entity.model_dir:
            return Path(entity.model_dir)
        return get_live_storage_dir()

    def stop(self) -> None:
        """Stop ollama — uses systemctl for managed services, pkill otherwise."""
        try:
            if self._is_systemd_managed():
                subprocess.run(["systemctl", "stop", "ollama"],
                               capture_output=True, check=False)
            else:
                subprocess.run(["pkill", "-f", "ollama serve"],
                               capture_output=True, check=False)
        except Exception:
            pass

    def start(self, entity) -> None:
        """Start ollama — sets OLLAMA_MODELS via systemd override or env var."""
        try:
            if self._is_systemd_managed():
                if entity.model_dir:
                    self._update_systemd_env("OLLAMA_MODELS", entity.model_dir)
                subprocess.run(["systemctl", "start", "ollama"],
                               capture_output=True, check=False)
            else:
                env = os.environ.copy()
                if entity.model_dir:
                    env["OLLAMA_MODELS"] = entity.model_dir
                subprocess.Popen(
                    ["ollama", "serve"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    env=env,
                )
        except Exception:
            pass

    def list_models(self) -> list:
        from entities.llm import ModelEntity
        models: list[ModelEntity] = []
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True, text=True, check=False,
            )
            if result.returncode != 0:
                return models
            for line in result.stdout.strip().splitlines()[1:]:
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                size_bytes: int | None = None
                if len(parts) >= 3:
                    try:
                        size_val = float(parts[2])
                        unit = parts[3].upper() if len(parts) > 3 else ""
                        if "GB" in unit or "GB" in parts[2].upper():
                            size_bytes = int(size_val * 1024 * 1024 * 1024)
                        elif "MB" in unit or "MB" in parts[2].upper():
                            size_bytes = int(size_val * 1024 * 1024)
                    except (ValueError, IndexError):
                        pass
                models.append(
                    __import__("entities.llm", fromlist=["ModelEntity"]).ModelEntity(
                        name=name, size_bytes=size_bytes, framework="ollama"
                    )
                )
        except Exception:
            pass
        return models

    def pull_model(self, name: str) -> None:
        try:
            subprocess.run(["ollama", "pull", name], check=False)
        except Exception:
            pass

    def delete_model(self, name: str) -> None:
        try:
            subprocess.run(["ollama", "rm", name], capture_output=True, check=False)
        except Exception:
            pass

    def move_models(
        self,
        entity,
        new_dir: str,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        """
        Move the ollama model store to new_dir.

        Source is ALWAYS the live running-process storage dir — never
        entity.model_dir, which may point to an already-empty destination
        from a previous failed attempt.

        Steps:
        1. Copy blobs + manifests from live dir to new_dir.
        2. Delete old storage dir.
        3. Update entity.model_dir.
        """
        new_path = Path(new_dir)
        new_path.mkdir(parents=True, exist_ok=True)

        # Always probe live process — do NOT use entity.model_dir as source
        source = get_live_storage_dir()

        if source == new_path:
            entity.model_dir = str(new_path)
            return

        if not source.exists():
            raise FileNotFoundError(
                f"Ollama model store not found at {source}. "
                "Is ollama running?"
            )

        if progress_cb:
            progress_cb(f"Copying from {source} ...")
        shutil.copytree(str(source), str(new_path), dirs_exist_ok=True)

        if progress_cb:
            progress_cb(f"Removing old store at {source} ...")
        shutil.rmtree(str(source))

        entity.model_dir = str(new_path)

    def refresh(self, entity) -> object:
        entity.is_installed = self.is_installed()
        entity.is_running = self.is_running(entity.port)
        if entity.is_running:
            entity.available_models = self.list_models()
        return entity
