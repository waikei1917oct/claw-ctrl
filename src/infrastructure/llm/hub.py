from __future__ import annotations

from entities.llm import LlmFrameworkEntity
from infrastructure.llm.ollama import OllamaAdapter
from infrastructure.llm.llama_cpp import LlamaCppAdapter

_DEFAULT_PORTS = {
    "ollama": 11434,
    "llama.cpp": 8080,
}


class LlmFrameworkHub:
    """Central hub for managing multiple LLM framework instances."""

    def __init__(self) -> None:
        self.entities: dict[str, LlmFrameworkEntity] = {
            "ollama": LlmFrameworkEntity(name="ollama", port=_DEFAULT_PORTS["ollama"]),
            "llama.cpp": LlmFrameworkEntity(name="llama.cpp", port=_DEFAULT_PORTS["llama.cpp"]),
        }
        self._adapters: dict[str, object] = {
            "ollama": OllamaAdapter(),
            "llama.cpp": LlamaCppAdapter(),
        }

    def _get_adapter(self, name: str) -> OllamaAdapter | LlamaCppAdapter | None:
        return self._adapters.get(name)  # type: ignore[return-value]

    def refresh_all(self) -> list[LlmFrameworkEntity]:
        """Refresh all framework entities and return them."""
        for name, entity in self.entities.items():
            adapter = self._get_adapter(name)
            if adapter is not None:
                try:
                    adapter.refresh(entity)
                except Exception:
                    pass
        return list(self.entities.values())

    def start(self, name: str) -> LlmFrameworkEntity:
        """Stop all other frameworks first (mutual exclusivity), then start the named one."""
        entity = self.entities.get(name)
        if entity is None:
            raise KeyError(f"Unknown framework: {name}")

        # Stop all others
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
        """Stop the named framework."""
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

    def get(self, name: str) -> LlmFrameworkEntity | None:
        return self.entities.get(name)

    def list_all(self) -> list[LlmFrameworkEntity]:
        return list(self.entities.values())
