"""Publishable three-arm report (E2.8): renders a matrix of `MatrixCell`s (per
(model/provider, axis-value, arm)) into a markdown write-up -- the Phase-2 exit gate
requires this exist and be readable by a human deciding whether §8 actually works,
not just a machine-readable pass/fail.
"""

from __future__ import annotations

from collections.abc import Sequence

from eval.arms.matrix import ALL_METRIC_NAMES, MatrixCell


def render_markdown(cells: Sequence[MatrixCell], *, title: str = "Pyrrhula secrets eval") -> str:
    lines = [f"# {title}", ""]
    lines.append("| Provider | Arm | " + " | ".join(ALL_METRIC_NAMES) + " |")
    lines.append("|" + "---|" * (2 + len(ALL_METRIC_NAMES)))
    for cell in sorted(cells, key=lambda c: (c.provider_label, c.arm)):
        values = [
            f"{cell.metrics[name]:.3f}" if name in cell.metrics else "—"
            for name in ALL_METRIC_NAMES
        ]
        lines.append(f"| {cell.provider_label} | {cell.arm} | " + " | ".join(values) + " |")
    return "\n".join(lines) + "\n"
