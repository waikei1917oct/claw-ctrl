from __future__ import annotations

import json
from pathlib import Path

from textual.app import App, ComposeResult
from textual.screen import Screen
from textual.widgets import (
    Header,
    Footer,
    Label,
    Button,
    ListView,
    ListItem,
    Static,
    DataTable,
    Input,
)
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.binding import Binding
from textual import events

# ---------------------------------------------------------------------------
# Lazy imports for infrastructure (may not be available in some test contexts)
# ---------------------------------------------------------------------------

def _load_openclaw_data():
    try:
        from infrastructure.openclaw.config_repo import OpenClawConfigRepo
        from infrastructure.openclaw.adapter import OpenClawAdapter
        repo = OpenClawConfigRepo()
        adapter = OpenClawAdapter()
        entity = repo.load()
        adapter.refresh(entity)
        return entity, repo
    except Exception:
        return None, None


def _load_llm_data():
    try:
        from infrastructure.llm.hub import LlmFrameworkHub
        hub = LlmFrameworkHub()
        hub.refresh_all()
        return hub
    except Exception:
        return None


def _load_service_data():
    try:
        from infrastructure.system.service import ClawCtrlService
        return ClawCtrlService()
    except Exception:
        return None


def _load_agents(repo) -> list:
    try:
        agent_names = repo.list_agents() if repo else []
        agents_dir = Path.home() / ".openclaw" / "agents"
        workspace_dir = Path.home() / ".openclaw" / "workspace"
        agents = []
        for name in agent_names:
            agent_path = agents_dir / name
            agent_cfg_dir = agent_path / "agent"
            agent_data = {"name": name, "path": agent_path}

            # Default model — from agent/models.json providers
            default_model = "N/A"
            models_file = agent_cfg_dir / "models.json"
            if models_file.exists():
                try:
                    raw = json.loads(models_file.read_text())
                    providers = raw.get("providers", {})
                    for provider_name, pdata in providers.items():
                        models = pdata.get("models", [])
                        if models:
                            default_model = f"{models[0]['id']} ({provider_name})"
                            break
                except Exception:
                    pass
            agent_data["default_model"] = default_model

            # Auth profiles — keys from agent/auth-profiles.json
            auth_profiles = []
            auth_profiles_file = agent_cfg_dir / "auth-profiles.json"
            if auth_profiles_file.exists():
                try:
                    raw = json.loads(auth_profiles_file.read_text())
                    auth_profiles = list(raw.get("profiles", {}).keys())
                except Exception:
                    pass
            agent_data["auth_profiles"] = auth_profiles

            # Persona files — in ~/.openclaw/workspace/ (uppercase)
            persona_files = {}
            for md_name in ("AGENTS.md", "HEARTBEAT.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "TOOLS.md", "USER.md"):
                # per-agent workspace subdir takes priority, fallback to shared workspace
                per_agent = workspace_dir / name / md_name
                shared = workspace_dir / md_name
                if per_agent.exists():
                    persona_files[md_name] = str(per_agent)
                elif shared.exists():
                    persona_files[md_name] = str(shared)
            agent_data["persona_files"] = persona_files

            # Channels — from global openclaw.json
            try:
                agent_data["channels"] = repo.list_channels() if repo else []
            except Exception:
                agent_data["channels"] = []

            # Current model and available models
            try:
                agent_data["current_model"] = repo.get_agent_current_model(name) if repo else None
                agent_data["available_models"] = repo.list_available_models() if repo else []
            except Exception:
                agent_data["current_model"] = None
                agent_data["available_models"] = []

            agents.append(agent_data)
        return agents
    except Exception:
        return []


def _fmt_bytes(n: int | None) -> str:
    if n is None:
        return "N/A"
    if n >= 1_073_741_824:
        return f"{n / 1_073_741_824:.1f} GB"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n} B"


# ---------------------------------------------------------------------------
# Model Select Screen
# ---------------------------------------------------------------------------

class ModelSelectScreen(Screen):
    BINDINGS = [Binding("q", "pop_screen", "Cancel")]

    def __init__(self, agent_name: str, current_model: str | None, available_models: list[str], repo) -> None:
        super().__init__()
        self._agent_name = agent_name
        self._current = current_model
        self._models = available_models
        self._repo = repo

    def compose(self) -> ComposeResult:
        items = [
            ListItem(Label(m + (" <- current" if m == self._current else "")))
            for m in self._models
        ]
        yield Header(show_clock=True)
        yield Footer()
        with Vertical():
            yield Static(f"[bold cyan]Select Model — {self._agent_name}[/bold cyan]", classes="section-title")
            yield Static(f"Current: {self._current or 'N/A'}", classes="info-line")
            yield Static("Press Enter to apply, q to cancel", classes="info-line-dim")
            yield ListView(*items, id="model-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._models):
            selected = self._models[idx]
            try:
                self._repo.set_agent_model(self._agent_name, selected)
                self.notify(f"Model updated to: {selected}", severity="information")
                self.notify("Restart openclaw to apply changes.", severity="warning")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
            self.app.pop_screen()

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Auth Profile Screen
# ---------------------------------------------------------------------------

class AuthProfileScreen(Screen):
    BINDINGS = [Binding("q", "pop_screen", "Back")]

    def __init__(self, agent_name: str) -> None:
        super().__init__()
        self._agent_name = agent_name
        # Load data eagerly in __init__ so compose can pre-populate ListView
        self._profile_entries: list[tuple[str, str]] = []
        self._info_text = "No auth profiles found."
        self._agent_repo = None
        try:
            from infrastructure.openclaw.agent_repo import AgentRepo
            self._agent_repo = AgentRepo()
            data = self._agent_repo.get_auth_profiles(agent_name)
            self._last_good = data.get("lastGood", {})
            self._grouped = data.get("grouped", {})
            lines = ["Active profiles:"]
            for p, k in self._last_good.items():
                lines.append(f"  {p}: {k}")
            self._info_text = "\n".join(lines)
            for provider, keys in self._grouped.items():
                for key in keys:
                    self._profile_entries.append((provider, key))
        except Exception as e:
            self._info_text = f"Error loading profiles: {e}"
            self._last_good = {}
            self._grouped = {}

    def compose(self) -> ComposeResult:
        items = [
            ListItem(Label(
                key + (" (active)" if self._last_good.get(provider) == key else "")
            ))
            for provider, key in self._profile_entries
        ]
        yield Header(show_clock=True)
        yield Footer()
        with Vertical():
            yield Static(f"Auth Profiles — {self._agent_name}", classes="section-title")
            yield Static("Select a profile to set it as active for that provider.", classes="info-line-dim")
            yield Static(self._info_text, id="auth-info")
            yield ListView(*items, id="auth-list")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or not self._profile_entries or self._agent_repo is None:
            return
        provider, profile_key = self._profile_entries[idx]
        try:
            self._agent_repo.set_last_good(self._agent_name, provider, profile_key)
            self.notify(f"{provider} -> {profile_key}", severity="information")
            # Re-push screen to reflect new state (re-populate cleanly)
            self.app.pop_screen()
            self.app.push_screen(AuthProfileScreen(self._agent_name))
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# New Agent Screen
# ---------------------------------------------------------------------------

class NewAgentScreen(Screen):
    BINDINGS = [Binding("escape", "pop_screen", "Cancel")]

    def __init__(self, available_models: list[str], repo) -> None:
        super().__init__()
        self._models = available_models
        self._repo = repo
        self._selected_model: str | None = available_models[0] if available_models else None

    def compose(self) -> ComposeResult:
        items = [ListItem(Label(m)) for m in self._models]
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="new-agent-layout"):
            yield Static("Create New Agent", classes="section-title")
            yield Static("Agent Name:", classes="info-line")
            yield Input(placeholder="e.g. assistant", id="agent-name-input")
            yield Static("Select Model:", classes="info-line")
            yield ListView(*items, id="new-agent-model-list")
            with Horizontal(classes="actions-bar"):
                yield Button("Create", id="btn_create", variant="success")
                yield Button("Cancel", id="btn_cancel", variant="default")

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._models):
            self._selected_model = self._models[idx]

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "btn_cancel":
            self.app.pop_screen()
        elif event.button.id == "btn_create":
            self._do_create()

    def _do_create(self) -> None:
        name = self.query_one("#agent-name-input", Input).value.strip()
        if not name:
            self.notify("Agent name cannot be empty.", severity="error")
            return
        if not self._selected_model:
            self.notify("Please select a model.", severity="error")
            return
        try:
            self._repo.create_agent(name, self._selected_model)
            self.notify(f"Agent '{name}' created. Restart openclaw to activate.", severity="information")
            self.app.pop_screen()
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Agent Detail Screen
# ---------------------------------------------------------------------------

class AgentDetailScreen(Screen):
    BINDINGS = [
        Binding("q", "pop_screen", "Back"),
        Binding("m", "change_model", "Change Model"),
        Binding("a", "auth_profiles", "Auth Profiles"),
    ]

    def __init__(self, agent_data: dict) -> None:
        super().__init__()
        self._agent = agent_data

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        a = self._agent
        with ScrollableContainer():
            yield Static(f"[bold cyan]Agent: {a['name']}[/bold cyan]", classes="section-title")
            yield Static(f"Path: {a['path']}", classes="info-line")
            yield Static(f"Default Model: {a.get('default_model', 'N/A')}", classes="info-line")
            yield Static(f"Current Model: {a.get('current_model') or 'N/A'}", classes="info-line")

            auth = a.get("auth_profiles", [])
            if auth:
                yield Static(f"Auth Profiles: {', '.join(auth)}", classes="info-line")

            yield Static("", classes="spacer")
            yield Static("[bold]Persona Files[/bold]", classes="section-title")
            persona = a.get("persona_files", {})
            if persona:
                for fname, fpath in persona.items():
                    yield Static(f"  [green]✓[/green] {fname}", classes="info-line")
            else:
                yield Static("  No persona files found", classes="info-line-dim")

            # Channels section
            yield Static("", classes="spacer")
            yield Static("[bold]Channels[/bold]", classes="section-title")
            channels = a.get("channels", [])
            if channels:
                for ch in channels:
                    enabled = "[green]enabled[/green]" if ch.get("enabled") else "[red]disabled[/red]"
                    yield Static(f"  • {ch['name']} ({enabled}) — {ch.get('account_count', 0)} account(s): {', '.join(ch.get('accounts', []))}", classes="info-line")
            else:
                yield Static("  No channels configured", classes="info-line-dim")

            # Actions
            yield Static("", classes="spacer")
            with Horizontal(classes="actions-bar"):
                yield Button("Change Model (m)", id="btn_change_model", variant="primary")
                yield Button("Auth Profiles (a)", id="btn_auth_profiles", variant="warning")
                yield Button("Back (q)", id="btn_back", variant="default")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_back":
            self.app.pop_screen()
        elif bid == "btn_change_model":
            from infrastructure.openclaw.config_repo import OpenClawConfigRepo
            repo = OpenClawConfigRepo()
            self.app.push_screen(ModelSelectScreen(
                self._agent["name"],
                self._agent.get("current_model"),
                self._agent.get("available_models", []),
                repo,
            ))
        elif bid == "btn_auth_profiles":
            self.app.push_screen(AuthProfileScreen(self._agent["name"]))

    def action_change_model(self) -> None:
        from infrastructure.openclaw.config_repo import OpenClawConfigRepo
        repo = OpenClawConfigRepo()
        self.app.push_screen(ModelSelectScreen(
            self._agent["name"],
            self._agent.get("current_model"),
            self._agent.get("available_models", []),
            repo,
        ))

    def action_auth_profiles(self) -> None:
        self.app.push_screen(AuthProfileScreen(self._agent["name"]))

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# OpenClaw Screen
# ---------------------------------------------------------------------------

class OpenClawScreen(Screen):
    BINDINGS = [
        Binding("s", "start_openclaw", "Start"),
        Binding("t", "stop_openclaw", "Stop"),
        Binding("r", "restart_openclaw", "Restart"),
        Binding("n", "new_agent", "New Agent"),
        Binding("x", "delete_agent", "Delete Agent"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._entity = None
        self._repo = None
        self._agents: list[dict] = []

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Horizontal(id="openclaw-layout"):
            with Vertical(id="oc-info-panel", classes="panel"):
                yield Static("[bold cyan]OpenClaw Instance[/bold cyan]", classes="panel-title")
                yield Static("Loading...", id="oc-info-content")
            with Vertical(id="oc-agents-panel", classes="panel"):
                yield Static("[bold cyan]Agents[/bold cyan]", classes="panel-title")
                yield ListView(id="agents-list")
        with Horizontal(id="oc-actions", classes="actions-bar"):
            yield Button("Start (s)", id="btn_oc_start", variant="success")
            yield Button("Stop (t)", id="btn_oc_stop", variant="error")
            yield Button("Restart (r)", id="btn_oc_restart", variant="warning")
            yield Button("New Agent (n)", id="btn_oc_new_agent", variant="primary")
            yield Button("Delete Agent (x)", id="btn_oc_del_agent", variant="error")
            yield Button("Back (q)", id="btn_oc_back", variant="default")

    def on_mount(self) -> None:
        self._entity, self._repo = _load_openclaw_data()
        self._agents = _load_agents(self._repo)
        self._refresh_info()
        self._refresh_agents()
        self.set_interval(10, self._live_refresh)

    def _live_refresh(self) -> None:
        """Reload live data from system and update display."""
        self._entity, self._repo = _load_openclaw_data()
        self._agents = _load_agents(self._repo)
        self._refresh_info()
        self._refresh_agents()

    def _refresh_info(self) -> None:
        info = self.query_one("#oc-info-content", Static)
        e = self._entity
        if e is None:
            info.update("[red]Failed to load openclaw data[/red]")
            return
        status_color = "green" if e.online else "red"
        status_text = "ONLINE" if e.online else "OFFLINE"
        lines = [
            f"[bold]Status:[/bold] [{status_color}]{status_text}[/{status_color}]",
            f"[bold]Version:[/bold] {e.version}",
            f"[bold]Install Path:[/bold] {e.install_path}",
            f"[bold]Config:[/bold] {e.config_path}",
            f"[bold]Gateway:[/bold] {e.gateway.endpoint or 'N/A'}",
            f"[bold]Mode:[/bold] {e.gateway.mode or 'N/A'}",
            f"[bold]PID:[/bold] {e.pid or 'N/A'}",
            f"[bold]CPU:[/bold] {f'{e.cpu_percent:.1f}%' if e.cpu_percent is not None else 'N/A'}",
            f"[bold]Mem:[/bold] {f'{e.mem_mb:.1f} MB' if e.mem_mb is not None else 'N/A'}",
        ]
        if e.last_restart_time:
            lines.append(f"[bold]Last Restart:[/bold] {e.last_restart_time}")
        if e.recent_error:
            lines.append(f"[bold red]Error:[/bold red] {e.recent_error}")

        from datetime import datetime, timezone
        lines.append(f"\n[dim]Live refresh every 10s — last: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC[/dim]")

        if e.workspaces:
            lines.append("")
            lines.append("[bold]Workspaces:[/bold]")
            for ws in e.workspaces:
                lines.append(f"  • {ws.name} → {ws.path}")

        info.update("\n".join(lines))

    def _refresh_agents(self) -> None:
        lv = self.query_one("#agents-list", ListView)
        lv.clear()
        if not self._agents:
            lv.append(ListItem(Label("No agents found")))
        else:
            for agent in self._agents:
                lv.append(ListItem(Label(agent["name"])))

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._agents):
            self.app.push_screen(AgentDetailScreen(self._agents[idx]))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_oc_start":
            self.action_start_openclaw()
        elif bid == "btn_oc_stop":
            self.action_stop_openclaw()
        elif bid == "btn_oc_restart":
            self.action_restart_openclaw()
        elif bid == "btn_oc_new_agent":
            self.action_new_agent()
        elif bid == "btn_oc_del_agent":
            self.action_delete_agent()
        elif bid == "btn_oc_back":
            self.app.pop_screen()

    def action_start_openclaw(self) -> None:
        if self._entity is None:
            self.notify("No openclaw entity loaded", severity="error")
            return
        try:
            from infrastructure.openclaw.adapter import OpenClawAdapter
            adapter = OpenClawAdapter()
            self._entity = adapter.start(self._entity)
            self._refresh_info()
            self.notify("OpenClaw start command sent", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_stop_openclaw(self) -> None:
        if self._entity is None:
            self.notify("No openclaw entity loaded", severity="error")
            return
        try:
            from infrastructure.openclaw.adapter import OpenClawAdapter
            adapter = OpenClawAdapter()
            self._entity = adapter.stop(self._entity)
            self._refresh_info()
            self.notify("OpenClaw stop command sent", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_restart_openclaw(self) -> None:
        if self._entity is None:
            self.notify("No openclaw entity loaded", severity="error")
            return
        try:
            from infrastructure.openclaw.adapter import OpenClawAdapter
            adapter = OpenClawAdapter()
            self._entity = adapter.restart(self._entity)
            self._refresh_info()
            self.notify("OpenClaw restart command sent", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_new_agent(self) -> None:
        if self._repo is None:
            self.notify("Repository not available", severity="error")
            return
        models = self._repo.list_available_models()
        self.app.push_screen(NewAgentScreen(models, self._repo))

    def action_delete_agent(self) -> None:
        lv = self.query_one("#agents-list", ListView)
        idx = lv.index
        if idx is None or not self._agents or idx >= len(self._agents):
            self.notify("No agent selected", severity="warning")
            return
        agent = self._agents[idx]
        name = agent["name"]
        if self._repo:
            try:
                self._repo.delete_agent(name)
                self.notify(f"Agent '{name}' removed from config. Files kept. Restart openclaw to apply.", severity="information")
                self._live_refresh()
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Framework Detail Screen
# ---------------------------------------------------------------------------

class FrameworkDetailScreen(Screen):
    BINDINGS = [
        Binding("p", "pull_model", "Pull"),
        Binding("d", "delete_model", "Delete"),
        Binding("c", "change_port", "Change Port"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, hub, framework_name: str) -> None:
        super().__init__()
        self._hub = hub
        self._framework_name = framework_name
        self._input_mode: str | None = None  # "pull", "port"

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="fw-detail-layout"):
            yield Static("", id="fw-detail-info", classes="panel")
            yield Static("[bold]Available Models[/bold]", classes="section-title")
            yield DataTable(id="models-table")
            yield Input(placeholder="Enter value...", id="fw-input", classes="hidden")
        with Horizontal(id="fw-actions", classes="actions-bar"):
            yield Button("Pull Model (p)", id="btn_pull", variant="primary")
            yield Button("Delete Model (d)", id="btn_delete", variant="error")
            yield Button("Change Port (c)", id="btn_port", variant="warning")
            yield Button("Back (q)", id="btn_fw_back", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Name", "Size", "Framework")
        self._refresh()

    def _refresh(self) -> None:
        if self._hub is None:
            return
        entity = self._hub.get(self._framework_name)
        if entity is None:
            return

        info = self.query_one("#fw-detail-info", Static)
        status_color = "green" if entity.is_running else "red"
        status_text = "RUNNING" if entity.is_running else "STOPPED"
        inst_color = "green" if entity.is_installed else "yellow"
        inst_text = "YES" if entity.is_installed else "NO"
        lines = [
            f"[bold]Framework:[/bold] {entity.name}",
            f"[bold]Status:[/bold] [{status_color}]{status_text}[/{status_color}]",
            f"[bold]Installed:[/bold] [{inst_color}]{inst_text}[/{inst_color}]",
            f"[bold]Port:[/bold] {entity.port}",
            f"[bold]Active Model:[/bold] {entity.active_model or 'None'}",
            f"[bold]Models:[/bold] {len(entity.available_models)}",
        ]
        info.update("\n".join(lines))

        table = self.query_one("#models-table", DataTable)
        table.clear()
        for model in entity.available_models:
            table.add_row(model.name, _fmt_bytes(model.size_bytes), model.framework or "")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_pull":
            self.action_pull_model()
        elif bid == "btn_delete":
            self.action_delete_model()
        elif bid == "btn_port":
            self.action_change_port()
        elif bid == "btn_fw_back":
            self.app.pop_screen()

    def action_pull_model(self) -> None:
        self._input_mode = "pull"
        inp = self.query_one("#fw-input", Input)
        inp.placeholder = "Model name to pull (e.g. llama3:8b)"
        inp.remove_class("hidden")
        inp.focus()

    def action_delete_model(self) -> None:
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity and entity.available_models:
            table = self.query_one("#models-table", DataTable)
            cursor_row = table.cursor_row
            if 0 <= cursor_row < len(entity.available_models):
                model_name = entity.available_models[cursor_row].name
                self._do_delete(model_name)

    def action_change_port(self) -> None:
        self._input_mode = "port"
        inp = self.query_one("#fw-input", Input)
        inp.placeholder = "New port number"
        inp.remove_class("hidden")
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        inp = self.query_one("#fw-input", Input)
        inp.add_class("hidden")
        inp.value = ""

        if not value:
            self._input_mode = None
            return

        mode = self._input_mode
        self._input_mode = None

        if mode == "pull":
            self._do_pull(value)
        elif mode == "port":
            try:
                new_port = int(value)
                entity = self._hub.get(self._framework_name) if self._hub else None
                if entity:
                    entity.set_port(new_port)
                    self.notify(f"Port changed to {new_port}", severity="information")
                    self._refresh()
            except ValueError:
                self.notify("Invalid port number", severity="error")

    def _do_pull(self, name: str) -> None:
        try:
            if self._framework_name == "ollama":
                from infrastructure.llm.ollama import OllamaAdapter
                adapter = OllamaAdapter()
                self.notify(f"Pulling {name}...", severity="information")
                adapter.pull_model(name)
                if self._hub:
                    self._hub.refresh_all()
                self._refresh()
                self.notify(f"Pulled {name}", severity="information")
            else:
                self.notify("Pull not supported for this framework", severity="warning")
        except Exception as e:
            self.notify(f"Error pulling model: {e}", severity="error")

    def _do_delete(self, name: str) -> None:
        try:
            if self._framework_name == "ollama":
                from infrastructure.llm.ollama import OllamaAdapter
                adapter = OllamaAdapter()
                adapter.delete_model(name)
                if self._hub:
                    self._hub.refresh_all()
                self._refresh()
                self.notify(f"Deleted {name}", severity="information")
            else:
                self.notify("Delete not supported for this framework", severity="warning")
        except Exception as e:
            self.notify(f"Error deleting model: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# LLM Screen
# ---------------------------------------------------------------------------

class LlmScreen(Screen):
    BINDINGS = [
        Binding("s", "start_framework", "Start"),
        Binding("t", "stop_framework", "Stop"),
        Binding("r", "refresh_frameworks", "Refresh"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._hub = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="llm-layout"):
            yield Static("[bold cyan]LLM Frameworks[/bold cyan]", classes="panel-title")
            yield DataTable(id="frameworks-table")
        with Horizontal(id="llm-actions", classes="actions-bar"):
            yield Button("Start (s)", id="btn_llm_start", variant="success")
            yield Button("Stop (t)", id="btn_llm_stop", variant="error")
            yield Button("Refresh (r)", id="btn_llm_refresh", variant="primary")
            yield Button("Back (q)", id="btn_llm_back", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#frameworks-table", DataTable)
        table.cursor_type = "row"
        table.add_columns("Name", "Installed", "Running", "Port", "Active Model", "Models")
        self._hub = _load_llm_data()
        self._refresh()
        self.set_interval(10, self._live_refresh)

    def _live_refresh(self) -> None:
        """Reload live data from system and update display."""
        self._hub = _load_llm_data()
        self._refresh()

    def _refresh(self) -> None:
        from datetime import datetime, timezone
        table = self.query_one("#frameworks-table", DataTable)
        table.clear()
        if self._hub is None:
            return
        for entity in self._hub.list_all():
            installed = "[green]Yes[/green]" if entity.is_installed else "[yellow]No[/yellow]"
            running = "[green]Running[/green]" if entity.is_running else "[red]Stopped[/red]"
            table.add_row(
                entity.name,
                installed,
                running,
                str(entity.port),
                entity.active_model or "-",
                str(len(entity.available_models)),
            )
        ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
        self.sub_title = f"Live refresh every 10s — last: {ts} UTC"

    def _get_selected_framework(self) -> str | None:
        table = self.query_one("#frameworks-table", DataTable)
        row = table.cursor_row
        if self._hub is None:
            return None
        frameworks = self._hub.list_all()
        if 0 <= row < len(frameworks):
            return frameworks[row].name
        return None

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        name = self._get_selected_framework()
        if name and self._hub:
            self.app.push_screen(FrameworkDetailScreen(self._hub, name))

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_llm_start":
            self.action_start_framework()
        elif bid == "btn_llm_stop":
            self.action_stop_framework()
        elif bid == "btn_llm_refresh":
            self.action_refresh_frameworks()
        elif bid == "btn_llm_back":
            self.app.pop_screen()

    def action_start_framework(self) -> None:
        name = self._get_selected_framework()
        if not name or not self._hub:
            self.notify("No framework selected", severity="warning")
            return
        try:
            self._hub.start(name)
            self._refresh()
            self.notify(f"Starting {name}...", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_stop_framework(self) -> None:
        name = self._get_selected_framework()
        if not name or not self._hub:
            self.notify("No framework selected", severity="warning")
            return
        try:
            self._hub.stop(name)
            self._refresh()
            self.notify(f"Stopped {name}", severity="information")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_refresh_frameworks(self) -> None:
        if self._hub:
            self._hub.refresh_all()
            self._refresh()
            self.notify("Refreshed", severity="information")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Self Service Screen
# ---------------------------------------------------------------------------

class SelfServiceScreen(Screen):
    BINDINGS = [
        Binding("i", "install_service", "Install"),
        Binding("u", "uninstall_service", "Uninstall"),
        Binding("s", "start_service", "Start"),
        Binding("t", "stop_service", "Stop"),
        Binding("r", "restart_service", "Restart"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._svc = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="svc-layout"):
            yield Static("[bold cyan]claw-ctrl Service[/bold cyan]", classes="panel-title")
            yield Static("Loading...", id="svc-info", classes="panel")
        with Horizontal(id="svc-actions", classes="actions-bar"):
            yield Button("Install (i)", id="btn_svc_install", variant="success")
            yield Button("Uninstall (u)", id="btn_svc_uninstall", variant="error")
            yield Button("Start (s)", id="btn_svc_start", variant="primary")
            yield Button("Stop (t)", id="btn_svc_stop", variant="warning")
            yield Button("Restart (r)", id="btn_svc_restart", variant="warning")
            yield Button("Back (q)", id="btn_svc_back", variant="default")

    def on_mount(self) -> None:
        self._svc = _load_service_data()
        self._refresh()

    def _refresh(self) -> None:
        info = self.query_one("#svc-info", Static)
        if self._svc is None:
            info.update("[red]Service manager unavailable[/red]")
            return
        try:
            installed = self._svc.is_installed()
            status = self._svc.status() if installed else {}
            inst_color = "green" if installed else "yellow"
            inst_text = "YES" if installed else "NO"
            active = status.get("active", False)
            enabled = status.get("enabled", False)
            active_color = "green" if active else "red"
            active_text = "ACTIVE" if active else "INACTIVE"
            enabled_color = "green" if enabled else "yellow"
            enabled_text = "ENABLED" if enabled else "DISABLED"
            lines = [
                f"[bold]Service:[/bold] {self._svc.SERVICE_NAME}",
                f"[bold]Installed:[/bold] [{inst_color}]{inst_text}[/{inst_color}]",
                f"[bold]Status:[/bold] [{active_color}]{active_text}[/{active_color}]",
                f"[bold]Boot:[/bold] [{enabled_color}]{enabled_text}[/{enabled_color}]",
            ]
            since = status.get("since")
            if since:
                lines.append(f"[bold]Since:[/bold] {since}")
            pid = status.get("pid")
            if pid:
                lines.append(f"[bold]PID:[/bold] {pid}")
            lines.append("")
            lines.append(f"[bold]Service File:[/bold] {self._svc.SERVICE_PATH}")
            lines.append(f"[bold]Exec:[/bold] {self._svc.EXEC_PATH}")
            info.update("\n".join(lines))
        except Exception as e:
            info.update(f"[red]Error loading service status: {e}[/red]")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        actions = {
            "btn_svc_install": self.action_install_service,
            "btn_svc_uninstall": self.action_uninstall_service,
            "btn_svc_start": self.action_start_service,
            "btn_svc_stop": self.action_stop_service,
            "btn_svc_restart": self.action_restart_service,
            "btn_svc_back": self.app.pop_screen,
        }
        fn = actions.get(bid)
        if fn:
            fn()

    def action_install_service(self) -> None:
        if self._svc:
            if self._svc.is_installed():
                self.notify("Service is already installed. Use Start/Restart to control it.", severity="warning")
                return
            try:
                self._svc.install()
                self._refresh()
                self.notify("Service installed and enabled", severity="information")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_uninstall_service(self) -> None:
        if self._svc:
            try:
                self._svc.uninstall()
                self._refresh()
                self.notify("Service uninstalled", severity="information")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_start_service(self) -> None:
        if self._svc:
            try:
                self._svc.start()
                self._refresh()
                self.notify("Service started", severity="information")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_stop_service(self) -> None:
        if self._svc:
            try:
                self._svc.stop()
                self._refresh()
                self.notify("Service stopped", severity="information")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_restart_service(self) -> None:
        if self._svc:
            try:
                self._svc.restart()
                self._refresh()
                self.notify("Service restarted", severity="information")
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Main Menu Screen
# ---------------------------------------------------------------------------

class MainMenuScreen(Screen):
    BINDINGS = [Binding("q", "quit_app", "Quit")]

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="main-menu"):
            yield Static("[bold cyan]claw-ctrl[/bold cyan]", id="app-title")
            yield Static("OpenClaw & LLM Management Console", id="app-subtitle")
            yield Static("", classes="spacer")
            yield ListView(
                ListItem(Label("  OpenClaw Management"), id="menu_openclaw"),
                ListItem(Label("  LLM Management"), id="menu_llm"),
                ListItem(Label("  Self Service"), id="menu_service"),
                id="main-list",
            )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        item_id = event.item.id
        if item_id == "menu_openclaw":
            self.app.push_screen(OpenClawScreen())
        elif item_id == "menu_llm":
            self.app.push_screen(LlmScreen())
        elif item_id == "menu_service":
            self.app.push_screen(SelfServiceScreen())

    def action_quit_app(self) -> None:
        self.app.exit()


# ---------------------------------------------------------------------------
# Main App
# ---------------------------------------------------------------------------

class ClawCtrlApp(App):
    TITLE = "claw-ctrl"
    SUB_TITLE = "OpenClaw & LLM Management"

    DEFAULT_CSS = """
    Screen {
        background: $surface;
    }

    #main-menu {
        align: center middle;
        height: 100%;
        width: 100%;
    }

    #app-title {
        text-align: center;
        text-style: bold;
        color: $accent;
        padding: 1 0;
        height: 3;
        content-align: center middle;
        text-align: center;
        width: 100%;
        text-style: bold;
    }

    #app-subtitle {
        text-align: center;
        color: $text-muted;
        width: 100%;
        content-align: center middle;
        height: 1;
        padding-bottom: 1;
    }

    #main-list {
        width: 50;
        min-width: 40;
        border: solid $accent;
        padding: 1 2;
        height: auto;
        margin: 1 0;
    }

    #main-list > ListItem {
        padding: 1 2;
        height: 3;
    }

    #main-list > ListItem:hover,
    #main-list > ListItem.--highlight {
        background: $accent 20%;
        color: $accent;
    }

    .panel {
        border: solid $primary;
        padding: 1 2;
        margin: 0 1;
        height: 1fr;
    }

    .panel-title {
        text-style: bold;
        color: $accent;
        padding: 0 1;
        height: 2;
        content-align: left middle;
    }

    .section-title {
        text-style: bold;
        color: $primary;
        padding: 0 1;
        height: 2;
        content-align: left middle;
    }

    .info-line {
        padding: 0 2;
        height: 1;
    }

    .info-line-dim {
        padding: 0 2;
        height: 1;
        color: $text-muted;
    }

    .spacer {
        height: 1;
    }

    .actions-bar {
        height: 3;
        align: center middle;
        padding: 0 1;
        dock: bottom;
        background: $surface-darken-1;
    }

    .actions-bar Button {
        margin: 0 1;
        min-width: 12;
    }

    #openclaw-layout {
        height: 1fr;
        width: 100%;
    }

    #oc-info-panel {
        width: 1fr;
    }

    #oc-agents-panel {
        width: 1fr;
    }

    #oc-info-content {
        padding: 0 1;
    }

    #llm-layout {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    #frameworks-table {
        height: 1fr;
        border: solid $primary;
    }

    #fw-detail-layout {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    #fw-detail-info {
        height: auto;
        max-height: 12;
    }

    #models-table {
        height: 1fr;
        border: solid $primary;
    }

    #fw-input {
        margin: 1 2;
    }

    .hidden {
        display: none;
    }

    #svc-layout {
        height: 1fr;
        width: 100%;
        padding: 1;
    }

    #svc-info {
        height: auto;
        max-height: 20;
    }

    #svc-actions Button {
        min-width: 10;
    }

    DataTable > .datatable--header {
        background: $primary-darken-2;
        color: $text;
        text-style: bold;
    }

    DataTable > .datatable--cursor {
        background: $accent 30%;
    }

    Button {
        margin: 0 1;
    }
    """

    def on_mount(self) -> None:
        self.push_screen(MainMenuScreen())


def run_tui() -> None:
    app = ClawCtrlApp()
    app.run()
