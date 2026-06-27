from __future__ import annotations

from ..database import Database
from ..utils import utc_now_iso


def log_payout_action(
    db: Database,
    guild_id: int,
    payout_id: int,
    *,
    actor_id: int | None,
    action: str,
    details: str | None = None,
) -> int:
    payout = db.fetch_one(
        "SELECT 1 FROM payouts WHERE id = ? AND guild_id = ?",
        (payout_id, guild_id),
    )
    if payout is None:
        raise ValueError("No encontre ese Split en este servidor.")
    return db.execute(
        """
        INSERT INTO payout_audit_logs (
            guild_id, payout_id, actor_id, action, details, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (guild_id, payout_id, actor_id, action, details, utc_now_iso()),
    )


def payout_audit_text(db: Database, guild_id: int, payout_id: int) -> str:
    payout = db.fetch_one(
        "SELECT code FROM payouts WHERE id = ? AND guild_id = ?",
        (payout_id, guild_id),
    )
    if payout is None:
        return "No encontre ese Split."
    rows = db.fetch_all(
        """
        SELECT actor_id, action, details, created_at
        FROM payout_audit_logs
        WHERE guild_id = ? AND payout_id = ?
        ORDER BY id DESC LIMIT 20
        """,
        (guild_id, payout_id),
    )
    lines = [f"🧾 **Auditoria del Split {payout['code']}**"]
    if not rows:
        lines.append("No hay auditoría registrada para este split.")
        return "\n".join(lines)
    for row in rows:
        actor = f"<@{row['actor_id']}>" if row["actor_id"] else "Sistema"
        detail = f" — {row['details']}" if row["details"] else ""
        lines.append(f"• {row['created_at']} — {actor} — {row['action']}{detail}")
    return "\n".join(lines)[:1900]
