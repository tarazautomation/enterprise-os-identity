"""new members start with no apps

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-10 09:00:00

Every app was given to every new member. Now an admin turns on the apps each person needs. Members who already
have apps keep them.
"""
from alembic import op


revision = '0002'
down_revision = '0001'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE apps SET default_enabled = false")


def downgrade() -> None:
    op.execute("UPDATE apps SET default_enabled = true")
