from __future__ import annotations

import contextlib
import logging
import os
from pathlib import Path
import re
import tempfile
from typing import Any

from config import settings

logger = logging.getLogger("local_ai_assistant.tools.file_manager")

MAX_FILE_SIZE_BYTES = 100 * 1024  # 100 KB limit for reads and writes
PROTECTED_EXTENSIONS = {".env", ".db", ".sqlite", ".sqlite3", ".pem", ".key", ".shm", ".wal", ".py", ".pyc", ".exe", ".sh", ".git", ".bat", ".cmd"}

PROTECTED_NAMES = {".env", ".git", ".gitignore", "credentials", "secrets", "id_rsa", "session", "assistant.db", "storage.db", "ai_identity.json", "package.json", "requirements.txt", "config.py"}

WINDOWS_RESERVED_NAMES = {
    "con", "prn", "aux", "nul",
    "com1", "com2", "com3", "com4", "com5", "com6", "com7", "com8", "com9",
    "lpt1", "lpt2", "lpt3", "lpt4", "lpt5", "lpt6", "lpt7", "lpt8", "lpt9",
}


class SafePathError(ValueError):
    """Raised when a file path violates sandbox security constraints."""
    pass


def get_default_workspace() -> Path:
    ws = getattr(settings, "tool_workspace_root", None)
    if ws is None:
        ws = Path.cwd() / "storage" / "workspace"
    p = Path(ws).resolve()
    p.mkdir(parents=True, exist_ok=True)
    return p


def _sanitize_error(exc: Exception, base: Path) -> str:
    """Ensure host paths and internal details are not leaked in user-facing error messages."""
    if isinstance(exc, SafePathError):
        return f"Error: {exc}"
    msg = str(exc)
    base_str = str(base)
    msg = msg.replace(base_str, "[workspace]").replace(base_str.replace("\\", "/"), "[workspace]")
    msg = re.sub(r"[a-zA-Z]:[\\/][^'\"\s]+", "[path]", msg)
    return f"Error: {msg}"


def resolve_safe_path(
    path: str | None = None,
    file_path: str | None = None,
    workspace_root: Path | str | None = None,
    app_settings: Any = None,
) -> Path:
    """Resolve and enforce sandbox boundaries for a candidate path."""
    target_str = file_path if file_path is not None else path
    if not target_str or not isinstance(target_str, str) or not target_str.strip():
        raise SafePathError("Invalid path: path cannot be empty.")

    cfg_ws = getattr(app_settings, "tool_workspace_root", None) if app_settings else None
    chosen_root = workspace_root or cfg_ws or get_default_workspace()
    base = Path(chosen_root).resolve()
    base.mkdir(parents=True, exist_ok=True)

    raw = target_str.strip().replace("\\", "/")

    # Reject Windows drive paths (e.g. C:, D:)
    if re.match(r"^[a-zA-Z]:", raw):
        raise SafePathError("Security violation: Absolute Windows drive paths are strictly prohibited.")

    # Reject UNC paths (// or \\)
    if raw.startswith("//") or raw.startswith("\\\\"):
        raise SafePathError("Security violation: UNC network paths are strictly prohibited.")

    # Reject leading absolute slash
    if raw.startswith("/"):
        raise SafePathError("Security violation: Root-relative absolute paths are strictly prohibited.")

    # Reject traversal tokens
    parts = raw.split("/")
    if ".." in parts:
        raise SafePathError("Security violation: Path traversal '..' is strictly prohibited.")

    # Resolve candidate
    candidate = (base / raw).resolve()

    # Enforce strict containment within approved workspace root (relative_to and commonpath)
    try:
        rel = candidate.relative_to(base)
    except ValueError:
        raise SafePathError("Security violation: Path is outside the permitted workspace.")

    try:
        common = os.path.commonpath([str(candidate), str(base)])
        if common != str(base):
            raise SafePathError("Security violation: Path is outside the permitted workspace.")
    except ValueError:
        raise SafePathError("Security violation: Path is outside the permitted workspace.")

    # Check symlink target if path exists and is symlink
    if candidate.is_symlink():
        real_target = Path(os.path.realpath(candidate)).resolve()
        try:
            real_target.relative_to(base)
            if os.path.commonpath([str(real_target), str(base)]) != str(base):
                raise SafePathError("Security violation: Symlink target is outside permitted workspace.")
        except ValueError:
            raise SafePathError("Security violation: Symlink target is outside permitted workspace.")

    # Validate each component of the relative path
    for part in rel.parts:
        part_lower = part.lower()
        stem = part_lower.split(".")[0]
        if part_lower in WINDOWS_RESERVED_NAMES or stem in WINDOWS_RESERVED_NAMES:
            raise SafePathError(f"Security violation: Reserved device name '{part}' is strictly prohibited.")
        if part_lower in PROTECTED_NAMES:
            raise SafePathError(f"Security violation: Access to protected path component '{part}' is strictly prohibited.")
        if part_lower.startswith("."):
            raise SafePathError(f"Security violation: Access to hidden files or directories ('{part}') is strictly prohibited.")
        if any(part_lower.endswith(ext) for ext in PROTECTED_EXTENSIONS):
            raise SafePathError(f"Security violation: Access to protected file type '{part}' is strictly prohibited.")

    return candidate


_normalize_path = resolve_safe_path


def list_files(
    path: str = ".",
    directory: str | None = None,
    workspace_root: Path | None = None,
    app_settings: Any = None,
) -> str:
    """List files in the safe workspace directory."""
    target_str = directory if directory is not None else path
    try:
        base = Path(getattr(app_settings, "tool_workspace_root", None) or workspace_root or get_default_workspace()).resolve()
        target = resolve_safe_path(target_str, workspace_root=base, app_settings=app_settings)
        if not target.exists():
            return "Error: Path not found."
        if not target.is_dir():
            return "Error: Target is not a directory."

        items: list[str] = []
        for item in sorted(target.iterdir()):
            name_lower = item.name.lower()
            if name_lower in PROTECTED_NAMES or any(name_lower.startswith(p) for p in (".env", ".git")):
                continue
            if any(name_lower.endswith(ext) for ext in PROTECTED_EXTENSIONS):
                continue
            try:
                items.append(str(item.relative_to(base)))
            except ValueError:
                continue

        return "\n".join(items) if items else "(empty directory)"
    except Exception as exc:
        logger.warning("list_files error for path '%s': %s", target_str, exc)
        return _sanitize_error(exc, base)


def read_file_content(
    path: str | None = None,
    file_path: str | None = None,
    workspace_root: Path | None = None,
    app_settings: Any = None,
) -> str:
    """Read file content from the safe workspace directory."""
    target_str = file_path if file_path is not None else path
    base = Path(getattr(app_settings, "tool_workspace_root", None) or workspace_root or get_default_workspace()).resolve()
    try:
        target = resolve_safe_path(target_str, workspace_root=base, app_settings=app_settings)
        if not target.exists():
            return f"Error: File not found: '{target_str}'"
        if not target.is_file():
            return f"Error: Path is not a regular file: '{target_str}'"

        file_size = target.stat().st_size
        if file_size > MAX_FILE_SIZE_BYTES:
            return f"Error: File size ({file_size} bytes) exceeds maximum allowed read size of {MAX_FILE_SIZE_BYTES} bytes."

        return target.read_text(encoding="utf-8")
    except Exception as exc:
        logger.warning("read_file_content error for '%s': %s", target_str, exc)
        return _sanitize_error(exc, base)


def write_file_content(
    path: str | None = None,
    content: str = "",
    file_path: str | None = None,
    workspace_root: Path | None = None,
    app_settings: Any = None,
) -> str:
    """Write file content atomically into the safe workspace directory."""
    target_str = file_path if file_path is not None else path
    base = Path(getattr(app_settings, "tool_workspace_root", None) or workspace_root or get_default_workspace()).resolve()
    try:
        target = resolve_safe_path(target_str, workspace_root=base, app_settings=app_settings)

        if not isinstance(content, str):
            return "Error: Content must be a string."

        content_bytes = content.encode("utf-8")
        if len(content_bytes) > MAX_FILE_SIZE_BYTES:
            return f"Error: Content size ({len(content_bytes)} bytes) exceeds maximum allowed write size of {MAX_FILE_SIZE_BYTES} bytes."

        target.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write via temporary file
        temp_fd, temp_path = tempfile.mkstemp(dir=target.parent, prefix=".tmp_write_", text=True)
        try:
            with os.fdopen(temp_fd, "w", encoding="utf-8") as f:
                f.write(content)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, target)
        except Exception:
            if os.path.exists(temp_path):
                with contextlib.suppress(OSError):
                    os.remove(temp_path)
            raise

        rel_path = target.relative_to(base)
        return f"Successfully wrote {len(content)} characters to '{rel_path}'."
    except Exception as exc:
        logger.warning("write_file_content error for '%s': %s", target_str, exc)
        return _sanitize_error(exc, base)
