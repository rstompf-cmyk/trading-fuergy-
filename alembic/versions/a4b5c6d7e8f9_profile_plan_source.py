"""profile.plan_source stlpec (D-1 predikcia vs realny denny trh 15-min)

Aditivne. Bez tohto stlpca load_profile spadol pre simulacne profily na 'predicted'
-> volba "Realny denny trh 15-min" sa po ulozeni stratila (JSON ju mal, ale DB
citanie vyhralo a stlpec chybal). Existujuce riadky dostanu 'predicted' (server_default);
real profily aj tak load_profile zafixuje na 'dentrh' za behu.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
Create Date: 2026-07-04 12:00:00.000000
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, Sequence[str], None] = 'f3a4b5c6d7e8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # batch mode = portabilne aj na SQLite (recreate table so zachovanim dat).
    with op.batch_alter_table('profile', schema=None) as batch:
        batch.add_column(sa.Column('plan_source', sa.String(length=16), nullable=False,
                                   server_default='predicted'))


def downgrade() -> None:
    with op.batch_alter_table('profile', schema=None) as batch:
        batch.drop_column('plan_source')
