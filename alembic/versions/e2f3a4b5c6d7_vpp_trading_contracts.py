"""VPP trading contract-rows: trade_order / allocation / availability_report

Perzistencia kontraktov core/schemas/vpp.py (Order/Allocation/AvailabilityReport).
Aditívne — kým trading vrstva nepíše, tabuľky existujú ale sú prázdne (žiadna
zmena správania). Zhodné s db/models.py (TradeOrder/AllocationRow/AvailabilityReportRow).

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-06-18 13:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, Sequence[str], None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'trade_order',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('order_id', sa.String(length=64), nullable=False),
        sa.Column('account_id', sa.String(length=64), nullable=False),
        sa.Column('block_id', sa.String(length=64), nullable=False),
        sa.Column('country', sa.String(length=4), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('slot_idx', sa.Integer(), nullable=False),
        sa.Column('side', sa.String(length=8), nullable=False),
        sa.Column('volume_kwh', sa.Float(), nullable=False),
        sa.Column('price_eur_mwh', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('status', sa.String(length=16), nullable=False),
        sa.Column('submitted_at', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("country IN ('sk','cz')", name='ck_order_country'),
        sa.CheckConstraint("side IN ('buy','sell')", name='ck_order_side'),
        sa.CheckConstraint("source IN ('dt','rt','vdt')", name='ck_order_source'),
    )
    op.create_index(op.f('ix_trade_order_order_id'), 'trade_order', ['order_id'], unique=True)
    op.create_index(op.f('ix_trade_order_block_id'), 'trade_order', ['block_id'], unique=False)
    op.create_index(op.f('ix_trade_order_day'), 'trade_order', ['day'], unique=False)
    op.create_index('idx_order_day_block', 'trade_order', ['day', 'block_id'], unique=False)

    op.create_table(
        'allocation',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('battery_id', sa.Integer(), nullable=False),
        sa.Column('block_id', sa.Integer(), nullable=True),
        sa.Column('order_id', sa.String(length=64), nullable=True),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('slot_idx', sa.Integer(), nullable=False),
        sa.Column('share_kwh', sa.Float(), nullable=False),
        sa.Column('setpoint_kw', sa.Float(), nullable=False),
        sa.Column('source', sa.String(length=8), nullable=False),
        sa.Column('applied_at', sa.String(length=32), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['battery_id'], ['battery.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_allocation_battery_id'), 'allocation', ['battery_id'], unique=False)
    op.create_index(op.f('ix_allocation_block_id'), 'allocation', ['block_id'], unique=False)
    op.create_index(op.f('ix_allocation_order_id'), 'allocation', ['order_id'], unique=False)
    op.create_index(op.f('ix_allocation_applied_at'), 'allocation', ['applied_at'], unique=False)
    op.create_index('idx_alloc_battery_slot', 'allocation', ['battery_id', 'day', 'slot_idx'], unique=False)

    op.create_table(
        'availability_report',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('battery_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('slot_idx', sa.Integer(), nullable=False),
        sa.Column('soc_pct', sa.Float(), nullable=False),
        sa.Column('free_charge_kw', sa.Float(), nullable=False),
        sa.Column('free_discharge_kw', sa.Float(), nullable=False),
        sa.Column('free_kwh', sa.Float(), nullable=False),
        sa.Column('eff', sa.Float(), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['battery_id'], ['battery.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_availability_report_battery_id'), 'availability_report',
                    ['battery_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_availability_report_battery_id'), table_name='availability_report')
    op.drop_table('availability_report')

    op.drop_index('idx_alloc_battery_slot', table_name='allocation')
    op.drop_index(op.f('ix_allocation_applied_at'), table_name='allocation')
    op.drop_index(op.f('ix_allocation_order_id'), table_name='allocation')
    op.drop_index(op.f('ix_allocation_block_id'), table_name='allocation')
    op.drop_index(op.f('ix_allocation_battery_id'), table_name='allocation')
    op.drop_table('allocation')

    op.drop_index('idx_order_day_block', table_name='trade_order')
    op.drop_index(op.f('ix_trade_order_day'), table_name='trade_order')
    op.drop_index(op.f('ix_trade_order_block_id'), table_name='trade_order')
    op.drop_index(op.f('ix_trade_order_order_id'), table_name='trade_order')
    op.drop_table('trade_order')
