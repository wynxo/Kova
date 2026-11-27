"""The extensions catalog: browse, verify and install plugins, skills and bots.

The catalog is one JSON index (see ``docs/EXTENSIONS.md``). Each entry names a
plugin, a skill or a bot, pins exactly one ``version`` and points at its ``source``
with a ``sha256`` checksum. Kova reads the index from ``[catalog] url`` and
falls back to the copy bundled in this package, so browsing works offline and
the first-party extensions shipped under ``catalog_data/`` always install.

Installing is two steps, and nothing runs in between:

1. :func:`stage` downloads (or copies) the source into a private temp folder
   and checks its checksum, name and version against the index.
2. The caller shows the user exactly what it adds — for a plugin, the same
   :func:`kova.plugins.describe` screen ``kova plugin install`` uses — and only
   after they confirm does :func:`install_staged` write anything.

A checksum is the SHA-256 of the downloaded bytes (an archive or a skill's
Markdown file), or :func:`tree_sha256` for a plugin or bot folder bundled with Kova.

A bot is data, never code: only its ``bot.toml``, ``persona.md`` and one avatar
image are taken from the download, and :func:`kova.bots.load_bot` validates
them exactly as it validates a bot the user wrote. A catalog bot never arrives
with more than ``manual`` approval or with a working folder from someone
else's machine (see :func:`_catalog_bot`).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import stat
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlparse

import httpx
import tomli_w

from kova import egress

from kova import bots, plugins
from kova.config import kova_home
from kova.skills import Skill, load_skills

#: The index used when ``[catalog] url`` is not set.
DEFAULT_URL = "https://raw.githubusercontent.com/wynxo/Kova/main/src/kova/catalog_data/index.json"
#: Kova's own copy of the index and the extensions it ships.
BUNDLED_DIR = Path(__file__).parent / "catalog_data"
BUNDLED_INDEX = BUNDLED_DIR / "index.json"
#: ``bundled:<path>`` sources live under :data:`BUNDLED_DIR`.
BUNDLED_SCHEME = "bundled:"
#: The index format this Kova reads.
SCHEMA = 1

KINDS = ("plugin", "skill", "bot")
STATUSES = ("available", "coming-soon")
#: Catalog-installed skills live in this folder of the skills root.
SKILL_SUBDIR = "catalog"
#: Version and source of each catalog skill, next to the skills.
SKILL_STATE = ".installed.json"
#: Version and source of each catalog bot, in the bots folder (a dot file, so
#: :func:`kova.bots.list_bots` never mistakes it for a bot).
BOT_STATE = ".catalog.json"
#: The most a downloaded bot may run without asking; the user can raise it.
BOT_CEILING = "manual"

#: Downloads are small text bundles; anything bigger is refused.
MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_ARCHIVE_FILES = 2000
INDEX_TIMEOUT = 8.0
DOWNLOAD_TIMEOUT = 30.0

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]*$")
#: Files that are not part of a plugin's contents for :func:`tree_sha256`.
_TREE_SKIP_DIRS = {".git", "__pycache__", ".venv"}
_TREE_SKIP_FILES = {plugins.STATE_FILE, ".DS_Store"}


class CatalogError(ValueError):
    """An index, download or install the catalog refuses; safe to show."""


@dataclass
class Entry:
    """One extension in the index."""
    name: str
    kind: str
    version: str
    description: str = ""
    author: str = ""
    homepage: str = ""
    url: str = ""
    sha256: str = ""
    permissions: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    status: str = "available"
    issue: int | None = None

    @property
    def available(self) -> bool:
        return self.status == "available"

    def as_dict(self) -> dict:
        return {"name": self.name, "kind": self.kind, "version": self.version,
                "description": self.description, "author": self.author,
                "homepage": self.homepage, "source": {"url": self.url, "sha256": self.sha256},
                "permissions": list(self.permissions), "tags": list(self.tags),
                "status": self.status, "issue": self.issue}

    def matches(self, query: str) -> bool:
        words = query.casefold().split()
        text = " ".join([self.name, self.description, self.author, self.kind,
                         *self.tags]).casefold()
        return all(word in text for word in words)


@dataclass
class Catalog:
    """A loaded index: its entries, where it came from and any fallback reason."""
    entries: list[Entry]
    source: str
    bundled: bool = False
    #: Why the configured index was not used (shown, never fatal).
    warning: str | None = None
    #: Entries the loader skipped, as readable lines.
    skipped: list[str] = field(default_factory=list)

    def get(self, name: str) -> Entry:
        for entry in self.entries:
            if entry.name == name:
                return entry
        raise CatalogError(f"No extension named {name!r} in the catalog. Try: kova catalog search")

    def search(self, query: str = "", kind: str | None = None) -> list[Entry]:
        return [entry for entry in self.entries
                if (kind is None or entry.kind == kind) and entry.matches(query or "")]


# ---------------------------------------------------------------------------
# Reading the index
# ---------------------------------------------------------------------------

def _strings(raw: dict, key: str) -> list[str]:
    value = raw.get(key, [])
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise CatalogError(f"{key!r} must be a list of strings")
    return [" ".join(item.split()) for item in value if item.strip()]


def _text(raw: dict, key: str) -> str:
    value = raw.get(key, "")
    if value is None:
        return ""
    if not isinstance(value, str):
        raise CatalogError(f"{key!r} must be a string")
    return " ".join(value.split())


def parse_entry(raw: object, *, kinds: tuple[str, ...] = KINDS) -> Entry:
    """Validate one index entry; raises :class:`CatalogError` with the reason."""
    if not isinstance(raw, dict):
        raise CatalogError("an entry must be an object")
    name = _text(raw, "name")
    if not _NAME_RE.match(name) or ".." in name:
        raise CatalogError(f"invalid name {name!r}")
    kind = _text(raw, "kind")
    if kind not in kinds:
        raise CatalogError(f"{name}: unsupported kind {kind!r}")
    version = _text(raw, "version")
    if not _VERSION_RE.match(version):
        raise CatalogError(f"{name}: invalid version {version!r}")
    status = _text(raw, "status") or "available"
    if status not in STATUSES:
        raise CatalogError(f"{name}: status must be available or coming-soon")
    source = raw.get("source") or {}
    if not isinstance(source, dict):
        raise CatalogError(f"{name}: source must be an object with url and sha256")
    url = _text(source, "url")
    sha = _text(source, "sha256").lower()
    if status == "available":
        if not url:
            raise CatalogError(f"{name}: source.url is required")
        if not _SHA_RE.match(sha):
            raise CatalogError(f"{name}: source.sha256 must be 64 hex characters")
    issue = raw.get("issue")
    if issue is not None and not (isinstance(issue, int) and not isinstance(issue, bool)):
        raise CatalogError(f"{name}: issue must be a number")
    return Entry(name=name, kind=kind, version=version, description=_text(raw, "description"),
                 author=_text(raw, "author"), homepage=_text(raw, "homepage"),
                 url=url, sha256=sha, permissions=_strings(raw, "permissions"),
                 tags=_strings(raw, "tags"), status=status, issue=issue)


def parse_index(data: object, source: str, *, kinds: tuple[str, ...] = KINDS) -> Catalog:
    """Turn decoded index JSON into a :class:`Catalog`; bad entries are skipped."""
    if not isinstance(data, dict):
        raise CatalogError("The catalog index must be a JSON object.")
    schema = data.get("schema", SCHEMA)
    if not isinstance(schema, int) or schema > SCHEMA:
        raise CatalogError(
            f"The catalog index uses format {schema!r}; this Kova reads format {SCHEMA}. "
            "Run: kova update")
    raw_entries = data.get("extensions")
    if not isinstance(raw_entries, list):
        raise CatalogError("The catalog index has no 'extensions' list.")
    entries: list[Entry] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for raw in raw_entries:
        try:
            entry = parse_entry(raw, kinds=kinds)
        except CatalogError as exc:
            skipped.append(str(exc))
            continue
        if entry.name in seen:
            skipped.append(f"{entry.name}: listed twice; the first entry wins")
            continue
        seen.add(entry.name)
        # Relative sources are relative to the index itself.
        if entry.url and not entry.url.startswith(BUNDLED_SCHEME) and not _is_remote(entry.url):
            entry.url = _join(source, entry.url)
        entries.append(entry)
    entries.sort(key=lambda item: (item.status != "available", item.kind, item.name))
    return Catalog(entries=entries, source=source, skipped=skipped)


def _is_remote(location: str) -> bool:
    return urlparse(location).scheme in ("http", "https")


def _join(base: str, relative: str) -> str:
    if _is_remote(base):
        return urljoin(base, relative)
    if base.startswith(BUNDLED_SCHEME) or base == "bundled":
        return BUNDLED_SCHEME + relative
    path = Path(relative).expanduser()
    return str(path if path.is_absolute() else Path(base).expanduser().parent / path)


def configured_url(config=None) -> str:
    """The index location: ``[catalog] url``, or the default."""
    url = getattr(config, "catalog_url", "") if config is not None else ""
    return (url or "").strip() or DEFAULT_URL


def _http_get(url: str, *, timeout: float, client: httpx.Client | None = None,
              limit: int = MAX_DOWNLOAD_BYTES) -> bytes:
    """GET ``url`` and return at most :data:`MAX_DOWNLOAD_BYTES` of its body."""
    if urlparse(url).scheme != "https" and urlparse(url).hostname not in ("127.0.0.1", "localhost"):
        raise CatalogError(f"Refusing a plain-http download: {url}")
    own = client is None
    client = client or egress.Client(timeout=timeout, follow_redirects=True)
    try:
        with client.stream("GET", url, timeout=timeout) as response:
            if response.status_code != 200:
                raise CatalogError(f"{url} answered HTTP {response.status_code}")
            chunks, size = [], 0
            for chunk in response.iter_bytes():
                size += len(chunk)
                if size > limit:
                    raise CatalogError(f"{url} exceeds the {limit}-byte download limit")
                chunks.append(chunk)
            return b"".join(chunks)
    except httpx.HTTPError as exc:
        raise CatalogError(f"Could not download {url}: {exc}") from exc
    finally:
        if own:
            client.close()


def load_bundled() -> Catalog:
    catalog = parse_index(json.loads(BUNDLED_INDEX.read_text()), "bundled")
    catalog.bundled = True
    return catalog


def load(url: str | None = None, *, client: httpx.Client | None = None,
         fallback: bool = True, kinds: tuple[str, ...] = KINDS) -> Catalog:
    """Load the index from ``url`` (https, a local file or ``bundled``).

    Any failure falls back to the bundled index with ``warning`` set, unless
    ``fallback`` is false.
    """
    url = (url or DEFAULT_URL).strip()
    if url in ("bundled", BUNDLED_SCHEME):
        return load_bundled()
    try:
        if _is_remote(url):
            raw = _http_get(url, timeout=INDEX_TIMEOUT, client=client)
        else:
            raw = Path(url).expanduser().read_bytes()
        try:
            data = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CatalogError(f"{url} is not valid JSON ({exc})") from exc
        return parse_index(data, url, kinds=kinds)
    except (CatalogError, OSError) as exc:
        if not fallback:
            raise CatalogError(str(exc)) from exc
        catalog = load_bundled()
        catalog.warning = f"Using the catalog bundled with Kova: {exc}"
        return catalog


# ---------------------------------------------------------------------------
# Checksums and safe unpacking
# ---------------------------------------------------------------------------

def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tree_sha256(root: Path) -> str:
    """A stable checksum of a folder: every file's relative path and contents.

    Version-control and cache folders and the plugin state file are skipped,
    so a checkout and an installed copy hash the same. Links are refused.
    """
    root = Path(root)
    digest = hashlib.sha256()
    files = []
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in _TREE_SKIP_DIRS for part in relative.parts):
            continue
        if path.is_symlink():
            raise CatalogError(f"Refusing a link inside an extension: {relative.as_posix()}")
        if path.is_file() and path.name not in _TREE_SKIP_FILES and path.suffix != ".pyc":
            files.append((relative.as_posix(), path))
    for name, path in files:
        digest.update(name.encode() + b"\0" + hashlib.sha256(path.read_bytes()).hexdigest().encode() + b"\n")
    return digest.hexdigest()


def _safe_member(name: str) -> str:
    clean = name.replace("\\", "/")
    parts = [part for part in clean.split("/") if part not in ("", ".")]
    if clean.startswith("/") or not parts or ".." in parts or ":" in parts[0]:
        raise CatalogError(f"Refusing an unsafe path in the archive: {name!r}")
    return "/".join(parts)


def _extract(data: bytes, url: str, target: Path) -> None:
    """Unpack a .tar.gz/.tgz/.zip into ``target``: regular files and folders only."""
    lowered = urlparse(url).path.lower() if _is_remote(url) else url.lower()
    count = 0
    if lowered.endswith(".zip"):
        try:
            archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc:
            raise CatalogError(f"Not a zip archive: {exc}") from exc
        with archive:
            for info in archive.infolist():
                count += 1
                if count > MAX_ARCHIVE_FILES:
                    raise CatalogError("The archive has too many files.")
                name = _safe_member(info.filename)
                mode = (info.external_attr >> 16) & 0o170000
                if mode and mode not in (stat.S_IFREG, stat.S_IFDIR):
                    raise CatalogError(f"Refusing a link or device in the archive: {name}")
                destination = target / name
                if info.is_dir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(info))
        return
    if lowered.endswith((".tar.gz", ".tgz", ".tar")):
        try:
            archive = tarfile.open(fileobj=io.BytesIO(data), mode="r:*")
        except tarfile.TarError as exc:
            raise CatalogError(f"Not a tar archive: {exc}") from exc
        with archive:
            for member in archive.getmembers():
                count += 1
                if count > MAX_ARCHIVE_FILES:
                    raise CatalogError("The archive has too many files.")
                name = _safe_member(member.name)
                if not (member.isfile() or member.isdir()):
                    raise CatalogError(f"Refusing a link or device in the archive: {name}")
                destination = target / name
                if member.isdir():
                    destination.mkdir(parents=True, exist_ok=True)
                    continue
                stream = archive.extractfile(member)
                if stream is None:
                    continue
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(stream.read())
        return
    raise CatalogError(f"A plugin source must be a .tar.gz, .tgz or .zip archive: {url}")


# ---------------------------------------------------------------------------
# Staging: fetch + verify, nothing installed yet
# ---------------------------------------------------------------------------

@dataclass
class Staged:
    """A verified download waiting for the user's confirmation."""
    entry: Entry
    workdir: Path
    #: The plugin folder (kind plugin) or the skill's Markdown file (kind skill).
    path: Path
    plugin: plugins.Plugin | None = None
    skill: Skill | None = None
    bot: bots.BotDefinition | None = None
    #: What :func:`_catalog_bot` changed, in plain words, for the confirmation.
    notes: list[str] = field(default_factory=list)

    def describe(self) -> dict:
        """What installing adds, plus where it came from and its checksum."""
        info: dict = {"name": self.entry.name, "kind": self.entry.kind,
                      "version": self.entry.version, "source": self.entry.url,
                      "sha256": self.entry.sha256, "verified": True,
                      "permissions": list(self.entry.permissions)}
        if self.plugin is not None:
            info["plugin"] = plugins.describe(self.plugin)
        if self.skill is not None:
            info["skill"] = {"name": self.skill.name, "description": self.skill.description,
                             "body": self.skill.body}
        if self.bot is not None:
            info["bot"] = describe_bot(self.bot)
            info["notes"] = list(self.notes)
        return info

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


def _bundled_path(url: str) -> Path:
    relative = url[len(BUNDLED_SCHEME):].strip().lstrip("/")
    base = BUNDLED_DIR.resolve()
    path = (BUNDLED_DIR / relative).resolve()
    if not relative or not path.is_relative_to(base):
        raise CatalogError(f"Invalid bundled source: {url}")
    if not path.exists():
        raise CatalogError(f"{url} is not part of this Kova; a newer version may ship it. Run: kova update")
    return path


def _check(entry: Entry, actual: str) -> None:
    if actual != entry.sha256:
        raise CatalogError(
            f"Checksum mismatch for {entry.name} {entry.version}: the catalog pins "
            f"{entry.sha256[:12]}…, the download is {actual[:12]}…. Nothing was installed.")


def read_theme(entry: Entry, index: Catalog, *, limit: int) -> bytes:
    """Read a checksum-pinned JSON payload; never unpack or stage it as a plugin."""
    url = entry.url
    if _is_remote(url):
        data = _http_get(url, timeout=INDEX_TIMEOUT, limit=limit)
    else:
        if _is_remote(index.source) or url.startswith(BUNDLED_SCHEME):
            raise CatalogError("A remote theme catalog cannot read local files.")
        path = Path(url).expanduser()
        if not path.is_file():
            raise CatalogError("Catalog themes must be JSON files.")
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            raise CatalogError("Catalog theme exceeds the download limit.")
    _check(entry, sha256_bytes(data))
    return data


def describe_bot(bot: bots.BotDefinition) -> dict:
    """Everything a bot would be allowed to do, for the confirmation screen."""
    return {"id": bot.id, "name": bot.name, "handle": bot.handle,
            "description": bot.description, "colour": bot.avatar.colour,
            "icon": bot.avatar.icon, "image": bot.avatar.image,
            "model": bot.model.name, "mode": bot.model.mode, "web": bot.model.web,
            "ceiling": bot.permissions.ceiling, "tools": list(bot.permissions.tools),
            "deny": list(bot.permissions.deny), "inherit_rules": bot.permissions.inherit_rules,
            "memory": {"own": bot.memory.own, "budget": bot.memory.budget,
                       "user_memory": bot.memory.user_memory},
            "persona": bot.persona}


def _catalog_bot(folder: Path, entry: Entry) -> tuple[bots.BotDefinition, list[str]]:
    """Validate a downloaded bot and make it safe to install; returns it and what changed.

    Only the definition, the persona and the avatar the definition names are
    kept; any other file in the download is deleted. ``permissions.folder`` is
    dropped (a path on the author's machine means nothing here) and the
    approval ceiling is capped at :data:`BOT_CEILING`.
    """
    definition = folder / bots.DEFINITION
    if not definition.is_file():
        raise CatalogError(f"{entry.name}: the download has no {bots.DEFINITION}.")
    try:
        data = tomllib.loads(definition.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise CatalogError(f"{entry.name}: {bots.DEFINITION} is not valid TOML ({exc}).") from None
    notes: list[str] = []
    permissions = data.get("permissions")
    if isinstance(permissions, dict):
        if permissions.pop("folder", None):
            notes.append("Its working folder is left out; it uses its own folder in Kova Files.")
        ceiling = str(permissions.get("ceiling") or "manual").strip().lower()
        if ceiling in bots.CEILINGS and ceiling != BOT_CEILING:
            permissions["ceiling"] = BOT_CEILING
            notes.append(f"It asks before each action (it wanted '{ceiling}'); "
                         "you can raise this in the bot's settings.")
    avatar = data.get("avatar") if isinstance(data.get("avatar"), dict) else {}
    keep = {bots.DEFINITION, bots.PERSONA}
    image = avatar.get("image")
    if isinstance(image, str) and image:
        mime = next((kind for kind, suffix in bots.AVATAR_TYPES.items()
                     if image.lower().endswith(suffix)), None)
        path = folder / image
        try:
            ok = (mime is not None and Path(image).name == image and path.is_file()
                  and not path.is_symlink() and path.stat().st_size <= bots.MAX_AVATAR_BYTES)
            if ok:
                bots._check_image(path.read_bytes(), mime)
        except (OSError, bots.BotError):
            ok = False
        if ok:
            keep.add(image)
        else:
            avatar.pop("image", None)
            notes.append("Its avatar image is left out: it is missing, too big or not a PNG, JPEG or WebP.")
    for child in list(folder.iterdir()):
        if child.name not in keep:
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
    definition.write_text(tomli_w.dumps(data), encoding="utf-8")
    if data.get("id") != entry.name:
        raise CatalogError(f"The download is the bot {data.get('id')!r}, but the catalog lists "
                           f"{entry.name}. Nothing was installed.")
    target = folder.parent / entry.name
    if folder != target:
        folder.rename(target)
    try:
        bot = bots.load_bot(target)
    except bots.BotError as exc:
        raise CatalogError(f"{entry.name}: {exc}") from None
    return bot, notes


def _fetch_into(entry: Entry, workdir: Path, client: httpx.Client | None) -> Path:
    """Copy or download the entry's source into ``workdir`` after checking it."""
    url = entry.url
    if url.startswith(BUNDLED_SCHEME) or not _is_remote(url):
        source = _bundled_path(url) if url.startswith(BUNDLED_SCHEME) else Path(url).expanduser()
        if source.is_dir():
            _check(entry, tree_sha256(source))
            target = workdir / "src"
            shutil.copytree(source, target, symlinks=False, ignore=shutil.ignore_patterns(
                *_TREE_SKIP_DIRS, *_TREE_SKIP_FILES, "*.pyc"))
            return target
        try:
            data = source.read_bytes()
        except OSError as exc:
            raise CatalogError(f"Could not read {url}: {exc}") from exc
    else:
        data = _http_get(url, timeout=DOWNLOAD_TIMEOUT, client=client)
    _check(entry, sha256_bytes(data))
    if entry.kind == "skill":
        target = workdir / f"{entry.name}.md"
        target.write_bytes(data)
        return target
    target = workdir / "src"
    target.mkdir()
    _extract(data, url, target)
    return target


def stage(entry: Entry, *, version: str | None = None,
          client: httpx.Client | None = None) -> Staged:
    """Fetch and verify an entry into a private temp folder; installs nothing.

    The caller must :meth:`Staged.cleanup` it (``install_staged`` does).
    """
    if not entry.available:
        raise CatalogError(f"{entry.name} is coming soon; it cannot be installed yet.")
    if version and version != entry.version:
        raise CatalogError(
            f"The catalog pins {entry.name} {entry.version}, not {version}. "
            f"Install {entry.name}@{entry.version}, or point [catalog] url at an index that has {version}.")
    workdir = Path(tempfile.mkdtemp(prefix="kova-catalog-"))
    try:
        path = _fetch_into(entry, workdir, client)
        staged = Staged(entry=entry, workdir=workdir, path=path)
        if entry.kind == "plugin":
            found = plugins.find_manifest(path)
            if found is None:
                raise CatalogError(f"{entry.name}: the download has no {plugins.MANIFEST}.")
            try:
                plugin = plugins.parse_manifest(found)
            except plugins.PluginError as exc:
                raise CatalogError(str(exc)) from exc
            if plugin.name != entry.name or plugin.version != entry.version:
                raise CatalogError(
                    f"The download is {plugin.name} {plugin.version}, but the catalog lists "
                    f"{entry.name} {entry.version}. Nothing was installed.")
            staged.path, staged.plugin = found.parent, plugin
        elif entry.kind == "bot":
            found = _find_bot(path)
            if found is None:
                raise CatalogError(f"{entry.name}: the download has no {bots.DEFINITION}.")
            # Move it to its own clean folder, named for its id, for validation.
            folder = workdir / "bot" / "candidate"
            folder.parent.mkdir()
            shutil.move(str(found), folder)
            staged.bot, staged.notes = _catalog_bot(folder, entry)
            staged.path = staged.bot.path
        else:
            loaded = load_skills([path])
            if not loaded or not loaded[0].description or not loaded[0].body:
                raise CatalogError(f"{entry.name}: the download is not a skill (description and body).")
            staged.skill = loaded[0]
        return staged
    except BaseException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise


def _find_bot(root: Path) -> Path | None:
    """The folder holding ``bot.toml``: the root, or its single top folder."""
    if (root / bots.DEFINITION).is_file():
        return root
    children = [child for child in root.iterdir() if child.is_dir() and not child.is_symlink()]
    if len(children) == 1 and (children[0] / bots.DEFINITION).is_file():
        return children[0]
    return None


# ---------------------------------------------------------------------------
# Installing
# ---------------------------------------------------------------------------

def skills_dir(home: Path) -> Path:
    return Path(home) / "skills" / SKILL_SUBDIR


def _skill_state(home: Path) -> dict:
    path = skills_dir(home) / SKILL_STATE
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_skill_state(home: Path, state: dict) -> None:
    plugins._atomic_json(skills_dir(home) / SKILL_STATE, state)


def installed_skills(home: Path | None = None) -> dict[str, dict]:
    """Catalog skills that are on disk, keyed by name: ``{version, source, sha256}``."""
    home = Path(home) if home else kova_home()
    state = _skill_state(home)
    return {name: info for name, info in state.items()
            if isinstance(info, dict) and (skills_dir(home) / f"{name}.md").is_file()}


def _bot_state(home: Path) -> dict:
    try:
        data = json.loads((bots.bot_root(home) / BOT_STATE).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def installed_bots(home: Path | None = None) -> dict[str, dict]:
    """Catalog bots whose folder still exists, keyed by name: ``{version, source, sha256}``."""
    home = Path(home) if home else kova_home()
    root = bots.bot_root(home)
    return {name: info for name, info in _bot_state(home).items()
            if isinstance(info, dict) and (root / name / bots.DEFINITION).is_file()}


def installed_version(entry: Entry, home: Path | None = None) -> str | None:
    """The installed version of an entry, or ``None`` when it is not installed."""
    home = Path(home) if home else kova_home()
    if entry.kind == "bot":
        info = installed_bots(home).get(entry.name)
        return str(info.get("version") or "?") if info else None
    if entry.kind == "plugin":
        for plugin in plugins.installed(home):
            if plugin.name == entry.name:
                return plugin.version
        return None
    info = installed_skills(home).get(entry.name)
    return str(info.get("version") or "?") if info else None


def install_staged(staged: Staged, home: Path | None = None, *, upgrade: bool = False) -> str:
    """Install a confirmed :class:`Staged` download; returns what was installed.

    Always cleans the staging folder up, whether or not it succeeds.
    """
    home = Path(home) if home else kova_home()
    entry = staged.entry
    try:
        if entry.kind == "plugin":
            source = f"catalog:{entry.name}"
            existing = {plugin.name: plugin for plugin in plugins.installed(home)}
            try:
                if entry.name in existing:
                    if not upgrade:
                        raise CatalogError(
                            f"{entry.name} is already installed ({existing[entry.name].version}). "
                            f"Update it with: kova plugin update {entry.name}")
                    plugins.update(entry.name, home, from_dir=staged.path)
                else:
                    plugins.install(staged.path, home, source_label=source)
            except plugins.PluginError as exc:
                raise CatalogError(str(exc)) from exc
        elif entry.kind == "bot":
            _install_bot(staged, home, upgrade=upgrade)
        else:
            target_dir = skills_dir(home)
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / f"{entry.name}.md"
            if target.is_symlink():
                raise CatalogError(f"Refusing to write a skill through a link: {target}")
            if target.exists() and not upgrade:
                raise CatalogError(f"The skill {entry.name} is already installed. Remove it first.")
            fd, temporary = tempfile.mkstemp(dir=target_dir, prefix=".kova-", suffix=".md")
            with os.fdopen(fd, "wb") as stream:
                stream.write(staged.path.read_bytes())
            os.replace(temporary, target)
            state = _skill_state(home)
            state[entry.name] = {"version": entry.version, "source": entry.url, "sha256": entry.sha256}
            _write_skill_state(home, state)
        return f"{entry.name} {entry.version}"
    finally:
        staged.cleanup()


def _install_bot(staged: Staged, home: Path, *, upgrade: bool) -> None:
    """Write a confirmed catalog bot to ``bots/<id>/``.

    A new install never replaces a bot the user already has. An upgrade only
    replaces a bot that came from the catalog, and keeps the user's own
    choices of approval ceiling and working folder, and its notes.
    """
    entry, bot = staged.entry, staged.bot
    assert bot is not None
    root = bots.bot_root(home)
    if root.is_symlink():
        raise CatalogError("The bots folder must not be a link.")
    folder = root / bot.id
    state = _bot_state(home)
    exists = folder.exists() or folder.is_symlink()
    if exists and not (upgrade and bot.id in installed_bots(home)):
        raise CatalogError(
            f"A bot with the id {bot.id} already exists. Rename or remove it first "
            f"(kova bots rm {bot.id}).")
    if exists:
        current = bots.load_bot(folder, kova_home=home) if (folder / bots.DEFINITION).is_file() else None
        if current is not None:
            bot = replace(bot, permissions=replace(
                bot.permissions, ceiling=current.permissions.ceiling,
                folder=current.permissions.folder))
    created = not exists
    try:
        root.mkdir(parents=True, exist_ok=True)
        folder.mkdir(exist_ok=True)
        if bot.avatar.image:
            shutil.copyfile(staged.path / bot.avatar.image, folder / bot.avatar.image)
        bots.save_bot(home, replace(bot, path=folder))
    except bots.BotError as exc:
        if created:
            bots.delete_bot(home, bot.id)
        raise CatalogError(f"{entry.name}: {exc}") from None
    except BaseException:
        if created:
            bots.delete_bot(home, bot.id)
        raise
    state[entry.name] = {"version": entry.version, "source": entry.url, "sha256": entry.sha256}
    plugins._atomic_json(root / BOT_STATE, state)


def remove_skill(name: str, home: Path | None = None) -> bool:
    """Delete a catalog-installed skill and its record."""
    home = Path(home) if home else kova_home()
    if not _NAME_RE.match(name):
        return False
    path = skills_dir(home) / f"{name}.md"
    removed = False
    if path.is_file() and not path.is_symlink():
        path.unlink()
        removed = True
    state = _skill_state(home)
    if state.pop(name, None) is not None:
        _write_skill_state(home, state)
    return removed


def install(name: str, home: Path | None = None, *, version: str | None = None,
            catalog: Catalog | None = None, confirm: Callable[[Staged], bool] | None = None,
            upgrade: bool = False, client: httpx.Client | None = None) -> str | None:
    """Find, verify, confirm and install one extension; ``None`` when declined."""
    home = Path(home) if home else kova_home()
    catalog = catalog or load()
    staged = stage(catalog.get(name), version=version, client=client)
    try:
        if confirm is not None and not confirm(staged):
            staged.cleanup()
            return None
    except BaseException:
        staged.cleanup()
        raise
    return install_staged(staged, home, upgrade=upgrade)


def split_spec(spec: str) -> tuple[str, str | None]:
    """``name@1.2.0`` → ``("name", "1.2.0")``; a bare name has no version pin."""
    name, _, version = spec.strip().partition("@")
    return name.strip(), (version.strip() or None)


def _main(argv: list[str]) -> int:
    """``python -m kova.catalog hash PATH``: the checksum to put in an index."""
    if len(argv) == 2 and argv[0] == "hash":
        path = Path(argv[1])
        print(tree_sha256(path) if path.is_dir() else sha256_bytes(path.read_bytes()))
        return 0
    print("usage: python -m kova.catalog hash PATH")
    return 2


if __name__ == "__main__":  # pragma: no cover
    import sys
    raise SystemExit(_main(sys.argv[1:]))
