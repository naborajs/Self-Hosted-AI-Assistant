from __future__ import annotations

import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console

from config import settings
from edith import __version__
from edith.client import (
    EdithApiClient,
    EdithApiAuthError,
    EdithApiConnectionError,
    EdithApiError,
    EdithApiRequestError,
)
from edith.formatter import (
    console,
    print_deliveries_table,
    print_doctor_results,
    print_integrations_overview,
    print_json,
    print_memory_table,
    print_models_table,
    print_panel,
    print_status_overview,
    print_tools_table,
    print_user_detail,
    print_users_table,
)

pass_context = click.make_pass_decorator(dict, ensure=True)


def json_flag(f):
    """Decorator to allow --json flag on subcommands as well as root."""
    def callback(ctx, param, value):
        if value:
            ctx.ensure_object(dict)["json"] = True
        return value
    return click.option("--json", is_flag=True, is_eager=True, callback=callback, expose_value=False, help="Output results as structured JSON.")(f)


def get_client(ctx_obj: dict[str, Any]) -> EdithApiClient:
    """Retrieve or create an authenticated EdithApiClient."""
    client = ctx_obj.get("client")
    if client is None:
        url = ctx_obj.get("url") or os.getenv("EDITH_API_URL", f"http://{settings.app_host}:{settings.app_port}")
        token = ctx_obj.get("token") or os.getenv("EDITH_ADMIN_TOKEN")
        client = EdithApiClient(base_url=url, token=token)

        username = ctx_obj.get("username") or os.getenv("EDITH_ADMIN_USERNAME")
        password = ctx_obj.get("password") or os.getenv("EDITH_ADMIN_PASSWORD")
        if not token and username and password:
            try:
                client.authenticate(username, password)
            except EdithApiError as exc:
                if ctx_obj.get("json"):
                    print_json({"error": str(exc), "exit_code": 2})
                else:
                    console.print(f"[bold red]Authentication Error:[/bold red] {exc}")
                sys.exit(2)

        ctx_obj["client"] = client
    return client


def handle_api_call(ctx_obj: dict[str, Any], fn, *args, **kwargs) -> Any:
    """Safely execute an API call and print error messages with appropriate exit codes."""
    is_json = ctx_obj.get("json", False)
    try:
        return fn(*args, **kwargs)
    except EdithApiAuthError as auth_err:
        if is_json:
            print_json({"error": str(auth_err), "code": 401, "exit_code": 2})
        else:
            console.print(f"[bold red]Authentication Error:[/bold red] {auth_err}")
        sys.exit(2)
    except EdithApiConnectionError as conn_err:
        if is_json:
            print_json({"error": str(conn_err), "code": 503, "exit_code": 1})
        else:
            console.print(f"[bold red]Connection Error:[/bold red] {conn_err}")
        sys.exit(1)
    except EdithApiRequestError as req_err:
        if is_json:
            print_json({"error": str(req_err), "code": req_err.status_code, "detail": req_err.detail, "exit_code": 1})
        else:
            console.print(f"[bold red]Request Error ({req_err.status_code}):[/bold red] {req_err}")
        sys.exit(1)
    except Exception as exc:
        if is_json:
            print_json({"error": str(exc), "exit_code": 1})
        else:
            console.print(f"[bold red]Unexpected Error:[/bold red] {exc}")
        sys.exit(1)


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--url", envvar="EDITH_API_URL", help="EDITH FastAPI backend base URL.")
@click.option("--token", envvar="EDITH_ADMIN_TOKEN", help="Administrator JWT bearer token.")
@click.option("--username", envvar="EDITH_ADMIN_USERNAME", help="Admin username for login.")
@click.option("--password", envvar="EDITH_ADMIN_PASSWORD", help="Admin password for login.")
@click.option("--json", "json_mode", is_flag=True, help="Output results as structured JSON.")
@click.version_option(__version__, "-v", "--version", prog_name="EDITH")
@pass_context
def main(
    ctx: dict[str, Any],
    url: str | None,
    token: str | None,
    username: str | None,
    password: str | None,
    json_mode: bool,
) -> None:
    """EDITH — Professional Terminal Interface & Operator Control."""
    ctx["url"] = url
    ctx["token"] = token
    ctx["username"] = username
    ctx["password"] = password
    ctx["json"] = json_mode


# ============================================================================
# System Commands
# ============================================================================

@main.command("version")
@json_flag
@pass_context
def version_cmd(ctx: dict[str, Any]) -> None:
    """Display EDITH version and build information."""
    info = {
        "name": "EDITH",
        "version": __version__,
        "python": sys.version.split()[0],
        "platform": sys.platform,
        "operating_model": "local-first-inference",
    }
    if ctx.get("json"):
        print_json(info)
    else:
        console.print(f"[bold cyan]EDITH Core[/bold cyan] v{__version__} (Python {info['python']} on {info['platform']})")


@main.command("status")
@json_flag
@pass_context
def status_cmd(ctx: dict[str, Any]) -> None:
    """Display real-time health and operational status of all services."""
    client = get_client(ctx)
    try:
        overview = client.get_overview()
        if ctx.get("json"):
            print_json(overview)
        else:
            print_status_overview(overview)
    except EdithApiConnectionError:
        # Backend is offline; show offline state with local details
        offline_data = {
            "status": "degraded",
            "backend_online": False,
            "url": client.base_url,
            "message": "EDITH FastAPI backend is not running or unreachable.",
            "instructions": "Run 'start.bat' or 'python main.py' to start services.",
        }
        if ctx.get("json"):
            print_json(offline_data)
        else:
            console.print("[bold red][OFFLINE] EDITH backend is OFFLINE[/bold red] (connection failed)")
            console.print(f"Target URL: {client.base_url}")
            console.print("Start backend services with: [cyan]start.bat[/cyan] or [cyan]python main.py[/cyan]")
        sys.exit(1)
    except Exception as exc:
        handle_api_call(ctx, lambda: (_ for _ in ()).throw(exc))


@main.command("doctor")
@json_flag
@pass_context
def doctor_cmd(ctx: dict[str, Any]) -> None:
    """Perform diagnostic health checks across all dependencies without inference."""
    checks = []

    # 1. Python runtime
    py_ok = sys.version_info >= (3, 12)
    checks.append({
        "name": "Python Environment",
        "passed": py_ok,
        "message": f"Python {sys.version.split()[0]} (required: >= 3.12)",
    })

    # 2. Configuration & Secrets
    has_secret = bool(settings.session_secret and settings.session_secret.get_secret_value())
    has_admin_hash = bool(settings.admin_password_hash and settings.admin_password_hash.get_secret_value())
    checks.append({
        "name": "Session Secret Security",
        "passed": has_secret,
        "message": "Configured via SESSION_SECRET" if has_secret else "SESSION_SECRET is missing or empty",
    })
    checks.append({
        "name": "Admin Password Security",
        "passed": has_admin_hash,
        "message": "Configured via ADMIN_PASSWORD_HASH" if has_admin_hash else "ADMIN_PASSWORD_HASH is not set",
    })

    # 3. Network & Loopback Policy
    is_loopback = settings.is_loopback_only
    remote_api = settings.allow_remote_api
    net_safe = is_loopback or remote_api
    checks.append({
        "name": "Network Exposure Policy",
        "passed": net_safe,
        "message": f"Host: {settings.app_host} (Loopback: {is_loopback}, Remote API: {remote_api})",
    })

    # 4. Storage & Workspace Paths
    storage_ok = settings.storage_path.exists() and os.access(settings.storage_path, os.W_OK)
    workspace_ok = settings.tool_workspace_root.exists() and os.access(settings.tool_workspace_root, os.W_OK)
    checks.append({
        "name": "Storage Directory",
        "passed": storage_ok,
        "message": f"{settings.storage_path} (Writable: {storage_ok})",
    })
    checks.append({
        "name": "Isolated Tool Workspace",
        "passed": workspace_ok,
        "message": f"{settings.tool_workspace_root} (Writable: {workspace_ok})",
    })

    # 5. SQLite Database
    db_passed = False
    db_msg = ""
    try:
        raw_db_path = settings.database_url.replace("sqlite+aiosqlite:///", "").replace("sqlite:///", "")
        db_path = Path(raw_db_path).resolve()
        if db_path.exists():
            conn = sqlite3.connect(str(db_path))
            cur = conn.cursor()
            cur.execute("PRAGMA journal_mode;")
            jmode = cur.fetchone()[0]
            conn.close()
            db_passed = True
            db_msg = f"{db_path.name} (Journal mode: {jmode})"
        else:
            db_passed = True
            db_msg = f"Database file will be created on startup ({db_path.name})"
    except Exception as exc:
        db_passed = False
        db_msg = f"Database error: {exc}"

    checks.append({
        "name": "SQLite Database",
        "passed": db_passed,
        "message": db_msg,
    })

    # 6. Ollama Server Reachability (WITHOUT inference)
    ollama_ok = False
    ollama_msg = ""
    installed_models = []
    try:
        with httpx.Client(timeout=2.0) as http_client:
            resp = http_client.get(f"{str(settings.ollama_url).rstrip('/')}/api/tags")
            if resp.status_code == 200:
                tags = resp.json().get("models", [])
                installed_models = [m.get("name") for m in tags]
                ollama_ok = True
                ollama_msg = f"Reachable at {settings.ollama_url} ({len(installed_models)} models installed)"
            else:
                ollama_msg = f"Ollama HTTP {resp.status_code}"
    except Exception as exc:
        ollama_msg = f"Unreachable at {settings.ollama_url} (run 'ollama serve')"

    checks.append({
        "name": "Ollama Inference Engine",
        "passed": ollama_ok,
        "message": ollama_msg,
    })

    # 7. Configured Default Model Presence
    if ollama_ok:
        model_name = settings.ollama_model
        model_installed = any(m == model_name or m.startswith(f"{model_name}:") for m in installed_models)
        checks.append({
            "name": f"Configured Model ({model_name})",
            "passed": model_installed,
            "message": f"Installed in Ollama" if model_installed else f"Model '{model_name}' is not installed in Ollama",
        })

    # 8. WhatsApp Bridge Reachability
    bridge_ok = False
    bridge_msg = ""
    try:
        with httpx.Client(timeout=2.0) as http_client:
            resp = http_client.get(f"{str(settings.whatsapp_bridge_url).rstrip('/')}/health")
            if resp.status_code == 200:
                bridge_ok = True
                bridge_msg = f"Reachable at {settings.whatsapp_bridge_url}"
            else:
                bridge_msg = f"Bridge HTTP {resp.status_code}"
    except Exception:
        bridge_msg = f"Unreachable at {settings.whatsapp_bridge_url} (start with 'npm start')"

    checks.append({
        "name": "WhatsApp Baileys Bridge",
        "passed": bridge_ok,
        "message": bridge_msg,
    })

    # 9. Telegram Gateway Config
    tg_ok = bool(settings.telegram_bot_token and ":" in settings.telegram_bot_token)
    checks.append({
        "name": "Telegram Bot Token",
        "passed": tg_ok,
        "message": "Token syntax valid" if tg_ok else "TELEGRAM_BOT_TOKEN missing or invalid format",
    })

    if ctx.get("json"):
        print_json({"doctor_checks": checks, "all_passed": all(c["passed"] for c in checks)})
    else:
        print_doctor_results(checks)

    if not all(c["passed"] for c in checks):
        sys.exit(1)


@main.group("config")
def config_group() -> None:
    """Inspect and validate local system configuration."""
    pass


@config_group.command("check")
@json_flag
@pass_context
def config_check_cmd(ctx: dict[str, Any]) -> None:
    """Validate active configuration settings and security boundaries."""
    conf = {
        "app_host": settings.app_host,
        "app_port": settings.app_port,
        "is_loopback_only": settings.is_loopback_only,
        "allow_remote_api": settings.allow_remote_api,
        "log_level": settings.log_level,
        "ollama_url": str(settings.ollama_url),
        "ollama_model": settings.ollama_model,
        "whatsapp_bridge_url": str(settings.whatsapp_bridge_url),
        "admin_users": settings.admin_list,
        "has_session_secret": bool(settings.session_secret and settings.session_secret.get_secret_value()),
        "has_admin_password_hash": bool(settings.admin_password_hash and settings.admin_password_hash.get_secret_value()),
        "has_whatsapp_bridge_secret": bool(settings.whatsapp_bridge_secret and settings.whatsapp_bridge_secret.get_secret_value()),
        "enable_tool_execution": settings.enable_tool_execution,
        "allow_external_network_tools": settings.allow_external_network_tools,
        "tool_max_rounds": settings.tool_max_rounds,
        "tool_max_calls_per_request": settings.tool_max_calls_per_request,
        "tool_timeout_seconds": settings.tool_timeout_seconds,
        "tool_max_output_chars": settings.tool_max_output_chars,
    }

    if ctx.get("json"):
        print_json(conf)
    else:
        print_panel("Active EDITH Configuration", json.dumps(conf, indent=2))


@main.command("logs")
@click.option("--lines", "-n", default=30, help="Number of log lines to show.")
@json_flag
@pass_context
def logs_cmd(ctx: dict[str, Any], lines: int) -> None:
    """View recent operational log lines."""
    log_candidates = [
        Path.cwd() / "edith.log",
        Path.cwd() / "storage" / "edith.log",
        Path.cwd() / "storage" / "app.log",
    ]
    found_log = next((p for p in log_candidates if p.exists()), None)
    if not found_log:
        if ctx.get("json"):
            print_json({"log_file": None, "message": "Standard output logging is active; no dedicated log file found."})
        else:
            console.print("[dim]Logging is configured to stdout/stderr. No dedicated log file found.[/dim]")
        return

    try:
        with open(found_log, "r", encoding="utf-8", errors="replace") as f:
            all_lines = f.readlines()
            tail_lines = all_lines[-lines:]
        if ctx.get("json"):
            print_json({"file": str(found_log), "lines": tail_lines})
        else:
            print_panel(f"Recent Logs ({found_log.name})", "".join(tail_lines))
    except Exception as exc:
        console.print(f"[bold red]Failed to read log file:[/bold red] {exc}")


# ============================================================================
# Model Management Commands
# ============================================================================

@main.group("models")
def models_group() -> None:
    """Manage installed Ollama AI models and routing preferences."""
    pass


@models_group.command("list")
@click.option("--refresh", is_flag=True, help="Force refresh installed models cache.")
@json_flag
@pass_context
def models_list_cmd(ctx: dict[str, Any], refresh: bool) -> None:
    """List installed Ollama models."""
    client = get_client(ctx)
    models_data = handle_api_call(ctx, client.list_models, refresh=refresh)
    cfg = handle_api_call(ctx, client.get_model_config)
    eff_default = cfg.get("effective_default_model")

    if ctx.get("json"):
        print_json(models_data)
    else:
        print_models_table(models_data, current_default=eff_default)


@models_group.command("status")
@json_flag
@pass_context
def models_status_cmd(ctx: dict[str, Any]) -> None:
    """Display model configuration and default model status."""
    client = get_client(ctx)
    cfg = handle_api_call(ctx, client.get_model_config)
    if ctx.get("json"):
        print_json(cfg)
    else:
        print_panel("Ollama Model Configuration", json.dumps(cfg, indent=2))


@models_group.command("default")
@json_flag
@pass_context
def models_default_cmd(ctx: dict[str, Any]) -> None:
    """Show the currently effective global default model."""
    client = get_client(ctx)
    cfg = handle_api_call(ctx, client.get_model_config)
    effective = cfg.get("effective_default_model")
    if ctx.get("json"):
        print_json({
            "effective_default_model": effective,
            "global_default_model": cfg.get("global_default_model"),
            "fallback_default_model": cfg.get("fallback_default_model"),
        })
    else:
        if effective:
            console.print(f"Current default model: [bold green]{effective}[/bold green]")
        else:
            console.print("[bold yellow]No default model currently available.[/bold yellow]")


@models_group.command("set-default")
@click.argument("model_name")
@json_flag
@pass_context
def models_set_default_cmd(ctx: dict[str, Any], model_name: str) -> None:
    """Set the global default Ollama model (must already be installed)."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.set_default_model, model_name=model_name)
    if ctx.get("json"):
        print_json(res)
    else:
        console.print(f"[bold green][OK] Global default model updated to:[/bold green] {model_name}")


@models_group.command("refresh")
@json_flag
@pass_context
def models_refresh_cmd(ctx: dict[str, Any]) -> None:
    """Force an immediate refresh of Ollama installed model discovery cache."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_models, refresh=True)
    if ctx.get("json"):
        print_json(res)
    else:
        count = res.get("count", 0)
        console.print(f"[bold green][OK] Model cache refreshed.[/bold green] {count} model(s) discovered.")



# ============================================================================
# User and Memory Commands
# ============================================================================

@main.group("users")
def users_group() -> None:
    """Inspect registered messaging users."""
    pass


@users_group.command("list")
@click.option("--limit", default=50, help="Maximum number of users to list.")
@click.option("--offset", default=0, help="Pagination offset.")
@json_flag
@pass_context
def users_list_cmd(ctx: dict[str, Any], limit: int, offset: int) -> None:
    """List registered messaging users with conversation counts and effective models."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_users, limit=limit, offset=offset)
    if ctx.get("json"):
        print_json(res)
    else:
        print_users_table(res.get("users", []))


@users_group.command("show")
@click.argument("user_id", type=int)
@json_flag
@pass_context
def users_show_cmd(ctx: dict[str, Any], user_id: int) -> None:
    """Show details and preferences for a specific user ID."""
    client = get_client(ctx)
    user = handle_api_call(ctx, client.get_user, user_id=user_id)
    if ctx.get("json"):
        print_json(user)
    else:
        print_user_detail(user)


@main.group("memory")
def memory_group() -> None:
    """Manage user facts stored in long-term memory."""
    pass


@memory_group.command("list")
@click.option("--user", "user_id", required=True, type=int, help="Target user ID.")
@json_flag
@pass_context
def memory_list_cmd(ctx: dict[str, Any], user_id: int) -> None:
    """List long-term memory facts for a user."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_memory, user_id=user_id)
    memories = res.get("memory", [])
    if ctx.get("json"):
        print_json(res)
    else:
        print_memory_table(memories, user_id=user_id)


@memory_group.command("search")
@click.option("--user", "user_id", required=True, type=int, help="Target user ID.")
@click.argument("query")
@json_flag
@pass_context
def memory_search_cmd(ctx: dict[str, Any], user_id: int, query: str) -> None:
    """Search long-term memory facts for a user."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_memory, user_id=user_id, query=query)
    memories = res.get("memory", [])
    if ctx.get("json"):
        print_json(res)
    else:
        print_memory_table(memories, user_id=user_id)


@memory_group.command("save")
@click.option("--user", "user_id", required=True, type=int, help="Target user ID.")
@click.argument("key")
@click.argument("value")
@click.option("--tags", default=None, help="Comma-separated tags.")
@json_flag
@pass_context
def memory_save_cmd(ctx: dict[str, Any], user_id: int, key: str, value: str, tags: str | None) -> None:
    """Save or update a fact in long-term memory for a user."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.save_memory, user_id=user_id, key=key, value=value, tags=tags)
    if ctx.get("json"):
        print_json(res)
    else:
        console.print(f"[bold green][OK] Fact saved for User #{user_id}:[/bold green] {key} = {value}")


@memory_group.command("delete")
@click.option("--user", "user_id", required=True, type=int, help="Target user ID.")
@click.argument("key")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@json_flag
@pass_context
def memory_delete_cmd(ctx: dict[str, Any], user_id: int, key: str, yes: bool) -> None:
    """Delete a fact from long-term memory for a user."""
    if not yes:
        click.confirm(f"Are you sure you want to delete fact '{key}' for User #{user_id}?", abort=True)

    client = get_client(ctx)
    res = handle_api_call(ctx, client.delete_memory, user_id=user_id, key=key)
    if ctx.get("json"):
        print_json(res)
    else:
        status_txt = res.get("status", "deleted")
        console.print(f"[bold green][OK] Fact '{key}' status:[/bold green] {status_txt}")



# ============================================================================
# Tool and Permission Commands
# ============================================================================

@main.group("tools")
def tools_group() -> None:
    """Inspect registered AI tools, permissions, and security policy."""
    pass


@tools_group.command("list")
@json_flag
@pass_context
def tools_list_cmd(ctx: dict[str, Any]) -> None:
    """List registered AI tools catalog and execution bounds."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_tools)
    if ctx.get("json"):
        print_json(res)
    else:
        print_tools_table(res)


@tools_group.command("status")
@json_flag
@pass_context
def tools_status_cmd(ctx: dict[str, Any]) -> None:
    """Show tool execution system enabled state and limits."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_tools)
    policy = res.get("policy", {})
    tools = res.get("tools", [])
    data = {
        "tool_execution_enabled": policy.get("enable_tool_execution"),
        "registered_tools_count": len(tools),
        "external_network_tools_allowed": policy.get("allow_external_network_tools"),
    }
    if ctx.get("json"):
        print_json(data)
    else:
        print_panel("Tools Execution Status", json.dumps(data, indent=2))


@tools_group.command("policy")
@json_flag
@pass_context
def tools_policy_cmd(ctx: dict[str, Any]) -> None:
    """Display detailed execution and security policy limits."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_tools)
    policy = res.get("policy", {})
    if ctx.get("json"):
        print_json(policy)
    else:
        print_panel("Tool Execution Policy Limits", json.dumps(policy, indent=2))


@tools_group.command("validate")
@json_flag
@pass_context
def tools_validate_cmd(ctx: dict[str, Any]) -> None:
    """Validate tool registry definitions and parameter schemas."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.list_tools)
    tools = res.get("tools", [])
    valid = True
    issues = []
    for t in tools:
        if not t.get("name"):
            valid = False
            issues.append("Found tool without name")
        if not t.get("description"):
            valid = False
            issues.append(f"Tool '{t.get('name')}' is missing description")
        if "parameters" not in t:
            valid = False
            issues.append(f"Tool '{t.get('name')}' is missing parameters schema")

    result = {
        "valid": valid,
        "tools_checked": len(tools),
        "issues": issues,
    }
    if ctx.get("json"):
        print_json(result)
    else:
        if valid:
            console.print(f"[bold green][OK] Tool registry valid:[/bold green] {len(tools)} tools inspected without schema defects.")
        else:
            console.print(f"[bold red][FAIL] Tool validation issues:[/bold red] {issues}")


# ============================================================================
# Queue and Delivery Commands
# ============================================================================

@main.group("queue")
def queue_group() -> None:
    """Inspect durable inbound and outbound message queues and diagnostics."""
    pass


@queue_group.command("status")
@json_flag
@pass_context
def queue_status_cmd(ctx: dict[str, Any]) -> None:
    """Display queue statistics and message backlog."""
    client = get_client(ctx)
    overview = handle_api_call(ctx, client.get_overview)
    queues = overview.get("queues", {})
    if ctx.get("json"):
        print_json(queues)
    else:
        inbound = queues.get("inbound", {})
        outbound = queues.get("outbound", {})
        console.print(f"Inbound Inbox:   [yellow]{inbound.get('received', 0)} received[/yellow], [green]{inbound.get('completed', 0)} completed[/green], [red]{inbound.get('failed', 0)} failed[/red]")
        console.print(f"Outbound Delivery: [yellow]{outbound.get('pending', 0)} pending[/yellow], [green]{outbound.get('sent', 0)} sent[/green], [red]{outbound.get('failed', 0)} failed[/red], [magenta]{outbound.get('ambiguous', 0)} ambiguous[/magenta]")


@queue_group.command("inbound")
@click.option("--status", "status_filter", default=None, help="Filter by status (e.g. RECEIVED, COMPLETED, FAILED).")
@click.option("--limit", default=30, help="Number of items to show.")
@json_flag
@pass_context
def queue_inbound_cmd(ctx: dict[str, Any], status_filter: str | None, limit: int) -> None:
    """List inbound messages diagnostic details."""
    client = get_client(ctx)
    deliveries = handle_api_call(ctx, client.get_deliveries, status=status_filter, limit=limit)
    if ctx.get("json"):
        print_json(deliveries.get("inbound", []))
    else:
        print_deliveries_table(deliveries, queue_filter="inbound")


@queue_group.command("outbound")
@click.option("--status", "status_filter", default=None, help="Filter by status (e.g. PENDING, SENT, FAILED, AMBIGUOUS).")
@click.option("--limit", default=30, help="Number of items to show.")
@json_flag
@pass_context
def queue_outbound_cmd(ctx: dict[str, Any], status_filter: str | None, limit: int) -> None:
    """List outbound delivery messages diagnostic details."""
    client = get_client(ctx)
    deliveries = handle_api_call(ctx, client.get_deliveries, status=status_filter, limit=limit)
    if ctx.get("json"):
        print_json(deliveries.get("outbound", []))
    else:
        print_deliveries_table(deliveries, queue_filter="outbound")


@queue_group.command("failed")
@click.option("--limit", default=30, help="Number of failed items to show.")
@json_flag
@pass_context
def queue_failed_cmd(ctx: dict[str, Any], limit: int) -> None:
    """List failed outbound delivery messages requiring operator attention."""
    client = get_client(ctx)
    deliveries = handle_api_call(ctx, client.get_deliveries, status="FAILED", limit=limit)
    if ctx.get("json"):
        print_json(deliveries.get("outbound", []))
    else:
        print_deliveries_table(deliveries, queue_filter="outbound")


@queue_group.command("ambiguous")
@click.option("--limit", default=30, help="Number of ambiguous items to show.")
@json_flag
@pass_context
def queue_ambiguous_cmd(ctx: dict[str, Any], limit: int) -> None:
    """List ambiguous outbound delivery messages halted to prevent blind duplicates."""
    client = get_client(ctx)
    deliveries = handle_api_call(ctx, client.get_deliveries, status="AMBIGUOUS", limit=limit)
    if ctx.get("json"):
        print_json(deliveries.get("outbound", []))
    else:
        print_deliveries_table(deliveries, queue_filter="outbound")


@queue_group.command("retry")
@click.argument("message_id", type=int)
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt.")
@json_flag
@pass_context
def queue_retry_cmd(ctx: dict[str, Any], message_id: int, yes: bool) -> None:
    """Safely reset a FAILED or AMBIGUOUS outbound message back to PENDING."""
    if not yes:
        click.confirm(
            f"Are you sure you want to requeue delivery for outbound message #{message_id}?\n"
            f"Note: If this message was AMBIGUOUS, confirm that the recipient did not already receive it.",
            abort=True,
        )

    client = get_client(ctx)
    res = handle_api_call(ctx, client.retry_outbound, message_id=message_id)
    if ctx.get("json"):
        print_json(res)
    else:
        console.print(f"[bold green][OK] Outbound message #{message_id} safely requeued to PENDING.[/bold green]")


# ============================================================================
# Integration Commands
# ============================================================================

@main.group("integrations")
def integrations_group() -> None:
    """Inspect and manage WhatsApp and Telegram messaging integrations."""
    pass


@integrations_group.command("status")
@json_flag
@pass_context
def integrations_status_cmd(ctx: dict[str, Any]) -> None:
    """Display connection status for all messaging gateways."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.get_integrations)
    if ctx.get("json"):
        print_json(res)
    else:
        print_integrations_overview(res)


@integrations_group.command("whatsapp")
@json_flag
@pass_context
def integrations_whatsapp_cmd(ctx: dict[str, Any]) -> None:
    """Show WhatsApp Baileys bridge details and pairing status."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.get_integrations)
    wa = res.get("whatsapp", {})
    if ctx.get("json"):
        print_json(wa)
    else:
        print_panel("WhatsApp Integration Status", json.dumps(wa, indent=2))


@integrations_group.command("telegram")
@json_flag
@pass_context
def integrations_telegram_cmd(ctx: dict[str, Any]) -> None:
    """Show Telegram gateway configuration status."""
    client = get_client(ctx)
    res = handle_api_call(ctx, client.get_integrations)
    tg = res.get("telegram", {})
    if ctx.get("json"):
        print_json(tg)
    else:
        print_panel("Telegram Integration Status", json.dumps(tg, indent=2))


if __name__ == "__main__":
    main()
