"""INV-10 across the export boundary (G4.5, plan §11.2): rebuild a turn's rendered
knowledge block from a `.pyr` bundle alone and check it against the hash the manifest
recorded, with no database in the loop.

**Why this is a real proof and not a tautology.** The bundle carries the manifest's entry
list (citation ids, order, chunk ids) and the chunk *bodies*; it does not carry the
rendered string. Rebuilding means re-running the assembler's own envelope and ordering
rules over bundle data and getting a byte-identical result -- so a bundle that dropped a
chunk, reordered entries, or mangled a body fails, which is exactly the class of bug an
export is prone to.

**What is in scope, stated precisely.** This rebuilds the *knowledge* section. A turn
whose context also contained recent history, entity state, behaviour directives, or a
secret injection has those in ``rendered_hash`` too, and they are not all reconstructible
from a bundle -- a *concealed secret's* injection deliberately never leaves the building
at all. So ``rebuild_rendered_knowledge`` reproduces the whole rendered context exactly
when the turn's context was knowledge-only, and reproduces the knowledge block otherwise.
That is a boundary of the data, not a shortcut: a sanitised bundle that could replay a
turn containing a concealed secret would be a leak, not a feature.
"""

from __future__ import annotations

import hashlib
from typing import Any

from core.assembler.context_assembler import citation_envelope
from core.portability.bundle import BundleReader


class BundleReplayError(Exception):
    """The bundle lacks something the manifest says the turn used -- a chunk file, a
    source's meta. Distinct from a hash mismatch: this is "cannot even attempt", which a
    caller should report differently from "attempted and differed"."""


def _chunk_index(reader: BundleReader) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for path in reader.paths_under("knowledge/"):
        if "/chunks/" in path and path.endswith(".json"):
            payload = reader.read_json(path)
            index[str(payload["id"])] = payload
    return index


def _source_names(reader: BundleReader) -> dict[str, str]:
    names: dict[str, str] = {}
    for path in reader.paths_under("knowledge/"):
        if path.endswith("/meta.json"):
            payload = reader.read_json(path)
            names[str(payload["id"])] = str(payload["name"])
    return names


def _entry_titles(reader: BundleReader) -> dict[str, str]:
    titles: dict[str, str] = {}
    for path in reader.paths_under("knowledge/"):
        if path.endswith(".meta.json"):
            payload = reader.read_json(path)
            titles[str(payload["id"])] = str(payload["title"])
    return titles


def rebuild_rendered_knowledge(reader: BundleReader, manifest_record: dict[str, Any]) -> str:
    """Re-renders the knowledge block for one ``manifests.jsonl`` record.

    Ordering is taken straight from the recorded ``entries`` list, which the assembler
    already wrote in final render order (stable entries first, then by class and rank --
    see ``_render_knowledge``). Reconstructing that ordering *rule* here instead would be
    a second implementation of it, free to drift from the first; reading the recorded
    order is both simpler and the thing that actually replays."""
    chunks = _chunk_index(reader)
    names = _source_names(reader)
    titles = _entry_titles(reader)

    stable: list[str] = []
    volatile: list[str] = []
    for entry in manifest_record.get("entries", []):
        chunk_id = str(entry["chunk_id"])
        chunk = chunks.get(chunk_id)
        if chunk is None:
            raise BundleReplayError(
                f"manifest at event_seq {manifest_record.get('event_seq')} cites chunk "
                f"{chunk_id}, which the bundle does not contain"
            )
        block = citation_envelope(
            str(entry["citation_id"]),
            str(entry["class"]),
            names.get(str(entry["source_id"]), str(entry["source_id"])),
            titles.get(str(entry["entry_id"]), str(entry["entry_key"])),
            str(chunk["text"]),
        )
        (stable if entry.get("why") == "constant" else volatile).append(block)

    return "\n\n".join(b for b in ["\n\n".join(stable), "\n\n".join(volatile)] if b)


def replays_from_bundle(reader: BundleReader, manifest_record: dict[str, Any]) -> bool:
    """True when the rebuilt knowledge block hashes to the manifest's ``rendered_hash``.
    Meaningful only for a knowledge-only turn -- see the module docstring for exactly why
    that boundary is the data's and not this function's."""
    rebuilt = rebuild_rendered_knowledge(reader, manifest_record)
    return hashlib.sha256(rebuilt.encode()).hexdigest() == str(manifest_record["rendered_hash"])
