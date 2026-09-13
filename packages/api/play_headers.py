"""Response headers and content types for serving a built web app.

Godot 4 web exports (and anything else using SharedArrayBuffer) only run when the page is
*cross-origin isolated*: COOP ``same-origin`` and COEP ``require-corp`` on the document
**and** every subresource it pulls. Two routes serve such builds -- the authenticated
per-repo play route and the public preview proxy -- and both must stamp the identical set,
so it lives here rather than being duplicated and quietly drifting apart.

Content types are derived from the path suffix rather than trusted from upstream:
``application/wasm`` is mandatory for ``WebAssembly.instantiateStreaming``, and a plain
static file server's mimetypes guess for ``.wasm`` cannot be relied on.
"""

from __future__ import annotations

PLAY_TYPES = {
    ".html": "text/html",
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".wasm": "application/wasm",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".json": "application/json",
    ".css": "text/css",
    ".wav": "audio/wav",
    ".ogg": "audio/ogg",
    # Godot ships the game data as .pck next to the wasm.
    ".pck": "application/octet-stream",
}

PLAY_HEADERS = {
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Embedder-Policy": "require-corp",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-cache",
}


def content_type_for(path: str) -> str:
    from pathlib import Path

    return PLAY_TYPES.get(Path(path).suffix.lower(), "application/octet-stream")
