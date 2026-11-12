"""File tools confined to the configured folder."""

from __future__ import annotations

from pathlib import Path

from . import Tool, ToolContext, ToolError


def resolve_in(root: Path, path: str) -> Path:
    """Resolve a relative path within root, rejecting escapes."""
    root.mkdir(parents=True, exist_ok=True)
    root = root.resolve()
    candidate = Path(path)
    if candidate.is_absolute():
        raise ToolError("Path is outside the Kova files folder")
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root):
        raise ToolError("Path is outside the Kova files folder")
    return resolved


def tools(ctx: ToolContext) -> list[Tool]:
    """Default-folder bindings; Agent replaces these with per-chat bindings."""
    from .core import FileSession
    return FileSession(ctx.config.files_path).tools()
