from __future__ import annotations

import shutil
import socket
import subprocess
from pathlib import Path

from entities.llm import LlmFrameworkEntity, ModelEntity

_GGUF_SCAN_DIR = Path("/root/models/gguf")

_LLAMA_SERVER_NAMES = ["llama-server", "llama.cpp/server", "llama-cpp-server"]


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class LlamaCppAdapter:
    """Adapter for managing the llama.cpp LLM framework."""

    def is_installed(self) -> bool:
        for name in _LLAMA_SERVER_NAMES:
            if shutil.which(name) is not None:
                return True
        # Check common paths
        common_paths = [
            Path("/usr/local/bin/llama-server"),
            Path("/usr/bin/llama-server"),
            Path.home() / "llama.cpp" / "server",
            Path.home() / "llama.cpp" / "llama-server",
            Path("/opt/llama.cpp/server"),
        ]
        return any(p.exists() for p in common_paths)

    def _find_binary(self) -> str | None:
        for name in _LLAMA_SERVER_NAMES:
            found = shutil.which(name)
            if found:
                return found
        common_paths = [
            Path("/usr/local/bin/llama-server"),
            Path("/usr/bin/llama-server"),
            Path.home() / "llama.cpp" / "server",
            Path.home() / "llama.cpp" / "llama-server",
            Path("/opt/llama.cpp/server"),
        ]
        for p in common_paths:
            if p.exists():
                return str(p)
        return None

    def is_running(self, port: int) -> bool:
        return _check_port("127.0.0.1", port)

    def start(self, entity: LlmFrameworkEntity) -> None:
        """Start llama-server in the background."""
        binary = self._find_binary()
        if not binary:
            return
        try:
            cmd = [binary, "--port", str(entity.port)]
            # If there's an active model, pass it
            if entity.active_model:
                cmd.extend(["--model", entity.active_model])
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            pass

    def stop(self) -> None:
        """Stop llama-server via pkill."""
        try:
            subprocess.run(
                ["pkill", "-f", "llama-server"],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["pkill", "-f", "llama.cpp/server"],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    def list_models(self) -> list[ModelEntity]:
        """Scan /root/models/gguf/ for .gguf files."""
        models: list[ModelEntity] = []
        if not _GGUF_SCAN_DIR.exists():
            return models
        try:
            for gguf_file in _GGUF_SCAN_DIR.glob("**/*.gguf"):
                size_bytes: int | None = None
                try:
                    size_bytes = gguf_file.stat().st_size
                except Exception:
                    pass
                models.append(
                    ModelEntity(
                        name=gguf_file.name,
                        size_bytes=size_bytes,
                        framework="llama.cpp",
                    )
                )
        except Exception:
            pass
        return models

    def refresh(self, entity: LlmFrameworkEntity) -> LlmFrameworkEntity:
        """Update entity with current state."""
        entity.is_installed = self.is_installed()
        entity.is_running = self.is_running(entity.port)
        entity.available_models = self.list_models()
        return entity
