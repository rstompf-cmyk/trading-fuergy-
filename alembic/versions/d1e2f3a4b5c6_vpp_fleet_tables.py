"""VPP fleet tabuľky: battery / block / account / assignment / instance_status / instance_command

Multi-batéria / reálne riadenie (VPP). Aditívne — kým fleet mód off, tabuľky
existujú ale nepoužívajú sa (žiadna zmena správania existujúcej appky). Zhodné s
db/models.py (Battery/Block/Account/Assignment/InstanceStatus/InstanceCommand).

Revision ID: d1e2f3a4b5c6
Revises: c9f1a2b3d4e5
Create Date: 2026-06-18 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, Sequence[str], None] = 'c9f1a2b3d4e5'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── battery ──────────────────────────────────────────────────────────
    op.create_table(
        'battery',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('country', sa.String(length=4), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=True),
        sa.Column('mode', sa.String(length=16), nullable=False),
        sa.Column('batt_kw', sa.Float(), nullable=False),
        sa.Column('batt_kwh', sa.Float(), nullable=False),
        sa.Column('eff', sa.Float(), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('realio_host', sa.String(length=255), nullable=True),
        sa.Column('realio_username', sa.String(length=64), nullable=True),
        sa.Column('realio_password', sa.Text(), nullable=True),
        sa.Column('realio_tags_read', sa.JSON(), nullable=False),
        sa.Column('realio_tags_write', sa.JSON(), nullable=False),
        sa.Column('realio_fve_control', sa.JSON(), nullable=False),
        sa.Column('realio_poll_sec', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id']),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("mode IN ('simulation','real')", name='ck_battery_mode'),
        sa.CheckConstraint("country IN ('sk','cz')", name='ck_battery_country'),
    )
    op.create_index(op.f('ix_battery_name'), 'battery', ['name'], unique=True)
    op.create_index(op.f('ix_battery_profile_id'), 'battery', ['profile_id'], unique=False)

    # ── block ────────────────────────────────────────────────────────────
    op.create_table(
        'block',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=64), nullable=False),
        sa.Column('country', sa.String(length=4), nullable=False),
        sa.Column('split_strategy', sa.String(length=24), nullable=False),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("country IN ('sk','cz')", name='ck_block_country'),
    )
    op.create_index(op.f('ix_block_name'), 'block', ['name'], unique=True)

    # ── account ──────────────────────────────────────────────────────────
    op.create_table(
        'account',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=64), nullable=False),
        sa.Column('country', sa.String(length=4), nullable=False),
        sa.Column('product', sa.String(length=8), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('password', sa.Text(), nullable=True),
        sa.Column('enabled', sa.Boolean(), nullable=False),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("country IN ('sk','cz')", name='ck_account_country'),
    )
    op.create_index(op.f('ix_account_label'), 'account', ['label'], unique=True)

    # ── assignment (versioned battery → block → account) ─────────────────
    op.create_table(
        'assignment',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('battery_id', sa.Integer(), nullable=False),
        sa.Column('block_id', sa.Integer(), nullable=True),
        sa.Column('account_id', sa.Integer(), nullable=True),
        sa.Column('valid_from', sa.String(length=32), nullable=False),
        sa.Column('valid_to', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['battery_id'], ['battery.id'], ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['block_id'], ['block.id']),
        sa.ForeignKeyConstraint(['account_id'], ['account.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_assignment_battery_id'), 'assignment', ['battery_id'], unique=False)
    op.create_index('idx_assignment_active', 'assignment', ['battery_id', 'valid_to'], unique=False)

    # ── instance_status (IPC: inštancia → jadro, UPSERT) ─────────────────
    op.create_table(
        'instance_status',
        sa.Column('battery_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.String(length=32), nullable=False),
        sa.Column('pid', sa.Integer(), nullable=True),
        sa.Column('alive', sa.Boolean(), nullable=False),
        sa.Column('health', sa.String(length=16), nullable=False),
        sa.Column('soc_pct', sa.Float(), nullable=True),
        sa.Column('last_setpoint_kw', sa.Float(), nullable=True),
        sa.Column('mode', sa.String(length=16), nullable=True),
        sa.Column('error', sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(['battery_id'], ['battery.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('battery_id'),
    )

    # ── instance_command (IPC: jadro → inštancia, FIFO) ──────────────────
    op.create_table(
        'instance_command',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('battery_id', sa.Integer(), nullable=False),
        sa.Column('ts', sa.String(length=32), nullable=False),
        sa.Column('type', sa.String(length=16), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('consumed_at', sa.String(length=32), nullable=True),
        sa.ForeignKeyConstraint(['battery_id'], ['battery.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_instance_command_battery_id'), 'instance_command',
                    ['battery_id'], unique=False)
    op.create_index(op.f('ix_instance_command_consumed_at'), 'instance_command',
                    ['consumed_at'], unique=False)
    op.create_index('idx_command_pending', 'instance_command',
                    ['battery_id', 'consumed_at'], unique=False)


def downgrade() -> None:
    op.drop_index('idx_command_pending', table_name='instance_command')
    op.drop_index(op.f('ix_instance_command_consumed_at'), table_name='instance_command')
    op.drop_index(op.f('ix_instance_command_battery_id'), table_name='instance_command')
    op.drop_table('instance_command')

    op.drop_table('instance_status')

    op.drop_index('idx_assignment_active', table_name='assignment')
    op.drop_index(op.f('ix_assignment_battery_id'), table_name='assignment')
    op.drop_table('assignment')

    op.drop_index(op.f('ix_account_label'), table_name='account')
    op.drop_table('account')

    op.drop_index(op.f('ix_block_name'), table_name='block')
    op.drop_table('block')

    op.drop_index(op.f('ix_battery_profile_id'), table_name='battery')
    op.drop_index(op.f('ix_battery_name'), table_name='battery')
    op.drop_table('battery')
