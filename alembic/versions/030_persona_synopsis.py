"""Allow synopsis memories and compiler/user-declared provenance.

Revision ID: 029_persona_synopsis
Revises: 028_add_audit_log, 028_add_curator_rule_versioning
Create Date: 2026-10-06
"""

import sqlalchemy as sa

from alembic import op

revision = "029_persona_synopsis"
down_revision = ("028_add_audit_log", "028_add_curator_rule_versioning")
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_memory_nodes_content_type", "memory_nodes", type_="check")
    op.create_check_constraint(
        "ck_memory_nodes_content_type",
        "memory_nodes",
        "content_type IN ('experiential', 'knowledge', 'behavioral', 'synopsis')",
    )

    op.drop_constraint("ck_memory_nodes_source", "memory_nodes", type_="check")
    op.create_check_constraint(
        "ck_memory_nodes_source",
        "memory_nodes",
        "source IN ('agent', 'dreaming', 'import', 'compiler', 'user-declared')",
    )


def downgrade() -> None:
    bind = op.get_bind()
    synopsis_count = bind.execute(
        sa.text("SELECT count(*) FROM memory_nodes WHERE content_type = 'synopsis'")
    ).scalar_one()
    new_source_count = bind.execute(
        sa.text("SELECT count(*) FROM memory_nodes WHERE source IN ('compiler', 'user-declared')")
    ).scalar_one()
    if synopsis_count or new_source_count:
        raise RuntimeError(
            "Cannot downgrade persona synopsis migration while synopsis memories "
            "or compiler/user-declared sources exist. Remove or convert those "
            "records before downgrading."
        )

    op.drop_constraint("ck_memory_nodes_content_type", "memory_nodes", type_="check")
    op.create_check_constraint(
        "ck_memory_nodes_content_type",
        "memory_nodes",
        "content_type IN ('experiential', 'knowledge', 'behavioral')",
    )

    op.drop_constraint("ck_memory_nodes_source", "memory_nodes", type_="check")
    op.create_check_constraint(
        "ck_memory_nodes_source",
        "memory_nodes",
        "source IN ('agent', 'dreaming', 'import')",
    )
