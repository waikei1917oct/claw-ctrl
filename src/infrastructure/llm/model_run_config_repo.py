from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

from entities.llm import ModelRunConfig, ModelRunProfile

_STORE_PATH = Path.home() / ".claw-ctrl" / "model_run_configs.json"


class ModelRunConfigRepo:
    """Persists per-model run profiles to ~/.claw-ctrl/model_run_configs.json."""

    def load_all(self) -> dict[str, ModelRunConfig]:
        """Return all stored configs keyed by model name."""
        if not _STORE_PATH.exists():
            return {}
        try:
            raw = json.loads(_STORE_PATH.read_text())
        except Exception:
            return {}
        result: dict[str, ModelRunConfig] = {}
        for model_name, cfg in raw.items():
            profiles: dict[str, ModelRunProfile] = {}
            for pname, pdata in cfg.get("profiles", {}).items():
                profiles[pname] = ModelRunProfile(
                    name=pname,
                    context_size=pdata.get("context_size", 65536),
                    max_tokens=pdata.get("max_tokens", 1024),
                    gpu_layers=pdata.get("gpu_layers", 80),
                    temp=pdata.get("temp", 0.7),
                    top_p=pdata.get("top_p", 0.95),
                    top_k=pdata.get("top_k", 40),
                    min_p=pdata.get("min_p", 0.05),
                    repeat_penalty=pdata.get("repeat_penalty", 1.05),
                )
            result[model_name] = ModelRunConfig(
                model_name=model_name,
                default_profile=cfg.get("default_profile", "daily"),
                profiles=profiles,
                extra_args=cfg.get("extra_args", []),
            )
        return result

    def get(self, model_name: str) -> ModelRunConfig | None:
        return self.load_all().get(model_name)

    def save(self, config: ModelRunConfig) -> None:
        all_configs = self.load_all()
        all_configs[config.model_name] = config
        self._write(all_configs)

    def delete(self, model_name: str) -> None:
        all_configs = self.load_all()
        all_configs.pop(model_name, None)
        self._write(all_configs)

    def upsert_profile(self, model_name: str, profile: ModelRunProfile) -> ModelRunConfig:
        """Add or replace a profile for a model. Creates ModelRunConfig if not present."""
        cfg = self.get(model_name) or ModelRunConfig(model_name=model_name)
        cfg.profiles[profile.name] = profile
        self.save(cfg)
        return cfg

    def delete_profile(self, model_name: str, profile_name: str) -> None:
        cfg = self.get(model_name)
        if cfg:
            cfg.profiles.pop(profile_name, None)
            if cfg.default_profile == profile_name:
                cfg.default_profile = next(iter(cfg.profiles), "daily")
            self.save(cfg)

    def set_default_profile(self, model_name: str, profile_name: str) -> None:
        cfg = self.get(model_name)
        if cfg and profile_name in cfg.profiles:
            cfg.default_profile = profile_name
            self.save(cfg)

    def set_extra_args(self, model_name: str, args: list[str]) -> None:
        """Replace the model-level extra_args list."""
        cfg = self.get(model_name) or ModelRunConfig(model_name=model_name)
        cfg.extra_args = args
        self.save(cfg)

    def _write(self, all_configs: dict[str, ModelRunConfig]) -> None:
        _STORE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data: dict = {}
        for model_name, cfg in all_configs.items():
            data[model_name] = {
                "default_profile": cfg.default_profile,
                "extra_args": cfg.extra_args,
                "profiles": {
                    pname: {
                        "context_size": p.context_size,
                        "max_tokens": p.max_tokens,
                        "gpu_layers": p.gpu_layers,
                        "temp": p.temp,
                        "top_p": p.top_p,
                        "top_k": p.top_k,
                        "min_p": p.min_p,
                        "repeat_penalty": p.repeat_penalty,
                    }
                    for pname, p in cfg.profiles.items()
                },
            }
        with tempfile.NamedTemporaryFile(
            "w", dir=_STORE_PATH.parent, delete=False, suffix=".tmp"
        ) as f:
            json.dump(data, f, indent=2)
            tmp = f.name
        os.replace(tmp, _STORE_PATH)
