"""rules.flow_kind: a user rule can say what a checking row is (docs/SPEC-checking.md §9)

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-14 21:00:00.000000
"""

import sqlalchemy as sa
from alembic import op

revision = '0010'
down_revision = '0009'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('rules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('flow_kind', sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table('rules', schema=None) as batch_op:
        batch_op.drop_column('flow_kind')
