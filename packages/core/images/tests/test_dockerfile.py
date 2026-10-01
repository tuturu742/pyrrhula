"""What the Dockerfile validator refuses, warns about, and lets through."""

from __future__ import annotations

from core.images.builds import content_hash, final_dockerfile, harness_layer
from core.images.dockerfile import validate_dockerfile

GOOD = """\
# A toolchain image
ARG GODOT=4.3
FROM debian:bookworm AS base
RUN apt-get update \\
 && apt-get install -y --no-install-recommends git ca-certificates curl \\
 && rm -rf /var/lib/apt/lists/*
FROM base
COPY --from=base /usr/bin/git /usr/bin/git
"""


def test_a_toolchain_dockerfile_passes_and_names_its_base() -> None:
    report = validate_dockerfile(GOOD)
    assert report.ok, report.errors
    assert report.from_refs == ["docker.io/library/debian:bookworm"]


def _errors(text: str) -> str:
    return " | ".join(validate_dockerfile(text).errors)


def test_the_refusals() -> None:
    assert "first instruction" in _errors("RUN echo hi\nFROM debian:12\n")
    assert "ADD" in _errors("FROM debian:12\nADD https://x/y.tgz /opt\n")
    assert "build context" in _errors("FROM debian:12\nCOPY . /src\n")
    assert "--mount" in _errors("FROM debian:12\nRUN --mount=type=secret,id=t cat /run/secrets/t\n")
    assert "heredoc" in _errors("FROM debian:12\nRUN <<EOF\necho hi\nEOF\n")
    assert "syntax" in _errors("# syntax=docker/dockerfile:1\nFROM debian:12\n")
    assert "ONBUILD" in _errors("FROM debian:12\nONBUILD RUN echo\n")
    assert "VOLUME" in _errors("FROM debian:12\nVOLUME /data\n")
    assert "pyrrhula.*" in _errors('FROM debian:12\nLABEL pyrrhula.build="x"\n')
    assert "has no tag" in _errors("FROM debian\n")
    assert "variable" in _errors("ARG B=debian:12\nFROM $B\n")
    assert "no FROM" in _errors("ARG X=1\n")


def test_the_warnings() -> None:
    warnings = " | ".join(
        validate_dockerfile(
            'FROM debian:12\nUSER app\nENTRYPOINT ["x"]\n'
            "RUN curl -fsSL https://get.example | sh\n"
            "ENV TOKEN=ghp_" + "a" * 36 + "\n"
        ).warnings
    )
    for expected in ("non-root", "ENTRYPOINT", "piping", "secret"):
        assert expected in warnings, expected


def test_the_harness_layer_is_the_operators_commands_appended_by_code() -> None:
    spec = {"key": "opencode", "setup_cmds": ["npm i -g opencode-ai@1.18.33"]}
    assert harness_layer(spec).splitlines()[-1] == "RUN npm i -g opencode-ai@1.18.33"
    final = final_dockerfile("FROM node:20", spec)
    assert final.startswith("FROM node:20\n") and final.rstrip().endswith("@1.18.33")
    assert final_dockerfile("FROM node:20\n", None) == "FROM node:20\n"


def test_the_content_hash_names_the_recipe() -> None:
    baked = {"key": "opencode", "version": "1.18.33", "fingerprint": "f"}
    same = content_hash("FROM a:1\n", baked)
    assert same == content_hash("FROM a:1\n", dict(baked))
    assert same != content_hash("FROM a:2\n", baked)
    assert same != content_hash("FROM a:1\n", None)
    assert same != content_hash("FROM a:1\n", baked, nonce="rebuild")
