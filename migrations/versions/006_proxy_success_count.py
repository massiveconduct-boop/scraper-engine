# migrations/versions/006_proxy_success_count.py
"""Add global_success_count to proxy_pool, paired with global_failure_count.

Round 32: ScoringEngine.compute_score() was wired into production for the
first time — its success_rate dimension needs a real success/failure count
to be meaningful (previously the formula was dead code, called from
nowhere). global_failure_count already existed but was itself unused until
this round; this migration adds the missing counterpart so
successes / (successes + failures) can be computed for real.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "006"
down_revision: str | None = "005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "proxy_pool",
        sa.Column(
            "global_success_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
    )


def downgrade() -> None:
    op.drop_column("proxy_pool", "global_success_count")
