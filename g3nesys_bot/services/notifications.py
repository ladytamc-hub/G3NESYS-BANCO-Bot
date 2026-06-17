from __future__ import annotations

import discord

from ..database import Database
from ..utils import utc_now_iso


async def send_dm_safe(
    db: Database,
    *,
    guild_id: int | None,
    user: discord.User | discord.Member,
    action: str,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> bool:
    try:
        await user.send(content=content, embed=embed, view=view)
    except discord.HTTPException as exc:
        db.execute(
            """
            INSERT INTO dm_logs (guild_id, user_id, action, success, error, created_at)
            VALUES (?, ?, ?, 0, ?, ?)
            """,
            (guild_id, user.id, action, str(exc), utc_now_iso()),
        )
        return False

    db.execute(
        """
        INSERT INTO dm_logs (guild_id, user_id, action, success, error, created_at)
        VALUES (?, ?, ?, 1, NULL, ?)
        """,
        (guild_id, user.id, action, utc_now_iso()),
    )
    return True
