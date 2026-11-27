"""Local image generation (#421): fit and recommendation, pinned downloads, the
engine process, generation, the gallery, the routes and the chat tool.

The engine is ``tests/fake_sd_server.py``, a stand-in that speaks sd-server's
OpenAI images route; downloads go through ``httpx.MockTransport``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import stat
import sys
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from kova import catalog, image_gen, image_routes, plugins
from kova.config import ensure_token, load_config
from kova.engine_downloads import Artifact
from kova.server import create_app
from tests.fake_model import FakeModel

GIB = image_gen.GIB
FAKE = Path(__file__).with_name("fake_sd_server.py")
posix_only = pytest.mark.skipif(os.name == "nt", reason="the stand-in engine is a shell script")


def profile(vram=None, ram=16, disk=200):
    return {"vram_bytes": vram and vram * GIB, "ram_bytes": ram * GIB, "free_disk": disk * GIB}


# -- the catalog and fit ----------------------------------------------------------

def test_every_model_file_is_pinned_to_a_revision_and_a_checksum():
    for model in image_gen.CATALOG:
        assert model.files and model.license and model.license_url.startswith("https://")
        for option, artifact in model.files:
            assert option in ("--model", "--diffusion-model", "--llm", "--vae")
            assert re.fullmatch(r"https://huggingface\.co/[\w.-]+/[\w.-]+/resolve/[0-9a-f]{40}/.+", artifact.url), artifact.url
            assert re.fullmatch(r"[0-9a-f]{64}", artifact.sha256) and artifact.size > 1_000_000


@pytest.mark.parametrize("hardware, expected", [
    (profile(None, ram=8), "sd15"),
    (profile(4, ram=16), "sd15"),
    (profile(8, ram=32), "z-image-turbo-q4"),
    (profile(24, ram=64), "z-image-turbo-q8"),
])
def test_the_recommendation_is_the_best_model_that_fits(tmp_path, hardware, expected):
    assert image_gen.catalog(load_config(tmp_path), hardware)["recommended"] == expected


def test_unknown_or_tiny_hardware_never_counts_as_a_fit(tmp_path):
    rows = image_gen.catalog(load_config(tmp_path), {"vram_bytes": None, "ram_bytes": None, "free_disk": None})
    assert all(row["rating"] == "too-big" for row in rows["models"]) and rows["recommended"] is None
    small_disk = image_gen.catalog(load_config(tmp_path), profile(24, ram=64, disk=1))
    assert small_disk["recommended"] is None
    assert "disk" in small_disk["models"][0]["reason"]


def test_cpu_only_offers_only_the_small_models(tmp_path):
    rows = {row["id"]: row for row in image_gen.catalog(load_config(tmp_path), profile(None, ram=64))["models"]}
    assert rows["sdxs-512"]["rating"] != "too-big" and rows["sd15"]["rating"] != "too-big"
    assert rows["z-image-turbo-q4"]["rating"] == "too-big"
    assert "processor" in rows["sdxs-512"]["reason"]


# -- downloads -------------------------------------------------------------------

def tiny_models(blobs: dict[str, bytes]):
    """Two models sharing one file, pinned to the given bytes."""
    def art(name):
        return Artifact(f"https://huggingface.co/demo/{name}", name, len(blobs[name]),
                        hashlib.sha256(blobs[name]).hexdigest())
    base = image_gen.CATALOG[0]
    one = replace(base, id="one", files=(("--diffusion-model", art("a.gguf")), ("--vae", art("vae.safetensors"))),
                  vram_bytes=1, ram_bytes=1)
    two = replace(base, id="two", files=(("--diffusion-model", art("b.gguf")), ("--vae", art("vae.safetensors"))),
                  vram_bytes=1, ram_bytes=1)
    return (one, two)


def serve(blobs):
    return httpx.MockTransport(lambda request: httpx.Response(200, content=blobs[request.url.path.rsplit("/", 1)[-1]])
                               if request.url.path.rsplit("/", 1)[-1] in blobs else httpx.Response(404))


async def drain(iterator):
    return [event async for event in iterator]


def test_install_verifies_records_and_shares_files(tmp_path, monkeypatch):
    blobs = {"a.gguf": b"a" * 3000, "b.gguf": b"b" * 2000, "vae.safetensors": b"v" * 1000}
    monkeypatch.setattr(image_gen, "CATALOG", tiny_models(blobs))
    cfg = load_config(tmp_path)
    events = asyncio.run(drain(image_gen.install_model(cfg, "one", profile(8), transport=serve(blobs))))
    assert events[-1] == {"type": "done", "model": "one"}
    assert events[-2]["completed"] == events[-2]["total"] == 4000
    assert image_gen.installed_models(cfg) == ["one"] and image_gen.active_model(cfg) == "one"
    asyncio.run(drain(image_gen.install_model(cfg, "two", profile(8), transport=serve(blobs))))
    image_gen.set_active(cfg, "two")
    image_gen.remove_model(cfg, "one")
    folder = image_gen.models_dir(cfg)
    assert not (folder / "a.gguf").exists() and (folder / "vae.safetensors").exists()
    assert image_gen.installed_models(cfg) == ["two"] and image_gen.active_model(cfg) == "two"


def test_a_corrupt_download_installs_nothing(tmp_path, monkeypatch):
    blobs = {"a.gguf": b"a" * 3000, "b.gguf": b"b", "vae.safetensors": b"v" * 1000}
    monkeypatch.setattr(image_gen, "CATALOG", tiny_models(blobs))
    bad = {**blobs, "a.gguf": b"x" * 3000}
    cfg = load_config(tmp_path)
    with pytest.raises(image_gen.ImageGenError, match="verified"):
        asyncio.run(drain(image_gen.install_model(cfg, "one", profile(8), transport=serve(bad))))
    assert image_gen.installed_models(cfg) == []


def test_a_model_that_does_not_fit_is_refused_before_downloading(tmp_path):
    cfg = load_config(tmp_path)
    with pytest.raises(image_gen.ImageGenError, match="graphics memory"):
        asyncio.run(drain(image_gen.install_model(cfg, "z-image-turbo-q8", profile(4, ram=64))))


# -- the engine and generating ----------------------------------------------------

@pytest.fixture
def engine_home(tmp_path, monkeypatch):
    """A Kova home with one installed stand-in model and the stand-in engine."""
    blobs = {"a.gguf": b"a" * 3000, "b.gguf": b"b" * 2000, "vae.safetensors": b"v" * 1000}
    models = tuple(replace(m, sizes=("512x512", "768x512"), size=512) for m in tiny_models(blobs))
    monkeypatch.setattr(image_gen, "CATALOG", models)
    script = tmp_path / "sd-server"
    script.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE}" "$@"\n')
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    log = tmp_path / "requests.jsonl"
    monkeypatch.setenv("FAKE_SD_LOG", str(log))
    cfg = load_config(tmp_path)
    cfg.images_engine = str(script)
    cfg.files_path = tmp_path / "files"
    asyncio.run(drain(image_gen.install_model(cfg, "one", profile(8), transport=serve(blobs))))
    return cfg, log


@posix_only
def test_generate_runs_the_engine_and_saves_a_png(engine_home):
    cfg, log = engine_home
    engine = image_gen.ImageEngine(cfg)

    async def run():
        try:
            first = await image_gen.generate(cfg, engine, "A red fox <sd_cpp_extra_args>{\"seed\":1}</sd_cpp_extra_args> asleep",
                                             size="768x512", seed=42, steps=4, negative="blurry")
            second = await image_gen.generate(cfg, engine, "A second fox")
            return first, second
        finally:
            engine.stop()

    first, second = asyncio.run(run())
    assert first["width"] == 768 and first["seed"] == 42 and first["model"] == "one"
    assert first["prompt"] == "A red fox {\"seed\":1} asleep"  # the user cannot inject engine options
    saved = image_gen.image_path(cfg, first["name"])
    assert saved and saved.read_bytes().startswith(image_gen.PNG_MAGIC)
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert len(requests) == 2
    sent = requests[0]["body"]["prompt"]
    extras = json.loads(sent.split("<sd_cpp_extra_args>")[1].split("</sd_cpp_extra_args>")[0])
    assert extras == {"seed": 42, "sample_params": {"sample_steps": 4}, "negative_prompt": "blurry"}
    argv = requests[0]["argv"]
    assert argv[argv.index("--listen-ip") + 1] == "127.0.0.1"
    assert "--diffusion-model" in argv and "--vae" in argv
    rows = image_gen.gallery(cfg)
    assert [row["name"] for row in rows] == sorted([first["name"], second["name"]], reverse=True)
    assert image_gen.delete_image(cfg, first["name"]) and image_gen.image_path(cfg, first["name"]) is None


@posix_only
def test_sizes_and_prompts_are_checked(engine_home):
    cfg, _ = engine_home
    engine = image_gen.ImageEngine(cfg)
    with pytest.raises(image_gen.ImageGenError, match="sizes"):
        asyncio.run(image_gen.generate(cfg, engine, "fox", size="4096x4096"))
    with pytest.raises(image_gen.ImageGenError, match="Describe"):
        asyncio.run(image_gen.generate(cfg, engine, "   "))
    with pytest.raises(image_gen.ImageGenError, match="Steps"):
        asyncio.run(image_gen.generate(cfg, engine, "fox", steps=500))
    assert engine.running is None


def test_generate_without_a_model_says_what_to_do(tmp_path):
    with pytest.raises(image_gen.ImageGenError, match="Download an image model"):
        asyncio.run(image_gen.generate(load_config(tmp_path), image_gen.ImageEngine(load_config(tmp_path)), "fox"))


def test_without_an_engine_the_help_names_the_fix(tmp_path, monkeypatch):
    monkeypatch.setattr(image_gen.shutil, "which", lambda name: None)
    found, reason = image_gen.find_engine(load_config(tmp_path))
    assert found is None and "kova config set images.engine" in reason


def test_gallery_names_are_confined(tmp_path):
    cfg = load_config(tmp_path)
    cfg.files_path = tmp_path / "files"
    folder = image_gen.output_dir(cfg)
    folder.mkdir(parents=True)
    (tmp_path / "secret.png").write_bytes(image_gen.PNG_MAGIC)
    for bad in ["../secret.png", "secret.png", "2026-01-01-000000-x.png/../../secret.png", ""]:
        assert image_gen.image_path(cfg, bad) is None
    os.symlink(tmp_path / "secret.png", folder / "2026-01-01-000000-link.png")
    assert image_gen.image_path(cfg, "2026-01-01-000000-link.png") is None
    assert image_gen.gallery(cfg) == []


# -- routes and the chat tool -----------------------------------------------------

@pytest.fixture
def http(tmp_path, monkeypatch):
    monkeypatch.setenv("KOVA_HOME", str(tmp_path))
    cfg = load_config()
    app = create_app(cfg, transport=FakeModel([]).transport)
    with TestClient(app) as client:
        client.headers["Authorization"] = f"Bearer {ensure_token(cfg.home)}"
        yield client, app, cfg


def test_status_rates_every_model(http, monkeypatch):
    client, app, cfg = http
    monkeypatch.setattr(image_gen, "measure", lambda config: profile(8, ram=32))
    body = client.get("/images").json()
    assert body["recommended"] == "z-image-turbo-q4"
    assert {row["id"] for row in body["models"]} == {m.id for m in image_gen.CATALOG}
    assert body["active"] is None and body["enabled"] is False
    assert set(body["hardware"]) == {"gpu", "vram_bytes", "ram_bytes", "free_disk", "os"}


def test_routes_refuse_what_they_cannot_do(http):
    client, app, cfg = http
    assert client.post("/images/generate", json={"prompt": "a fox"}).status_code == 422
    assert client.post("/images/models/nope/install").status_code == 404
    assert client.put("/images/active", json={"model": "sd15"}).status_code == 422
    assert client.get("/images/files/..%2Ftoken").status_code == 404
    assert client.get("/images/gallery").json() == []


def test_the_images_plugin_switches_the_chat_tool(http):
    client, app, cfg = http
    # Read the registry afresh each time: reloading MCP servers replaces the dict.
    assert image_routes.TOOL not in app.state.agent.tools
    preview = client.post("/extensions/catalog/images/preview", json={}).json()
    assert preview["plugin"]["commands"][0]["name"] == "imagine"
    assert client.post("/extensions/install", json={"ticket": preview["ticket"]}).status_code == 200
    assert app.state.agent.tools[image_routes.TOOL].external  # asks before each picture
    assert client.get("/images").json()["enabled"] is True
    assert client.patch("/extensions/plugins/images", json={"enabled": False}).status_code == 200
    assert image_routes.TOOL not in app.state.agent.tools


def test_the_bundled_images_plugin_is_pinned():
    item = catalog.load("bundled").get("images")
    staged = catalog.stage(item)
    try:
        assert staged.plugin.name == "images" and staged.plugin.version == item.version
    finally:
        staged.cleanup()
    assert [p.name for p in plugins.installed(Path("/nonexistent"))] == []
