"""The delete verb in the codegen protocol.

The protocol could only write. Both commit paths write, so a file the model omitted
stayed exactly where it was, and "remove the dead module" produced an empty pull request
with nothing anywhere explaining why -- the one shape of ordinary work the pipeline
could not express.
"""

from __future__ import annotations

from adapters.mcp.codegen import parse_deletions, parse_file_blocks


def test_delete_blocks_are_extracted() -> None:
    text = (
        "===FILE: src/new.py===\nprint('hi')\n===END===\n"
        "===DELETE: src/old.py===\n"
        "===DELETE: docs/stale.md===\n"
    )
    assert parse_file_blocks(text) == {"src/new.py": "print('hi')\n"}
    assert parse_deletions(text) == {"src/old.py", "docs/stale.md"}


def test_a_write_in_the_same_response_beats_a_delete() -> None:
    """A model that emits both for one path has contradicted itself. Writing wins: a
    delete that discarded content the model had just produced is the more expensive way
    to be wrong."""
    text = "===FILE: a.py===\nkeep me\n===END===\n===DELETE: a.py===\n"
    files = parse_file_blocks(text)
    assert parse_deletions(text, keep=files) == set()


def test_traversal_and_absolute_paths_are_refused() -> None:
    """Same confinement as file blocks, and for a sharper reason: this verb removes
    things. Checked before any normalisation, since stripping would turn `../evil` into
    a path that looks fine."""
    text = "===DELETE: ../../etc/passwd===\n===DELETE: /etc/shadow===\n===DELETE: ok/path.py===\n"
    assert parse_deletions(text) == {"ok/path.py"}


def test_a_response_that_only_deletes_is_valid() -> None:
    """Removing files without writing any is a whole, ordinary task -- it must not look
    like the model failed to answer."""
    text = "===DELETE: tasks/old.md===\n"
    assert parse_file_blocks(text) == {}
    assert parse_deletions(text) == {"tasks/old.md"}
