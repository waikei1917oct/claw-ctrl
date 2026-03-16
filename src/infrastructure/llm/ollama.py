from __future__ import annotations

import shutil
import socket
import subprocess

from entities.llm import LlmFrameworkEntity, ModelEntity


def _check_port(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
        return False


class OllamaAdapter:
    """Adapter for managing the Ollama LLM framework."""

    def is_installed(self) -> bool:
        return shutil.which("ollama") is not None

    def is_running(self, port: int) -> bool:
        return _check_port("127.0.0.1", port)

    def start(self, entity: LlmFrameworkEntity) -> None:
        """Start ollama serve in the background."""
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except FileNotFoundError:
            pass
        except Exception:
            pass

    def stop(self) -> None:
        """Stop ollama via pkill."""
        try:
            subprocess.run(
                ["pkill", "-f", "ollama serve"],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    def list_models(self) -> list[ModelEntity]:
        """Parse `ollama list` output into ModelEntity objects."""
        models: list[ModelEntity] = []
        try:
            result = subprocess.run(
                ["ollama", "list"],
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                return models

            lines = result.stdout.strip().splitlines()
            # Skip header line
            for line in lines[1:]:
                parts = line.split()
                if not parts:
                    continue
                name = parts[0]
                size_bytes: int | None = None
                # Try to parse size from output (e.g. "4.7 GB" or "4.7GB")
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
                models.append(ModelEntity(name=name, size_bytes=size_bytes, framework="ollama"))
        except Exception:
            pass
        return models

    def pull_model(self, name: str) -> None:
        """Pull a model by name."""
        try:
            subprocess.run(
                ["ollama", "pull", name],
                check=False,
            )
        except Exception:
            pass

    def delete_model(self, name: str) -> None:
        """Delete a model by name."""
        try:
            subprocess.run(
                ["ollama", "rm", name],
                capture_output=True,
                check=False,
            )
        except Exception:
            pass

    def refresh(self, entity: LlmFrameworkEntity) -> LlmFrameworkEntity:
        """Update entity with current state."""
        entity.is_installed = self.is_installed()
        entity.is_running = self.is_running(entity.port)
        if entity.is_running:
            entity.available_models = self.list_models()
        return entity
