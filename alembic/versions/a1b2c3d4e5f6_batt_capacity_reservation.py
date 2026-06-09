"""Bug #611: batt_capacity_reservation table

Capacity ledger pre rezervácie kapacity batérie per 15-min slot.
Drží explicitné poradie D-1 → VDT → RT s audit trailom.

Revision ID: a1b2c3d4e5f6
Revises: cd48b8f3d8eb
Create Date: 2026-06-09 03:30:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, Sequence[str], None] = 'cd48b8f3d8eb'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'batt_capacity_reservation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('slot_idx', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(length=16), nullable=False),
        sa.Column('direction', sa.String(length=10), nullable=False),
        sa.Column('kw', sa.Float(), nullable=False),
        sa.Column('trade_id', sa.String(length=64), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'day', 'slot_idx', 'source', 'direction',
                              'trade_id', name='uq_batt_reservation'),
        sa.CheckConstraint("source IN ('d1','vdt','rt','auto_control')",
                            name='ck_batt_source'),
        sa.CheckConstraint("direction IN ('charge','discharge')",
                            name='ck_batt_direction'),
        sa.CheckConstraint("slot_idx >= 0 AND slot_idx <= 95",
                            name='ck_batt_slot_idx'),
        sa.CheckConstraint("kw >= 0", name='ck_batt_kw_positive'),
    )
    op.create_index('idx_batt_lookup', 'batt_capacity_reservation',
                     ['profile_id', 'day', 'slot_idx'])
    op.create_index('ix_batt_capacity_reservation_profile_id',
                     'batt_capacity_reservation', ['profile_id'])
    op.create_index('ix_batt_capacity_reservation_day',
                     'batt_capacity_reservation', ['day'])


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index('ix_batt_capacity_reservation_day',
                    table_name='batt_capacity_reservation')
    op.drop_index('ix_batt_capacity_reservation_profile_id',
                    table_name='batt_capacity_reservation')
    op.drop_index('idx_batt_lookup', table_name='batt_capacity_reservation')
    op.drop_table('batt_capacity_reservation')
