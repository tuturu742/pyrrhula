"""Rename rule_system.dice_grammar to expression_grammar.

CLAUDE.md rule 1 keeps domain words out of ``packages/core``; ``dice`` is on its
list and had been sitting in a core table's column name, the ORM attribute, the
tool key and the built-in handler's name since C1.5. The mechanical half of that
cleanup is this one column -- everything else the rename touched lives in Python
or in pack JSON.

The grammar's *contents* move with it: ``max_dice_count`` becomes
``max_term_count`` inside the JSONB, so existing rows are rewritten rather than
just relabelled. ``allowed_sides`` and ``allow_keep_drop`` are unchanged -- they
name an expression's shape, not the domain that uses it.

Two more keys move with it, for the same reason and in the same sweep: the
``dice_roller`` tool becomes ``randomizer``, and the ``campaign_recap`` report
template becomes ``narrative_recap``.

Revision ID: e2a7c5b18f64
Revises: d1f7a4c82b93
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e2a7c5b18f64"
down_revision: str | None = "d1f7a4c82b93"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.alter_column("rule_system", "dice_grammar", new_column_name="expression_grammar")
    op.execute(
        sa.text(
            "UPDATE rule_system SET expression_grammar = "
            "(expression_grammar - 'max_dice_count') || "
            "jsonb_build_object('max_term_count', expression_grammar -> 'max_dice_count') "
            "WHERE expression_grammar ? 'max_dice_count'"
        )
    )
    # A tenant that already carried both keys would break the (tenant_id, key) unique
    # constraint on rename. No deployment can have one today -- `randomizer` is new in
    # this revision -- but the rename must not be the thing that discovers otherwise.
    op.execute(
        sa.text(
            "DELETE FROM tool_definition t WHERE t.key = 'randomizer' AND EXISTS ("
            "SELECT 1 FROM tool_definition o "
            "WHERE o.tenant_id = t.tenant_id AND o.key = 'dice_roller')"
        )
    )
    op.execute(sa.text("UPDATE tool_definition SET key = 'randomizer' WHERE key = 'dice_roller'"))
    op.execute(
        sa.text(
            "UPDATE tool_definition SET impl_ref = 'builtin:randomizer' "
            "WHERE impl_ref = 'builtin:dice_roller'"
        )
    )
    # Same rule, same sweep: a core report template was keyed "campaign_recap" while
    # already emitting `report.recap` as its label_key -- the display string was
    # overlay-resolved, the key was not.
    op.execute(
        sa.text(
            "UPDATE report SET template_key = 'narrative_recap' "
            "WHERE template_key = 'campaign_recap'"
        )
    )
    # `resolution_record.tool_key` is deliberately left alone: the table is append-only
    # (CLAUDE.md rule 5, no UPDATE grant), and a record should keep the tool key that
    # actually produced it.


def downgrade() -> None:
    op.execute(
        sa.text(
            "UPDATE report SET template_key = 'campaign_recap' "
            "WHERE template_key = 'narrative_recap'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE tool_definition SET impl_ref = 'builtin:dice_roller' "
            "WHERE impl_ref = 'builtin:randomizer'"
        )
    )
    op.execute(sa.text("UPDATE tool_definition SET key = 'dice_roller' WHERE key = 'randomizer'"))
    op.execute(
        sa.text(
            "UPDATE rule_system SET expression_grammar = "
            "(expression_grammar - 'max_term_count') || "
            "jsonb_build_object('max_dice_count', expression_grammar -> 'max_term_count') "
            "WHERE expression_grammar ? 'max_term_count'"
        )
    )
    op.alter_column("rule_system", "expression_grammar", new_column_name="dice_grammar")
