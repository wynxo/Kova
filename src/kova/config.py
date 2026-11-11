"""Kova's config file (~/.kova/config.toml) and login token."""

from __future__ import annotations

import os
import secrets
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import tomli_w

DEFAULT_MODEL = {
    "name": "tiel",
    "base_url": "http://192.168.178.29:8080/v1",
    "model": "cyber-tiel-coder-35b-a3b-mtp-ud-iq3_xxs",
    "context": 131072,
}


@dataclass
class ModelEntry:
    name: str
    base_url: str
    model: str
    api_key: str | None = None
    context: int = 131072


@dataclass
class Config:
    home: Path
    models: list[ModelEntry]
    active: str
    memory_path: Path
    files_path: Path
    port: int = 7700
    memory_write_subdir: str | None = None
    _extra: dict = field(default_factory=dict, repr=False)

    def active_model(self) -> ModelEntry:
        for m in self.models:
            if m.name == self.active:
                return m
        raise KeyError(self.active)


def kova_home() -> Path:
    env = os.environ.get("KOVA_HOME")
    return Path(env).expanduser() if env else Path.home() / ".kova"


def _default(home: Path) -> Config:
    return Config(
        home=home,
        models=[ModelEntry(**DEFAULT_MODEL)],
        active=DEFAULT_MODEL["name"],
        memory_path=home / "memory",
        files_path=Path.home() / "Kova Files",
    )


def load_config(home: Path | None = None) -> Config:
    home = Path(home) if home else kova_home()
    home.mkdir(parents=True, exist_ok=True)
    path = home / "config.toml"
    if not path.exists():
        cfg = _default(home)
        save_config(cfg)
        return cfg
    data = tomllib.loads(path.read_text())
    models = [ModelEntry(**m) for m in data.get("models", [])] or [ModelEntry(**DEFAULT_MODEL)]
    mem = data.get("memory", {})
    return Config(
        home=home,
        models=models,
        active=data.get("active", models[0].name),
        memory_path=Path(mem.get("path", str(home / "memory"))).expanduser(),
        memory_write_subdir=mem.get("write_subdir") or None,
        files_path=Path(data.get("files_path", str(Path.home() / "Kova Files"))).expanduser(),
        port=int(data.get("port", 7700)),
    )


def save_config(cfg: Config) -> None:
    models = []
    for m in cfg.models:
        d = {"name": m.name, "base_url": m.base_url, "model": m.model, "context": m.context}
        if m.api_key:
            d["api_key"] = m.api_key
        models.append(d)
    memory = {"path": str(cfg.memory_path)}
    if cfg.memory_write_subdir:
        memory["write_subdir"] = cfg.memory_write_subdir
    data = {
        "active": cfg.active,
        "port": cfg.port,
        "files_path": str(cfg.files_path),
        "memory": memory,
        "models": models,
    }
    cfg.home.mkdir(parents=True, exist_ok=True)
    (cfg.home / "config.toml").write_text(tomli_w.dumps(data))


def ensure_token(home: Path) -> str:
    path = Path(home) / "token"
    if path.exists():
        return path.read_text().strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token)
    return token
