# migrations/versions/003_promotion_tracking.py
"""Add promotion_attempts and last_promotion_attempt_at to proxy_pool.

Per plan §4.1: caps retries on dead proxies at 5 attempts with a 15-minute
cooldown window. Without attempt tracking, dead datacenter IPs that accept
TCP connects get re-validated forever at zero benefit and real cost.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "003"
down_revision: str | None = "002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proxy_pool",
        sa.Column(
            "promotion_attempts",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "proxy_pool",
        sa.Column("last_promotion_attempt_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_column("proxy_pool", "last_promotion_attempt_at")
    op.drop_column("proxy_pool", "promotion_attempts")
