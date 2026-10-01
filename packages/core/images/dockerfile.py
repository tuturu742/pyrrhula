"""What a Dockerfile written here may contain -- checked before any builder sees it.

An image built here is a **toolchain**: the tools a repository's delegations need. The
build context is the Dockerfile alone, never repository files, so nothing from the code
being worked on (or a secret checked into it) can be baked in. The rules follow from that
and from portability across the builders an operator may have chosen -- a classic Docker
builder behind Portainer, BuildKit in CI:

Refused:
- anything before the first ``FROM`` other than ``ARG`` (Docker's own rule);
- ``ADD`` (fetches URLs and unpacks archives -- a second, unchecked way in);
- ``COPY`` without ``--from`` (there is no context to copy from);
- BuildKit-only syntax: ``RUN --mount|--network|--security``, heredocs, a ``# syntax=``
  directive -- the same file has to build on every kind of builder;
- ``ONBUILD`` and ``VOLUME`` (behaviour that fires somewhere else, later);
- ``LABEL pyrrhula.*`` (the platform's own labels are set by the platform);
- an untagged ``FROM`` (it would build from whatever ``latest`` is that day).

Warned, not refused: a non-root ``USER`` (delegations expect to install), an
``ENTRYPOINT`` (engines run their own command), ``curl … | sh``, and anything that looks
like a secret -- an image is readable by everyone who can pull it.

``FROM`` references are returned so the caller can hold them to the namespace and the
runtime-image allowlist, which needs the database and so is not done here.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

MAX_BYTES = 32 * 1024

_INSTRUCTION = re.compile(r"^\s*([A-Za-z]+)(?:\s+(.*))?$", re.DOTALL)
_DIRECTIVE = re.compile(r"^#\s*([a-zA-Z]+)\s*=")
_SECRETISH = [
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"(?i)-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\b(password|passwd|secret|token|api[_-]?key)\s*=\s*\S{6,}"),
]
_PIPE_TO_SHELL = re.compile(r"(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da)?sh\b")


@dataclass
class DockerfileReport:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    # Every external base image (stage names and ``scratch`` excluded).
    from_refs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def _logical_lines(text: str) -> list[tuple[int, str]]:
    """(first physical line number, instruction) with continuations joined and comments
    dropped. Parser directives are reported separately by the caller."""
    out: list[tuple[int, str]] = []
    buffer = ""
    start = 0
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.rstrip()
        stripped = line.lstrip()
        if not buffer and (not stripped or stripped.startswith("#")):
            continue
        if buffer and stripped.startswith("#"):
            continue  # a comment inside a continuation is dropped, as Docker does
        if not buffer:
            start = number
        if line.endswith("\\"):
            buffer += line[:-1] + " "
            continue
        buffer += line
        out.append((start, buffer.strip()))
        buffer = ""
    if buffer.strip():
        out.append((start, buffer.strip()))
    return out


def _flags_and_rest(args: str) -> tuple[dict[str, str], str]:
    flags: dict[str, str] = {}
    rest = args
    while rest.startswith("--"):
        token, _, rest = rest.partition(" ")
        name, _, value = token[2:].partition("=")
        flags[name.lower()] = value
        rest = rest.lstrip()
    return flags, rest


def validate_dockerfile(text: str) -> DockerfileReport:
    from core.repos.image_ref import (
        ImageRefError,
        canonical,
        has_tag_or_digest,
        normalise_image_ref,
    )

    report = DockerfileReport()
    if len(text.encode()) > MAX_BYTES:
        report.errors.append(f"the Dockerfile is larger than {MAX_BYTES // 1024} KiB")
        return report
    for raw in text.splitlines():
        if not raw.strip():
            continue
        match = _DIRECTIVE.match(raw.strip())
        if match is None:
            break
        if match.group(1).lower() == "syntax":
            report.errors.append(
                "line 1: '# syntax=' selects a BuildKit frontend; the same file has to "
                "build on every builder this deployment may use"
            )
    if "<<" in text and re.search(r"<<-?\s*['\"]?[A-Za-z_]+", text):
        report.errors.append("heredocs are BuildKit-only; use RUN with && and line continuations")

    lines = _logical_lines(text)
    if not lines:
        report.errors.append("the Dockerfile is empty")
        return report

    stages: set[str] = set()
    seen_from = False
    for number, line in lines:
        match = _INSTRUCTION.match(line)
        if match is None:
            report.errors.append(f"line {number}: not an instruction")
            continue
        instruction = match.group(1).upper()
        args = (match.group(2) or "").strip()

        if not seen_from and instruction not in ("FROM", "ARG"):
            report.errors.append(f"line {number}: the first instruction must be FROM")
            continue

        if instruction == "FROM":
            seen_from = True
            flags, rest = _flags_and_rest(args)
            parts = rest.split()
            if not parts:
                report.errors.append(f"line {number}: FROM needs an image")
                continue
            base = parts[0]
            if len(parts) >= 3 and parts[1].upper() == "AS":
                stages.add(parts[2].lower())
            if base.lower() in stages or base == "scratch":
                continue
            if "$" in base:
                report.errors.append(
                    f"line {number}: FROM {base}: a variable base image cannot be checked "
                    "before it is built; write the image out"
                )
                continue
            try:
                normalised = normalise_image_ref(base) or ""
            except ImageRefError as exc:
                report.errors.append(f"line {number}: FROM {base}: {exc}")
                continue
            if not has_tag_or_digest(normalised):
                report.errors.append(f"line {number}: FROM {base}: name a tag or a digest")
                continue
            report.from_refs.append(canonical(normalised))
        elif instruction == "ADD":
            report.errors.append(
                f"line {number}: ADD is not allowed -- fetch with RUN (and check what you "
                "fetched), or COPY --from another stage"
            )
        elif instruction == "COPY":
            flags, _ = _flags_and_rest(args)
            if "from" not in flags:
                report.errors.append(
                    f"line {number}: COPY without --from: an image built here has no build "
                    "context -- it is the tools, never the repository's files"
                )
        elif instruction == "RUN":
            flags, rest = _flags_and_rest(args)
            for name in ("mount", "network", "security"):
                if name in flags:
                    report.errors.append(
                        f"line {number}: RUN --{name} is BuildKit-only and is not allowed"
                    )
            if _PIPE_TO_SHELL.search(rest):
                report.warnings.append(
                    f"line {number}: piping a download into a shell runs whatever the server "
                    "sends that day; prefer a pinned package or a checked checksum"
                )
        elif instruction in ("ONBUILD", "VOLUME"):
            report.errors.append(f"line {number}: {instruction} is not allowed")
        elif instruction == "LABEL":
            if re.search(r"(^|\s)[\"']?pyrrhula\.", args):
                report.errors.append(
                    f"line {number}: pyrrhula.* labels are set by the platform, not the file"
                )
        elif instruction == "USER":
            user = args.split(":", 1)[0].strip()
            if user not in ("root", "0"):
                report.warnings.append(
                    f"line {number}: USER {user}: delegations install packages and run "
                    "setup commands; a non-root user will make those fail"
                )
        elif instruction == "ENTRYPOINT":
            report.warnings.append(
                f"line {number}: ENTRYPOINT is replaced when an engine runs a delegation; "
                "it will not run"
            )

    if not seen_from:
        report.errors.append("there is no FROM instruction")
    for pattern in _SECRETISH:
        if pattern.search(text):
            report.warnings.append(
                "something here looks like a secret; an image is readable by everyone who "
                "can pull it, and so is every layer of it"
            )
            break
    return report
