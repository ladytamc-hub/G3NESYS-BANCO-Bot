from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime

import discord
from discord.ext import commands

from ..permissions import has_any_configured_role, is_admin_subject
from ..services.audit import log_action
from ..utils import utc_now_iso

LOGGER = logging.getLogger("g3nesys.regears")

REGEAR_CHANNEL_SETTING_KEY = "channel_requips_id"
REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY = "channel_notify_requips_id"
REGEAR_REVIEWER_ROLE_SETTING_KEY = "regear_reviewer_role_ids"
REGEAR_CODE_PREFIX = "REQ"
IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp")

REGEAR_STATUS_META = {
    "pending": ("⏳", "Pendiente de revisión", discord.Color.gold()),
    "paid": ("✅", "Pagado", discord.Color.green()),
    "pending_payment": ("🕒", "Pendiente de pago", discord.Color.orange()),
    "rejected": ("❌", "Rechazado", discord.Color.red()),
}
REGEAR_REACTION_EMOJIS = [meta[0] for meta in REGEAR_STATUS_META.values()]


async def private_response(
    interaction: discord.Interaction,
    content: str,
    **kwargs,
) -> None:
    kwargs.setdefault("ephemeral", True)
    if interaction.response.is_done():
        await interaction.followup.send(content, **kwargs)
        return
    await interaction.response.send_message(content, **kwargs)


def row_value(row: sqlite3.Row, key: str, default=None):
    return row[key] if key in row.keys() else default


def status_display(status: str) -> str:
    emoji, label, _color = REGEAR_STATUS_META.get(status, REGEAR_STATUS_META["pending"])
    return f"{emoji} {label}"


def status_label(status: str) -> str:
    _emoji, label, _color = REGEAR_STATUS_META.get(status, REGEAR_STATUS_META["pending"])
    return label


def status_color(status: str) -> discord.Color:
    _emoji, _label, color = REGEAR_STATUS_META.get(status, REGEAR_STATUS_META["pending"])
    return color


def discord_time(value: str | None) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    return f"<t:{int(parsed.timestamp())}:f>"


def is_image_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()
    if content_type.startswith("image/"):
        return True
    return attachment.filename.lower().endswith(IMAGE_EXTENSIONS)


def build_regear_embed(request: sqlite3.Row) -> discord.Embed:
    status = str(request["status"] or "pending")
    embed = discord.Embed(
        title=f"🛡️ Solicitud de Requip {request['request_code']}",
        color=status_color(status),
    )
    embed.add_field(name="Jugador", value=f"<@{request['user_id']}>", inline=False)
    embed.add_field(name="Estado", value=status_display(status), inline=False)
    embed.add_field(name="Imagen", value=f"[Ver captura]({request['image_url']})", inline=True)
    message_url = request["message_url"]
    embed.add_field(
        name="Mensaje original",
        value=f"[Ver mensaje]({message_url})" if message_url else "No disponible",
        inline=True,
    )
    if request["reviewed_by"]:
        embed.add_field(name="Revisado por", value=f"<@{request['reviewed_by']}>", inline=True)
    reviewed_at = discord_time(request["reviewed_at"])
    if reviewed_at:
        embed.add_field(name="Fecha de revisión", value=reviewed_at, inline=True)
    created_at = discord_time(request["created_at"])
    if created_at:
        embed.add_field(name="Creada", value=created_at, inline=True)
    embed.set_image(url=request["image_url"])
    return embed


class RegearReviewView(discord.ui.View):
    def __init__(self, cog: "Regears", request_code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_code = request_code
        self._add_button("paid", "Pagado", "✅", discord.ButtonStyle.success)
        self._add_button("pending_payment", "Pendiente de pago", "🕒", discord.ButtonStyle.secondary)
        self._add_button("rejected", "Rechazar", "❌", discord.ButtonStyle.danger)

    def _add_button(
        self,
        status: str,
        label: str,
        emoji: str,
        style: discord.ButtonStyle,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:regear:{self.request_code}:{status}",
        )
        button.callback = self._make_callback(status)
        self.add_item(button)

    def _make_callback(self, status: str):
        async def callback(interaction: discord.Interaction) -> None:
            await self.cog.review_request(interaction, self.request_code, status, self)

        return callback


class Regears(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        rows = self.db.fetch_all(
            """
            SELECT request_code
            FROM regear_requests
            WHERE bot_message_id IS NOT NULL
            """
        )
        seen: set[str] = set()
        for row in rows:
            request_code = str(row["request_code"])
            if request_code in seen:
                continue
            seen.add(request_code)
            self.bot.add_view(RegearReviewView(self, request_code))

    def int_setting(self, guild_id: int, key: str) -> int | None:
        raw = self.db.get_setting(guild_id, key)
        try:
            return int(raw) if raw else None
        except ValueError:
            return None

    def configured_channel_id(self, guild_id: int) -> int | None:
        return self.int_setting(guild_id, REGEAR_CHANNEL_SETTING_KEY)

    def configured_notification_channel_id(self, guild_id: int) -> int | None:
        return self.int_setting(guild_id, REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY)

    async def messageable_channel(
        self,
        guild: discord.Guild,
        channel_id: int | None,
    ) -> discord.abc.Messageable | None:
        if channel_id is None:
            return None
        channel = guild.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None
        if not isinstance(channel, discord.abc.Messageable):
            return None
        return channel

    async def notification_channel(self, guild: discord.Guild) -> discord.abc.Messageable | None:
        channel_id = self.configured_notification_channel_id(guild.id)
        return await self.messageable_channel(guild, channel_id)

    def can_review(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            return False
        if is_admin_subject(self.db, interaction):
            return True
        role_ids = self.db.get_setting(interaction.guild.id, REGEAR_REVIEWER_ROLE_SETTING_KEY)
        return has_any_configured_role(interaction.user, role_ids)

    def get_request(self, guild_id: int, request_code: str) -> sqlite3.Row | None:
        return self.db.fetch_one(
            """
            SELECT *
            FROM regear_requests
            WHERE guild_id = ? AND request_code = ?
            """,
            (guild_id, request_code),
        )

    def message_already_registered(self, guild_id: int, message_id: int) -> bool:
        row = self.db.fetch_one(
            """
            SELECT 1
            FROM regear_requests
            WHERE guild_id = ? AND message_id = ?
            """,
            (guild_id, message_id),
        )
        return row is not None

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.author.bot:
            return
        configured_channel_id = self.configured_channel_id(message.guild.id)
        if configured_channel_id is None or message.channel.id != configured_channel_id:
            return
        if self._is_command_message(message):
            return

        image = next((attachment for attachment in message.attachments if is_image_attachment(attachment)), None)
        if image is None:
            await self.warn_missing_image(message)
            return
        if self.message_already_registered(message.guild.id, message.id):
            return

        review_channel = await self.notification_channel(message.guild)
        if review_channel is None:
            LOGGER.warning(
                "Canal de notificaciones de Requips no configurado o invalido en guild %s",
                message.guild.id,
            )
            return
        await self.create_request(message, image, review_channel)

    def _is_command_message(self, message: discord.Message) -> bool:
        prefix = self.bot.command_prefix
        return isinstance(prefix, str) and bool(message.content) and message.content.startswith(prefix)

    async def warn_missing_image(self, message: discord.Message) -> None:
        try:
            warning = await message.reply(
                "Para solicitar requip, sube una captura en este canal.",
                mention_author=False,
            )
        except discord.HTTPException:
            return
        await asyncio.sleep(8)
        try:
            await warning.delete()
        except discord.HTTPException:
            pass
        me = message.guild.me if message.guild else None
        if me is None:
            return
        try:
            permissions = message.channel.permissions_for(me)
        except AttributeError:
            return
        if not permissions.manage_messages:
            return
        try:
            await message.delete()
        except discord.HTTPException:
            pass

    async def create_request(
        self,
        message: discord.Message,
        attachment: discord.Attachment,
        review_channel: discord.abc.Messageable,
    ) -> None:
        guild = message.guild
        if guild is None:
            return
        request_code = self.db.next_code(guild.id, REGEAR_CODE_PREFIX)
        now = utc_now_iso()
        try:
            request_id = self.db.execute(
                """
                INSERT INTO regear_requests (
                    guild_id, request_code, user_id, channel_id, message_id,
                    image_url, message_url, status, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                """,
                (
                    guild.id,
                    request_code,
                    message.author.id,
                    message.channel.id,
                    message.id,
                    attachment.url,
                    message.jump_url,
                    now,
                    now,
                ),
            )
        except sqlite3.IntegrityError:
            return

        try:
            await message.add_reaction(REGEAR_STATUS_META["pending"][0])
        except discord.HTTPException as exc:
            LOGGER.warning("No pude agregar reaccion pending a requip %s: %s", request_code, exc)

        request = self.get_request(guild.id, request_code)
        if request is None:
            return
        view = RegearReviewView(self, request_code)
        try:
            if int(getattr(review_channel, "id", 0)) == message.channel.id:
                bot_message = await message.reply(
                    embed=build_regear_embed(request),
                    view=view,
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                bot_message = await review_channel.send(
                    embed=build_regear_embed(request),
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
        except discord.HTTPException:
            LOGGER.exception("No pude publicar la solicitud de requip %s", request_code)
            return

        self.db.execute(
            """
            UPDATE regear_requests
            SET bot_message_id = ?, bot_channel_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (bot_message.id, int(getattr(bot_message.channel, "id", 0)), utc_now_iso(), request_id),
        )
        self.bot.add_view(view)
        log_action(
            self.db,
            guild.id,
            admin_id=None,
            action="Crear solicitud de requip",
            affected_user_id=message.author.id,
            system="Requips",
            observation=request_code,
        )

    async def review_request(
        self,
        interaction: discord.Interaction,
        request_code: str,
        status: str,
        view: RegearReviewView,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta solicitud solo funciona dentro del servidor.")
            return
        if not self.can_review(interaction):
            await private_response(interaction, "No tienes permiso para revisar requips.")
            return
        request = self.get_request(interaction.guild.id, request_code)
        if request is None:
            await private_response(interaction, "No encontré esa solicitud de requip.")
            return
        if status not in REGEAR_STATUS_META:
            await private_response(interaction, "Estado de requip no válido.")
            return

        await interaction.response.defer(ephemeral=True)
        reviewed_at = utc_now_iso()
        self.db.execute(
            """
            UPDATE regear_requests
            SET status = ?, reviewed_by = ?, reviewed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, interaction.user.id, reviewed_at, reviewed_at, request["id"]),
        )
        updated = self.get_request(interaction.guild.id, request_code)
        if updated is None:
            await private_response(interaction, "No pude volver a leer la solicitud actualizada.")
            return

        await self.sync_original_reaction(interaction.guild, updated)
        await self.edit_review_message(interaction, updated, view)
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Actualizar solicitud de requip",
            affected_user_id=int(updated["user_id"]),
            system="Requips",
            observation=f"{request_code}: {status_label(status)}",
        )
        await private_response(
            interaction,
            f"Solicitud {request_code} actualizada a: {status_label(status)}.",
        )

    async def sync_original_reaction(self, guild: discord.Guild, request: sqlite3.Row) -> None:
        original_message = await self.fetch_request_message(guild, request)
        if original_message is None:
            return
        for emoji in REGEAR_REACTION_EMOJIS:
            try:
                await original_message.clear_reaction(emoji)
            except discord.HTTPException:
                await self.remove_own_reaction(original_message, emoji)
        try:
            await original_message.add_reaction(REGEAR_STATUS_META[str(request["status"])][0])
        except discord.HTTPException as exc:
            LOGGER.warning("No pude actualizar reaccion de requip %s: %s", request["request_code"], exc)

    async def remove_own_reaction(self, message: discord.Message, emoji: str) -> None:
        if self.bot.user is None:
            return
        try:
            await message.remove_reaction(emoji, self.bot.user)
        except discord.HTTPException:
            pass

    async def fetch_message_from_channel(
        self,
        guild: discord.Guild,
        channel_id: int,
        message_id: int,
    ) -> discord.Message | None:
        channel = await self.messageable_channel(guild, channel_id)
        fetch_message = getattr(channel, "fetch_message", None)
        if fetch_message is None:
            return None
        try:
            return await fetch_message(message_id)
        except discord.HTTPException:
            return None

    async def fetch_request_message(
        self,
        guild: discord.Guild,
        request: sqlite3.Row,
    ) -> discord.Message | None:
        return await self.fetch_message_from_channel(
            guild,
            int(request["channel_id"]),
            int(request["message_id"]),
        )

    async def edit_review_message(
        self,
        interaction: discord.Interaction,
        request: sqlite3.Row,
        view: RegearReviewView,
    ) -> None:
        embed = build_regear_embed(request)
        try:
            if interaction.message is not None:
                await interaction.message.edit(embed=embed, view=view)
                return
        except discord.HTTPException as exc:
            LOGGER.warning("No pude editar el mensaje de requip %s: %s", request["request_code"], exc)

        bot_message_id = request["bot_message_id"]
        if not bot_message_id or interaction.guild is None:
            return
        bot_channel_id = row_value(request, "bot_channel_id") or request["channel_id"]
        bot_message = await self.fetch_message_from_channel(
            interaction.guild,
            int(bot_channel_id),
            int(bot_message_id),
        )
        if bot_message is None:
            return
        try:
            await bot_message.edit(embed=embed, view=view)
        except discord.HTTPException as exc:
            LOGGER.warning("No pude editar el mensaje persistente de requip %s: %s", request["request_code"], exc)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Regears(bot))