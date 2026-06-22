"""CDC backend + Zakaznik: customer table + battery.customer_id/backend/cdc_prefix

Aditivne. Kym sa CDC/Zakaznik nepouziva, nemeni spravanie existujucej appky:
  - nova tabulka 'customer' (zoskupenie baterii),
  - battery: + customer_id (FK customer), + backend (default 'realio'), + cdc_prefix.
Existujuce baterie dostanu backend='realio' (server_default) -> spravaju sa ako dnes.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
Create Date: 2026-06-22 10:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f3a4b5c6d7e8'
down_revision: Union[str, Sequence[str], None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # -- customer (zakaznik) --------------------------------------------------
    op.create_table(
        'customer',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=96), nullable=False),
        sa.Column('country', sa.String(length=4), nullable=False),
        sa.Column('note', sa.Text(), nullable=True),
        sa.Column('created_at', sa.String(length=32), nullable=False),
        sa.Column('updated_at', sa.String(length=32), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.CheckConstraint("country IN ('sk','cz')", name='ck_customer_country'),
    )
    op.create_index(op.f('ix_customer_name'), 'customer', ['name'], unique=True)

    # -- battery: + customer_id / backend / cdc_prefix ------------------------
    # batch mode = portabilne aj na SQLite (recreate table so zachovanim dat).
    with op.batch_alter_table('battery', schema=None) as batch:
        batch.add_column(sa.Column('customer_id', sa.Integer(), nullable=True))
        batch.add_column(sa.Column('backend', sa.String(length=16), nullable=False,
                                   server_default='realio'))
        batch.add_column(sa.Column('cdc_prefix', sa.String(length=64), nullable=True))
        batch.create_foreign_key('fk_battery_customer', 'customer',
                                 ['customer_id'], ['id'])
        batch.create_index(op.f('ix_battery_customer_id'), ['customer_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('battery', schema=None) as batch:
        batch.drop_index(op.f('ix_battery_customer_id'))
        batch.drop_constraint('fk_battery_customer', type_='foreignkey')
        batch.drop_column('cdc_prefix')
        batch.drop_column('backend')
        batch.drop_column('customer_id')

    op.drop_index(op.f('ix_customer_name'), table_name='customer')
    op.drop_table('customer')
