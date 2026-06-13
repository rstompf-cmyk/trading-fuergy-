"""CSV→DB krok 2: livesim_meta + livesim_trace_day tabuľky

Nahrádza per-profil livesim CSV + meta.json. Flag-gated (LIVESIM_STORE=db) —
kým off, tabuľky existujú ale nepoužívajú sa (žiadna zmena správania).

Revision ID: c9f1a2b3d4e5
Revises: b7c2d9e4f1a8
Create Date: 2026-06-13 18:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9f1a2b3d4e5'
down_revision: Union[str, Sequence[str], None] = 'b7c2d9e4f1a8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'livesim_meta',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('market', sa.String(length=2), nullable=False),
        sa.Column('case', sa.String(length=32), nullable=False),
        sa.Column('start_date', sa.String(length=10), nullable=True),
        sa.Column('done_through', sa.String(length=10), nullable=True),
        sa.Column('last_min', sa.String(length=20), nullable=True),
        sa.Column('soc_after_done', sa.Float(), nullable=True),
        sa.Column('cum_dt_done', sa.Float(), nullable=True, server_default='0'),
        sa.Column('cum_rt_done', sa.Float(), nullable=True, server_default='0'),
        sa.Column('settings_sig', sa.Text(), nullable=True),
        sa.Column('params', sa.JSON(), nullable=True),
        sa.Column('skipped', sa.JSON(), nullable=True),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'market', 'case', name='uq_livesim_meta_pmc'),
        sa.CheckConstraint("market IN ('cz','sk')", name='ck_livesim_meta_market'),
    )
    op.create_index(op.f('ix_livesim_meta_profile_id'), 'livesim_meta',
                     ['profile_id'], unique=False)

    op.create_table(
        'livesim_trace_day',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('market', sa.String(length=2), nullable=False),
        sa.Column('case', sa.String(length=32), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('payload', sa.Text(), nullable=False),
        sa.Column('n_rows', sa.Integer(), nullable=True, server_default='0'),
        sa.Column('soc_end', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'market', 'case', 'day', name='uq_livesim_trace_pmcd'),
        sa.CheckConstraint("market IN ('cz','sk')", name='ck_livesim_trace_market'),
    )
    op.create_index('idx_livesim_trace_range', 'livesim_trace_day',
                     ['profile_id', 'market', 'case', 'day'], unique=False)
    op.create_index(op.f('ix_livesim_trace_day_profile_id'), 'livesim_trace_day',
                     ['profile_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_livesim_trace_day_profile_id'), table_name='livesim_trace_day')
    op.drop_index('idx_livesim_trace_range', table_name='livesim_trace_day')
    op.drop_table('livesim_trace_day')
    op.drop_index(op.f('ix_livesim_meta_profile_id'), table_name='livesim_meta')
    op.drop_table('livesim_meta')
