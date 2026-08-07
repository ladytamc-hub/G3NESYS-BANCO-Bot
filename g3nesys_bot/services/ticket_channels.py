from __future__ import annotations

import discord


TICKET_CHANNEL_LABEL = "Canal de notificaciones de tickets"
TICKET_CHANNEL_SETTING_KEY = "ticket_channel_id"
TICKET_CONVERSATION_CHANNEL_LABEL = "Canal de conversaciones de tickets"
TICKET_CONVERSATION_CHANNEL_SETTING_KEY = "ticket_conversation_channel_id"

TICKET_NOTIFICATION_REQUIRED_PERMISSIONS = (
    ("view_channel", "Ver el canal"),
    ("send_messages", "Enviar mensajes"),
    ("embed_links", "Insertar enlaces"),
)
TICKET_CONVERSATION_REQUIRED_PERMISSIONS = (
    ("view_channel", "Ver el canal"),
    ("send_messages", "Enviar mensajes"),
    ("create_private_threads", "Crear hilos privados"),
    ("send_messages_in_threads", "Enviar mensajes dentro de hilos"),
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


def is_normal_text_ticket_channel(channel) -> bool:
    if isinstance(channel, discord.TextChannel):
        return True
    return getattr(channel, "type", None) == discord.ChannelType.text


def ticket_channel_permission_errors(
    channel,
    guild: discord.Guild | None,
    *,
    conversation: bool = False,
) -> list[str]:
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
    required_permissions = (
        TICKET_CONVERSATION_REQUIRED_PERMISSIONS
        if conversation
        else TICKET_NOTIFICATION_REQUIRED_PERMISSIONS
    )
    for attr, label in required_permissions:
        if not bool(getattr(permissions, attr, False)):
            missing.append(label)
    return missing
