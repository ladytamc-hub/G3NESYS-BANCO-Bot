from __future__ import annotations

import discord

from ..database import Database
from ..utils import utc_now_iso


ADMIN_CHANNEL_SETTINGS = {
    "splits": (
        "channel_notify_splits_id",
        "channel_notify_general_admin_id",
        "channel_repartos_id",
        "channel_admin_id",
    ),
    "withdrawals": (
        "channel_notify_withdrawals_id",
        "channel_notify_general_admin_id",
        "channel_cobros_id",
        "channel_admin_id",
    ),
    "registration": (
        "channel_notify_registration_id",
        "channel_notify_general_admin_id",
        "channel_admin_id",
    ),
    "activities": (
        "channel_notify_activities_id",
        "channel_notify_general_admin_id",
        "channel_admin_id",
    ),
    "fines": (
        "channel_notify_fines_id",
        "channel_notify_general_admin_id",
        "channel_multas_id",
        "channel_admin_id",
    ),
    "general_admin": (
        "channel_notify_general_admin_id",
        "channel_admin_id",
    ),
}


def get_admin_notification_channel(
    db: Database,
    guild: discord.Guild,
    category: str,
):
    keys = ADMIN_CHANNEL_SETTINGS.get(category)
    if keys is None:
        raise ValueError(f"Categoria administrativa desconocida: {category}")
    for key in keys:
        raw_channel_id = db.get_setting(guild.id, key)
        if not raw_channel_id:
            continue
        try:
            channel = guild.get_channel(int(raw_channel_id))
        except ValueError:
            continue
        if channel is not None and callable(getattr(channel, "send", None)):
            return channel
    return None


async def send_admin_notification(
    db: Database,
    *,
    guild: discord.Guild,
    category: str,
    content: str | None = None,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
):
    channel = get_admin_notification_channel(db, guild, category)
    if channel is None:
        return None
    try:
        return await channel.send(content=content, embed=embed, view=view)
    except (discord.Forbidden, discord.HTTPException):
        return None


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
