"""DB unify F1: effect_minute + effect_daily tables

Bug #650 / DB unify F1: jediný zdroj pravdy pre €-hodnoty livesim.
Karty, chC graf, Excel, PDF čítajú odtiaľto cez core/effect_db.py.

Revision ID: b7c2d9e4f1a8
Revises: a1b2c3d4e5f6
Create Date: 2026-06-10 14:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7c2d9e4f1a8'
down_revision: Union[str, Sequence[str], None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema — pridáva effect_minute a effect_daily tabuľky."""
    # effect_minute: per profile + minute timestamp
    op.create_table(
        'effect_minute',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('time_iso', sa.String(length=20), nullable=False),
        sa.Column('time_ms', sa.Integer(), nullable=False),
        sa.Column('market', sa.String(length=2), nullable=False),
        sa.Column('dt_rev_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_batt_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_ftv_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_load_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_curtail_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('vdt_arb_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('baseline_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('batt_kw_real', sa.Float(), nullable=True),
        sa.Column('plan_batt_kw', sa.Float(), nullable=True),
        sa.Column('ftv_kw_real', sa.Float(), nullable=True),
        sa.Column('load_kw_real', sa.Float(), nullable=True),
        sa.Column('soc_pct', sa.Float(), nullable=True),
        sa.Column('zco_eur', sa.Float(), nullable=True),
        sa.Column('dt_eur_mwh', sa.Float(), nullable=True),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'time_ms', name='uq_effect_minute_ptime'),
        sa.CheckConstraint("market IN ('cz','sk')", name='ck_effect_minute_market'),
    )
    op.create_index('idx_effect_minute_range', 'effect_minute',
                     ['profile_id', 'time_ms'], unique=False)
    op.create_index(op.f('ix_effect_minute_profile_id'), 'effect_minute',
                     ['profile_id'], unique=False)
    op.create_index(op.f('ix_effect_minute_time_ms'), 'effect_minute',
                     ['time_ms'], unique=False)

    # effect_daily: denný agregát pre rýchle obdobie queries
    op.create_table(
        'effect_daily',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False),
        sa.Column('day', sa.String(length=10), nullable=False),
        sa.Column('market', sa.String(length=2), nullable=False),
        sa.Column('dt_rev_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_batt_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_ftv_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_load_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('rt_curtail_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('vdt_arb_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('baseline_eur', sa.Float(), nullable=True, server_default='0'),
        sa.Column('ftv_kwh', sa.Float(), nullable=True),
        sa.Column('load_kwh', sa.Float(), nullable=True),
        sa.Column('soc_end_pct', sa.Float(), nullable=True),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.ForeignKeyConstraint(['profile_id'], ['profile.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('profile_id', 'day', name='uq_effect_daily_pday'),
        sa.CheckConstraint("market IN ('cz','sk')", name='ck_effect_daily_market'),
    )
    op.create_index('idx_effect_daily_range', 'effect_daily',
                     ['profile_id', 'day'], unique=False)
    op.create_index(op.f('ix_effect_daily_profile_id'), 'effect_daily',
                     ['profile_id'], unique=False)
    op.create_index(op.f('ix_effect_daily_day'), 'effect_daily',
                     ['day'], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_effect_daily_day'), table_name='effect_daily')
    op.drop_index(op.f('ix_effect_daily_profile_id'), table_name='effect_daily')
    op.drop_index('idx_effect_daily_range', table_name='effect_daily')
    op.drop_table('effect_daily')
    op.drop_index(op.f('ix_effect_minute_time_ms'), table_name='effect_minute')
    op.drop_index(op.f('ix_effect_minute_profile_id'), table_name='effect_minute')
    op.drop_index('idx_effect_minute_range', table_name='effect_minute')
    op.drop_table('effect_minute')
