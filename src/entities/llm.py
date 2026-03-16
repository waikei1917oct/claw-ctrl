from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ModelEntity:
    name: str
    size_bytes: int | None = None
    framework: str | None = None


@dataclass
class LlmFrameworkEntity:
    name: str          # "ollama" | "llama.cpp" | "vllm"
    port: int
    is_running: bool = False
    is_installed: bool = False
    active_model: str | None = None
    available_models: list[ModelEntity] = field(default_factory=list)
    vram_limit_mb: int | None = None

    def set_port(self, new_port: int) -> None:
        if not (1024 <= new_port <= 65535):
            raise ValueError(f"Invalid port: {new_port}")
        self.port = new_port
