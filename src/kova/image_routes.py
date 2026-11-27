"""HTTP routes and the chat tool for local image generation (#421).

``GET /images`` reports this PC's hardware, every image model rated for it with
the recommended one, and the engine. Installing streams SSE progress like
``/local-models/install``. Generating is a plain request: it holds one
``sd-server`` (see :mod:`kova.image_gen`) and returns the saved picture's row.

The ``generate_image`` tool joins the agent only while the first-party
``images`` plugin is enabled, and asks before each run like any program Kova
does not control.
"""
from __future__ import annotations

import asyncio
import contextlib
import json

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from kova import image_gen, plugins
from kova.config import Config
from kova.pairing import require_capability
from kova.tools import Tool, ToolError

#: The plugin that switches image generation on in chat.
PLUGIN = "images"
TOOL = "generate_image"


class GenerateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    prompt: str = Field(min_length=1, max_length=image_gen.MAX_PROMPT)
    size: str | None = None
    seed: int | None = None
    steps: int | None = None
    negative: str | None = Field(default=None, max_length=image_gen.MAX_PROMPT)


class ActiveBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str


def enabled(config: Config) -> bool:
    """Whether the images plugin is installed and switched on."""
    return any(plugin.name == PLUGIN and plugin.enabled for plugin in plugins.installed(config.home))


def engine(app: FastAPI) -> image_gen.ImageEngine:
    if getattr(app.state, "image_engine", None) is None:
        app.state.image_engine = image_gen.ImageEngine(app.state.config)
    return app.state.image_engine


def frame(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event)}\n\n"


def image_tool(app: FastAPI) -> Tool:
    async def run(args: dict) -> str:
        try:
            row = await image_gen.generate(app.state.config, engine(app), str(args.get("prompt", "")),
                                           size=args.get("size") or None)
        except image_gen.ImageGenError as exc:
            raise ToolError(str(exc)) from None
        return (f"Saved {row['name']} ({row['width']}x{row['height']}, {row['model']}, seed {row['seed']}) "
                f"in Kova Files/{image_gen.IMAGES_FOLDER}. Show it to the user with this Markdown, unchanged: "
                f"![{row['prompt'][:80]}](/images/files/{row['name']})")

    return Tool(
        TOOL,
        "Draw a picture on this computer from a text description, with the local image model the user "
        "chose. Write the prompt as a vivid visual description: subject, setting, style, lighting, "
        "composition. Takes seconds to a few minutes. Use it only when the user asks for an image.",
        {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "What to draw, in English, as a visual description."},
                "size": {"type": "string", "description": "WIDTHxHEIGHT, e.g. 1024x1024 or 768x1024. Optional."},
            },
            "required": ["prompt"],
        },
        run,
        external=True,
    )


def sync_tool(app: FastAPI) -> None:
    """Add or remove ``generate_image`` to match the plugin's state; no restart needed."""
    agent = getattr(app.state, "agent", None)
    if agent is None:
        return
    if enabled(app.state.config):
        agent.tools.setdefault(TOOL, image_tool(app))
    else:
        agent.tools.pop(TOOL, None)


def install_image_routes(app: FastAPI, config: Config, require_token) -> None:
    router = APIRouter(dependencies=[Depends(require_token)])
    read = [Depends(require_capability("models:read"))]
    write = [Depends(require_capability("models:write"))]
    draw = [Depends(require_capability("chat"))]
    operation = asyncio.Lock()

    @router.get("/images", dependencies=read)
    async def status() -> dict:
        profile = await asyncio.to_thread(image_gen.measure, config)
        found, source = image_gen.find_engine(config)
        cpu = not profile.get("vram_bytes")
        return {
            "hardware": {key: profile.get(key) for key in ("gpu", "vram_bytes", "ram_bytes", "free_disk", "os")},
            **image_gen.catalog(config, profile),
            "active": image_gen.active_model(config),
            "engine": {"ready": found is not None or bool(config.images_server),
                       "source": "server" if config.images_server else (source if found else None),
                       "help": None if found or config.images_server else source,
                       "managed": image_gen.engine_artifact(cpu=cpu) is not None},
            "running": engine(app).status(),
            "enabled": enabled(config),
        }

    @router.post("/images/models/{model_id}/install", dependencies=write)
    async def install(model_id: str, force: bool = False):
        if operation.locked():
            raise HTTPException(409, "An image model is already downloading. Wait for it to finish.")
        try:
            image_gen.get_model(model_id)
        except image_gen.ImageGenError as exc:
            raise HTTPException(404, str(exc)) from None
        profile = await asyncio.to_thread(image_gen.measure, config)

        async def events():
            async with operation:
                steps = []
                found, _ = image_gen.find_engine(config)
                if found is None and not config.images_server and image_gen.engine_artifact(cpu=not profile.get("vram_bytes")):
                    steps.append(image_gen.install_engine(config, cpu=not profile.get("vram_bytes")))
                steps.append(image_gen.install_model(config, model_id, profile, force=force))
                for iterator in steps:
                    pending = None
                    try:
                        while True:
                            pending = asyncio.create_task(anext(iterator))
                            while not pending.done():
                                done, _ = await asyncio.wait({pending}, timeout=10)
                                if not done:
                                    yield frame({"type": "ping"})
                            try:
                                yield frame(pending.result())
                            except StopAsyncIteration:
                                break
                    except image_gen.ImageGenError as exc:
                        yield frame({"type": "error", "message": str(exc)})
                        return
                    finally:
                        if pending is not None and not pending.done():
                            pending.cancel()
                            with contextlib.suppress(asyncio.CancelledError, StopAsyncIteration, image_gen.ImageGenError):
                                await pending
                        await iterator.aclose()

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    @router.delete("/images/models/{model_id}", dependencies=write)
    async def remove(model_id: str) -> dict:
        if operation.locked():
            raise HTTPException(409, "An image model is downloading. Retry when it finishes.")
        try:
            image_gen.get_model(model_id)
        except image_gen.ImageGenError as exc:
            raise HTTPException(404, str(exc)) from None
        if engine(app).status()["model"] == model_id:
            engine(app).stop()
        await asyncio.to_thread(image_gen.remove_model, config, model_id)
        return {"removed": model_id}

    @router.put("/images/active", dependencies=write)
    async def choose(body: ActiveBody) -> dict:
        try:
            image_gen.set_active(config, body.model)
        except image_gen.ImageGenError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"active": body.model}

    @router.post("/images/generate", dependencies=draw)
    async def generate(body: GenerateBody) -> dict:
        try:
            return await image_gen.generate(config, engine(app), body.prompt, size=body.size,
                                            seed=body.seed, steps=body.steps, negative=body.negative)
        except image_gen.ImageGenError as exc:
            raise HTTPException(422, str(exc)) from None

    @router.get("/images/gallery", dependencies=draw)
    async def gallery() -> list[dict]:
        return await asyncio.to_thread(image_gen.gallery, config)

    @router.get("/images/files/{name}", dependencies=draw)
    async def picture(name: str):
        path = image_gen.image_path(config, name)
        if path is None:
            raise HTTPException(404, "No such picture.")
        return FileResponse(path, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})

    @router.delete("/images/files/{name}", dependencies=draw)
    async def delete(name: str) -> dict:
        if not await asyncio.to_thread(image_gen.delete_image, config, name):
            raise HTTPException(404, "No such picture.")
        return {"deleted": name}

    app.include_router(router)
