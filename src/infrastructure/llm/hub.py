from __future__ import annotations

import shutil
import time
from pathlib import Path
from typing import Callable

from entities.llm import LlmFrameworkEntity
from infrastructure.llm.ollama import OllamaAdapter, get_live_storage_dir
from infrastructure.llm.llama_cpp import LlamaCppAdapter
from infrastructure.llm.model_dir_repo import ModelDirRepo

_DEFAULT_PORTS = {
    "ollama": 11434,
    "llama.cpp": 8080,
}


class LlmFrameworkHub:
    """Central hub for managing multiple LLM framework instances."""

    def __init__(self) -> None:
        self._dir_repo = ModelDirRepo()
        saved_dirs = self._dir_repo.load()
        self._prev_running: dict[str, bool] = {}

        self.entities: dict[str, LlmFrameworkEntity] = {
            "ollama": LlmFrameworkEntity(
                name="ollama",
                port=self._dir_repo.get_port("ollama") or _DEFAULT_PORTS["ollama"],
                model_dir=saved_dirs.get("ollama"),
            ),
            "llama.cpp": LlmFrameworkEntity(
                name="llama.cpp",
                port=self._dir_repo.get_port("llama.cpp") or _DEFAULT_PORTS["llama.cpp"],
                model_dir=saved_dirs.get("llama.cpp"),
            ),
        }
        self._adapters: dict[str, OllamaAdapter | LlamaCppAdapter] = {
            "ollama": OllamaAdapter(),
            "llama.cpp": LlamaCppAdapter(),
        }

    def _get_adapter(self, name: str) -> OllamaAdapter | LlamaCppAdapter | None:
        return self._adapters.get(name)  # type: ignore[return-value]

    def refresh_all(self) -> list[LlmFrameworkEntity]:
        for name, entity in self.entities.items():
            adapter = self._get_adapter(name)
            if adapter is not None:
                try:
                    adapter.refresh(entity)
                except Exception:
                    pass
        # Persist any auto-detected port changes so they survive restarts
        for name, entity in self.entities.items():
            saved = self._dir_repo.get_port(name)
            if entity.port != (saved or _DEFAULT_PORTS.get(name)):
                try:
                    self._dir_repo.set_port(name, entity.port)
                except Exception:
                    pass
        return list(self.entities.values())

    def enforce_exclusivity(self) -> list[str]:
        """
        Detect frameworks that started externally and stop conflicting ones.
        Returns a list of framework names that were auto-stopped.
        Only acts when exactly one framework is newly running — avoids
        false positives on the very first refresh (all prev states unknown).
        """
        newly_started = [
            name for name, entity in self.entities.items()
            if entity.is_running and not self._prev_running.get(name, False)
        ]
        stopped: list[str] = []
        # If a framework just started, stop all others that are running
        if newly_started:
            trigger = newly_started[0]
            for name, entity in self.entities.items():
                if name != trigger and entity.is_running:
                    try:
                        adapter = self._get_adapter(name)
                        if adapter is not None:
                            adapter.stop()
                            adapter.refresh(entity)
                            stopped.append(name)
                    except Exception:
                        pass
        # Update prev state
        self._prev_running = {name: entity.is_running for name, entity in self.entities.items()}
        return stopped

    def start(self, name: str) -> LlmFrameworkEntity:
        entity = self.entities.get(name)
        if entity is None:
            raise KeyError(f"Unknown framework: {name}")
        for other_name, other_entity in self.entities.items():
            if other_name != name and other_entity.is_running:
                self.stop(other_name)
        adapter = self._get_adapter(name)
        if adapter is not None:
            try:
                adapter.start(entity)
                adapter.refresh(entity)
            except Exception:
                pass
        return entity

    def stop(self, name: str) -> LlmFrameworkEntity:
        entity = self.entities.get(name)
        if entity is None:
            raise KeyError(f"Unknown framework: {name}")
        adapter = self._get_adapter(name)
        if adapter is not None:
            try:
                adapter.stop()
                adapter.refresh(entity)
            except Exception:
                pass
        return entity

    def delete_model(self, framework_name: str, model_name: str) -> None:
        entity = self.entities.get(framework_name)
        adapter = self._get_adapter(framework_name)
        if entity is None or adapter is None:
            raise KeyError(f"Unknown framework: {framework_name}")
        if isinstance(adapter, OllamaAdapter):
            adapter.delete_model(model_name)
        elif isinstance(adapter, LlamaCppAdapter):
            model_dir = adapter.get_effective_model_dir(entity)
            adapter.delete_model(model_name, model_dir)
        adapter.refresh(entity)

    def get_live_storage_dir(self, framework_name: str) -> Path | None:
        """
        Return where the framework ACTUALLY stores models right now
        (from the running process), regardless of entity.model_dir.
        """
        if framework_name == "ollama":
            return get_live_storage_dir()
        elif framework_name == "llama.cpp":
            entity = self.entities.get(framework_name)
            adapter = self._get_adapter(framework_name)
            if entity and isinstance(adapter, LlamaCppAdapter):
                return adapter.get_effective_model_dir(entity)
        return None

    def purge_dir(self, framework_name: str, dir_path: str) -> int:
        """
        Delete all model files in dir_path for the given framework.
        Returns the number of files deleted.
        """
        path = Path(dir_path)
        if not path.exists():
            return 0
        count = 0
        if framework_name == "llama.cpp":
            for f in list(path.glob("**/*.gguf")):
                try:
                    f.unlink()
                    count += 1
                except Exception:
                    pass
        elif framework_name == "ollama":
            # For ollama: remove the entire blob+manifest store
            for subdir in ["blobs", "manifests"]:
                target = path / subdir
                if target.exists():
                    shutil.rmtree(str(target))
                    count += 1
        return count

    def set_model_dir(
        self,
        framework_name: str,
        new_dir: str,
        move_existing: bool = True,
        progress_cb: Callable[[str], None] | None = None,
    ) -> None:
        """
        Move the model storage directory for a framework.

        For ollama:
          - Stops the systemd service (prevents auto-restart interfering).
          - Moves blobs+manifests from live storage dir to new_dir.
          - Writes OLLAMA_MODELS to systemd override.conf.
          - Restarts the service.

        For llama.cpp:
          - Copies .gguf files to new_dir then deletes originals.
        """
        entity = self.entities.get(framework_name)
        adapter = self._get_adapter(framework_name)
        if entity is None or adapter is None:
            raise KeyError(f"Unknown framework: {framework_name}")

        was_running = entity.is_running

        if framework_name == "ollama" and isinstance(adapter, OllamaAdapter):
            if progress_cb:
                progress_cb("Stopping ollama service ...")
            adapter.stop()

        if move_existing:
            adapter.move_models(entity, new_dir, progress_cb=progress_cb)
        else:
            entity.model_dir = new_dir

        # Persist new dir
        self._dir_repo.set(framework_name, str(entity.model_dir))

        if framework_name == "ollama" and isinstance(adapter, OllamaAdapter):
            # Always (re)start so the service picks up OLLAMA_MODELS from override.conf
            if progress_cb:
                progress_cb("Starting ollama with new model dir ...")
            adapter.start(entity)
            time.sleep(3)  # wait for server to come up

        adapter.refresh(entity)

    def get(self, name: str) -> LlmFrameworkEntity | None:
        return self.entities.get(name)

    def list_all(self) -> list[LlmFrameworkEntity]:
        return list(self.entities.values())
