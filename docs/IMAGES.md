# Images

Kova can draw pictures on your own computer. It measures your hardware, recommends the best
image model that fits, downloads it only when you say so, and generates in chat or in the
Images page of the native app. Prompts and pictures never leave the machine.

## Turn it on

1. Install the **Images** plugin from the Marketplace (`kova catalog install images`). It gives
   the model the `generate_image` tool and the `/imagine` command.
2. Pick a model. The native app's Images page, or `GET /images`, lists every model with how it
   fits this PC and marks the recommended one. Installing downloads and verifies it.
3. Get the engine: stable-diffusion.cpp's `sd-server`. Kova uses, in order, the program in
   `[images] engine`, a managed copy when this Kova pins one for your system, or `sd-server`
   on the PATH. Download a release from
   [stable-diffusion.cpp's releases](https://github.com/leejet/stable-diffusion.cpp/releases),
   then `kova config set images.engine /path/to/sd-server`.

Ask for a picture in any chat ("draw a lighthouse at dusk in watercolour") or run `/imagine`.
Kova asks before each picture, like any program it does not control, and shows it inline when
it's done. Pictures are PNGs in `Kova Files/Images`, each with a small `.json` beside it holding
the prompt, size, seed and model.

## How the recommendation works

The same rules as local chat models: memory is reserved for the operating system and the display,
only the largest single GPU counts, and unknown memory or disk is never treated as a fit. Each
model is rated *fits comfortably*, *fits tightly* or *too big*, with the reason, and the
recommendation is the best model that fits. Without a GPU only the small models are offered,
with a speed warning.

| Model | Download | Needs | Licence |
| --- | --- | --- | --- |
| SDXS 512 | 0.7 GB | 2 GB VRAM, or a CPU | OpenRAIL++ |
| Stable Diffusion 1.5 | 1.8 GB | 3 GB VRAM, or a CPU (slow) | CreativeML OpenRAIL-M |
| Z-Image Turbo (compact) | 6.7 GB | 6 GB VRAM | Apache-2.0 |
| Z-Image Turbo (full quality) | 11.2 GB | 12 GB VRAM | Apache-2.0 |

Every file is pinned to a Hugging Face revision and its SHA-256 in `src/kova/image_gen.py`.
Downloads resume from a `.part` file and are only used once their exact length and checksum
match. A file two models share (Z-Image's VAE) is kept until neither needs it.

## The engine

Kova starts `sd-server` on a free loopback port with the chosen model's files and defaults
(steps, guidance, size), waits for it to answer, and keeps it running for the next picture.
Switching models restarts it; stopping the server stops it. Per-picture seed, steps and a
negative prompt go to sd-server in its `<sd_cpp_extra_args>` field; Kova strips that tag from
anything you type so a prompt cannot change engine options. Its output goes to
`images/engine.log` in the Kova folder.

`[images] server` points at an already-running OpenAI-compatible images server instead
(`POST /v1/images/generations`, answering with `b64_json`). Local only refuses one that is not
on this computer.

Managed engine builds are pinned like the llama.cpp engine: a release's archive names, sizes and
SHA-256 digests, reviewed in source. Until a release is pinned, `ENGINE_BUILDS` is empty and Kova
uses the program you point it at.

## API

| Route | Purpose |
| --- | --- |
| `GET /images` | hardware, every model rated, the recommendation, the engine, the active model |
| `POST /images/models/{id}/install` | download and verify a model; SSE `progress`, `done`, `error` |
| `DELETE /images/models/{id}` | delete a model's files |
| `PUT /images/active` | choose the model that draws |
| `POST /images/generate` | `{"prompt", "size"?, "seed"?, "steps"?, "negative"?}` → the saved picture's row |
| `GET /images/gallery` | saved pictures, newest first |
| `GET /images/files/{name}` · `DELETE …` | one picture |
