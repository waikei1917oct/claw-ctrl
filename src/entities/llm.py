from __future__ import annotations
from dataclasses import dataclass, field


@dataclass
class ModelRunProfile:
    """Runtime parameters for a single named execution profile."""
    name: str
    context_size: int = 65536       # -c  (token context window)
    max_tokens: int = 1024          # -n  (tokens to generate)
    gpu_layers: int = 80            # -ngl
    temp: float = 0.7               # --temp
    top_p: float = 0.95             # --top-p
    top_k: int = 40                 # --top-k
    min_p: float = 0.05             # --min-p
    repeat_penalty: float = 1.05    # --repeat-penalty

    def to_args(self) -> list[str]:
        """Return the llama.cpp CLI flags for this profile."""
        return [
            "-c", str(self.context_size),
            "-n", str(self.max_tokens),
            "-ngl", str(self.gpu_layers),
            "--temp", str(self.temp),
            "--top-p", str(self.top_p),
            "--top-k", str(self.top_k),
            "--min-p", str(self.min_p),
            "--repeat-penalty", str(self.repeat_penalty),
        ]


@dataclass
class ModelRunConfig:
    """All run profiles for a single model file, plus which is the default."""
    model_name: str
    default_profile: str = "daily"
    profiles: dict[str, ModelRunProfile] = field(default_factory=dict)
    extra_args: list[str] = field(default_factory=list)
    """Model-level extra CLI flags appended after every profile's args.
    Examples: ['--jinja'] for Qwen chat-template models."""

    def get_default(self) -> ModelRunProfile | None:
        return self.profiles.get(self.default_profile)


@dataclass
class ModelEntity:
    name: str
    size_bytes: int | None = None
    framework: str | None = None
    full_path: str | None = None   # absolute path on disk (llama.cpp only)


@dataclass
class LlmFrameworkEntity:
    name: str          # "ollama" | "llama.cpp" | "vllm"
    port: int
    is_running: bool = False
    is_installed: bool = False
    active_model: str | None = None
    available_models: list[ModelEntity] = field(default_factory=list)
    vram_limit_mb: int | None = None
    model_dir: str | None = None  # custom model storage directory

    def set_port(self, new_port: int) -> None:
        if not (1024 <= new_port <= 65535):
            raise ValueError(f"Invalid port: {new_port}")
        self.port = new_port
