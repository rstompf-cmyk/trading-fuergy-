"""Bug #614: d1_soc_trajectory table

SOC trajektória očakávaná D-1 plánom per 15-min slot.
RT engine musí rešpektovať túto trajektóriu (±tolerance %),
inak sa obmedzí.

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-09 09:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'd1_soc_trajectory',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('slot_idx', sa.Integer(), nullable=False),
        sa.Column('expected_soc_pct', sa.Float(), nullable=False),
        sa.Column('tolerance_pct', sa.Float(), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'day', 'slot_idx',
                              name='uq_d1_soc_trajectory'),
        sa.CheckConstraint("slot_idx >= 0 AND slot_idx <= 95",
                            name='ck_d1_soc_slot_idx'),
        sa.CheckConstraint("expected_soc_pct >= 0 AND expected_soc_pct <= 100",
                            name='ck_d1_soc_pct'),
        sa.CheckConstraint("tolerance_pct >= 0 AND tolerance_pct <= 100",
                            name='ck_d1_soc_tolerance'),
    )
    op.create_index('idx_d1_soc_lookup', 'd1_soc_trajectory',
                     ['profile_id', 'day', 'slot_idx'])
    op.create_index('ix_d1_soc_trajectory_profile_id',
                     'd1_soc_trajectory', ['profile_id'])
    op.create_index('ix_d1_soc_trajectory_day',
                     'd1_soc_trajectory', ['day'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_d1_soc_trajectory_day', table_name='d1_soc_trajectory')
    op.drop_index('ix_d1_soc_trajectory_profile_id', table_name='d1_soc_trajectory')
    op.drop_index('idx_d1_soc_lookup', table_name='d1_soc_trajectory')
    op.drop_table('d1_soc_trajectory')
