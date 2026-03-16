from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class PersonaFiles:
    agents_md: str | None = None
    heartbeat_md: str | None = None
    identity_md: str | None = None
    memory_md: str | None = None
    soul_md: str | None = None
    tools_md: str | None = None
    user_md: str | None = None


@dataclass
class ChannelEntity:
    name: str
    channel_type: str
    config: dict = field(default_factory=dict)


@dataclass
class AgentEntity:
    name: str
    workspace_path: Path
    default_model: str | None = None
    auth_profiles: list[str] = field(default_factory=list)
    persona: PersonaFiles = field(default_factory=PersonaFiles)
    channels: list[ChannelEntity] = field(default_factory=list)
