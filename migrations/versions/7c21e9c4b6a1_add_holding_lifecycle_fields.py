"""Add rolling holding lifecycle fields.

Revision ID: 7c21e9c4b6a1
Revises: 3a1845d3b0eb
"""
from alembic import op
import sqlalchemy as sa


revision = "7c21e9c4b6a1"
down_revision = "3a1845d3b0eb"
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table("portfolio_tickers") as batch_op:
        batch_op.add_column(sa.Column("sale_price", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("quantity", sa.Float(), nullable=True))
        batch_op.add_column(sa.Column("company_name", sa.String(length=120), nullable=True))
        batch_op.add_column(sa.Column("notes", sa.String(length=500), nullable=True))


def downgrade():
    with op.batch_alter_table("portfolio_tickers") as batch_op:
        batch_op.drop_column("notes")
        batch_op.drop_column("company_name")
        batch_op.drop_column("quantity")
        batch_op.drop_column("sale_price")
