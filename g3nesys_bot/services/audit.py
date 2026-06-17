from __future__ import annotations

from ..database import Database
from ..utils import utc_now_iso


def log_action(
    db: Database,
    guild_id: int,
    *,
    admin_id: int | None,
    action: str,
    system: str,
    affected_user_id: int | None = None,
    amount: int | None = None,
    observation: str | None = None,
) -> None:
    db.execute(
        """
        INSERT INTO audit_logs (
            guild_id, admin_id, action, affected_user_id, amount,
            system, observation, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            guild_id,
            admin_id,
            action,
            affected_user_id,
            amount,
            system,
            observation,
            utc_now_iso(),
        ),
    )
