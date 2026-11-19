#!/usr/bin/env python3
"""Validate Kova theme files and measure their contrast.

Themes are untrusted input: they arrive from the gallery, from a friend, or from a
folder the user dropped a file into. This validates one against
`design/theme.schema.json` (a hand-rolled subset of JSON Schema - no dependency), then
checks the two things a schema cannot express:

1. **Contrast.** The theme is merged over its base and every declared WCAG pair must
   still pass, using the same maths as `scripts/build_tokens.py`.
2. **No smuggled values.** Only semantic tokens may be overridden, and only with hex or
   rgb()/rgba(). Unknown keys are errors, not warnings.

Usage:
    python scripts/validate_theme.py themes/*.json
    python scripts/validate_theme.py --explain my-theme.json
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import re
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "design" / "theme.schema.json"


def _load_tokens_module():
    spec = importlib.util.spec_from_file_location(
        "kova_build_tokens", ROOT / "scripts" / "build_tokens.py"
    )
    if spec is None or spec.loader is None:  # pragma: no cover - broken checkout
        raise RuntimeError("cannot import scripts/build_tokens.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


tokens = _load_tokens_module()
from kova import themes
from kova.theme_contrast import contrast_failures


# --------------------------------------------------------------- a small JSON Schema


def _type_ok(value: Any, expected: str) -> bool:
    checks = {
        "object": lambda v: isinstance(v, dict),
        "array": lambda v: isinstance(v, list),
        "string": lambda v: isinstance(v, str),
        "boolean": lambda v: isinstance(v, bool),
        "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
        "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
        "null": lambda v: v is None,
    }
    check = checks.get(expected)
    return True if check is None else check(value)


def _resolve_ref(ref: str, root: dict[str, Any]) -> dict[str, Any]:
    """Resolve a local `#/$defs/name` pointer. Remote refs are refused on purpose."""
    if not ref.startswith("#/"):
        raise ValueError(f"only local $ref is supported, got {ref!r}")
    node: Any = root
    for part in ref[2:].split("/"):
        node = node[part]
    return node


def validate(instance: Any, schema: dict[str, Any], root: dict[str, Any], path: str = "$") -> list[str]:
    """Validate `instance`, returning a list of `path: message` strings."""
    errors: list[str] = []
    if "$ref" in schema:
        return validate(instance, _resolve_ref(schema["$ref"], root), root, path)

    # A background is one of two disjoint shapes. Exactly one branch must pass,
    # so an image cannot also carry gradient stops (and vice versa).
    if "oneOf" in schema:
        branches = [validate(instance, branch, root, path) for branch in schema["oneOf"]]
        if sum(1 for branch in branches if not branch) != 1:
            errors.append(f"{path}: does not match exactly one allowed shape")
        return errors

    expected = schema.get("type")
    if expected is not None:
        types = expected if isinstance(expected, list) else [expected]
        if not any(_type_ok(instance, t) for t in types):
            return [f"{path}: expected {' or '.join(types)}, got {type(instance).__name__}"]

    if "enum" in schema and instance not in schema["enum"]:
        errors.append(f"{path}: must be one of {schema['enum']}, got {instance!r}")

    if isinstance(instance, (int, float)) and not isinstance(instance, bool):
        if "minimum" in schema and instance < schema["minimum"]:
            errors.append(f"{path}: smaller than {schema['minimum']}")
        if "maximum" in schema and instance > schema["maximum"]:
            errors.append(f"{path}: larger than {schema['maximum']}")

    if isinstance(instance, str):
        if "minLength" in schema and len(instance) < schema["minLength"]:
            errors.append(f"{path}: shorter than {schema['minLength']} characters")
        if "maxLength" in schema and len(instance) > schema["maxLength"]:
            errors.append(f"{path}: longer than {schema['maxLength']} characters")
        if "pattern" in schema and not re.search(schema["pattern"], instance):
            errors.append(f"{path}: {instance!r} does not match {schema['pattern']}")

    if isinstance(instance, list):
        if "minItems" in schema and len(instance) < schema["minItems"]:
            errors.append(f"{path}: needs at least {schema['minItems']} items")
        if "maxItems" in schema and len(instance) > schema["maxItems"]:
            errors.append(f"{path}: allows at most {schema['maxItems']} items")
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(instance):
                errors.extend(validate(item, item_schema, root, f"{path}[{index}]"))

    if isinstance(instance, dict):
        for required in schema.get("required", []):
            if required not in instance:
                errors.append(f"{path}: missing required key {required!r}")
        properties = schema.get("properties", {})
        extra = schema.get("additionalProperties", True)
        for key, value in instance.items():
            if key in properties:
                errors.extend(validate(value, properties[key], root, f"{path}.{key}"))
            elif extra is False:
                errors.append(f"{path}: unexpected key {key!r}")
            elif isinstance(extra, dict):
                errors.extend(validate(value, extra, root, f"{path}.{key}"))
        names = schema.get("propertyNames")
        if isinstance(names, dict) and "pattern" in names:
            for key in instance:
                if not re.search(names["pattern"], key):
                    errors.append(f"{path}: key {key!r} does not match {names['pattern']}")
    return errors


# --------------------------------------------------------------------- theme semantics


def load_schema() -> dict[str, Any]:
    with SCHEMA_PATH.open(encoding="utf-8") as handle:
        return json.load(handle)


def merged_tokens(theme: dict[str, Any]) -> dict[str, Any]:
    """The theme's semantic tokens laid over the base it extends."""
    base = theme.get("extends", "kova-dark" if theme.get("appearance") == "dark" else "kova-light")
    merged = tokens.theme_tokens(base)
    for group, values in (theme.get("tokens") or {}).items():
        for key, value in values.items():
            merged[f"{group}.{key}"] = value
    if "terminal" in theme:
        merged["terminal"] = theme["terminal"]
    return merged


def check_theme(theme: dict[str, Any]) -> list[str]:
    """Semantic checks a schema cannot express. Returns failures."""
    merged = merged_tokens(theme)
    terminal = merged.pop("terminal", None)
    return contrast_failures(merged, terminal)


def check_file(path: Path) -> list[str]:
    try:
        with path.open(encoding="utf-8") as handle:
            theme = json.load(handle)
    except json.JSONDecodeError as error:
        return [f"{path}: not valid JSON ({error.msg} at line {error.lineno})"]
    schema = load_schema()
    errors = [f"{path}: {e}" for e in validate(theme, schema, schema)]
    if errors:
        return errors
    try:
        themes.validate(theme)
    except themes.ThemeError as exc:
        return [f"{path}: {exc}"]
    return [f"{path}: {failure}" for failure in check_theme(theme)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("paths", nargs="+", type=Path, help="theme files to validate")
    parser.add_argument("--explain", action="store_true", help="print effective tokens on success")
    args = parser.parse_args(argv)

    failures: list[str] = []
    for path in args.paths:
        problems = check_file(path)
        failures.extend(problems)
        if not problems:
            print(f"ok {path}")
            if args.explain:
                for name, value in sorted(merged_tokens(json.loads(path.read_text())).items()):
                    if not isinstance(value, list):
                        print(f"    {name}: {value}")

    if failures:
        for failure in failures:
            print(f"FAIL {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
