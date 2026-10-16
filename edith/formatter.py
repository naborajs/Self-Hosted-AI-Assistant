from __future__ import annotations

import json
from typing import Any
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.tree import Tree

console = Console(highlight=False)


def print_json(data: Any) -> None:
    """Print structured data as indented JSON."""
    console.print_json(json.dumps(data, default=str))


def print_panel(title: str, content: Any, style: str = "cyan") -> None:
    """Print text inside a styled panel."""
    console.print(Panel(str(content), title=title, border_style=style, box=box.ASCII))


def status_badge(is_healthy: bool, true_label: str = "ONLINE", false_label: str = "OFFLINE") -> str:
    """Return colored status badge."""
    if is_healthy:
        return f"[bold green]{true_label}[/bold green]"
    return f"[bold red]{false_label}[/bold red]"


def format_bytes(bytes_count: int | float | str | None) -> str:
    """Format bytes count into human readable units."""
    if isinstance(bytes_count, str):
        return bytes_count
    if not bytes_count or bytes_count <= 0:
        return "0 B"
    num = float(bytes_count)
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if num < 1024.0:
            return f"{num:3.1f} {unit}"
        num /= 1024.0
    return f"{num:.1f} PB"


def print_status_overview(overview: dict[str, Any]) -> None:
    """Render a comprehensive overview dashboard."""
    status_str = overview.get("status", "unknown").upper()
    status_style = "green" if status_str == "HEALTHY" else "yellow"

    # Main Header
    sys_info = overview.get("system", {})
    header_text = (
        f"EDITH Core v{sys_info.get('version', '0.1.0')}  |  "
        f"Host: {sys_info.get('host')}:{sys_info.get('port')}  |  "
        f"Loopback: {'Yes' if sys_info.get('loopback_only') else 'No'}  |  "
        f"Remote API: {'Enabled' if sys_info.get('remote_api_allowed') else 'Disabled'}"
    )
    console.print(Panel(header_text, title=f"[{status_style}]System Status: {status_str}[/{status_style}]", border_style=status_style))

    # Dependencies Table
    deps = overview.get("dependencies", {})
    dep_table = Table(title="Dependencies & Services", border_style="dim", show_header=True, box=box.ASCII)
    dep_table.add_column("Component", style="bold")
    dep_table.add_column("State")
    dep_table.add_column("Details")

    ollama = deps.get("ollama", {})
    ollama_state = "[bold green]REACHABLE[/bold green]" if ollama.get("reachable") else "[bold red]UNREACHABLE[/bold red]"
    ollama_details = (
        f"Models: {ollama.get('installed_model_count', 0)} installed  |  "
        f"Effective: {ollama.get('effective_default_model') or 'None'}"
    )
    dep_table.add_row("Ollama Inference", ollama_state, ollama_details)

    wa = deps.get("whatsapp", {})
    wa_state = "[bold green]CONNECTED[/bold green]" if wa.get("connected") else (
        "[bold yellow]WAITING FOR PAIRING[/bold yellow]" if wa.get("reachable") else "[bold red]UNREACHABLE[/bold red]"
    )
    wa_details = f"Reachable: {'Yes' if wa.get('reachable') else 'No'}  |  Pairing Code: {'Available' if wa.get('pairing_code_available') else 'None'}"
    dep_table.add_row("WhatsApp Bridge", wa_state, wa_details)

    tg = deps.get("telegram", {})
    tg_state = "[bold green]CONFIGURED[/bold green]" if tg.get("configured") else "[bold red]UNCONFIGURED[/bold red]"
    dep_table.add_row("Telegram Gateway", tg_state, "Bot token configured" if tg.get("configured") else "Token missing in environment")

    db_healthy = deps.get("database", False)
    db_state = "[bold green]HEALTHY[/bold green]" if db_healthy else "[bold red]ERROR[/bold red]"
    dep_table.add_row("SQLite Database", db_state, "WAL mode active")

    console.print(dep_table)

    # Queues Summary Table
    queues = overview.get("queues", {})
    q_table = Table(title="Message Queues & Processing Pipeline", border_style="dim", show_header=True, box=box.ASCII)
    q_table.add_column("Queue", style="bold")
    q_table.add_column("Pending / In-Flight")
    q_table.add_column("Successful")
    q_table.add_column("Failed / Dead-Letter")
    q_table.add_column("Total")

    inbound = queues.get("inbound", {})
    in_inflight = inbound.get("received", 0) + inbound.get("processing", 0) + inbound.get("response_ready", 0)
    q_table.add_row(
        "Inbound Inbox",
        f"[yellow]{in_inflight}[/yellow]",
        f"[green]{inbound.get('completed', 0)}[/green]",
        f"[red]{inbound.get('failed', 0)}[/red]",
        str(inbound.get("total", 0)),
    )

    outbound = queues.get("outbound", {})
    out_inflight = outbound.get("pending", 0) + outbound.get("sending", 0)
    ambiguous_badge = f" [bold magenta]({outbound.get('ambiguous', 0)} ambiguous)[/bold magenta]" if outbound.get("ambiguous", 0) > 0 else ""
    q_table.add_row(
        "Outbound Delivery",
        f"[yellow]{out_inflight}[/yellow]",
        f"[green]{outbound.get('sent', 0)}[/green]",
        f"[red]{outbound.get('failed', 0)}[/red]{ambiguous_badge}",
        str(outbound.get("total", 0)),
    )

    console.print(q_table)

    # Operational Warnings
    warnings = overview.get("warnings", [])
    if warnings:
        warn_text = "\n".join(f"* [bold yellow]{w}[/bold yellow]" for w in warnings)
        console.print(Panel(warn_text, title="[bold yellow]Operational Warnings[/bold yellow]", border_style="yellow", box=box.ASCII))


def print_doctor_results(checks: list[dict[str, Any]]) -> None:
    """Render system diagnostic check results."""
    table = Table(title="EDITH System Diagnostic (Doctor)", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("Diagnostic Check", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    all_passed = True
    for item in checks:
        passed = item.get("passed", False)
        if not passed:
            all_passed = False
        status_txt = "[bold green]PASS[/bold green]" if passed else "[bold red]FAIL[/bold red]"
        table.add_row(item.get("name", ""), status_txt, item.get("message", ""))

    console.print(table)
    if all_passed:
        console.print("[bold green][OK] All diagnostic checks passed. System is operational.[/bold green]")
    else:
        console.print("[bold red][FAIL] Some diagnostic checks failed. Review details above.[/bold red]")


def print_models_table(models_data: dict[str, Any], current_default: str | None = None) -> None:
    """Render list of installed models."""
    models = models_data.get("models", [])
    table = Table(title=f"Installed Ollama Models ({len(models)} available)", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("Model Name", style="bold")
    table.add_column("Size")
    table.add_column("Default")
    table.add_column("Modified")

    for m in models:
        name = m.get("name", "")
        is_default = (name == current_default) or (name.split(":")[0] == current_default)
        default_badge = "[bold green]CURRENT DEFAULT[/bold green]" if is_default else ""
        size_fmt = format_bytes(m.get("size", 0))
        mod_date = (m.get("modified_at") or "")[:19].replace("T", " ")
        table.add_row(name, size_fmt, default_badge, mod_date)

    console.print(table)


def print_users_table(users: list[dict[str, Any]]) -> None:
    """Render users list."""
    table = Table(title=f"Registered Users ({len(users)})", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("ID", style="bold", justify="right")
    table.add_column("External Identifier", no_wrap=True)
    table.add_column("Display Name")
    table.add_column("Admin")
    table.add_column("Conversations")
    table.add_column("Effective Model")

    for u in users:
        admin_badge = "[green]Yes[/green]" if u.get("is_admin") else "No"
        eff_model = u.get("effective_model") or "[dim]Default[/dim]"
        ext_id = u.get("external_id_masked") or u.get("external_id", "")
        table.add_row(
            str(u.get("id")),
            ext_id,
            u.get("display_name", ""),
            admin_badge,
            str(u.get("conversation_count", 0)),
            eff_model,
        )

    console.print(table)


def print_user_detail(user: dict[str, Any]) -> None:
    """Render single user detail."""
    tree = Tree(f"[bold cyan]User #{user.get('id')} - {user.get('display_name')}[/bold cyan]")
    tree.add(f"External ID: [bold]{user.get('external_id_masked') or user.get('external_id')}[/bold]")
    tree.add(f"Email: {user.get('email') or 'None'}")
    tree.add(f"Admin Role: {'Yes' if user.get('is_admin') else 'No'}")
    tree.add(f"Created: {user.get('created_at') or 'Unknown'}")
    tree.add(f"Conversations: {user.get('conversation_count', 0)}")
    
    prefs = tree.add("Preferences")
    prefs.add(f"Preferred Model: {user.get('preferred_model') or 'None (System Default)'}")
    prefs.add(f"Language: {user.get('preferred_language') or 'Default'}")
    prefs.add(f"Response Style: {user.get('response_style') or 'Default'}")
    prefs.add(f"Effective Model: [bold green]{user.get('effective_model') or 'Default'}[/bold green]")
    console.print(tree)


def print_memory_table(memories: list[dict[str, Any]], user_id: int) -> None:
    """Render long-term memory facts for a user."""
    table = Table(title=f"Long-Term Memory Facts for User #{user_id} ({len(memories)} entries)", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("Key", style="bold cyan")
    table.add_column("Value")
    table.add_column("Tags", style="dim")

    for m in memories:
        table.add_row(m.get("key", ""), m.get("value", ""), m.get("tags") or "")

    console.print(table)


def print_tools_table(tools_data: dict[str, Any]) -> None:
    """Render tools catalog and policy."""
    tools = tools_data.get("tools", [])
    policy = tools_data.get("policy", {})

    # Policy Banner
    policy_text = (
        f"Execution Enabled: {'Yes' if policy.get('enable_tool_execution') else 'No'}  |  "
        f"External Network Tools: {'Allowed' if policy.get('allow_external_network_tools') else 'Disabled (Offline Only)'}\n"
        f"Max Rounds: {policy.get('max_rounds')}  |  "
        f"Max Calls: {policy.get('max_calls_per_request')}  |  "
        f"Timeout: {policy.get('timeout_seconds')}s  |  "
        f"Max Output: {policy.get('max_output_chars')} chars  |  "
        f"Workspace: {policy.get('workspace_root')}"
    )
    console.print(Panel(policy_text, title="Tool Execution & Security Policy", border_style="cyan", box=box.ASCII))

    table = Table(title=f"Registered AI Tools Catalog ({len(tools)} tools)", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("Tool Name", style="bold")
    table.add_column("Permission")
    table.add_column("Confirmation")
    table.add_column("Description")

    for t in tools:
        perm = t.get("permission", "read_only")
        perm_style = "green" if perm == "read_only" else ("yellow" if perm == "network" else "red")
        confirm_badge = "[bold red]REQUIRED[/bold red]" if t.get("requires_confirmation") else "[dim]No[/dim]"
        table.add_row(t.get("name", ""), f"[{perm_style}]{perm}[/{perm_style}]", confirm_badge, t.get("description", ""))

    console.print(table)


def print_integrations_overview(integrations: dict[str, Any]) -> None:
    """Render integrations status."""
    wa = integrations.get("whatsapp", {})
    tg = integrations.get("telegram", {})

    table = Table(title="Integration Gateways", border_style="cyan", show_header=True, box=box.ASCII)
    table.add_column("Integration", style="bold")
    table.add_column("Status")
    table.add_column("Details")

    # WhatsApp
    wa_status = "[bold green]CONNECTED[/bold green]" if wa.get("connected") else (
        "[bold yellow]WAITING FOR PAIRING[/bold yellow]" if wa.get("reachable") else "[bold red]OFFLINE[/bold red]"
    )
    wa_details = (
        f"Phone: {wa.get('configured_phone_masked', 'None')}  |  "
        f"Bridge: {wa.get('bridge_url')}  |  "
        f"Secret: {'Configured' if wa.get('has_bridge_secret') else 'None'}"
    )
    table.add_row("WhatsApp (Baileys Bridge)", wa_status, wa_details)

    # Telegram
    tg_status = "[bold green]READY[/bold green]" if tg.get("configured") else "[bold red]UNCONFIGURED[/bold red]"
    tg_details = f"Token: {tg.get('token_masked') or 'None'}"
    table.add_row("Telegram (Bot API)", tg_status, tg_details)

    console.print(table)


def print_deliveries_table(deliveries: dict[str, Any], queue_filter: str = "all") -> None:
    """Render diagnostic delivery messages."""
    inbound = deliveries.get("inbound", [])
    outbound = deliveries.get("outbound", [])

    if queue_filter in ("all", "inbound") and inbound:
        in_table = Table(title=f"Inbound Queue Diagnostics ({len(inbound)} items)", border_style="cyan", show_header=True, box=box.ASCII)
        in_table.add_column("ID", style="bold", justify="right")
        in_table.add_column("Platform")
        in_table.add_column("Chat ID")
        in_table.add_column("Sender")
        in_table.add_column("Status")
        in_table.add_column("Attempts")
        in_table.add_column("Last Error", style="red")

        for m in inbound:
            st = m.get("status", "")
            st_style = "green" if st == "COMPLETED" else ("yellow" if st in ("PROCESSING", "RESPONSE_READY") else "red")
            in_table.add_row(
                str(m.get("id")),
                m.get("platform", ""),
                m.get("chat_id", ""),
                m.get("sender", ""),
                f"[{st_style}]{st}[/{st_style}]",
                str(m.get("attempt_count", 0)),
                m.get("last_error") or "",
            )
        console.print(in_table)

    if queue_filter in ("all", "outbound") and outbound:
        out_table = Table(title=f"Outbound Delivery Diagnostics ({len(outbound)} items)", border_style="cyan", show_header=True, box=box.ASCII)
        out_table.add_column("ID", style="bold", justify="right")
        out_table.add_column("Platform")
        out_table.add_column("Chat ID")
        out_table.add_column("Status")
        out_table.add_column("Attempts")
        out_table.add_column("Correlation ID")
        out_table.add_column("Last Error", style="red")

        for m in outbound:
            st = m.get("status", "")
            st_style = "green" if st == "SENT" else ("yellow" if st in ("PENDING", "SENDING") else ("magenta" if st == "AMBIGUOUS" else "red"))
            out_table.add_row(
                str(m.get("id")),
                m.get("platform", ""),
                m.get("chat_id", ""),
                f"[{st_style}]{st}[/{st_style}]",
                f"{m.get('attempt_count', 0)}/{m.get('max_attempts', 3)}",
                m.get("correlation_id", ""),
                m.get("last_error") or "",
            )
        console.print(out_table)
