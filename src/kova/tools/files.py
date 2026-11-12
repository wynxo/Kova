"""File tools confined to the configured folder."""

from __future__ import annotations

import json
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
    """Create file tools for a context."""
    def list_files(args: dict) -> str:
        path = resolve_in(ctx.config.files_path, args.get("path", "."))
        return json.dumps(sorted(item.name for item in path.iterdir()))

    def read_file(args: dict) -> str:
        path = resolve_in(ctx.config.files_path, args["path"])
        with path.open("rb") as file:
            data = file.read(100 * 1024 + 1)
        truncated = len(data) > 100 * 1024
        return data[:100 * 1024].decode("utf-8", errors="replace") + ("\n[truncated]" if truncated else "")

    def write_file(args: dict) -> str:
        path = resolve_in(ctx.config.files_path, args["path"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(args["content"], encoding="utf-8")
        return f"Wrote {args['path']}"

    path_property = {"type": "string", "description": "Path relative to the files folder"}
    return [
        Tool("list_files", "List the files in a folder inside the Kova files folder. Leave path empty for the top level.", {"type": "object", "properties": {"path": path_property}}, list_files),
        Tool("read_file", "Read a text file from the Kova files folder (up to 100 KB).", {"type": "object", "properties": {"path": path_property}, "required": ["path"]}, read_file),
        Tool("write_file", "Create or overwrite a text file in the Kova files folder. Folders are created as needed.", {"type": "object", "properties": {"path": path_property, "content": {"type": "string"}}, "required": ["path", "content"]}, write_file),
    ]
