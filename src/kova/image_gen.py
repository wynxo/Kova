"""Local image generation (#421): pick a model that fits this PC, fetch it, draw.

Three parts, each small:

* **Models.** A short curated list of text-to-image models that
  stable-diffusion.cpp runs, each pinned to a Hugging Face revision with the
  SHA-256 of every file. :func:`rate` estimates fit against the measured
  hardware with the same rules as chat models (reserves for the OS and
  display, the largest single GPU, unknown memory never counts as a fit), and
  :func:`recommend` picks the best one that fits.
* **Engine.** Kova drives stable-diffusion.cpp's ``sd-server`` over its
  OpenAI-compatible ``/v1/images/generations`` route. It runs the program from
  ``[images] engine``, a managed copy pinned below, or ``sd-server`` on the
  PATH, always on a loopback port. ``[images] server`` points at an
  already-running OpenAI-compatible images server instead.
* **Pictures.** Each image is a PNG in ``<files>/Images`` with a JSON sidecar
  holding its prompt and settings. Nothing is uploaded anywhere.

Downloads reuse :func:`kova.engine_downloads.download`: resumable ``.part``
files, exact length and SHA-256 before a file is used. Local only refuses the
downloads (they are not loopback) but not generation, which stays on this PC.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import platform
import re
import secrets
import shutil
import socket
import subprocess
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
import psutil

from kova import egress
from kova import engine_downloads as downloads
from kova.config import Config
from kova.engine_downloads import Artifact
from kova.local_models import LocalModelError
from kova.tools.system_info import hardware_profile

GIB = 1024 ** 3
DISK_RESERVE = 2 * GIB
GPU_RESERVE = GIB
RAM_RESERVE = 2 * GIB
#: Loading a model the first time reads gigabytes from disk.
START_TIMEOUT = 300
GENERATE_TIMEOUT = 600
MAX_PROMPT = 2_000
#: PNG files only; a server answering with anything else is refused.
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
#: The largest image Kova accepts back from a server.
MAX_IMAGE_BYTES = 40 * 1024 * 1024
#: The folder inside Kova Files where pictures are saved.
IMAGES_FOLDER = "Images"
_NAME = re.compile(r"\A[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[a-z0-9-]{1,40}(-[0-9]+)?\.png\Z")
_EXTRA_TAG = re.compile(r"</?sd_cpp_extra_args>", re.IGNORECASE)


class ImageGenError(LocalModelError):
    """An actionable image-generation failure, safe to show the user."""


def _hf(repo: str, revision: str, path: str, size: int, sha256: str) -> Artifact:
    return Artifact(f"https://huggingface.co/{repo}/resolve/{revision}/{path}",
                    path.rsplit("/", 1)[-1], size, sha256)


@dataclass(frozen=True)
class ImageModel:
    """One curated model: its files, the memory it needs and how to drive it."""
    id: str
    name: str
    about: str
    #: ``(sd-server option, file)``: ``--model`` for single-file checkpoints,
    #: ``--diffusion-model``/``--llm``/``--vae`` for split ones.
    files: tuple[tuple[str, Artifact], ...]
    vram_bytes: int
    ram_bytes: int
    #: Small enough that CPU-only generation finishes in reasonable time.
    cpu_ok: bool
    steps: int
    cfg_scale: float
    size: int
    sizes: tuple[str, ...]
    license: str
    license_url: str
    speed: str
    extra_args: tuple[str, ...] = ()

    @property
    def download_bytes(self) -> int:
        return sum(artifact.size for _, artifact in self.files)


_Z_VAE = _hf("Comfy-Org/Z-Image-Turbo", "6fc90a3b1b653e935a0d175e260736de25b84df5",
             "split_files/vae/ae.safetensors", 335_304_388,
             "afc8e28272cd15db3919bacdb6918ce9c1ed22e96cb12c4d5ed0fba823529e38")
_SQUARE_SMALL = ("512x512", "512x768", "768x512")
_SQUARE_LARGE = ("1024x1024", "768x1024", "1024x768", "768x768", "512x512")

#: Ordered from lightest to best; :func:`recommend` takes the last that fits.
# Memory figures are rounded up from the file sizes plus working memory at the
# default size; they are estimates for choosing, not promises about peak use.
CATALOG: tuple[ImageModel, ...] = (
    ImageModel(
        "sdxs-512", "SDXS 512", "Tiny one-step model: a rough picture in seconds, even without a GPU.",
        (("--model", _hf("concedo/sdxs-512-tinySDdistilled-GGUF", "3144d898d61492f8382ffcabec055733fc5b2a0e",
                         "sdxs-512-tinySDdistilled_Q8_0.gguf", 682_847_200,
                         "409ab23582ee074c6b9d5395784fc0741b0599fb9d138686c69087c71678eb6a")),),
        vram_bytes=2 * GIB, ram_bytes=3 * GIB, cpu_ok=True, steps=1, cfg_scale=1.0, size=512,
        sizes=_SQUARE_SMALL, license="OpenRAIL++",
        license_url="https://huggingface.co/akleine/sdxs-512", speed="seconds on a GPU, under a minute on a CPU"),
    ImageModel(
        "sd15", "Stable Diffusion 1.5", "The classic all-rounder at 512 px; huge community know-how for prompts.",
        (("--model", _hf("second-state/stable-diffusion-v1-5-GGUF", "031b5f5df991f511b3f5fa8fed6d99048ababb69",
                         "stable-diffusion-v1-5-pruned-emaonly-Q8_0.gguf", 1_763_578_176,
                         "d0555243938c62faeefb4ac93f6c7a053ad373a4290c5256bce229aeb193bf94")),),
        vram_bytes=3 * GIB, ram_bytes=5 * GIB, cpu_ok=True, steps=20, cfg_scale=7.0, size=512,
        sizes=_SQUARE_SMALL, license="CreativeML OpenRAIL-M",
        license_url="https://huggingface.co/stable-diffusion-v1-5/stable-diffusion-v1-5",
        speed="a few seconds on a GPU, a few minutes on a CPU"),
    ImageModel(
        "z-image-turbo-q4", "Z-Image Turbo (compact)", "Sharp, modern 1024 px pictures that follow long prompts and draw text.",
        (("--diffusion-model", _hf("leejet/Z-Image-Turbo-GGUF", "c61c0e422dc8b541b7548cf33a4ef8302b0f8085",
                                   "z_image_turbo-Q4_K.gguf", 3_864_250_304,
                                   "14b375ab4f226bc5378f68f37e899ef3c2242b8541e61e2bc1aff40976086fbd")),
         ("--llm", _hf("unsloth/Qwen3-4B-Instruct-2507-GGUF", "a06e946bb6b655725eafa393f4a9745d460374c9",
                       "Qwen3-4B-Instruct-2507-Q4_K_M.gguf", 2_497_281_120,
                       "3605803b982cb64aead44f6c1b2ae36e3acdb41d8e46c8a94c6533bc4c67e597")),
         ("--vae", _Z_VAE)),
        vram_bytes=6 * GIB, ram_bytes=10 * GIB, cpu_ok=False, steps=8, cfg_scale=1.0, size=1024,
        sizes=_SQUARE_LARGE, license="Apache-2.0",
        license_url="https://huggingface.co/Tongyi-MAI/Z-Image-Turbo",
        speed="10–30 seconds on a mid-range GPU", extra_args=("--offload-to-cpu",)),
    ImageModel(
        "z-image-turbo-q8", "Z-Image Turbo (full quality)", "The same model at near-full precision, for GPUs with room to spare.",
        (("--diffusion-model", _hf("leejet/Z-Image-Turbo-GGUF", "c61c0e422dc8b541b7548cf33a4ef8302b0f8085",
                                   "z_image_turbo-Q8_0.gguf", 6_577_440_704,
                                   "df1c5baa86d1398c979495a6072dbcee79444fdb884a2445582ba0769c44e9a1")),
         ("--llm", _hf("unsloth/Qwen3-4B-Instruct-2507-GGUF", "a06e946bb6b655725eafa393f4a9745d460374c9",
                       "Qwen3-4B-Instruct-2507-Q8_0.gguf", 4_280_405_600,
                       "391c1e410fd9f4cf2de2b510273b56a84c19ce18f4fa3bfb3774031dac4ef068")),
         ("--vae", _Z_VAE)),
        vram_bytes=12 * GIB, ram_bytes=16 * GIB, cpu_ok=False, steps=8, cfg_scale=1.0, size=1024,
        sizes=_SQUARE_LARGE, license="Apache-2.0",
        license_url="https://huggingface.co/Tongyi-MAI/Z-Image-Turbo",
        speed="5–20 seconds on a large GPU"),
)


def get_model(model_id: str) -> ImageModel:
    for model in CATALOG:
        if model.id == model_id:
            return model
    raise ImageGenError("Choose an image model from the list.")


# ---------------------------------------------------------------------------
# Fit and recommendation
# ---------------------------------------------------------------------------

def rate(model: ImageModel, profile: dict, *, cpu: bool = False, check_disk: bool = True,
         installed: bool = False) -> dict:
    """Estimate fit with explicit reserves; unknown resources never imply fit."""
    result = {"rating": "too-big", "reason": "", "cpu": cpu}
    free = profile.get("free_disk")
    if check_disk and not installed:
        if free is None:
            result["reason"] = "Free disk space could not be measured."
            return result
        if free < model.download_bytes + DISK_RESERVE:
            need = model.download_bytes + DISK_RESERVE - free
            result["reason"] = f"Not enough disk space: free {_gib(need)} more."
            return result
    ram = profile.get("ram_bytes")
    if ram is None or ram < model.ram_bytes + RAM_RESERVE:
        result["reason"] = "Not enough memory (RAM) for this model plus the operating system."
        return result
    if cpu:
        if not model.cpu_ok:
            result["reason"] = "Too slow without a graphics card; choose a smaller model."
            return result
        budget, needed = ram - RAM_RESERVE, model.ram_bytes
        warning = "Runs on the processor, which is slower. "
    else:
        vram = profile.get("vram_bytes")
        if not vram:
            result["reason"] = "No graphics memory was measured."
            return result
        budget, needed = vram - GPU_RESERVE, model.vram_bytes
        warning = ""
    if budget < needed:
        result["reason"] = "Needs more graphics memory than this PC has." if not cpu else \
            "Needs more memory than this PC has free."
        return result
    tight = budget < needed * 1.5
    result["rating"] = "fits-tightly" if tight else "fits-comfortably"
    result["reason"] = warning + ("Little room to spare; close other heavy apps. " if tight else "") + \
        f"Expect {model.speed}."
    return result


def _gib(size: int) -> str:
    return f"{size / GIB:.1f} GB"


def recommend(rows: list[dict]) -> str | None:
    """The best model that fits: the last fitting one in catalog order.

    A model that fits comfortably beats a better one that only fits tightly
    when the better one is a mere precision upgrade of the same model.
    """
    fitting = [row for row in rows if row["rating"] != "too-big"]
    if not fitting:
        return None
    best = fitting[-1]
    comfortable = [row for row in fitting if row["rating"] == "fits-comfortably"]
    if best["rating"] == "fits-tightly" and comfortable:
        family = best["id"].rsplit("-", 1)[0]
        if comfortable[-1]["id"].startswith(family):
            best = comfortable[-1]
    return best["id"]


def measure(cfg: Config) -> dict:
    """Hardware for image models: disk is measured where models are stored."""
    folder = models_dir(cfg)
    probe = folder if folder.exists() else cfg.home
    return hardware_profile(str(probe))


def catalog(cfg: Config, profile: dict) -> dict:
    """Every model rated for this PC, with the recommendation and install state."""
    cpu = not profile.get("vram_bytes")
    rows = []
    done = installed_models(cfg)
    for model in CATALOG:
        rows.append({
            "id": model.id, "name": model.name, "about": model.about,
            "download_bytes": model.download_bytes, "license": model.license,
            "license_url": model.license_url, "size": model.size, "sizes": list(model.sizes),
            "installed": model.id in done,
            **rate(model, profile, cpu=cpu, installed=model.id in done),
        })
    return {"models": rows, "recommended": recommend(rows), "cpu": cpu}


# ---------------------------------------------------------------------------
# Files on disk
# ---------------------------------------------------------------------------

def root(cfg: Config) -> Path:
    return Path(cfg.home) / "images"


def models_dir(cfg: Config) -> Path:
    return root(cfg) / "models"


def output_dir(cfg: Config) -> Path:
    return Path(cfg.files_path) / IMAGES_FOLDER


def _state_path(cfg: Config) -> Path:
    return root(cfg) / "state.json"


def _state(cfg: Config) -> dict:
    try:
        data = json.loads(_state_path(cfg).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_state(cfg: Config, data: dict) -> None:
    path = _state_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, indent=2))
    os.replace(temporary, path)


def _file(cfg: Config, artifact: Artifact) -> Path:
    return models_dir(cfg) / artifact.name


def installed_models(cfg: Config) -> list[str]:
    """Models whose every file was verified and is still the pinned size."""
    verified = set(_state(cfg).get("verified") or [])
    result = []
    for model in CATALOG:
        if model.id not in verified:
            continue
        if all(_file(cfg, artifact).is_file() and _file(cfg, artifact).stat().st_size == artifact.size
               for _, artifact in model.files):
            result.append(model.id)
    return result


def active_model(cfg: Config) -> str | None:
    chosen = _state(cfg).get("active")
    done = installed_models(cfg)
    if chosen in done:
        return chosen
    return done[-1] if done else None


def set_active(cfg: Config, model_id: str) -> None:
    get_model(model_id)
    if model_id not in installed_models(cfg):
        raise ImageGenError("Download that model first.")
    state = _state(cfg)
    state["active"] = model_id
    _write_state(cfg, state)


async def install_model(cfg: Config, model_id: str, profile: dict, *,
                        transport: httpx.AsyncBaseTransport | None = None,
                        force: bool = False) -> AsyncIterator[dict]:
    """Download and verify every file of one model; yields progress events."""
    model = get_model(model_id)
    cpu = not profile.get("vram_bytes")
    fit = rate(model, profile, cpu=cpu, installed=model_id in installed_models(cfg))
    if fit["rating"] == "too-big" and not force:
        raise ImageGenError(fit["reason"] or "This model does not fit this PC.")
    total = model.download_bytes
    done = 0
    async with egress.AsyncClient(transport=transport, timeout=httpx.Timeout(60, read=120)) as client:
        for index, (_, artifact) in enumerate(model.files, 1):
            label = f"File {index} of {len(model.files)}"
            try:
                async for event in downloads.download(client, artifact, _file(cfg, artifact)):
                    completed = done + int(event.get("completed") or 0)
                    yield {"type": "progress", "message": label, "completed": completed, "total": total}
            except LocalModelError:
                raise ImageGenError("A download could not be verified or the host is unreachable. "
                                    "Check your connection and free disk space, then retry: it resumes.") from None
            done += artifact.size
    state = _state(cfg)
    state["verified"] = sorted(set(state.get("verified") or []) | {model_id})
    state.setdefault("active", model_id)
    if state["active"] not in state["verified"]:
        state["active"] = model_id
    _write_state(cfg, state)
    yield {"type": "done", "model": model_id}


def remove_model(cfg: Config, model_id: str) -> None:
    """Delete one model's files, keeping any file another installed model shares."""
    model = get_model(model_id)
    state = _state(cfg)
    keep = {artifact.name for other in CATALOG if other.id != model_id and other.id in installed_models(cfg)
            for _, artifact in other.files}
    for _, artifact in model.files:
        if artifact.name not in keep:
            _file(cfg, artifact).unlink(missing_ok=True)
            _file(cfg, artifact).with_suffix(_file(cfg, artifact).suffix + ".part").unlink(missing_ok=True)
    state["verified"] = [item for item in state.get("verified") or [] if item != model_id]
    if state.get("active") == model_id:
        state.pop("active", None)
    _write_state(cfg, state)


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------

#: Pinned stable-diffusion.cpp release builds: key -> (archive name, size,
#: SHA-256). Filled by ``scripts/pin_sd_engine.py`` from GitHub's release
#: digests and reviewed like any other pin. Empty means there is no managed
#: build yet: Kova then uses ``[images] engine`` or ``sd-server`` on the PATH.
ENGINE_RELEASE = ""
ENGINE_BUILDS: dict[str, tuple[str, int, str]] = {}
ENGINE_NAMES = ("sd-server", "sd-server.exe")
ENGINE_HELP = ("Kova needs stable-diffusion.cpp's sd-server to draw. Download a release for your system "
               "from https://github.com/leejet/stable-diffusion.cpp/releases, then run: "
               "kova config set images.engine /path/to/sd-server")


def engine_key(*, cpu: bool) -> str | None:
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    system = {"Linux": "linux", "Windows": "win", "Darwin": "macos"}.get(platform.system())
    if not arch or not system:
        return None
    backend = "metal" if system == "macos" else ("cpu" if cpu else "vulkan")
    return f"{system}-{backend}-{arch}"


def engine_artifact(*, cpu: bool) -> Artifact | None:
    key = engine_key(cpu=cpu)
    if not ENGINE_RELEASE or key not in ENGINE_BUILDS:
        return None
    name, size, digest = ENGINE_BUILDS[key]
    url = f"https://github.com/leejet/stable-diffusion.cpp/releases/download/{ENGINE_RELEASE}/{name}"
    return Artifact(url, name, size, digest)


def _managed_binary(cfg: Config) -> Path | None:
    folder = root(cfg) / "engine"
    if not folder.is_dir():
        return None
    # Only an unpacked bundle that finished (its .complete marker) counts.
    found = [path for bundle in folder.glob("*.d") if (bundle / ".complete").is_file()
             for path in bundle.rglob("sd-server*") if path.name in ENGINE_NAMES and path.is_file()]
    return found[0] if len(found) == 1 else None


def find_engine(cfg: Config) -> tuple[Path | None, str]:
    """The sd-server to run and where it came from, or ``(None, reason)``."""
    if cfg.images_engine:
        path = Path(cfg.images_engine).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return path, "configured"
        return None, "The program in [images] engine is missing or not executable."
    managed = _managed_binary(cfg)
    if managed is not None:
        return managed, "managed"
    found = next((shutil.which(name) for name in ENGINE_NAMES if shutil.which(name)), None)
    if found:
        return Path(found), "path"
    return None, ENGINE_HELP


async def install_engine(cfg: Config, *, cpu: bool,
                         transport: httpx.AsyncBaseTransport | None = None) -> AsyncIterator[dict]:
    """Fetch the pinned sd-server build, when this Kova pins one for this system."""
    artifact = engine_artifact(cpu=cpu)
    if artifact is None:
        raise ImageGenError(ENGINE_HELP)
    folder = root(cfg) / "engine"
    archive = folder / artifact.name
    destination = folder / (artifact.name + ".d")
    staging = folder / (artifact.name + ".unpack")
    async with egress.AsyncClient(transport=transport, timeout=httpx.Timeout(60, read=120)) as client:
        async for event in downloads.download(client, artifact, archive):
            yield {**event, "message": "Image engine"}
    try:
        if not (destination / ".complete").is_file():
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            downloads._extract(archive, staging)
            (staging / ".complete").write_text(artifact.sha256)
            shutil.rmtree(destination, ignore_errors=True)
            staging.replace(destination)
        for path in destination.rglob("sd-server*"):
            if path.name in ENGINE_NAMES:
                path.chmod(0o700)
    except (OSError, ValueError) as exc:
        raise ImageGenError("The image engine could not be unpacked. Free some disk space and retry.") from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@dataclass
class _Running:
    process: subprocess.Popen
    model: str
    url: str
    started: float = field(default_factory=time.monotonic)


class ImageEngine:
    """One sd-server owned by this Kova server, restarted to switch models."""

    def __init__(self, cfg: Config, *, transport: httpx.AsyncBaseTransport | None = None):
        self.cfg = cfg
        self.transport = transport
        self.running: _Running | None = None
        self.lock = asyncio.Lock()
        self.busy = False

    def command(self, binary: Path, model: ImageModel, port: int) -> list[str]:
        argv = [str(binary)]
        for option, artifact in model.files:
            argv += [option, str(_file(self.cfg, artifact))]
        argv += ["--listen-ip", "127.0.0.1", "--listen-port", str(port),
                 "--steps", str(model.steps), "--cfg-scale", str(model.cfg_scale),
                 "-W", str(model.size), "-H", str(model.size), *model.extra_args]
        return argv

    async def _start(self, model: ImageModel) -> _Running:
        binary, reason = find_engine(self.cfg)
        if binary is None:
            raise ImageGenError(reason)
        port = _free_port()
        argv = self.command(binary, model, port)
        log = root(self.cfg) / "engine.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        flags = {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if os.name == "nt" else {}
        with log.open("ab") as output:
            process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=output, stderr=output,
                                       cwd=str(root(self.cfg)), **flags)
        url = f"http://127.0.0.1:{port}"
        running = _Running(process, model.id, url)
        deadline = time.monotonic() + START_TIMEOUT
        async with egress.AsyncClient(transport=self.transport, timeout=5) as client:
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise ImageGenError("The image engine stopped while starting. Its log is images/engine.log "
                                        "in the Kova folder; the model may not fit in memory.")
                try:
                    response = await client.get(url + "/v1/models")
                    if response.status_code == 200 and response.json().get("data"):
                        return running
                except (httpx.HTTPError, ValueError):
                    pass
                await asyncio.sleep(0.5)
        _terminate(process)
        raise ImageGenError("The image engine took too long to start.")

    async def url_for(self, model_id: str) -> str:
        model = get_model(model_id)
        if self.running and self.running.model == model_id and self.running.process.poll() is None:
            return self.running.url
        self.stop()
        self.running = await self._start(model)
        return self.running.url

    def stop(self) -> None:
        if self.running is not None:
            _terminate(self.running.process)
            self.running = None

    def status(self) -> dict:
        alive = self.running is not None and self.running.process.poll() is None
        return {"running": alive, "model": self.running.model if alive else None, "busy": self.busy}


def _terminate(process: subprocess.Popen) -> None:
    if process.poll() is not None:
        return
    try:
        parent = psutil.Process(process.pid)
        children = parent.children(recursive=True)
    except psutil.Error:
        children = []
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    for child in children:
        try:
            child.kill()
        except psutil.Error:
            pass


# ---------------------------------------------------------------------------
# Generating
# ---------------------------------------------------------------------------

def _clean_prompt(prompt: str) -> str:
    text = _EXTRA_TAG.sub("", str(prompt or "")).strip()
    if not text:
        raise ImageGenError("Describe the picture you want.")
    if len(text) > MAX_PROMPT:
        raise ImageGenError(f"Keep the description under {MAX_PROMPT} characters.")
    return text


def _slug(text: str) -> str:
    words = re.findall(r"[a-z0-9]+", text.casefold())
    slug = "-".join(words)[:40].strip("-")
    return slug or "image"


def _save(cfg: Config, data: bytes, meta: dict) -> dict:
    if not data.startswith(PNG_MAGIC) or len(data) > MAX_IMAGE_BYTES:
        raise ImageGenError("The image server did not send back a PNG picture.")
    folder = output_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    stem = datetime.now().strftime("%Y-%m-%d-%H%M%S") + "-" + _slug(meta["prompt"])
    name, counter = stem + ".png", 1
    while (folder / name).exists():
        counter += 1
        name = f"{stem}-{counter}.png"
    temporary = folder / f".{secrets.token_hex(6)}.part"
    temporary.write_bytes(data)
    os.replace(temporary, folder / name)
    (folder / (name[:-4] + ".json")).write_text(json.dumps(meta, indent=2), encoding="utf-8")
    return {**meta, "name": name}


def parse_size(size: str | None, allowed: tuple[str, ...]) -> tuple[int, int]:
    chosen = size or allowed[0]
    if chosen not in allowed:
        raise ImageGenError("Choose one of these sizes: " + ", ".join(allowed) + ".")
    width, height = chosen.split("x")
    return int(width), int(height)


async def generate(cfg: Config, engine: ImageEngine, prompt: str, *, size: str | None = None,
                   seed: int | None = None, steps: int | None = None,
                   negative: str | None = None) -> dict:
    """Draw one picture and save it; returns its gallery row."""
    text = _clean_prompt(prompt)
    negative_text = _EXTRA_TAG.sub("", negative or "").strip()[:MAX_PROMPT]
    if seed is None:
        seed = secrets.randbelow(2 ** 31)
    if not 0 <= int(seed) < 2 ** 31:
        raise ImageGenError("The seed must be a whole number from 0 to 2147483647.")
    external = cfg.images_server.strip().rstrip("/")
    if external:
        width, height = parse_size(size, _SQUARE_LARGE + _SQUARE_SMALL)
        url, model_label, body_prompt = external, "server", text
    else:
        model_id = active_model(cfg)
        if model_id is None:
            raise ImageGenError("Download an image model first: open Images and pick one.")
        model = get_model(model_id)
        width, height = parse_size(size or f"{model.size}x{model.size}", model.sizes)
        if steps is not None and not 1 <= int(steps) <= 60:
            raise ImageGenError("Steps must be from 1 to 60.")
        extras: dict = {"seed": int(seed)}
        if steps is not None:
            extras["sample_params"] = {"sample_steps": int(steps)}
        if negative_text:
            extras["negative_prompt"] = negative_text
        body_prompt = f"{text}<sd_cpp_extra_args>{json.dumps(extras)}</sd_cpp_extra_args>"
        model_label = model_id
    if engine.busy:
        raise ImageGenError("Kova is already drawing a picture. Wait for it to finish.")
    async with engine.lock:
        engine.busy = True
        started = time.monotonic()
        try:
            if not external:
                url = await engine.url_for(model_label)
            async with egress.AsyncClient(transport=engine.transport,
                                          timeout=httpx.Timeout(GENERATE_TIMEOUT, connect=10)) as client:
                response = await client.post(url + "/v1/images/generations", json={
                    "prompt": body_prompt, "n": 1, "size": f"{width}x{height}", "output_format": "png"})
            if response.status_code != 200:
                raise ImageGenError(f"The image server answered HTTP {response.status_code}.")
            items = response.json().get("data") or []
            encoded = items[0].get("b64_json") if items and isinstance(items[0], dict) else None
            if not isinstance(encoded, str):
                raise ImageGenError("The image server sent no picture (it must answer with b64_json).")
            data = base64.b64decode(encoded, validate=True)
        except egress.LocalOnlyError as exc:
            raise ImageGenError(str(exc)) from None
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            if isinstance(exc, ImageGenError):
                raise
            raise ImageGenError("The image engine did not answer. Try again; a first picture can take a while.") from None
        finally:
            engine.busy = False
    meta = {"prompt": text, "negative": negative_text or None, "model": model_label,
            "width": width, "height": height, "seed": int(seed),
            "steps": int(steps) if steps is not None else None,
            "seconds": round(time.monotonic() - started, 1),
            "created": datetime.now().astimezone().isoformat(timespec="seconds")}
    return await asyncio.to_thread(_save, cfg, data, meta)


# ---------------------------------------------------------------------------
# Gallery
# ---------------------------------------------------------------------------

def image_path(cfg: Config, name: str) -> Path | None:
    """A saved picture by name, confined to the Images folder; ``None`` otherwise."""
    if not _NAME.match(name or ""):
        return None
    path = output_dir(cfg) / name
    if path.is_symlink() or not path.is_file():
        return None
    return path


def gallery(cfg: Config, *, limit: int = 200) -> list[dict]:
    folder = output_dir(cfg)
    if not folder.is_dir():
        return []
    rows = []
    for path in sorted(folder.glob("*.png"), key=lambda item: item.name, reverse=True)[:limit]:
        if not _NAME.match(path.name) or path.is_symlink():
            continue
        meta: dict = {}
        try:
            loaded = json.loads((folder / (path.name[:-4] + ".json")).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                meta = {key: loaded.get(key) for key in
                        ("prompt", "negative", "model", "width", "height", "seed", "steps", "seconds", "created")}
        except (OSError, ValueError):
            pass
        rows.append({"name": path.name, **meta})
    return rows


def delete_image(cfg: Config, name: str) -> bool:
    path = image_path(cfg, name)
    if path is None:
        return False
    path.unlink()
    (path.parent / (name[:-4] + ".json")).unlink(missing_ok=True)
    return True
