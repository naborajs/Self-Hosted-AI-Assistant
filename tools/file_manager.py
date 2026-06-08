from __future__ import annotations

from pathlib import Path

BASE_DIR = Path.cwd()


def _normalize_path(path: str) -> Path:
    candidate = (BASE_DIR / path).resolve()
    if not str(candidate).startswith(str(BASE_DIR.resolve())):
        raise ValueError("Path is outside the permitted workspace")
    return candidate


def list_files(path: str = ".") -> str:
    target = _normalize_path(path)
    if not target.exists():
        return "Path not found."
    return "\n".join(sorted(str(item.relative_to(BASE_DIR)) for item in target.iterdir()))


def read_file_content(path: str) -> str:
    target = _normalize_path(path)
    if not target.is_file():
        raise ValueError("File not found")
    return target.read_text(encoding="utf-8")


def write_file_content(path: str, content: str) -> str:
    target = _normalize_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"Saved file {target.relative_to(BASE_DIR)}"
