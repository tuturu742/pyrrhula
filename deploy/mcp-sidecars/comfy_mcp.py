"""comfy-mcp: asset generation over MCP, bridging to a ComfyUI instance (M-D).

``generate_image(prompt, width, height)`` queues a plain txt2img workflow on ComfyUI
(``COMFY_URL``, default the host instance), polls history until the image exists,
stores a copy under its own ``/assets`` volume, and returns a URL the sidecar itself
serves -- an agent commits the asset into a repo through the delegation file-write
path, or a human fetches it for review. No base64 in tool results: a generated image
in a model context is tokens burned on noise.

``COMFY_CHECKPOINT`` names the checkpoint to load (must exist on the ComfyUI side).
"""

from __future__ import annotations

import json
import os
import random
import uuid
from pathlib import Path
from typing import Any

import httpx
from fastapi.responses import FileResponse
from mcp_http import McpServer

COMFY_URL = os.environ.get("COMFY_URL", "http://127.0.0.1:8188")
CHECKPOINT = os.environ.get("COMFY_CHECKPOINT", "sd_xl_base_1.0.safetensors")
ASSETS_DIR = Path(os.environ.get("ASSETS_DIR", "/assets"))
PUBLIC_URL = os.environ.get("PUBLIC_URL", "http://comfy-mcp:8091")
TIMEOUT_S = float(os.environ.get("COMFY_TIMEOUT_S", "300"))

server = McpServer("comfy-mcp")


def _workflow(prompt: str, width: int, height: int, seed: int) -> dict[str, Any]:
    return {
        "1": {"class_type": "CheckpointLoaderSimple", "inputs": {"ckpt_name": CHECKPOINT}},
        "2": {"class_type": "CLIPTextEncode", "inputs": {"text": prompt, "clip": ["1", 1]}},
        "3": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": "blurry, low quality, watermark", "clip": ["1", 1]},
        },
        "4": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "5": {
            "class_type": "KSampler",
            "inputs": {
                "model": ["1", 0],
                "positive": ["2", 0],
                "negative": ["3", 0],
                "latent_image": ["4", 0],
                "seed": seed,
                "steps": 20,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
            },
        },
        "6": {"class_type": "VAEDecode", "inputs": {"samples": ["5", 0], "vae": ["1", 2]}},
        "7": {
            "class_type": "SaveImage",
            "inputs": {"images": ["6", 0], "filename_prefix": "pyrrhula"},
        },
    }


@server.tool(
    "generate_image",
    "Generate an image with ComfyUI and return a URL to the stored asset. Use for "
    "sprites, textures, and concept art; commit the file into the repo via delegation.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string"},
            "width": {"type": "integer", "default": 512},
            "height": {"type": "integer", "default": 512},
            "transparent_background": {"type": "boolean", "default": False},
        },
        "required": ["prompt"],
    },
)
async def generate_image(
    prompt: str = "",
    width: int = 512,
    height: int = 512,
    transparent_background: bool = False,
) -> dict[str, Any]:
    seed = random.randint(0, 2**31 - 1)
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, read=TIMEOUT_S)) as client:
        queued = await client.post(
            f"{COMFY_URL}/prompt", json={"prompt": _workflow(prompt, width, height, seed)}
        )
        queued.raise_for_status()
        prompt_id = queued.json()["prompt_id"]

        image_ref: dict[str, Any] | None = None
        import asyncio

        deadline = asyncio.get_event_loop().time() + TIMEOUT_S
        while asyncio.get_event_loop().time() < deadline:
            history = (await client.get(f"{COMFY_URL}/history/{prompt_id}")).json()
            entry = history.get(prompt_id)
            if entry:
                status = entry.get("status", {})
                if status.get("status_str") == "error":
                    raise RuntimeError(f"ComfyUI reported an error: {json.dumps(status)[:400]}")
                for node_output in entry.get("outputs", {}).values():
                    images = node_output.get("images") or []
                    if images:
                        image_ref = images[0]
                        break
            if image_ref:
                break
            await asyncio.sleep(2)
        if image_ref is None:
            raise RuntimeError(f"ComfyUI produced no image within {TIMEOUT_S}s")

        fetched = await client.get(
            f"{COMFY_URL}/view",
            params={
                "filename": image_ref["filename"],
                "subfolder": image_ref.get("subfolder", ""),
                "type": image_ref.get("type", "output"),
            },
        )
        fetched.raise_for_status()

    payload = fetched.content
    if transparent_background:
        payload = _key_out_background(payload)

    ASSETS_DIR.mkdir(parents=True, exist_ok=True)
    asset_id = f"{uuid.uuid4().hex}.png"
    (ASSETS_DIR / asset_id).write_bytes(payload)
    url = f"{PUBLIC_URL}/assets/{asset_id}"
    return {
        "_text": f"image generated: {url} (seed {seed}, {width}x{height})",
        "asset_url": url,
        "seed": seed,
        "width": width,
        "height": height,
        "transparent_background": transparent_background,
    }


def _key_out_background(png_bytes: bytes) -> bytes:
    """Make the near-white surround transparent so a sprite can sit on any background.

    Diffusion models emit opaque RGB; a sprite asked for on a "plain background"
    therefore arrives as a BOX, which is exactly what it looks like drawn onto a game
    scene. Two things make this survive real generations:

    * the key colour is sampled from the border rather than assumed. Asking for white
      does not get white -- the same prompt produced a white surround on one seed and a
      black one on the next, and a hardcoded white key silently did nothing to the black.
    * the fill starts at the border and works inward, so colour matching the background
      *inside* the subject -- a cat's white chest, the whites of eyes -- stays opaque,
      where a global colour key would punch holes straight through it.
    """
    from collections import Counter, deque
    from io import BytesIO

    from PIL import Image

    image = Image.open(BytesIO(png_bytes)).convert("RGBA")
    w, h = image.size
    pixels = image.load()

    border = [pixels[x, y][:3] for x in range(w) for y in (0, h - 1)]
    border += [pixels[x, y][:3] for y in range(h) for x in (0, w - 1)]
    key = Counter(border).most_common(1)[0][0]
    tolerance = 48  # generous: diffusion backgrounds are flat but not perfectly uniform

    def is_bg(x: int, y: int) -> bool:
        r, g, b, _ = pixels[x, y]
        return (
            abs(r - key[0]) <= tolerance
            and abs(g - key[1]) <= tolerance
            and abs(b - key[2]) <= tolerance
        )

    seen = bytearray(w * h)
    queue: deque[tuple[int, int]] = deque()
    for x in range(w):
        for y in (0, h - 1):
            if is_bg(x, y) and not seen[y * w + x]:
                seen[y * w + x] = 1
                queue.append((x, y))
    for y in range(h):
        for x in (0, w - 1):
            if is_bg(x, y) and not seen[y * w + x]:
                seen[y * w + x] = 1
                queue.append((x, y))

    while queue:
        x, y = queue.popleft()
        pixels[x, y] = (255, 255, 255, 0)
        for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
            if 0 <= nx < w and 0 <= ny < h and not seen[ny * w + nx] and is_bg(nx, ny):
                seen[ny * w + nx] = 1
                queue.append((nx, ny))

    out = BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


app = server.build_app()


@app.get("/assets/{asset_id}")
async def get_asset(asset_id: str) -> FileResponse:
    path = (ASSETS_DIR / asset_id).resolve()
    if not str(path).startswith(str(ASSETS_DIR.resolve())) or not path.is_file():
        raise FileNotFoundError(asset_id)
    return FileResponse(path, media_type="image/png")
