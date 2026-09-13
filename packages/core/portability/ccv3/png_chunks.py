"""PNG `tEXt` chunk read/write for Character Card V2/V3 files (G4.8, plan §11.3).

A card is a PNG whose metadata carries a base64-encoded JSON document in a `tEXt` chunk --
`chara` for V2, `ccv3` for V3, and card-writing frontends emit both for backward compatibility.
This module is the narrow parsing slice §11.3 recommends porting from `character-foundry`
rather than running a Node sidecar for one import path.

Deliberately stdlib-only (`struct`, `zlib`, `base64`): a PNG chunk is length + type + data
+ CRC32, and a dependency for that would be more surface than the format has.

**This module knows nothing about what the JSON means.** It returns bytes keyed by chunk
keyword; `normalise.py` decides what a `chara` payload *is*. Keeping the split means a
malformed card fails in one place with one kind of error, rather than half-parsing into
something a mapper then has to second-guess.
"""

from __future__ import annotations

import base64
import binascii
import json
import struct
import zlib
from typing import Any

_PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"

# Card keywords, most-preferred first: a file carrying both is a V3 card that also wrote a
# V2 chunk for older tools, so `ccv3` wins (§11.3).
CARD_KEYWORDS: tuple[str, ...] = ("ccv3", "chara")


class NotAPngError(ValueError):
    pass


class MalformedCardError(ValueError):
    """The PNG parsed but its card payload did not: not base64, not JSON, or not an
    object. Distinct from ``NotAPngError`` because the remedies differ -- one is "that
    isn't a card", the other is "that card is damaged"."""


def read_text_chunks(data: bytes) -> dict[str, bytes]:
    """Every `tEXt` chunk as ``{keyword: raw_bytes}``. Later chunks with the same keyword
    win, matching how PNG readers generally treat repeats.

    Non-`tEXt` chunks are skipped without inspection -- a card's image data is none of this
    module's business, and walking it would only create ways to fail on a valid file."""
    if not data.startswith(_PNG_SIGNATURE):
        raise NotAPngError("data does not start with the PNG signature")

    chunks: dict[str, bytes] = {}
    offset = len(_PNG_SIGNATURE)
    while offset + 8 <= len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunk_type = data[offset + 4 : offset + 8]
        body = data[offset + 8 : offset + 8 + length]
        if chunk_type == b"tEXt":
            keyword, _, value = body.partition(b"\x00")
            chunks[keyword.decode("latin-1")] = value
        offset += 8 + length + 4  # length + type + data + CRC
        if chunk_type == b"IEND":
            break
    return chunks


def decode_card_payload(raw: bytes) -> dict[str, Any]:
    """base64 -> UTF-8 -> JSON object. Every failure becomes ``MalformedCardError`` with
    the stage named, because "invalid card" alone sends a user to a forum."""
    try:
        decoded = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise MalformedCardError(f"card payload is not valid base64: {exc}") from exc
    try:
        parsed = json.loads(decoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MalformedCardError(f"card payload is not valid UTF-8 JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise MalformedCardError(
            f"card payload is a {type(parsed).__name__}, expected a JSON object"
        )
    return parsed


def extract_card(data: bytes) -> tuple[str, dict[str, Any]]:
    """``(keyword, payload)`` for the best card chunk present, preferring `ccv3`.

    Also accepts a plain JSON file: the ecosystem shares cards both ways, and refusing the
    JSON one would be refusing content for a reason the user cannot see. Returns the
    keyword so a caller can tell which spec version it actually got rather than inferring
    it from the payload's own (frequently wrong) `spec` field."""
    if not data.startswith(_PNG_SIGNATURE):
        try:
            parsed = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise NotAPngError("data is neither a PNG nor a JSON card file") from exc
        if not isinstance(parsed, dict):
            raise MalformedCardError("JSON card file is not an object")
        return ("json", parsed)

    chunks = read_text_chunks(data)
    for keyword in CARD_KEYWORDS:
        if keyword in chunks:
            return (keyword, decode_card_payload(chunks[keyword]))
    raise MalformedCardError(
        f"PNG carries no card chunk; looked for {list(CARD_KEYWORDS)}, found {sorted(chunks)}"
    )


def write_text_chunks(image: bytes, chunks: dict[str, bytes]) -> bytes:
    """Rewrites ``image`` with ``chunks`` inserted before `IEND`, replacing any existing
    `tEXt` chunk of the same keyword. Used by G4.9's export; here because reading and
    writing the same byte format from two modules is how they drift apart."""
    if not image.startswith(_PNG_SIGNATURE):
        raise NotAPngError("data does not start with the PNG signature")

    out = bytearray(_PNG_SIGNATURE)
    offset = len(_PNG_SIGNATURE)
    replaced = set(chunks)
    while offset + 8 <= len(image):
        (length,) = struct.unpack(">I", image[offset : offset + 4])
        chunk_type = image[offset + 4 : offset + 8]
        end = offset + 8 + length + 4
        body = image[offset + 8 : offset + 8 + length]

        if chunk_type == b"tEXt":
            existing_keyword, _, _ = body.partition(b"\x00")
            if existing_keyword.decode("latin-1") in replaced:
                offset = end
                continue
        if chunk_type == b"IEND":
            for keyword, value in chunks.items():
                out += _text_chunk(keyword, value)
        out += image[offset:end]
        offset = end
    return bytes(out)


def _text_chunk(keyword: str, value: bytes) -> bytes:
    body = keyword.encode("latin-1") + b"\x00" + value
    return (
        struct.pack(">I", len(body))
        + b"tEXt"
        + body
        + struct.pack(">I", zlib.crc32(b"tEXt" + body) & 0xFFFFFFFF)
    )


def encode_card_payload(payload: dict[str, Any]) -> bytes:
    """The inverse of ``decode_card_payload``. Sorted keys so exporting the same card twice
    produces identical bytes -- the same determinism `.pyr` bundles get, and for the same
    reason: two exports that differ only in dict ordering are two exports nobody can diff."""
    return base64.b64encode(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode())
