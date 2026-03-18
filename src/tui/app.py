from __future__ import annotations

import json
import subprocess
import threading
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
        main_workspace = Path.home() / ".openclaw" / "workspace"
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

            # Workspace — read per-agent path from openclaw.json; fall back to main workspace
            try:
                agent_workspace = repo.get_agent_workspace(name) if repo else main_workspace
            except Exception:
                agent_workspace = main_workspace
            agent_data["workspace"] = str(agent_workspace)

            # Persona files — read from the agent's own workspace
            persona_files = {}
            for md_name in ("AGENTS.md", "HEARTBEAT.md", "IDENTITY.md", "MEMORY.md", "SOUL.md", "TOOLS.md", "USER.md"):
                p = agent_workspace / md_name
                if p.exists():
                    persona_files[md_name] = str(p)
            agent_data["persona_files"] = persona_files

            # Channel bindings — only bindings assigned to this specific agent
            try:
                agent_data["channels"] = repo.get_agent_channel_bindings(name) if repo else []
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
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
                self.app.pop_screen()
                return
            self.app.pop_screen()
            self.notify(f"Model set to: {selected}. Validating + restarting openclaw ...", severity="information")
            def _worker() -> None:
                try:
                    v = subprocess.run(["openclaw", "config", "validate"], capture_output=True, text=True)
                    if v.returncode != 0:
                        msg = (v.stderr or v.stdout).strip().splitlines()[-1] if (v.stderr or v.stdout).strip() else "validate failed"
                        self.app.call_from_thread(self.notify, f"validate error: {msg}", severity="error")
                        return
                    subprocess.run(["openclaw", "gateway", "restart"], capture_output=True, text=True)
                    self.app.call_from_thread(self.notify, "openclaw restarted with new model.", severity="information")
                except Exception as exc:
                    self.app.call_from_thread(self.notify, f"Error: {exc}", severity="error")
            threading.Thread(target=_worker, daemon=True).start()

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
# Agent Profile Switch Screen
# ---------------------------------------------------------------------------

def _profile_to_openclaw_params(profile) -> dict:
    """Map a ModelRunProfile to openclaw params dict."""
    return {
        "temperature": profile.temp,
        "maxTokens": profile.max_tokens,
        "topP": profile.top_p,
        "topK": profile.top_k,
        "minP": profile.min_p,
        "repeatPenalty": profile.repeat_penalty,
        "contextSize": profile.context_size,
    }


def _model_key_to_run_config(model_key: str):
    """
    Given an openclaw model key like 'llamaLocal/model.gguf', extract the
    model filename and look it up in ModelRunConfigRepo.
    Returns None if no config found (including for non-llama.cpp models).
    """
    if "/" not in model_key:
        return None
    model_name = model_key.split("/", 1)[1]
    try:
        from infrastructure.llm.model_run_config_repo import ModelRunConfigRepo
        repo = ModelRunConfigRepo()
        # Try exact match, with/without .gguf
        return (
            repo.get(model_name)
            or repo.get(model_name + ".gguf")
            or repo.get(model_name[:-5] if model_name.lower().endswith(".gguf") else model_name)
        )
    except Exception:
        return None


class AgentProfileSwitchScreen(Screen):
    """Choose a llama.cpp run profile and apply its params to the agent."""

    BINDINGS = [Binding("q", "pop_screen", "Cancel")]

    def __init__(self, agent_name: str, model_key: str, repo) -> None:
        super().__init__()
        self._agent_name = agent_name
        self._model_key = model_key
        self._repo = repo
        self._profiles: list[tuple[str, object]] = []
        cfg = _model_key_to_run_config(model_key)
        if cfg:
            self._profiles = list(cfg.profiles.items())
        self._current_params = {}
        try:
            self._current_params = repo.get_agent_model_params(agent_name)
        except Exception:
            pass

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical():
            yield Static(
                f"[bold cyan]Switch Profile — {self._agent_name}[/bold cyan]",
                classes="section-title",
            )
            yield Static(f"Model: {self._model_key}", classes="info-line")
            if self._current_params:
                params_str = "  ".join(f"{k}={v}" for k, v in self._current_params.items())
                yield Static(f"Current overrides: {params_str}", classes="info-line-dim")
            else:
                yield Static("Current overrides: (none — using defaults)", classes="info-line-dim")
            yield Static("", classes="spacer")
            if self._profiles:
                yield Static("Select a profile to apply to this agent:", classes="info-line")
                items = [
                    ListItem(Label(
                        f"{name}  [dim]temp={p.temp}  ctx={p.context_size}  "
                        f"ngl={p.gpu_layers}  maxTok={p.max_tokens}[/dim]"
                    ))
                    for name, p in self._profiles
                ]
                yield ListView(*items, id="agent-profile-list")
            else:
                yield Static(
                    "[yellow]No profiles found for this model.[/yellow]\n"
                    "Create profiles in LLM Management first.",
                    classes="info-line",
                )

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is None or idx >= len(self._profiles):
            return
        profile_name, profile = self._profiles[idx]
        params = _profile_to_openclaw_params(profile)
        try:
            self._repo.set_agent_model_params(self._agent_name, self._model_key, params)
            self.notify(
                f"Profile '{profile_name}' applied to {self._agent_name} "
                f"(written to defaults.models[{self._model_key}]). "
                "Restart openclaw to take effect.",
                severity="information",
            )
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
        Binding("p", "switch_profile", "Switch Profile"),
        Binding("x", "delete_agent", "Delete Agent"),
    ]

    def __init__(self, agent_data: dict, repo) -> None:
        super().__init__()
        self._agent = agent_data
        self._repo = repo

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        a = self._agent
        # Detect stale agent (dir exists but not in openclaw.json)
        in_config = bool(a.get("workspace"))  # workspace is only set for agents in JSON
        stale = not in_config or (
            a.get("workspace") == str(Path.home() / ".openclaw" / "workspace")
            and a["name"] != "main"
        )
        with ScrollableContainer():
            title = f"[bold cyan]Agent: {a['name']}[/bold cyan]"
            if not in_config:
                title += "  [yellow](stale — not in openclaw.json)[/yellow]"
            yield Static(title, classes="section-title")
            yield Static(f"Agent Dir: {a['path']}", classes="info-line")
            yield Static(f"Workspace: {a.get('workspace', 'N/A')}", classes="info-line")
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

            # Channel bindings section — per-agent only
            yield Static("", classes="spacer")
            yield Static("[bold]Channel Bindings[/bold]", classes="section-title")
            channels = a.get("channels", [])
            if channels:
                for ch in channels:
                    channel = ch.get("channel", "")
                    account = ch.get("accountId", "")
                    match = ch.get("match", {})
                    extra = {k: v for k, v in match.items() if k not in ("channel", "accountId")}
                    extra_str = f"  [{', '.join(f'{k}={v}' for k, v in extra.items())}]" if extra else ""
                    yield Static(f"  • {channel} / {account}{extra_str}", classes="info-line")
            else:
                yield Static("  No channel bindings for this agent", classes="info-line-dim")

            # Actions
            yield Static("", classes="spacer")
            with Horizontal(classes="actions-bar"):
                yield Button("Change Model (m)", id="btn_change_model", variant="primary")
                yield Button("Auth Profiles (a)", id="btn_auth_profiles", variant="warning")
                yield Button("Switch Profile (p)", id="btn_switch_profile", variant="success")
                yield Button("Delete Agent (x)", id="btn_delete_agent", variant="error")
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
        elif bid == "btn_switch_profile":
            self.action_switch_profile()
        elif bid == "btn_delete_agent":
            self.action_delete_agent()

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

    def action_switch_profile(self) -> None:
        current_model = self._agent.get("current_model")
        if not current_model:
            self.notify("No model set for this agent", severity="warning")
            return
        if _model_key_to_run_config(current_model) is None:
            self.notify(
                "No run profiles found for this model's framework",
                severity="warning",
            )
            return
        if self._repo:
            self.app.push_screen(
                AgentProfileSwitchScreen(self._agent["name"], current_model, self._repo)
            )
        else:
            self.notify("Repository not available", severity="error")

    def action_delete_agent(self) -> None:
        name = self._agent["name"]
        if self._repo:
            try:
                self._repo.delete_agent(name)
                self.notify(
                    f"Agent '{name}' removed. Files kept. Restart openclaw to apply.",
                    severity="information",
                )
                self.app.pop_screen()
            except Exception as e:
                self.notify(f"Error: {e}", severity="error")
        else:
            self.notify("No repo available", severity="error")

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

    def on_screen_resume(self) -> None:
        """Refresh agent list when returning from AgentDetailScreen (e.g. after deletion)."""
        self._live_refresh()

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        idx = event.list_view.index
        if idx is not None and 0 <= idx < len(self._agents):
            self.app.push_screen(AgentDetailScreen(self._agents[idx], self._repo))

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

    def action_pop_screen(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# Framework Detail Screen
# ---------------------------------------------------------------------------

class FrameworkDetailScreen(Screen):
    BINDINGS = [
        Binding("p", "pull_model", "Pull"),
        Binding("d", "delete_model", "Delete"),
        Binding("e", "edit_run_profile", "Profiles"),
        Binding("f", "set_default", "Set Default"),
        Binding("o", "add_to_openclaw", "Add to OpenClaw"),
        Binding("r", "remove_from_openclaw", "Remove from OpenClaw"),
        Binding("c", "change_port", "Change Port"),
        Binding("l", "set_model_dir", "Model Dir"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, hub, framework_name: str) -> None:
        super().__init__()
        self._hub = hub
        self._framework_name = framework_name
        # "pull" | "port" | "model_dir" | "move_confirm" | "purge_confirm"
        self._input_mode: str | None = None
        self._pending_model_dir: str | None = None
        self._pending_purge_dir: str | None = None
        self._busy = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="fw-detail-layout"):
            yield Static("", id="fw-detail-info", classes="panel")
            yield Static("", id="fw-progress", classes="hidden")
            yield Static("[bold]Available Models[/bold]", classes="section-title")
            yield DataTable(id="models-table")
            yield Input(placeholder="Enter value...", id="fw-input", classes="hidden")
        with Horizontal(id="fw-actions", classes="actions-bar"):
            yield Button("Pull (p)", id="btn_pull", variant="primary")
            yield Button("Delete (d)", id="btn_delete", variant="error")
            yield Button("Profiles (e)", id="btn_run_profile", variant="success")
            yield Button("Default (f)", id="btn_set_default", variant="warning")
            yield Button("Add OC (o)", id="btn_openclaw", variant="primary")
            yield Button("Rm OC (r)", id="btn_remove_openclaw", variant="error")
            yield Button("Port (c)", id="btn_port", variant="warning")
            yield Button("Dir (l)", id="btn_model_dir", variant="success")
            yield Button("Back (q)", id="btn_fw_back", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#models-table", DataTable)
        table.add_columns("Name", "Size", "Framework", "Run Profile")
        self._refresh()

    def on_screen_resume(self) -> None:
        self._refresh()

    def _get_live_storage_dir(self) -> str | None:
        """Return where the framework is currently storing models (live probe)."""
        try:
            if self._hub:
                live = self._hub.get_live_storage_dir(self._framework_name)
                return str(live) if live else None
        except Exception:
            pass
        return None

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

        live_dir = self._get_live_storage_dir()
        configured_dir = entity.model_dir or None

        # Highlight mismatch between configured and live storage
        if live_dir and configured_dir and live_dir != configured_dir:
            dir_line = (
                f"[bold]Model Dir (actual):[/bold]     [yellow]{live_dir}[/yellow]\n"
                f"[bold]Model Dir (configured):[/bold] [cyan]{configured_dir}[/cyan]  "
                f"[dim](not yet in effect — move models to apply)[/dim]"
            )
        elif configured_dir:
            dir_line = f"[bold]Model Dir:[/bold] [cyan]{configured_dir}[/cyan]"
        else:
            dir_line = f"[bold]Model Dir:[/bold] [dim]{live_dir or '(default)'}[/dim]"

        # Show saved default model/profile (llama.cpp only)
        default_line = ""
        if self._framework_name == "llama.cpp":
            try:
                from infrastructure.llm.model_dir_repo import ModelDirRepo
                saved = ModelDirRepo().get_default("llama.cpp")
                if saved:
                    default_line = (
                        f"[bold]Default:[/bold] [cyan]{saved.get('model', '?')}[/cyan]"
                        f" / [green]{saved.get('profile', '?')}[/green]"
                    )
                else:
                    default_line = "[bold]Default:[/bold] [dim](not set)[/dim]"
            except Exception:
                default_line = ""

        lines = [
            f"[bold]Framework:[/bold] {entity.name}",
            f"[bold]Status:[/bold] [{status_color}]{status_text}[/{status_color}]",
            f"[bold]Installed:[/bold] [{inst_color}]{inst_text}[/{inst_color}]",
            f"[bold]Port:[/bold] {entity.port}",
            f"[bold]Active Model:[/bold] {entity.active_model or 'None'}",
            f"[bold]Models:[/bold] {len(entity.available_models)}",
            dir_line,
        ]
        if default_line:
            lines.append(default_line)
        info.update("\n".join(lines))

        table = self.query_one("#models-table", DataTable)
        table.clear()
        try:
            from infrastructure.llm.model_run_config_repo import ModelRunConfigRepo
            run_repo = ModelRunConfigRepo()
            all_run_cfgs = run_repo.load_all()
        except Exception:
            all_run_cfgs = {}
        for model in entity.available_models:
            run_cfg = all_run_cfgs.get(model.name)
            if run_cfg and run_cfg.profiles:
                default = run_cfg.default_profile
                n = len(run_cfg.profiles)
                profile_label = f"[green]{default}[/green] ({n})"
            else:
                profile_label = "[dim]none[/dim]"
            table.add_row(
                model.name, _fmt_bytes(model.size_bytes), model.framework or "", profile_label
            )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_pull":
            self.action_pull_model()
        elif bid == "btn_delete":
            self.action_delete_model()
        elif bid == "btn_run_profile":
            self.action_edit_run_profile()
        elif bid == "btn_openclaw":
            self.action_add_to_openclaw()
        elif bid == "btn_remove_openclaw":
            self.action_remove_from_openclaw()
        elif bid == "btn_port":
            self.action_change_port()
        elif bid == "btn_model_dir":
            self.action_set_model_dir()
        elif bid == "btn_set_default":
            self.action_set_default()
        elif bid == "btn_fw_back":
            self.app.pop_screen()

    def _guard_busy(self) -> bool:
        if self._busy:
            self.notify("Please wait — operation in progress", severity="warning")
            return True
        return False

    def action_pull_model(self) -> None:
        if self._guard_busy():
            return
        self._input_mode = "pull"
        inp = self.query_one("#fw-input", Input)
        inp.placeholder = "Model name to pull (e.g. llama3:8b)"
        inp.remove_class("hidden")
        inp.focus()

    def action_delete_model(self) -> None:
        if self._guard_busy():
            return
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity and entity.available_models:
            table = self.query_one("#models-table", DataTable)
            cursor_row = table.cursor_row
            if 0 <= cursor_row < len(entity.available_models):
                self._do_delete(entity.available_models[cursor_row].name)

    def action_change_port(self) -> None:
        if self._guard_busy():
            return
        self._input_mode = "port"
        inp = self.query_one("#fw-input", Input)
        inp.placeholder = "New port number"
        inp.remove_class("hidden")
        inp.focus()

    def action_set_model_dir(self) -> None:
        if self._guard_busy():
            return
        self._input_mode = "model_dir"
        live_dir = self._get_live_storage_dir()
        inp = self.query_one("#fw-input", Input)
        inp.placeholder = f"New model directory path (current: {live_dir or 'unknown'})"
        inp.remove_class("hidden")
        inp.focus()

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity is None:
            return
        row = event.cursor_row
        if 0 <= row < len(entity.available_models):
            model = entity.available_models[row]
            self.app.push_screen(
                ModelRunProfileScreen(model.name, model_path=model.full_path, hub=self._hub)
            )

    def action_edit_run_profile(self) -> None:
        """Open run profile manager for the selected model (keyboard shortcut)."""
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity is None:
            return
        table = self.query_one("#models-table", DataTable)
        row = table.cursor_row
        if 0 <= row < len(entity.available_models):
            model = entity.available_models[row]
            self.app.push_screen(
                ModelRunProfileScreen(model.name, model_path=model.full_path, hub=self._hub)
            )
        else:
            self.notify("Select a model first", severity="warning")

    def action_add_to_openclaw(self) -> None:
        """Register the selected model into openclaw agents.defaults.models."""
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity is None:
            return
        table = self.query_one("#models-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(entity.available_models)):
            self.notify("Select a model first", severity="warning")
            return

        model = entity.available_models[row]

        # Build the openclaw model key using the actual provider ID from openclaw.json
        if self._framework_name == "llama.cpp":
            try:
                from infrastructure.openclaw.config_repo import OpenClawConfigRepo as _OCR
                provider_id = _OCR().get_llamacpp_provider_id() or "llamaLocal"
            except Exception:
                provider_id = "llamaLocal"
            model_key = f"{provider_id}/{model.name}"  # keep .gguf
        elif self._framework_name == "ollama":
            model_key = f"ollama/{model.name}"
        else:
            model_key = f"{self._framework_name}/{model.name}"

        # Look up the default profile for this model (llama.cpp only)
        params: dict = {}
        if self._framework_name == "llama.cpp":
            try:
                from infrastructure.llm.model_run_config_repo import ModelRunConfigRepo
                run_repo = ModelRunConfigRepo()
                cfg = run_repo.get(model.name)
                default = cfg.get_default() if cfg else None
                if default:
                    params = _profile_to_openclaw_params(default)
            except Exception:
                pass

        try:
            from infrastructure.openclaw.config_repo import OpenClawConfigRepo
            repo = OpenClawConfigRepo()

            # Check for duplicate
            raw = repo._read_raw()
            provider_models = (
                raw.get("models", {}).get("providers", {})
                .get(provider_id, {}).get("models", [])
            )
            already_in_provider = any(m.get("id") == model.name for m in provider_models)
            already_in_defaults = model_key in raw.get("agents", {}).get("defaults", {}).get("models", {})
            if already_in_provider or already_in_defaults:
                self.notify(
                    f"'{model.name}' is already registered in openclaw.",
                    severity="warning",
                )
                return

            # 1. Register in models.providers (llama.cpp only; ollama managed by server)
            if self._framework_name == "llama.cpp":
                context_window = 131072 if "35B" in model.name else 65536 if "27B" in model.name else 32768
                max_tokens = min(context_window, 32768)
                model_entry = {
                    "id": model.name,
                    "name": model.name.replace(".gguf", "").replace("-", " ").replace("_", " ") + " (llama.cpp)",
                    "api": "openai-completions",
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": context_window,
                    "maxTokens": max_tokens,
                }
                repo.add_model_to_provider(provider_id, model_entry)
            # 2. Register in agents.defaults.models (always; params may be empty)
            repo.add_model_to_defaults(model_key, params)
            profile_note = " (with profile params)" if params else ""
            self.notify(
                f"Added '{model_key}' to openclaw{profile_note}. "
                "Restart openclaw to apply.",
                severity="information",
            )
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_remove_from_openclaw(self) -> None:
        """Remove the selected model from openclaw providers and agents.defaults.models."""
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity is None:
            return
        table = self.query_one("#models-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(entity.available_models)):
            self.notify("Select a model first", severity="warning")
            return

        model = entity.available_models[row]

        if self._framework_name == "llama.cpp":
            try:
                from infrastructure.openclaw.config_repo import OpenClawConfigRepo as _OCR
                provider_id = _OCR().get_llamacpp_provider_id() or "llamaLocal"
            except Exception:
                provider_id = "llamaLocal"
        elif self._framework_name == "ollama":
            provider_id = "ollama"
        else:
            provider_id = self._framework_name

        try:
            from infrastructure.openclaw.config_repo import OpenClawConfigRepo
            repo = OpenClawConfigRepo()
            removed = repo.remove_model_from_provider(provider_id, model.name)
            if removed:
                self.notify(
                    f"Removed '{model.name}' from openclaw. Restart openclaw to apply.",
                    severity="information",
                )
            else:
                self.notify(f"'{model.name}' was not registered in openclaw.", severity="warning")
        except Exception as e:
            self.notify(f"Error: {e}", severity="error")

    def action_set_default(self) -> None:
        """Set the selected model + its default profile as the llama.cpp service default."""
        if self._framework_name != "llama.cpp":
            self.notify("Set Default only applies to llama.cpp", severity="warning")
            return
        if self._guard_busy():
            return
        entity = self._hub.get(self._framework_name) if self._hub else None
        if entity is None:
            return
        table = self.query_one("#models-table", DataTable)
        row = table.cursor_row
        if not (0 <= row < len(entity.available_models)):
            self.notify("Select a model first", severity="warning")
            return

        model = entity.available_models[row]

        try:
            from infrastructure.llm.model_run_config_repo import ModelRunConfigRepo
            run_repo = ModelRunConfigRepo()
            cfg = run_repo.get(model.name)
            if not cfg or not cfg.profiles:
                self.notify(
                    "No profiles configured for this model. Add a profile first.",
                    severity="warning",
                )
                return
            profile_name = cfg.default_profile
            profile = cfg.profiles.get(profile_name)
            if profile is None:
                profile_name = next(iter(cfg.profiles))
                profile = cfg.profiles[profile_name]
            extra_args = cfg.extra_args or []
        except Exception as e:
            self.notify(f"Error reading profile: {e}", severity="error")
            return

        model_dir = self._hub.get_live_storage_dir(self._framework_name) if self._hub else None
        if model_dir:
            model_path = str(Path(model_dir) / model.name)
        else:
            model_path = model.full_path or model.name

        self._busy = True
        progress = self.query_one("#fw-progress", Static)
        progress.update(f"[yellow]Updating service: {model.name} / {profile_name} ...[/yellow]")
        progress.remove_class("hidden")

        def _worker() -> None:
            try:
                from infrastructure.llm.llama_cpp import LlamaCppAdapter
                adapter = LlamaCppAdapter()
                adapter.update_service(model_path, profile, extra_args, entity.port)
                from infrastructure.llm.model_dir_repo import ModelDirRepo
                ModelDirRepo().set_default("llama.cpp", model.name, profile_name)
                if self._hub:
                    self._hub.refresh_all()
                def _done() -> None:
                    self._busy = False
                    progress.add_class("hidden")
                    self._refresh()
                    self.notify(
                        f"Default set: {model.name} / {profile_name}. Service restarted.",
                        severity="information",
                    )
                self.app.call_from_thread(_done)
            except Exception as exc:
                def _err(e=exc) -> None:
                    self._busy = False
                    progress.add_class("hidden")
                    self.notify(f"Error: {e}", severity="error")
                self.app.call_from_thread(_err)

        threading.Thread(target=_worker, daemon=True).start()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        inp = self.query_one("#fw-input", Input)
        inp.value = ""
        mode = self._input_mode

        if mode == "move_confirm":
            inp.add_class("hidden")
            self._input_mode = None
            new_dir = self._pending_model_dir
            self._pending_model_dir = None
            if not new_dir:
                return
            move = value.lower() in ("y", "yes")
            self._start_move_thread(new_dir, move_existing=move)
            return

        if mode == "purge_confirm":
            inp.add_class("hidden")
            self._input_mode = None
            old_dir = self._pending_purge_dir
            self._pending_purge_dir = None
            if old_dir and value.lower() in ("y", "yes"):
                self._do_purge(old_dir)
            return

        inp.add_class("hidden")

        if not value:
            self._input_mode = None
            self._pending_model_dir = None
            return

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
        elif mode == "model_dir":
            # Check if there are models at the live storage location to move
            live_dir = self._get_live_storage_dir()
            entity = self._hub.get(self._framework_name) if self._hub else None
            has_models = entity and len(entity.available_models) > 0
            if has_models and live_dir and live_dir != value:
                self._pending_model_dir = value
                self._input_mode = "move_confirm"
                n = len(entity.available_models)
                inp.placeholder = f"Move {n} model(s) from {live_dir} to new dir? (y/n)"
                inp.remove_class("hidden")
                inp.focus()
            else:
                self._start_move_thread(value, move_existing=False)

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
            if self._hub:
                self._hub.delete_model(self._framework_name, name)
            self._refresh()
            self.notify(f"Deleted {name}", severity="information")
        except Exception as e:
            self.notify(f"Error deleting model: {e}", severity="error")

    def _do_purge(self, old_dir: str) -> None:
        try:
            count = self._hub.purge_dir(self._framework_name, old_dir) if self._hub else 0
            self.notify(f"Purged {count} item(s) from {old_dir}", severity="information")
            self._refresh()
        except Exception as e:
            self.notify(f"Error purging: {e}", severity="error")

    # ------------------------------------------------------------------
    # Background move with live progress
    # ------------------------------------------------------------------

    def _start_move_thread(self, new_dir: str, move_existing: bool) -> None:
        """Launch model dir change in a background thread."""
        # Capture old live dir before the move (for cleanup offer)
        old_live_dir = self._get_live_storage_dir() if move_existing else None

        self._busy = True
        progress = self.query_one("#fw-progress", Static)
        progress.remove_class("hidden")
        progress.update("[yellow]Starting ...[/yellow]")

        def progress_cb(msg: str) -> None:
            self.app.call_from_thread(
                self.query_one("#fw-progress", Static).update,
                f"[yellow]{msg}[/yellow]",
            )

        def worker() -> None:
            error: str | None = None
            try:
                if self._hub:
                    self._hub.set_model_dir(
                        self._framework_name,
                        new_dir,
                        move_existing=move_existing,
                        progress_cb=progress_cb,
                    )
            except Exception as exc:
                error = str(exc)
            self.app.call_from_thread(
                self._on_move_done, new_dir, move_existing, old_live_dir, error
            )

        threading.Thread(target=worker, daemon=True).start()

    def _on_move_done(
        self,
        new_dir: str,
        moved: bool,
        old_live_dir: str | None,
        error: str | None,
    ) -> None:
        """Called on the main thread when the background move finishes."""
        self._busy = False
        progress = self.query_one("#fw-progress", Static)

        if error:
            progress.update(f"[red]Error: {error}[/red]")
            self.notify(f"Error: {error}", severity="error")
            self._refresh()
            return

        progress.add_class("hidden")
        action = "Moved" if moved else "Set"
        self.notify(f"{action} model dir → {new_dir}", severity="information")
        self._refresh()

        # After a successful move, check if old location still has leftover files
        # (can happen if the old dir is different from the new and wasn't auto-deleted)
        if moved and old_live_dir and old_live_dir != new_dir:
            from pathlib import Path as _Path
            old_path = _Path(old_live_dir)
            leftover = False
            if self._framework_name == "llama.cpp":
                leftover = bool(list(old_path.glob("**/*.gguf"))) if old_path.exists() else False
            elif self._framework_name == "ollama":
                leftover = (
                    (old_path / "blobs").exists()
                    and any((old_path / "blobs").iterdir())
                ) if old_path.exists() else False

            if leftover:
                self._pending_purge_dir = old_live_dir
                self._input_mode = "purge_confirm"
                inp = self.query_one("#fw-input", Input)
                inp.placeholder = f"Old files still at {old_live_dir} — delete? (y/n)"
                inp.remove_class("hidden")
                inp.focus()

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
        # Seed prev_running with current state so first live_refresh
        # only detects changes that happen AFTER the TUI starts.
        if self._hub is not None:
            self._hub._prev_running = {
                name: entity.is_running
                for name, entity in self._hub.entities.items()
            }
        self._refresh()
        self.set_interval(10, self._live_refresh)

    def _live_refresh(self) -> None:
        """Reload live data from system and update display."""
        if self._hub is None:
            self._hub = _load_llm_data()
        else:
            self._hub.refresh_all()
        if self._hub is not None:
            stopped = self._hub.enforce_exclusivity()
            for name in stopped:
                self.notify(f"Auto-stopped {name} (VRAM conflict)", severity="warning")
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
# Model Run Profile Screen
# ---------------------------------------------------------------------------

_PROFILE_FIELDS = [
    ("name",           "Profile name (e.g. daily, coding, creative)"),
    ("context_size",   "Context window -c  (tokens, e.g. 65536)"),
    ("max_tokens",     "Max output tokens -n  (e.g. 1024)"),
    ("gpu_layers",     "GPU layers -ngl  (e.g. 80, or 0 for CPU)"),
    ("temp",           "Temperature --temp  (e.g. 0.7)"),
    ("top_p",          "Top-p --top-p  (e.g. 0.95)"),
    ("top_k",          "Top-k --top-k  (e.g. 40, or 0 to disable)"),
    ("min_p",          "Min-p --min-p  (e.g. 0.05, or 0 to disable)"),
    ("repeat_penalty", "Repeat penalty --repeat-penalty  (e.g. 1.05)"),
]


class ModelRunProfileScreen(Screen):
    """Manage run profiles for a single model."""

    BINDINGS = [
        Binding("n", "new_profile", "New"),
        Binding("e", "edit_profile", "Edit"),
        Binding("d", "delete_profile", "Delete"),
        Binding("s", "set_profile", "Set Profile"),
        Binding("t", "test_profile", "Test"),
        Binding("k", "stop_test", "Stop Test"),
        Binding("x", "edit_extra_args", "Extra Args"),
        Binding("q", "pop_screen", "Back"),
    ]

    def __init__(self, model_name: str, model_path: str | None = None, hub=None) -> None:
        super().__init__()
        self._model_name = model_name
        self._model_path = model_path   # full path on disk (None = test unavailable)
        self._hub = hub                 # LlmFrameworkHub, for server stop/restart
        from infrastructure.llm.model_run_config_repo import ModelRunConfigRepo
        self._repo = ModelRunConfigRepo()
        self._input_step: int = 0           # current field index in _PROFILE_FIELDS
        self._input_values: dict[str, str] = {}
        self._editing_profile: str | None = None  # None = new; str = editing existing
        self._active = False  # True when sequential profile input is running
        self._editing_extra_args = False  # True when extra_args input is active
        self._busy = False    # True when test is running
        self._test_proc = None   # live subprocess handle for kill support
        self._progress_lines: list[str] = []  # accumulated progress log
        self._saved_server_cmd: list[str] | None = None  # llama-server cmd before test

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Footer()
        with Vertical(id="profile-layout"):
            yield Static(
                f"[bold cyan]Run Profiles — {self._model_name}[/bold cyan]",
                classes="panel-title",
            )
            yield Static("", id="profile-detail", classes="panel")
            yield Static("", id="extra-args-display", classes="panel")
            yield Static("", id="profile-test-result", classes="hidden")
            yield Static("[bold]Profiles[/bold]", classes="section-title")
            yield DataTable(id="profiles-table")
            yield Input(placeholder="", id="profile-input", classes="hidden")
        with Horizontal(id="profile-actions", classes="actions-bar"):
            yield Button("New (n)", id="btn_p_new", variant="success")
            yield Button("Edit (e)", id="btn_p_edit", variant="primary")
            yield Button("Delete (d)", id="btn_p_delete", variant="error")
            yield Button("Set Profile (s)", id="btn_p_default", variant="warning")
            yield Button("Test (t)", id="btn_p_test", variant="primary")
            yield Button("Stop Test (k)", id="btn_p_stop_test", variant="error")
            yield Button("Extra Args (x)", id="btn_p_extra", variant="warning")
            yield Button("Back (q)", id="btn_p_back", variant="default")

    def on_mount(self) -> None:
        table = self.query_one("#profiles-table", DataTable)
        table.cursor_type = "row"
        table.add_columns(
            "Profile", "Context (-c)", "Max Tokens (-n)",
            "GPU Layers (-ngl)", "Temp", "Top-p", "Top-k", "Min-p", "Repeat Penalty", "Default",
        )
        self._refresh()

    def _refresh(self) -> None:
        cfg = self._repo.get(self._model_name)
        table = self.query_one("#profiles-table", DataTable)
        table.clear()
        # Extra args display
        extra_display = self.query_one("#extra-args-display", Static)
        if cfg and cfg.extra_args:
            flags = " ".join(cfg.extra_args)
            extra_display.update(f"[bold]Extra flags:[/bold] [yellow]{flags}[/yellow]  [dim](x to edit)[/dim]")
        else:
            extra_display.update("[dim]No extra flags  (x to set, e.g. --jinja)[/dim]")
        if cfg is None or not cfg.profiles:
            self.query_one("#profile-detail", Static).update(
                "[dim]No profiles yet — press [n] to create one.[/dim]"
            )
            return
        for pname, p in cfg.profiles.items():
            marker = "[green]★[/green]" if pname == cfg.default_profile else ""
            table.add_row(
                pname,
                str(p.context_size),
                str(p.max_tokens),
                str(p.gpu_layers),
                str(p.temp),
                str(p.top_p),
                str(p.top_k),
                str(p.min_p),
                str(p.repeat_penalty),
                marker,
            )
        # Show args for selected / default profile
        default = cfg.get_default()
        if default:
            args = " ".join(default.to_args())
            self.query_one("#profile-detail", Static).update(
                f"[bold]Default profile args:[/bold]\n[dim]{args}[/dim]"
            )

    def _get_selected_profile_name(self) -> str | None:
        table = self.query_one("#profiles-table", DataTable)
        cfg = self._repo.get(self._model_name)
        if cfg is None:
            return None
        names = list(cfg.profiles.keys())
        row = table.cursor_row
        if 0 <= row < len(names):
            return names[row]
        return None

    def _start_input_sequence(self, editing_profile: str | None) -> None:
        """Begin sequential prompting for all profile fields."""
        self._editing_profile = editing_profile
        self._input_values = {}
        self._input_step = 0
        self._active = True
        # Pre-fill defaults from existing profile if editing
        existing: dict[str, str] = {}
        if editing_profile:
            cfg = self._repo.get(self._model_name)
            if cfg:
                p = cfg.profiles.get(editing_profile)
                if p:
                    existing = {
                        "name": p.name,
                        "context_size": str(p.context_size),
                        "max_tokens": str(p.max_tokens),
                        "gpu_layers": str(p.gpu_layers),
                        "temp": str(p.temp),
                        "top_p": str(p.top_p),
                        "top_k": str(p.top_k),
                        "min_p": str(p.min_p),
                        "repeat_penalty": str(p.repeat_penalty),
                    }
        self._existing_defaults = existing
        self._prompt_step()

    def _prompt_step(self) -> None:
        field, hint = _PROFILE_FIELDS[self._input_step]
        inp = self.query_one("#profile-input", Input)
        default_val = self._existing_defaults.get(field, "")
        inp.placeholder = f"{hint}  (current: {default_val})" if default_val else hint
        inp.value = default_val
        inp.remove_class("hidden")
        inp.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if self._editing_extra_args:
            self._editing_extra_args = False
            inp = self.query_one("#profile-input", Input)
            inp.add_class("hidden")
            raw = event.value.strip()
            args = raw.split() if raw else []
            self._repo.set_extra_args(self._model_name, args)
            self.notify("Extra args saved", severity="information")
            self._refresh()
            return
        if not self._active:
            return
        value = event.value.strip()
        inp = self.query_one("#profile-input", Input)
        field, _ = _PROFILE_FIELDS[self._input_step]
        # Use existing default if user submitted empty
        if not value and self._editing_profile:
            value = self._existing_defaults.get(field, "")
        if not value:
            self.notify(f"'{field}' is required", severity="warning")
            self._prompt_step()
            return
        self._input_values[field] = value
        self._input_step += 1
        inp.value = ""
        if self._input_step < len(_PROFILE_FIELDS):
            self._prompt_step()
        else:
            inp.add_class("hidden")
            self._active = False
            self._save_profile()

    def _maybe_restart_service(self, profile_name: str) -> None:
        """If this model is the current llama.cpp service default, restart it with profile_name."""
        if not self._model_path or self._busy:
            return
        try:
            from infrastructure.llm.model_dir_repo import ModelDirRepo
            saved = ModelDirRepo().get_default("llama.cpp")
            if not saved or saved.get("model") != self._model_name:
                return
        except Exception:
            return
        cfg = self._repo.get(self._model_name)
        if not cfg:
            return
        profile = cfg.profiles.get(profile_name)
        if not profile:
            return
        extra_args = cfg.extra_args or []
        entity = self._hub.get("llama.cpp") if self._hub else None
        port = entity.port if entity else 8080

        self._busy = True
        result_widget = self.query_one("#profile-test-result", Static)
        result_widget.update("[yellow]Restarting llama.cpp with updated profile ...[/yellow]")
        result_widget.remove_class("hidden")

        import threading as _th

        def _worker() -> None:
            try:
                from infrastructure.llm.llama_cpp import LlamaCppAdapter
                LlamaCppAdapter().update_service(self._model_path, profile, extra_args, port)
                from infrastructure.llm.model_dir_repo import ModelDirRepo
                ModelDirRepo().set_default("llama.cpp", self._model_name, profile_name)
                if self._hub:
                    self._hub.refresh_all()
                def _done() -> None:
                    self._busy = False
                    result_widget.add_class("hidden")
                    self.notify(f"Service restarted with profile '{profile_name}'", severity="information")
                self.app.call_from_thread(_done)
            except Exception as exc:
                def _err(e=exc) -> None:
                    self._busy = False
                    result_widget.add_class("hidden")
                    self.notify(f"Restart failed: {e}", severity="error")
                self.app.call_from_thread(_err)

        _th.Thread(target=_worker, daemon=True).start()

    def _save_profile(self) -> None:
        v = self._input_values
        try:
            from entities.llm import ModelRunProfile
            profile = ModelRunProfile(
                name=v["name"],
                context_size=int(v["context_size"]),
                max_tokens=int(v["max_tokens"]),
                gpu_layers=int(v["gpu_layers"]),
                temp=float(v["temp"]),
                top_p=float(v["top_p"]),
                top_k=int(v["top_k"]),
                min_p=float(v["min_p"]),
                repeat_penalty=float(v["repeat_penalty"]),
            )
        except (KeyError, ValueError) as exc:
            self.notify(f"Invalid value: {exc}", severity="error")
            return
        # If renaming (edit + new name != old name), delete the old profile first
        if self._editing_profile and self._editing_profile != profile.name:
            self._repo.delete_profile(self._model_name, self._editing_profile)
        self._repo.upsert_profile(self._model_name, profile)
        self.notify(f"Profile '{profile.name}' saved", severity="information")
        self._refresh()
        # Restart service if this profile is the current default and model is service default
        cfg = self._repo.get(self._model_name)
        if cfg and cfg.default_profile == profile.name:
            self._maybe_restart_service(profile.name)

    def on_button_pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id
        if bid == "btn_p_new":
            self.action_new_profile()
        elif bid == "btn_p_edit":
            self.action_edit_profile()
        elif bid == "btn_p_delete":
            self.action_delete_profile()
        elif bid == "btn_p_default":
            self.action_set_profile()
        elif bid == "btn_p_test":
            self.action_test_profile()
        elif bid == "btn_p_stop_test":
            self.action_stop_test()
        elif bid == "btn_p_extra":
            self.action_edit_extra_args()
        elif bid == "btn_p_back":
            self.app.pop_screen()

    def action_edit_extra_args(self) -> None:
        if self._active:
            self.notify("Finish profile input first", severity="warning")
            return
        cfg = self._repo.get(self._model_name)
        current = " ".join(cfg.extra_args) if cfg and cfg.extra_args else ""
        inp = self.query_one("#profile-input", Input)
        inp.placeholder = "Space-separated flags, e.g: --jinja --no-context-shift  (empty = clear)"
        inp.value = current
        inp.remove_class("hidden")
        inp.focus()
        self._editing_extra_args = True

    def action_new_profile(self) -> None:
        self._existing_defaults = {}
        self._start_input_sequence(editing_profile=None)

    def action_edit_profile(self) -> None:
        name = self._get_selected_profile_name()
        if not name:
            self.notify("Select a profile first", severity="warning")
            return
        self._start_input_sequence(editing_profile=name)

    def action_delete_profile(self) -> None:
        name = self._get_selected_profile_name()
        if not name:
            self.notify("Select a profile first", severity="warning")
            return
        self._repo.delete_profile(self._model_name, name)
        self.notify(f"Deleted profile '{name}'", severity="information")
        self._refresh()

    def action_set_profile(self) -> None:
        name = self._get_selected_profile_name()
        if not name:
            self.notify("Select a profile first", severity="warning")
            return
        self._repo.set_default_profile(self._model_name, name)
        self.notify(f"Active profile set to '{name}'", severity="information")
        self._refresh()
        self._maybe_restart_service(name)

    def action_test_profile(self) -> None:
        if self._busy:
            self.notify("Test already running — press k to stop it first", severity="warning")
            return
        if not self._model_path:
            self.notify("No model path available (ollama models cannot be tested this way)", severity="warning")
            return
        profile_name = self._get_selected_profile_name()
        if not profile_name:
            self.notify("Select a profile to test", severity="warning")
            return
        cfg = self._repo.get(self._model_name)
        if not cfg:
            return
        profile = cfg.profiles.get(profile_name)
        if not profile:
            return

        self._busy = True
        self._test_proc = None
        self._progress_lines = []
        self._saved_server_cmd = None
        result_box = self.query_one("#profile-test-result", Static)
        result_box.remove_class("hidden")
        result_box.update("[yellow]Preparing test ... [dim](k = stop)[/dim][/yellow]")

        def progress_cb(msg: str) -> None:
            from rich.markup import escape

            def _update() -> None:
                self._progress_lines.append(escape(msg))
                visible = self._progress_lines[-12:]  # keep last 12 lines
                body = "\n".join(f"[yellow]{l}[/yellow]" for l in visible)
                self.query_one("#profile-test-result", Static).update(
                    body + "\n[dim](k = stop)[/dim]"
                )

            self.app.call_from_thread(_update)

        def on_proc(proc) -> None:
            self._test_proc = proc

        model_path = self._model_path
        hub = self._hub

        def worker() -> None:
            from infrastructure.llm.llama_cpp import test_profile_load, get_running_llamaserver_cmd
            import time as _time

            # Check if llama.cpp is running so we know whether to restart it after test
            saved_cmd = bool(get_running_llamaserver_cmd())

            # Stop ollama and llama.cpp servers before the test
            if hub:
                for fw_name, entity in list(hub.entities.items()):
                    if entity.is_running:
                        progress_cb(f"Stopping {fw_name} server ...")
                        try:
                            hub.stop(fw_name)
                            hub.refresh_all()
                        except Exception:
                            pass
                _time.sleep(1)

            success, message = test_profile_load(
                model_path, profile, progress_cb=progress_cb, on_proc=on_proc
            )

            # Restart llama.cpp server via systemctl (single managed instance)
            if saved_cmd:
                progress_cb("Restarting llama.cpp server ...")
                try:
                    subprocess.run(
                        ["systemctl", "start", "llama-cpp.service"],
                        capture_output=True, check=False,
                    )
                    _time.sleep(2)
                    if hub:
                        hub.refresh_all()
                except Exception as exc:
                    progress_cb(f"Warning: could not restart server: {exc}")
            elif hub:
                # Fallback: start via hub (may use wrong port if not persisted)
                progress_cb("Restarting llama.cpp via hub ...")
                try:
                    hub.start("llama.cpp")
                    hub.refresh_all()
                except Exception:
                    pass

            self.app.call_from_thread(self._on_test_done, success, message)

        threading.Thread(target=worker, daemon=True).start()

    def action_stop_test(self) -> None:
        if not self._busy:
            self.notify("No test is running", severity="warning")
            return
        proc = self._test_proc
        if proc is not None:
            try:
                proc.kill()
            except Exception:
                pass
        self._test_proc = None
        self._busy = False
        result_box = self.query_one("#profile-test-result", Static)
        result_box.update("[yellow]Test stopped by user.[/yellow]")
        self.notify("Test stopped", severity="warning")

    def _on_test_done(self, success: bool, message: str) -> None:
        # Guard: if user already stopped the test, ignore late callback
        if not self._busy:
            return
        self._busy = False
        self._test_proc = None
        result_box = self.query_one("#profile-test-result", Static)

        # Build log tail from accumulated progress lines
        from rich.markup import escape
        log_tail = "\n".join(
            f"[dim]{l}[/dim]" for l in self._progress_lines[-8:]
        )
        sep = "\n─────\n" if log_tail else ""

        if not success:
            result_box.update(
                f"{log_tail}{sep}[red]FAIL — model could not load (OOM)[/red]\n[dim]{escape(message)}[/dim]"
            )
            self.notify("Test FAILED — model could not load", severity="error")
        elif message.startswith("WARN"):
            result_box.update(
                f"{log_tail}{sep}[yellow]WARN — loaded with OOM warnings[/yellow]\n[dim]{escape(message)}[/dim]"
            )
            self.notify("OOM warnings detected — try reducing -c or -ngl", severity="warning")
        elif message.startswith("CLEAN"):
            result_box.update(
                f"{log_tail}{sep}[green]CLEAN — no OOM[/green]\n[dim]{escape(message)}[/dim]"
            )
            self.notify("Profile is clean — no OOM warnings", severity="information")
        else:
            # Timeout or other non-OOM result
            result_box.update(f"{log_tail}{sep}[yellow]{escape(message)}[/yellow]")
            self.notify(message[:80], severity="warning")

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

    #fw-actions Button {
        margin: 0 1;
        min-width: 10;
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
