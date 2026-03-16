from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GatewayEntity:
    host: str | None = None
    port: int | None = None
    mode: str | None = None

    @property
    def endpoint(self) -> str | None:
        if self.host and self.port:
            return f"{self.host}:{self.port}"
        return None


@dataclass
class WorkspaceEntity:
    name: str
    path: Path


@dataclass
class OpenClawEntity:
    name: str
    version: str
    install_path: Path
    config_path: str
    gateway: GatewayEntity = field(default_factory=GatewayEntity)
    workspaces: list[WorkspaceEntity] = field(default_factory=list)
    online: bool = False
    pid: int | None = None
    cpu_percent: float | None = None
    mem_mb: float | None = None
    recent_error: str | None = None
    last_restart_time: str | None = None
