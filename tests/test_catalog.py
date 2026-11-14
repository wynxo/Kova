"""The extensions catalog (#137): index, checksums, install, skills and routes.

All network access goes through ``httpx.MockTransport``; nothing leaves the
machine.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import zipfile
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from kova import catalog, cli, extension_routes, extensions, plugins
from kova.config import ensure_token, load_config, save_config
from kova.server import create_app
from kova.skills import SkillError, Skills
from tests.fake_model import FakeModel

INDEX_URL = "https://catalog.example/index.json"

MANIFEST = """\
name = "demo"
version = "{version}"
description = "A demo plugin."
author = "Tester"
skills = ["skills"]

[[commands]]
name = "demo"
description = "Say hello"
prompt = "Say hello."
"""

SKILL = """---
name: Demo skill
description: How to say hello well.
---
Say hello, then ask how you can help.
"""


def tar_plugin(version="0.1.0", extra: dict[str, bytes] | None = None, prefix="demo/") -> bytes:
    files = {f"{prefix}kova-plugin.toml": MANIFEST.format(version=version).encode(),
             f"{prefix}skills/demo.md": SKILL.encode(), **(extra or {})}
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def entry(name="demo", kind="plugin", version="0.1.0", url="demo-0.1.0.tar.gz", digest="0" * 64, **extra):
    return {"name": name, "kind": kind, "version": version, "description": f"The {name} thing.",
            "author": "Tester", "source": {"url": url, "sha256": digest},
            "permissions": ["Says hello."], "tags": ["demo"], **extra}


def serve(files: dict[str, bytes], seen: list | None = None) -> httpx.Client:
    """A mock HTTPS host serving ``files`` by path."""
    def handler(request: httpx.Request) -> httpx.Response:
        if seen is not None:
            seen.append(str(request.url))
        body = files.get(request.url.path)
        return httpx.Response(200, content=body) if body is not None else httpx.Response(404)
    return httpx.Client(transport=httpx.MockTransport(handler))


def remote(entries: list[dict], files: dict[str, bytes] | None = None):
    index = json.dumps({"schema": 1, "extensions": entries}).encode()
    client = serve({"/index.json": index, **{f"/{k}": v for k, v in (files or {}).items()}})
    return catalog.load(INDEX_URL, client=client, fallback=False), client


# -- the bundled index ------------------------------------------------------------

def test_bundled_index_is_valid_and_every_checksum_is_current():
    loaded = catalog.load("bundled")
    assert loaded.bundled and not loaded.skipped
    names = {item.name for item in loaded.entries}
    assert {"clipboard", "morning-briefing", "file-watcher", "explain-error", "weekly-review"} <= names
    for item in loaded.entries:
        if not item.available:
            continue
        staged = catalog.stage(item)  # verifies checksum, name and version
        try:
            assert staged.describe()["verified"]
            if item.kind == "plugin":
                assert staged.plugin.name == item.name and staged.plugin.version == item.version
        finally:
            staged.cleanup()


def test_bundled_skills_have_real_front_matter():
    from kova.skills import load_skills
    for path in sorted(catalog.BUNDLED_DIR.rglob("*.md")):
        [skill] = load_skills([path])
        assert not skill.name.startswith(("name:", "---")), path.name
        assert skill.description and skill.body and not skill.body.startswith("---"), path.name


def test_coming_soon_entries_are_listed_but_not_installable():
    item = catalog.load("bundled").get("file-watcher")
    assert item.status == "coming-soon" and item.issue == 108
    with pytest.raises(catalog.CatalogError, match="coming soon"):
        catalog.stage(item)


def test_default_url_falls_back_to_bundled_without_network(monkeypatch):
    monkeypatch.setattr(catalog, "DEFAULT_URL", INDEX_URL)

    def offline(*args, **kwargs):
        raise catalog.CatalogError("offline")
    monkeypatch.setattr(catalog, "_http_get", offline)
    loaded = catalog.load(catalog.configured_url(None))
    assert loaded.bundled and "offline" in loaded.warning


def test_search_matches_names_descriptions_and_tags():
    loaded = catalog.load("bundled")
    assert [item.name for item in loaded.search("clip")] == ["clipboard"]
    assert {item.name for item in loaded.search("", "skill")} == {"explain-error", "weekly-review"}
    assert loaded.search("first-party productivity")
    assert loaded.search("no such words anywhere") == []


# -- parsing ----------------------------------------------------------------------

@pytest.mark.parametrize("bad, reason", [
    ({"kind": "theme"}, "kind"),
    ({"name": "Bad Name"}, "invalid name"),
    ({"name": "../up"}, "invalid name"),
    ({"version": ""}, "version"),
    ({"source": {"url": "x.tar.gz", "sha256": "abc"}}, "sha256"),
    ({"source": {"sha256": "0" * 64}}, "url"),
    ({"tags": "demo"}, "tags"),
    ({"status": "beta"}, "status"),
])
def test_invalid_entries_are_skipped_with_a_reason(bad, reason):
    loaded = catalog.parse_index({"extensions": [{**entry(), **bad}, entry(name="good")]}, INDEX_URL)
    assert [item.name for item in loaded.entries] == ["good"]
    assert reason in loaded.skipped[0]


def test_duplicates_keep_the_first_and_newer_schemas_are_refused():
    loaded = catalog.parse_index({"extensions": [entry(), entry(version="9.9.9")]}, INDEX_URL)
    assert [item.version for item in loaded.entries] == ["0.1.0"]
    with pytest.raises(catalog.CatalogError, match="kova update"):
        catalog.parse_index({"schema": 99, "extensions": []}, INDEX_URL)


def test_relative_sources_resolve_against_the_index(tmp_path):
    loaded = catalog.parse_index({"extensions": [entry()]}, INDEX_URL)
    assert loaded.get("demo").url == "https://catalog.example/demo-0.1.0.tar.gz"
    local = catalog.parse_index({"extensions": [entry()]}, str(tmp_path / "index.json"))
    assert local.get("demo").url == str(tmp_path / "demo-0.1.0.tar.gz")


def test_remote_index_errors_fall_back_or_raise():
    broken = serve({"/index.json": b"{not json"})
    assert catalog.load(INDEX_URL, client=broken).bundled
    with pytest.raises(catalog.CatalogError, match="not valid JSON"):
        catalog.load(INDEX_URL, client=broken, fallback=False)
    missing = serve({})
    fallback = catalog.load(INDEX_URL, client=missing)
    assert fallback.bundled and "404" in fallback.warning


def test_plain_http_is_refused():
    with pytest.raises(catalog.CatalogError, match="plain-http"):
        catalog.load("http://catalog.example/index.json", client=serve({}), fallback=False)


# -- staging and installing plugins ----------------------------------------------

def test_install_verifies_shows_and_records_a_catalog_source(tmp_path):
    archive = tar_plugin()
    loaded, client = remote([entry(digest=sha(archive))], {"demo-0.1.0.tar.gz": archive})
    shown = []

    def confirm(staged):
        shown.append(staged.describe())
        return True

    assert catalog.install("demo", tmp_path, catalog=loaded, confirm=confirm, client=client) == "demo 0.1.0"
    info = shown[0]
    assert info["verified"] and info["sha256"] == sha(archive)
    assert info["source"] == "https://catalog.example/demo-0.1.0.tar.gz"
    assert info["plugin"]["commands"][0]["name"] == "demo"
    assert info["plugin"]["skills"][0]["name"] == "Demo skill"
    [plugin] = plugins.installed(tmp_path)
    assert (plugin.name, plugin.version, plugin.source) == ("demo", "0.1.0", "catalog:demo")
    assert catalog.installed_version(loaded.get("demo"), tmp_path) == "0.1.0"


def test_declining_writes_nothing_and_cleans_up(tmp_path, monkeypatch):
    archive = tar_plugin()
    loaded, client = remote([entry(digest=sha(archive))], {"demo-0.1.0.tar.gz": archive})
    workdirs = []
    real_stage = catalog.stage

    def spy(*args, **kwargs):
        staged = real_stage(*args, **kwargs)
        workdirs.append(staged.workdir)
        return staged
    monkeypatch.setattr(catalog, "stage", spy)
    assert catalog.install("demo", tmp_path, catalog=loaded, confirm=lambda staged: False, client=client) is None
    assert plugins.installed(tmp_path) == []
    assert not workdirs[0].exists()


def test_checksum_mismatch_installs_nothing(tmp_path):
    archive = tar_plugin()
    loaded, client = remote([entry(digest="f" * 64)], {"demo-0.1.0.tar.gz": archive})
    with pytest.raises(catalog.CatalogError, match="Checksum mismatch"):
        catalog.install("demo", tmp_path, catalog=loaded, confirm=lambda staged: pytest.fail("shown"),
                        client=client)
    assert plugins.installed(tmp_path) == []


def test_manifest_must_match_the_pinned_version(tmp_path):
    archive = tar_plugin(version="0.2.0")
    loaded, client = remote([entry(digest=sha(archive))], {"demo-0.1.0.tar.gz": archive})
    with pytest.raises(catalog.CatalogError, match="catalog lists demo 0.1.0"):
        catalog.install("demo", tmp_path, catalog=loaded, client=client)


def test_a_version_pin_must_match_the_index(tmp_path):
    loaded, client = remote([entry()])
    with pytest.raises(catalog.CatalogError, match="pins demo 0.1.0, not 2.0"):
        catalog.install("demo", tmp_path, version="2.0", catalog=loaded, client=client)
    assert catalog.split_spec("demo@2.0") == ("demo", "2.0")
    assert catalog.split_spec("demo") == ("demo", None)


@pytest.mark.parametrize("name", ["../evil.toml", "/etc/evil", "demo/../../evil"])
def test_archives_cannot_escape(tmp_path, name):
    archive = tar_plugin(extra={name: b"x"})
    loaded, client = remote([entry(digest=sha(archive))], {"demo-0.1.0.tar.gz": archive})
    with pytest.raises(catalog.CatalogError, match="unsafe path"):
        catalog.install("demo", tmp_path, catalog=loaded, client=client)


def test_archives_cannot_carry_links(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        link = tarfile.TarInfo("demo/link")
        link.type = tarfile.SYMTYPE
        link.linkname = "/etc/passwd"
        archive.addfile(link)
    data = buffer.getvalue()
    loaded, client = remote([entry(digest=sha(data))], {"demo-0.1.0.tar.gz": data})
    with pytest.raises(catalog.CatalogError, match="link"):
        catalog.install("demo", tmp_path, catalog=loaded, client=client)


def test_zip_archives_install(tmp_path):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("kova-plugin.toml", MANIFEST.format(version="0.1.0"))
        archive.writestr("skills/demo.md", SKILL)
    data = buffer.getvalue()
    loaded, client = remote([entry(url="demo.zip", digest=sha(data))], {"demo.zip": data})
    assert catalog.install("demo", tmp_path, catalog=loaded, client=client) == "demo 0.1.0"


def test_upgrade_replaces_an_older_version_and_keeps_the_source(tmp_path):
    old, new = tar_plugin("0.1.0"), tar_plugin("0.2.0")
    loaded, client = remote([entry(digest=sha(old))], {"demo-0.1.0.tar.gz": old})
    catalog.install("demo", tmp_path, catalog=loaded, client=client)
    plugins.disable("demo", tmp_path)
    loaded, client = remote([entry(version="0.2.0", url="demo-0.2.0.tar.gz", digest=sha(new))],
                            {"demo-0.2.0.tar.gz": new})
    with pytest.raises(catalog.CatalogError, match="already installed"):
        catalog.install("demo", tmp_path, catalog=loaded, client=client)
    catalog.install("demo", tmp_path, catalog=loaded, client=client, upgrade=True)
    [plugin] = plugins.installed(tmp_path)
    assert (plugin.version, plugin.source, plugin.enabled) == ("0.2.0", "catalog:demo", False)


def test_plugin_update_points_catalog_plugins_back_to_the_catalog(tmp_path):
    catalog.install("clipboard", tmp_path, catalog=catalog.load("bundled"))
    with pytest.raises(plugins.PluginError, match="kova catalog install clipboard --upgrade"):
        plugins.update("clipboard", tmp_path)


# -- skills ------------------------------------------------------------------------

def test_skill_install_lists_with_its_origin_and_deletes_cleanly(tmp_path):
    data = SKILL.encode()
    loaded, client = remote([entry(name="hello", kind="skill", url="hello.md", digest=sha(data))],
                            {"hello.md": data})
    seen = []
    catalog.install("hello", tmp_path, catalog=loaded, client=client,
                    confirm=lambda staged: seen.append(staged.describe()) or True)
    assert seen[0]["skill"]["body"].startswith("Say hello")
    assert (tmp_path / "skills" / "catalog" / "hello.md").read_bytes() == data
    [row] = extensions.list_skills(tmp_path)
    assert (row["origin"], row["version"], row["editable"], row["id"]) == ("catalog", "0.1.0", True, "catalog/hello.md")
    # The model sees it too: catalog skills live under the normal skills root.
    assert "Demo skill" in Skills.for_home(tmp_path).prompt_block()
    extensions.delete_skill(tmp_path, row["id"])
    assert extensions.list_skills(tmp_path) == []
    assert catalog.installed_skills(tmp_path) == {}


def test_list_skills_labels_every_origin(tmp_path):
    Skills.for_home(tmp_path).save("Backups", "How I back up the laptop.", "Run the backup script.")
    (tmp_path / "skills" / "mine.md").write_text(SKILL.replace("Demo skill", "My own"))
    catalog.install("clipboard", tmp_path, catalog=catalog.load("bundled"))
    rows = {row["name"]: row for row in extensions.list_skills(tmp_path)}
    assert rows["Backups"]["origin"] == "learned"
    assert rows["My own"]["origin"] == "yours"
    clip = rows["Clipboard"]
    assert (clip["origin"], clip["plugin"], clip["editable"]) == ("plugin", "clipboard", False)
    assert clip["id"] == "plugin:clipboard/skills/clipboard.md"
    plugins.disable("clipboard", tmp_path)
    assert "Clipboard" not in {row["name"] for row in extensions.list_skills(tmp_path)}


def test_editing_and_deleting_skills_is_confined(tmp_path):
    saved = Skills.for_home(tmp_path).save("Backups", "How I back up.", "Run it.")
    row = extensions.update_skill(tmp_path, "Kova/backups.md", "Backups", "How I back up, weekly.", "Run it on Sunday.")
    assert row["description"] == "How I back up, weekly." and row["body"] == "Run it on Sunday."
    assert saved.path.read_text().startswith("---\nname: Backups\n")
    with pytest.raises(SkillError, match="description"):
        extensions.update_skill(tmp_path, "Kova/backups.md", "Backups", "", "x")
    for bad in ["../config.toml", "../../etc/passwd.md", "/etc/passwd.md", "Kova/missing.md"]:
        with pytest.raises(SkillError, match="No such skill"):
            extensions.delete_skill(tmp_path, bad)
    catalog.install("clipboard", tmp_path, catalog=catalog.load("bundled"))
    with pytest.raises(SkillError, match="clipboard plugin"):
        extensions.delete_skill(tmp_path, "plugin:clipboard/skills/clipboard.md")
    extensions.delete_skill(tmp_path, "Kova/backups.md")
    assert not saved.path.exists()


# -- CLI ---------------------------------------------------------------------------

@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "get_client", lambda: pytest.fail("the catalog must not need the server"))
    return tmp_path


def run(*args, input=None):
    return CliRunner().invoke(cli.app, list(args), input=input)


def test_cli_search_show_and_install(home):
    result = run("catalog", "search")
    assert result.exit_code == 0, result.output
    for name in ("clipboard", "morning-briefing", "file-watcher", "explain-error"):
        assert name in result.output
    assert "coming soon" in result.output
    shown = run("catalog", "show", "clipboard")
    assert shown.exit_code == 0, shown.output
    assert "bundled:plugins/clipboard" in shown.output and "SHA-256" in shown.output

    declined = run("catalog", "install", "clipboard", input="n\n")
    assert "/clip" in declined.output and "Cancelled" in declined.output
    assert plugins.installed(home) == []

    accepted = run("catalog", "install", "clipboard@0.1.0", input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "(verified)" in accepted.output and "Installed clipboard 0.1.0" in accepted.output
    again = run("catalog", "install", "clipboard")
    assert "already installed" in again.output

    listed = json.loads(run("catalog", "search", "--json").output)
    assert next(row for row in listed if row["name"] == "clipboard")["installed_version"] == "0.1.0"


def test_cli_plugin_install_accepts_a_catalog_name(home):
    result = run("plugin", "install", "morning-briefing", "--yes")
    assert result.exit_code == 0, result.output
    assert "/briefing" in result.output
    assert [plugin.name for plugin in plugins.installed(home)] == ["morning-briefing"]
    wrong = run("plugin", "install", "explain-error", "--yes")
    assert wrong.exit_code == 1 and "is a skill" in wrong.output


def test_cli_coming_soon_and_unknown(home):
    soon = run("catalog", "install", "file-watcher", "--yes")
    assert soon.exit_code == 1 and "coming soon" in soon.output
    missing = run("catalog", "show", "nope")
    assert missing.exit_code == 1 and "No extension named" in missing.output


def test_cli_skills_list_show_rm(home):
    assert run("catalog", "install", "explain-error", "--yes").exit_code == 0
    Skills.for_home(home).save("Backups", "How I back up.", "Run it.")
    listed = run("skills", "list")
    assert listed.exit_code == 0, listed.output
    assert "from the catalog" in listed.output and "learned by Kova" in listed.output
    shown = run("skills", "show", "Explain an error")
    assert "next concrete step" in shown.output
    removed = run("skills", "rm", "explain-error", "--yes")
    assert removed.exit_code == 0, removed.output
    assert [row["name"] for row in extensions.list_skills(home)] == ["Backups"]


def test_cli_config_catalog_url(home):
    assert run("config", "set", "catalog.url", "http://catalog.example/index.json").exit_code == 1
    ok = run("config", "set", "catalog.url", "https://catalog.example/index.json")
    assert ok.exit_code == 0, ok.output
    assert load_config(home).catalog_url == "https://catalog.example/index.json"
    assert "[catalog]" in (home / "config.toml").read_text()
    assert run("config", "set", "catalog.url", "").exit_code == 0
    assert load_config(home).catalog_url == ""
    assert "[catalog]" not in (home / "config.toml").read_text()


# -- HTTP routes -------------------------------------------------------------------

@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(cfg.home)}"
        yield client, app, cfg


def test_routes_preview_then_install_then_manage(http):
    client, app, cfg = http
    listing = client.get("/extensions/catalog").json()
    assert listing["bundled"] and {"clipboard", "weekly-review"} <= {row["name"] for row in listing["entries"]}
    assert [row["name"] for row in client.get("/extensions/catalog", params={"q": "clip"}).json()["entries"]] == ["clipboard"]

    preview = client.post("/extensions/catalog/clipboard/preview", json={}).json()
    assert preview["verified"] and preview["plugin"]["commands"][0]["name"] == "clip"
    assert plugins.installed(cfg.home) == []  # a preview writes nothing
    done = client.post("/extensions/install", json={"ticket": preview["ticket"]})
    assert done.status_code == 200, done.text
    assert client.post("/extensions/install", json={"ticket": preview["ticket"]}).status_code == 410
    agent = app.state.agent
    assert any(Path(root).name == "skills" and "clipboard" in str(root) for root in agent.skills.extra_roots)

    [row] = client.get("/extensions/plugins").json()
    assert row["name"] == "clipboard" and row["enabled"] and row["source"] == "catalog:clipboard"
    skills = client.get("/extensions/skills").json()
    assert [(skill["name"], skill["origin"]) for skill in skills] == [("Clipboard", "plugin")]

    assert client.patch("/extensions/plugins/clipboard", json={"enabled": False}).json()["enabled"] is False
    assert agent.skills.extra_roots == []
    assert client.get("/extensions/skills").json() == []
    assert client.delete("/extensions/plugins/clipboard").status_code == 200
    assert client.get("/extensions/plugins").json() == []


def test_routes_cancel_and_errors(http):
    client, app, cfg = http
    preview = client.post("/extensions/catalog/weekly-review/preview", json={}).json()
    assert preview["skill"]["name"] == "Weekly review"
    assert client.delete(f"/extensions/catalog/preview/{preview['ticket']}").status_code == 200
    assert client.post("/extensions/install", json={"ticket": preview["ticket"]}).status_code == 410
    assert client.post("/extensions/catalog/file-watcher/preview", json={}).status_code == 422
    assert client.post("/extensions/catalog/nope/preview", json={}).status_code == 422
    assert client.patch("/extensions/plugins/nope", json={"enabled": False}).status_code == 404


def test_routes_view_edit_delete_skills(http):
    client, app, cfg = http
    Skills.for_home(cfg.home).save("Backups", "How I back up.", "Run it.")
    [row] = client.get("/extensions/skills").json()
    assert row["origin"] == "learned" and row["editable"]
    assert client.get(f"/extensions/skills/{row['id']}").json()["body"] == "Run it."
    edited = client.put(f"/extensions/skills/{row['id']}",
                        json={"name": "Backups", "description": "Weekly backups.", "body": "Sunday."})
    assert edited.status_code == 200 and edited.json()["body"] == "Sunday."
    bad = client.put(f"/extensions/skills/{row['id']}", json={"name": "", "description": "x", "body": "y"})
    assert bad.status_code == 422
    assert client.get("/extensions/skills/Kova/missing.md").status_code == 404
    assert client.delete(f"/extensions/skills/{row['id']}").status_code == 200
    assert client.get("/extensions/skills").json() == []


def test_enabling_a_plugin_is_refused_from_a_phone(http, monkeypatch):
    client, app, cfg = http
    catalog.install("clipboard", cfg.home, catalog=catalog.load("bundled"))
    plugins.disable("clipboard", cfg.home)
    monkeypatch.setattr(extension_routes, "is_lan_request", lambda request: True)
    assert client.patch("/extensions/plugins/clipboard", json={"enabled": True}).status_code == 403
    assert client.patch("/extensions/plugins/clipboard", json={"enabled": False}).status_code == 200


def test_routes_use_the_configured_index(http, monkeypatch):
    client, app, cfg = http
    seen = []

    def fake_get(url, *, timeout, client=None):
        seen.append(url)
        return json.dumps({"schema": 1, "extensions": [entry()]}).encode()
    monkeypatch.setattr(catalog, "_http_get", fake_get)
    cfg.catalog_url = INDEX_URL
    save_config(cfg)
    listing = client.get("/extensions/catalog", params={"refresh": True}).json()
    assert seen == [INDEX_URL] and listing["source"] == INDEX_URL
    assert [row["name"] for row in listing["entries"]] == ["demo"]
