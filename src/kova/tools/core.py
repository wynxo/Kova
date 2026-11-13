"""Schemas and per-chat file state for Kova's work tools.

Adapted from Kaine code_tools.py, planning.py and tool_execution.py. Registration
is ordinary dependency injection: no global registry or GUI dependencies.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import Tool, ToolError
from . import code_files
from .commands import run_command

FILE_TOOLS = code_files.FILE_TOOLS
WRITE_TOOLS = code_files.WRITE_TOOLS
CORE_TOOLS = FILE_TOOLS | {"run_command", "update_plan", "ask_user"}


def validate(schema: dict, value, label: str = "arguments") -> None:
    """Validate tool payloads before risk policy, approval, or side effects."""
    kind = schema.get("type")
    checks = {"object": lambda: isinstance(value, dict),
              "array": lambda: isinstance(value, list),
              "string": lambda: isinstance(value, str),
              "boolean": lambda: isinstance(value, bool),
              "integer": lambda: isinstance(value, int) and not isinstance(value, bool),
              "number": lambda: isinstance(value, (int, float)) and not isinstance(value, bool)}
    if kind in checks and not checks[kind]():
        raise ToolError(f"{label} must be {kind}")
    if "enum" in schema and value not in schema["enum"]:
        raise ToolError(f"{label} must be one of {schema['enum']}")
    if kind == "object":
        properties = schema.get("properties", {})
        for name in schema.get("required", []):
            if name not in value:
                raise ToolError(f"{label}.{name} is required")
        if schema.get("additionalProperties") is False and value.keys() - properties.keys():
            raise ToolError(f"Unknown {label} field: {next(iter(value.keys() - properties.keys()))}")
        for name, item in value.items():
            if name in properties:
                validate(properties[name], item, f"{label}.{name}")
    elif kind in {"string", "array"}:
        suffix = "Length" if kind == "string" else "Items"
        if len(value) < schema.get("min" + suffix, 0) or len(value) > schema.get("max" + suffix, float('inf')):
            raise ToolError(f"{label} has invalid length")
        if kind == "array":
            for item in value:
                validate(schema["items"], item, label + " item")
    elif kind in {"integer", "number"}:
        if not schema.get("minimum", -float('inf')) <= value <= schema.get("maximum", float('inf')):
            raise ToolError(f"{label} is out of range")


def schema(properties: dict, required=()) -> dict:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


PATH = {"type": "string", "minLength": 1, "maxLength": 4096}
TEXT = {"type": "string", "maxLength": 1_000_000}


def fingerprint(target: Path) -> str | None:
    if not target.exists():
        return None
    if not target.is_file():
        raise ToolError("Path is not a regular file")
    digest = hashlib.sha256()
    with target.open("rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


class FileSession:
    """Read snapshots survive turns, but stay isolated per chat and working folder."""
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.observed: dict[Path, str | None] = {}

    def prepare(self, name: str, args: dict) -> tuple[Path, str | None] | None:
        target = code_files.resolve_path(args.get("path", "."), str(self.root))
        if name not in WRITE_TOOLS:
            return None
        current = fingerprint(target)
        if target in self.observed and self.observed[target] != current:
            raise ToolError("File changed on disk since it was read. Read it again before writing.")
        self.observed[target] = current
        return target, current

    def check(self, args: dict, prepared: tuple[Path, str | None]) -> None:
        target, expected = prepared
        if code_files.resolve_path(args["path"], str(self.root)) != target or fingerprint(target) != expected:
            raise ToolError("File changed while waiting for approval. Read it again before writing.")

    def execute(self, name: str, args: dict) -> str:
        self.root.mkdir(parents=True, exist_ok=True)
        path = args.get("path") or "."
        target = code_files.resolve_path(path, str(self.root))
        root = str(self.root)
        if name == "read_file":
            observed = fingerprint(target)
            if "start_line" in args or "end_line" in args:
                result = code_files.read_file(root, path, args.get("start_line", 1), args.get("end_line"))
                if not result["ok"]:
                    raise ToolError(result["error"])
                text = json.dumps(result)
            else:
                # Keep Kova's original simple read_file result and 100 KB cap.
                with target.open("rb") as handle:
                    data = handle.read(100 * 1024 + 1)
                if b'\0' in data[:8192]:
                    raise ToolError("File looks binary; cannot read as text")
                text = data[:100 * 1024].decode("utf-8", "replace") + ("\n[truncated]" if len(data) > 100 * 1024 else "")
            if fingerprint(target) != observed:
                raise ToolError("File changed while reading; read it again")
            self.observed[target] = observed
            return text
        if name == "list_files":
            result = code_files.list_files(root, path, args.get("pattern"))
        elif name == "search_files":
            result = code_files.search_files(root, args["query"], path, args.get("regex", False), args.get("glob"))
        elif name == "edit_file":
            self.prepare(name, args)
            result = code_files.edit_file(root, path, args["old_text"], args["new_text"])
        else:
            self.prepare(name, args)
            result = code_files.write_file(root, path, args["content"])
        if not result["ok"]:
            raise ToolError(result["error"])
        if name in WRITE_TOOLS:
            self.observed[target] = fingerprint(target)
        return json.dumps(result)

    def tools(self) -> list[Tool]:
        definitions = [
            ("list_files", "List files under the working folder, skipping dependency/build folders.", schema({"path": {"type": "string", "maxLength": 4096}, "pattern": TEXT})),
            ("read_file", "Read text in the working folder (100 KB). Optional start_line/end_line return numbered lines.", schema({"path": PATH, "start_line": {"type": "integer", "minimum": 1}, "end_line": {"type": "integer", "minimum": 1}}, ["path"])),
            ("write_file", "Atomically create or overwrite a UTF-8 file in the working folder.", schema({"path": PATH, "content": TEXT}, ["path", "content"])),
            ("search_files", "Search text under the working folder, using ripgrep or Python. Literal unless regex is true.", schema({"query": {"type": "string", "minLength": 1, "maxLength": 1000}, "path": {"type": "string", "maxLength": 4096}, "regex": {"type": "boolean"}, "glob": TEXT}, ["query"])),
            ("edit_file", "Replace exact old_text matched once with new_text. Read the file first.", schema({"path": PATH, "old_text": {"type": "string", "minLength": 1, "maxLength": 200000}, "new_text": TEXT}, ["path", "old_text", "new_text"])),
        ]
        return [Tool(name, description, params, lambda args, n=name: self.execute(n, args)) for name, description, params in definitions]


def tools(cwd: Path) -> list[Tool]:
    async def command(args):
        return json.dumps(await run_command(args["command"], cwd, args.get("timeout", 60)))

    def plan(args):
        return json.dumps({"ok": True, "items": args["items"]})

    def question(args):
        raise ToolError("ask_user must be handled by the active conversation")

    return [
        Tool("ask_user", "Ask the user a clarifying question and wait for their answer. Provide options for multiple choice or omit them for free text.", schema({"question": {"type": "string", "minLength": 1, "maxLength": 1000}, "options": {"type": "array", "maxItems": 8, "items": {"type": "string", "minLength": 1, "maxLength": 200}}}, ["question"]), question),
        Tool("run_command", "Run a shell command in the working folder; output and exit code. No stdin or automatic elevation. Default timeout 60 seconds, maximum 300.", schema({"command": {"type": "string", "minLength": 1, "maxLength": 200000}, "timeout": {"type": "number", "minimum": 0.01, "maximum": 300}}, ["command"]), command),
        Tool("update_plan", "Publish a concise checklist for genuine multi-step work. Keep it updated as work progresses.", schema({"items": {"type": "array", "maxItems": 8, "items": schema({"text": {"type": "string", "minLength": 1, "maxLength": 180}, "status": {"type": "string", "enum": ["pending", "in_progress", "done"]}}, ["text", "status"])}}, ["items"]), plan),
    ]
