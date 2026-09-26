"""Declarative committed-credential patterns (CLAUDE.md rule 10).

Gitleaks-style rules **as configuration data**: a regex, a name, and (for high-entropy
generic strings) a minimum Shannon entropy. No plugin surface, no user-authored code -- a
tenant adding a rule is adding a row, not a scanner.

**This is posture, not INV-8.** INV-8 is a structural guarantee about secrets Pyrrhula
*knows about*: the assembler cannot exclude a secret nobody registered. A credential
committed to somebody's repository is exactly such a secret, and no amount of scanning
turns "we probably caught it" into "it structurally cannot reach a model". So a hit
quarantines the chunk and asks a human, and the task file says the same thing rather than
implying a guarantee that isn't there.

Entropy is used sparingly. A pure entropy threshold flags every UUID, hash, and minified
asset in a repository, which is a queue nobody reviews -- and a review queue nobody reviews
is worse than no queue, because it looks like a control.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SecretPattern:
    key: str
    description: str
    pattern: re.Pattern[str]
    min_entropy: float | None = None


def _rule(
    key: str, description: str, pattern: str, *, min_entropy: float | None = None
) -> SecretPattern:
    return SecretPattern(
        key=key,
        description=description,
        pattern=re.compile(pattern),
        min_entropy=min_entropy,
    )


# Prefix-anchored wherever the vendor gives us a prefix: a rule that matches a known
# prefix has almost no false-positive rate, and a rule that matches "40 hex characters"
# has almost nothing else.
RULES: tuple[SecretPattern, ...] = (
    _rule("aws_access_key_id", "AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    _rule(
        "aws_secret_access_key",
        "AWS secret access key",
        r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?([A-Za-z0-9/+=]{40})",
        min_entropy=4.0,
    ),
    _rule("github_pat", "GitHub personal access token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    _rule("openai_key", "OpenAI-style API key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    _rule("slack_token", "Slack token", r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    _rule("google_api_key", "Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    _rule(
        "private_key_block",
        "PEM private key block",
        r"-----BEGIN (?:RSA |EC |OPENSSH |PGP )?PRIVATE KEY-----",
    ),
    _rule(
        "generic_assigned_secret",
        "a high-entropy value assigned to a secret-shaped name",
        r"(?i)\b(?:api[_-]?key|secret|token|passwd|password)\s*[=:]\s*['\"]([^'\"\s]{16,})['\"]",
        min_entropy=3.5,
    ),
)


@dataclass(frozen=True)
class SecretFinding:
    rule_key: str
    description: str
    excerpt: str

    def render(self) -> str:
        return f"{self.rule_key}: {self.description} ({self.excerpt})"


def shannon_entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = Counter(value)
    length = len(value)
    return -sum((c / length) * math.log2(c / length) for c in counts.values())


def scan_for_secrets(text: str, *, rules: tuple[SecretPattern, ...] = RULES) -> list[SecretFinding]:
    """Every rule that fires. The excerpt is **redacted**: rule name, position, and a
    masked fragment. A quarantine reason that quoted the credential would put it in a
    second table, which is the thing the quarantine exists to prevent."""
    findings: list[SecretFinding] = []
    for rule in rules:
        for match in rule.pattern.finditer(text):
            candidate = match.group(1) if match.groups() else match.group(0)
            if rule.min_entropy is not None and shannon_entropy(candidate) < rule.min_entropy:
                continue
            findings.append(
                SecretFinding(
                    rule_key=rule.key,
                    description=rule.description,
                    excerpt=_mask(candidate, match.start()),
                )
            )
            break  # one finding per rule is enough to quarantine; more is noise
    return findings


def quarantine_reason(findings: list[SecretFinding]) -> str | None:
    if not findings:
        return None
    return "; ".join(f.render() for f in findings)


def _mask(value: str, offset: int) -> str:
    """First four characters, then the length. Enough for a reviewer to find it in the
    file; not enough to be the credential."""
    head = value[:4]
    return f"at offset {offset}, starts {head!r}, {len(value)} chars"
