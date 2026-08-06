from __future__ import annotations

import discord


TICKET_CHANNEL_LABEL = "Canal de tickets"
TICKET_CHANNEL_SETTING_KEY = "ticket_channel_id"

TICKET_CHANNEL_REQUIRED_PERMISSIONS = (
    ("view_channel", "Ver el canal"),
    ("send_messages", "Enviar mensajes"),
    ("create_public_threads", "Crear hilos"),
    ("send_messages_in_threads", "Enviar mensajes dentro de hilos"),
    ("embed_links", "Insertar enlaces"),
    ("attach_files", "Adjuntar archivos"),
)


def is_text_ticket_channel(channel) -> bool:
    if isinstance(channel, discord.TextChannel):
        return True
    channel_type = getattr(channel, "type", None)
    if channel_type in {discord.ChannelType.text, discord.ChannelType.news}:
        return True
    return bool(
        callable(getattr(channel, "send", None))
        and callable(getattr(channel, "create_thread", None))
        and hasattr(channel, "id")
    )


def ticket_channel_permission_errors(channel, guild: discord.Guild | None) -> list[str]:
    if guild is None:
        return ["Validar servidor"]
    me = getattr(guild, "me", None)
    if me is None and callable(getattr(guild, "get_member", None)):
        bot_user = getattr(getattr(guild, "_state", None), "user", None)
        bot_user_id = getattr(bot_user, "id", None)
        if bot_user_id is not None:
            me = guild.get_member(int(bot_user_id))
    if me is None:
        return []
    permissions_for = getattr(channel, "permissions_for", None)
    if not callable(permissions_for):
        return []
    permissions = permissions_for(me)
    missing = []
    for attr, label in TICKET_CHANNEL_REQUIRED_PERMISSIONS:
        if not bool(getattr(permissions, attr, False)):
            missing.append(label)
    return missing
