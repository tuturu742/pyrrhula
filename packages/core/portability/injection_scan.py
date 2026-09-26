"""Prompt-injection scanning for imported text.

A `.pyr` bundle, a character card, or a repository README is **attacker-controlled input**
headed for a tool-calling agent's context. No marketplace exists, but that is not the threat:
out-of-band sharing means bundles arrive from strangers anyway.

**What this is and is not.** It is a pattern scanner: declarative rules over text, no model
call, no plugin surface, no user-supplied logic (CLAUDE.md rule 10). It catches the shapes
injection *takes* -- an imperative aimed at the model, a forged role header, tool-call
syntax, a fake system envelope -- not injection *semantics*, which no regex can decide. It
will miss cleverly-phrased attacks and it will occasionally flag innocent prose.

That asymmetry is deliberate and it is why the response is **quarantine, not rejection**:
a false positive costs a human one click, a false negative costs a leak. Nothing here
decides whether content is malicious; it decides whether a human should look before an
agent does.

**It is posture, not a control.** The real controls are the citation envelope (contents are
data, never instructions), the standing system rule that makes that envelope mean
something, and INV-8's exclusion. This scanner is the layer that makes a human notice.
Saying so plainly matters: a team that believes the scanner is the control will eventually
weaken one of the actual ones to make an import go through.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class InjectionRule:
    key: str
    pattern: re.Pattern[str]
    description: str


@dataclass(frozen=True)
class InjectionFinding:
    rule_key: str
    description: str
    excerpt: str

    def render(self) -> str:
        return f"{self.rule_key}: {self.description} ({self.excerpt!r})"


def _rule(key: str, pattern: str, description: str) -> InjectionRule:
    return InjectionRule(
        key=key,
        pattern=re.compile(pattern, re.IGNORECASE | re.MULTILINE),
        description=description,
    )


# Ordered most-specific first, so a match's `rule_key` names the sharpest thing found
# rather than whichever generic rule happened to be checked earliest.
RULES: tuple[InjectionRule, ...] = (
    _rule(
        "role_header",
        r"(\[/?(?:system|assistant|user)\]|<\|im_(?:start|end)\|>|^###\s*(?:system|instruction)s?\b)",
        "a forged role header or chat-template marker -- text pretending to be a turn "
        "boundary the model should honour",
    ),
    _rule(
        "instruction_override",
        r"\b(?:ignore|disregard|forget|override)\b[^.\n]{0,40}\b(?:previous|prior|above|"
        r"earlier|all)\b[^.\n]{0,20}\b(?:instruction|prompt|rule|direction|message)s?\b",
        "an imperative telling the model to discard its instructions",
    ),
    _rule(
        "persona_hijack",
        r"\byou\s+are\s+now\b|\bfrom\s+now\s+on,?\s+you\b|\bact\s+as\s+(?:if\s+you\s+are\s+)?"
        r"(?:a\s+)?(?:different|new)\b",
        "an attempt to replace the agent's persona from inside content",
    ),
    _rule(
        "tool_call_syntax",
        r"<(?:tool_call|function_call|invoke)\b|\"tool_calls\"\s*:|\bfunction\s*=\s*\{",
        "tool-call syntax embedded in prose -- content trying to look like a call the "
        "runtime should execute",
    ),
    _rule(
        "secret_extraction",
        r"\b(?:reveal|disclose|print|output|repeat|show)\b[^.\n]{0,40}\b(?:system\s+prompt|"
        r"your\s+instructions|the\s+secret|hidden\s+(?:text|content))\b",
        "an instruction aimed at extracting the system prompt or concealed content",
    ),
    _rule(
        "exfiltration",
        r"\b(?:send|post|upload|exfiltrate|email)\b[^.\n]{0,30}\b(?:to\s+https?://|to\s+"
        r"[\w.-]+@[\w.-]+)",
        "an instruction to send content to an external address",
    ),
)


def scan_text(text: str, *, rules: tuple[InjectionRule, ...] = RULES) -> list[InjectionFinding]:
    """Every rule that matches, with the matched excerpt. Returns all findings rather than
    stopping at the first: a reviewer deciding whether to clear an entry wants to see
    everything the scanner objected to, not the alphabetically-luckiest one."""
    findings: list[InjectionFinding] = []
    for rule in rules:
        match = rule.pattern.search(text)
        if match is not None:
            findings.append(
                InjectionFinding(
                    rule_key=rule.key,
                    description=rule.description,
                    excerpt=_excerpt(text, match.start(), match.end()),
                )
            )
    return findings


def quarantine_reason(findings: list[InjectionFinding]) -> str | None:
    """The human-readable reason stored on the quarantined row. ``None`` when nothing
    matched -- so a caller can write ``reason = quarantine_reason(scan_text(body))`` and
    let ``None`` mean "not quarantined" without a second branch."""
    if not findings:
        return None
    return "; ".join(f.render() for f in findings)


def _excerpt(text: str, start: int, end: int, *, pad: int = 24) -> str:
    lo = max(0, start - pad)
    hi = min(len(text), end + pad)
    snippet = text[lo:hi].replace("\n", " ").strip()
    return f"…{snippet}…" if (lo > 0 or hi < len(text)) else snippet
