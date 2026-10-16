from __future__ import annotations

import asyncio
from typing import Any, Optional

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.widgets import (
    Button,
    DataTable,
    Footer,
    Header,
    Label,
    Markdown,
    Static,
    TabPane,
    TabbedContent,
)

from edith import __version__
from edith.client import EdithApiClient, EdithApiError
from edith.formatter import format_bytes


class EdithTUIApp(App):
    """EDITH Interactive Terminal User Interface."""

    TITLE = "EDITH — Local AI Assistant & Integration Engine"
    SUB_TITLE = f"v{__version__} • Terminal Control"
    CSS = """
    Screen {
        background: $surface;
    }
    #header-box {
        height: 3;
        background: $panel;
        padding: 0 1;
        content-align: center middle;
        border-bottom: solid $primary;
    }
    .panel-box {
        border: solid $primary;
        padding: 1;
        margin: 1;
        height: auto;
    }
    .section-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    DataTable {
        height: 1fr;
        border: solid $secondary;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        background: $panel;
        color: $text-muted;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit", show=True),
        Binding("r", "refresh_data", "Refresh", show=True),
        Binding("d", "toggle_dark", "Toggle Theme", show=True),
    ]

    def __init__(self, client: EdithApiClient) -> None:
        super().__init__()
        self.client = client
        self.overview_data: dict[str, Any] = {}
        self.models_data: dict[str, Any] = {}
        self.tools_data: dict[str, Any] = {}
        self.integrations_data: dict[str, Any] = {}
        self.deliveries_data: dict[str, Any] = {}
        self.users_data: list[dict[str, Any]] = []
        self.backend_online: bool = False

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with TabbedContent(initial="tab-overview"):
            with TabPane("Overview", id="tab-overview"):
                with VerticalScroll():
                    yield Static(id="overview-content", classes="panel-box")
            with TabPane("Models", id="tab-models"):
                yield DataTable(id="models-table")
            with TabPane("Tools & Policy", id="tab-tools"):
                with VerticalScroll():
                    yield Static(id="tools-policy-content", classes="panel-box")
                    yield DataTable(id="tools-table")
            with TabPane("Queues & Backlog", id="tab-queues"):
                with VerticalScroll():
                    yield Static(id="queues-summary-content", classes="panel-box")
                    yield DataTable(id="deliveries-table")
            with TabPane("Integrations", id="tab-integrations"):
                with VerticalScroll():
                    yield Static(id="integrations-content", classes="panel-box")
            with TabPane("Users", id="tab-users"):
                yield DataTable(id="users-table")
        yield Footer()

    def on_mount(self) -> None:
        """Initialize tables and load initial data."""
        self._setup_tables()
        self.action_refresh_data()

    def _setup_tables(self) -> None:
        # Models Table
        models_table = self.query_one("#models-table", DataTable)
        models_table.add_columns("Model Name", "Size", "Current Default", "Modified")
        models_table.cursor_type = "row"

        # Tools Table
        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.add_columns("Tool Name", "Permission", "Confirmation", "Description")
        tools_table.cursor_type = "row"

        # Deliveries Table
        deliv_table = self.query_one("#deliveries-table", DataTable)
        deliv_table.add_columns("ID", "Platform", "Chat ID", "Status", "Attempts", "Correlation ID", "Last Error")
        deliv_table.cursor_type = "row"

        # Users Table
        users_table = self.query_one("#users-table", DataTable)
        users_table.add_columns("ID", "External ID", "Display Name", "Admin", "Conversations", "Effective Model")
        users_table.cursor_type = "row"

    def action_refresh_data(self) -> None:
        """Fetch fresh state from backend API."""
        self.run_worker(self._load_data(), exclusive=True)

    async def _load_data(self) -> None:
        try:
            overview = await asyncio.to_thread(self.client.get_overview)
            self.overview_data = overview
            self.backend_online = True
        except Exception:
            self.backend_online = False
            self.overview_data = {}

        if self.backend_online:
            try:
                models = await asyncio.to_thread(self.client.list_models, False)
                self.models_data = models
            except Exception:
                self.models_data = {}

            try:
                tools = await asyncio.to_thread(self.client.list_tools)
                self.tools_data = tools
            except Exception:
                self.tools_data = {}

            try:
                integrations = await asyncio.to_thread(self.client.get_integrations)
                self.integrations_data = integrations
            except Exception:
                self.integrations_data = {}

            try:
                deliv = await asyncio.to_thread(self.client.get_deliveries, None, None, 50, 0)
                self.deliveries_data = deliv
            except Exception:
                self.deliveries_data = {}

            try:
                users_res = await asyncio.to_thread(self.client.list_users, 50, 0)
                self.users_data = users_res.get("users", [])
            except Exception:
                self.users_data = []

        self._render_all()

    def _render_all(self) -> None:
        # Overview
        overview_widget = self.query_one("#overview-content", Static)
        if not self.backend_online:
            overview_widget.update(
                f"[bold red][OFFLINE] EDITH Backend is unreachable at {self.client.base_url}[/bold red]\n\n"
                "To start the assistant services, open a terminal and execute:\n"
                "  • start.bat (Windows)\n"
                "  • python main.py (Universal)\n\n"
                "Press 'r' to retry connection, or 'q' to exit."
            )
            return

        status_str = self.overview_data.get("status", "unknown").upper()
        status_color = "green" if status_str == "HEALTHY" else "yellow"
        deps = self.overview_data.get("dependencies", {})
        ollama = deps.get("ollama", {})
        wa = deps.get("whatsapp", {})
        tg = deps.get("telegram", {})
        queues = self.overview_data.get("queues", {})
        inbound = queues.get("inbound", {})
        outbound = queues.get("outbound", {})

        overview_md = (
            f"## System Status: [{status_color}]{status_str}[/{status_color}]\n\n"
            f"**Backend Host:** `{self.client.base_url}` | **Python:** Active | **SQLite:** Connected\n\n"
            f"### Dependency State\n"
            f"- **Ollama Inference:** {'[green]ONLINE[/green]' if ollama.get('reachable') else '[red]OFFLINE[/red]'} "
            f"({ollama.get('installed_model_count', 0)} models installed, Default: `{ollama.get('effective_default_model') or 'None'}`)\n"
            f"- **WhatsApp Bridge:** {'[green]CONNECTED[/green]' if wa.get('connected') else ('[yellow]WAITING FOR PAIRING[/yellow]' if wa.get('reachable') else '[red]OFFLINE[/red]')}\n"
            f"- **Telegram Gateway:** {'[green]CONFIGURED[/green]' if tg.get('configured') else '[red]UNCONFIGURED[/red]'}\n\n"
            f"### Backlog & Pipeline\n"
            f"- **Inbound Inbox:** {inbound.get('received', 0)} received, {inbound.get('completed', 0)} completed, {inbound.get('failed', 0)} failed\n"
            f"- **Outbound Queue:** {outbound.get('pending', 0)} pending, {outbound.get('sent', 0)} sent, {outbound.get('failed', 0)} failed, [magenta]{outbound.get('ambiguous', 0)} ambiguous[/magenta]\n"
            f"- **Active Tools:** {self.overview_data.get('tools_count', 0)} registered\n"
            f"- **Pending Confirmations:** {self.overview_data.get('pending_actions_count', 0)}\n"
        )
        warnings = self.overview_data.get("warnings", [])
        if warnings:
            overview_md += "\n### Operational Warnings\n"
            for w in warnings:
                overview_md += f"- [yellow]{w}[/yellow]\n"

        overview_widget.update(overview_md)

        # Models Table
        models_table = self.query_one("#models-table", DataTable)
        models_table.clear()
        eff_def = self.overview_data.get("dependencies", {}).get("ollama", {}).get("effective_default_model")
        for m in self.models_data.get("models", []):
            name = m.get("name", "")
            is_def = (name == eff_def) or (name.split(":")[0] == eff_def)
            def_badge = "[bold green]YES[/bold green]" if is_def else "No"
            size_mb = format_bytes(m.get("size", 0))
            mod = (m.get("modified_at") or "")[:19].replace("T", " ")
            models_table.add_row(name, size_mb, def_badge, mod)

        # Tools Table & Policy
        policy = self.tools_data.get("policy", {})
        policy_widget = self.query_one("#tools-policy-content", Static)
        policy_widget.update(
            f"### Tool Execution Policy\n"
            f"- **Execution Enabled:** {'Yes' if policy.get('enable_tool_execution') else 'No'}\n"
            f"- **External Network Tools:** {'[yellow]Allowed[/yellow]' if policy.get('allow_external_network_tools') else '[green]Disabled (Offline Only)[/green]'}\n"
            f"- **Bounds:** Max {policy.get('max_rounds')} rounds, {policy.get('max_calls_per_request')} calls/request, {policy.get('timeout_seconds')}s timeout\n"
            f"- **Workspace Root:** `{policy.get('workspace_root')}`\n"
        )
        tools_table = self.query_one("#tools-table", DataTable)
        tools_table.clear()
        for t in self.tools_data.get("tools", []):
            perm = t.get("permission", "read_only")
            conf = "[red]Required[/red]" if t.get("requires_confirmation") else "No"
            tools_table.add_row(t.get("name", ""), perm, conf, t.get("description", ""))

        # Queues & Deliveries Table
        queues_summary = self.query_one("#queues-summary-content", Static)
        queues_summary.update(
            f"### Queue Backlog Overview\n"
            f"Inbound: {inbound.get('total', 0)} total | Outbound: {outbound.get('total', 0)} total | "
            f"Ambiguous: [magenta]{outbound.get('ambiguous', 0)}[/magenta] | Failed: [red]{outbound.get('failed', 0)}[/red]"
        )
        deliv_table = self.query_one("#deliveries-table", DataTable)
        deliv_table.clear()
        for o in self.deliveries_data.get("outbound", []):
            st = o.get("status", "")
            st_badge = f"[green]{st}[/green]" if st == "SENT" else (
                f"[magenta]{st}[/magenta]" if st == "AMBIGUOUS" else f"[red]{st}[/red]"
            )
            deliv_table.add_row(
                str(o.get("id")),
                o.get("platform", ""),
                o.get("chat_id", ""),
                st_badge,
                f"{o.get('attempt_count', 0)}/{o.get('max_attempts', 3)}",
                o.get("correlation_id", ""),
                o.get("last_error") or "",
            )

        # Integrations
        integ_widget = self.query_one("#integrations-content", Static)
        wa_info = self.integrations_data.get("whatsapp", {})
        tg_info = self.integrations_data.get("telegram", {})
        integ_widget.update(
            f"### Messaging Integrations\n\n"
            f"#### WhatsApp (Baileys Bridge)\n"
            f"- **Connection State:** {'[green]CONNECTED[/green]' if wa_info.get('connected') else ('[yellow]WAITING FOR PAIRING[/yellow]' if wa_info.get('reachable') else '[red]OFFLINE[/red]')}\n"
            f"- **Bridge URL:** `{wa_info.get('bridge_url')}`\n"
            f"- **Configured Phone:** `{wa_info.get('configured_phone_masked', 'None')}`\n"
            f"- **Bridge Secret:** {'Configured' if wa_info.get('has_bridge_secret') else 'None'}\n\n"
            f"#### Telegram (Bot API Gateway)\n"
            f"- **Status:** {'[green]READY[/green]' if tg_info.get('configured') else '[red]UNCONFIGURED[/red]'}\n"
            f"- **Token:** `{tg_info.get('token_masked') or 'None'}`\n"
        )

        # Users Table
        users_table = self.query_one("#users-table", DataTable)
        users_table.clear()
        for u in self.users_data:
            admin_str = "[green]Yes[/green]" if u.get("is_admin") else "No"
            users_table.add_row(
                str(u.get("id")),
                u.get("external_id_masked") or u.get("external_id", ""),
                u.get("display_name", ""),
                admin_str,
                str(u.get("conversation_count", 0)),
                u.get("effective_model") or "Default",
            )


def run_tui(client: EdithApiClient) -> None:
    """Entrypoint to execute the interactive terminal UI."""
    app = EdithTUIApp(client)
    app.run()
