from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..permissions import is_admin_subject, require_admin_context
from ..services.audit import log_action
from ..services.support_guides import GUIDES, build_guide_embed, build_guides_home_embed
from ..services.tickets import (
    TICKET_CLOSED,
    TICKET_IN_PROGRESS,
    TICKET_PENDING,
    TICKET_RESOLVED,
    TICKET_WAITING_USER,
    count_tickets_by_user,
    search_tickets_by_user,
)
from ..utils import utc_now_iso


LOGGER = logging.getLogger(__name__)

SUPPORT_PANEL_TYPE = "support"
SUPPORT_PANEL_CHANNEL_SETTING_KEY = "support_panel_channel_id"
SUPPORT_PANEL_BANNER_SETTING_KEY = "support_panel_banner_url"
SUPPORT_TICKETS_PAGE_SIZE = 5

SUPPORT_TICKET_STATUS_LABELS = {
    TICKET_PENDING: "Abierto",
    TICKET_IN_PROGRESS: "En revisión",
    TICKET_WAITING_USER: "Respondido",
    TICKET_RESOLVED: "Cerrado",
    TICKET_CLOSED: "Cerrado",
}


async def private_response(interaction: discord.Interaction, content: str | None = None, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content=content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content=content, ephemeral=True, **kwargs)


def channel_setting_text(raw_channel_id: str) -> str:
    return f"<#{raw_channel_id}>" if raw_channel_id and raw_channel_id.isdigit() else "No configurado"


def is_panel_text_channel(channel) -> bool:
    return getattr(channel, "type", None) in {discord.ChannelType.text, discord.ChannelType.news}


def support_panel_permission_errors(channel, guild: discord.Guild) -> list[str]:
    member = getattr(guild, "me", None)
    if member is None or not callable(getattr(channel, "permissions_for", None)):
        return []
    perms = channel.permissions_for(member)
    checks = (
        ("view_channel", "Ver el canal"),
        ("send_messages", "Enviar mensajes"),
        ("embed_links", "Insertar enlaces"),
    )
    return [label for attr, label in checks if not getattr(perms, attr, False)]


class SupportPanelView(discord.ui.View):
    def __init__(self, cog: "Support"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="ABRIR TICKET", emoji="🎫", style=discord.ButtonStyle.primary, custom_id="support:open_ticket", row=0)
    async def open_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        bank_cog = self.cog.bot.get_cog("Bank")
        if bank_cog is None:
            await private_response(interaction, "El sistema de tickets no está disponible en este momento.")
            return
        await bank_cog.open_ticket_modal(interaction)

    @discord.ui.button(label="MIS TICKETS", emoji="📂", style=discord.ButtonStyle.secondary, custom_id="support:my_tickets", row=0)
    async def my_tickets(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_my_tickets(interaction, interaction.user.id, page=0)

    @discord.ui.button(label="GUÍAS", emoji="📚", style=discord.ButtonStyle.secondary, custom_id="support:guides", row=0)
    async def guides(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await private_response(
            interaction,
            embed=build_guides_home_embed(),
            view=SupportGuidesView(self.cog),
        )


class UserTicketsView(discord.ui.View):
    def __init__(self, cog: "Support", *, user_id: int, page: int, total: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.user_id = user_id
        self.page = page
        self.total = total

    async def require_owner(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.user_id:
            return True
        await private_response(interaction, "Solo puedes consultar tus propios tickets.")
        return False

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="support:my_tickets:prev", row=0)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner(interaction):
            await self.cog.show_my_tickets(interaction, self.user_id, page=max(0, self.page - 1), edit=True)

    @discord.ui.button(label="Siguiente", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="support:my_tickets:next", row=0)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner(interaction):
            last_page = max(0, (self.total - 1) // SUPPORT_TICKETS_PAGE_SIZE)
            await self.cog.show_my_tickets(interaction, self.user_id, page=min(last_page, self.page + 1), edit=True)


class SupportGuideSelect(discord.ui.Select):
    def __init__(self, cog: "Support"):
        self.cog = cog
        super().__init__(
            placeholder="Selecciona una categoría de guía",
            min_values=1,
            max_values=1,
            custom_id="support:guides:select",
            options=[
                discord.SelectOption(
                    label=guide.title,
                    value=guide.key,
                    description=guide.description[:100],
                    emoji=guide.emoji,
                )
                for guide in GUIDES.values()
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        guide = GUIDES.get(self.values[0])
        if guide is None:
            await private_response(interaction, "No encontré esa guía.")
            return
        await interaction.response.edit_message(
            embed=build_guide_embed(guide),
            view=SupportGuideDetailView(self.cog, guide.key),
        )


class SupportGuidesView(discord.ui.View):
    def __init__(self, cog: "Support"):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(SupportGuideSelect(cog))


class SupportGuideDetailView(discord.ui.View):
    def __init__(self, cog: "Support", guide_key: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.guide_key = guide_key
        guide = GUIDES.get(guide_key)
        if guide is not None and guide.video_url:
            self.add_item(discord.ui.Button(label="Ver video", emoji="▶️", style=discord.ButtonStyle.link, url=guide.video_url))

    @discord.ui.button(label="Volver a guías", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="support:guides:back", row=1)
    async def back_to_guides(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=build_guides_home_embed(),
            view=SupportGuidesView(self.cog),
        )


class SupportPanelChannelConfigView(discord.ui.View):
    def __init__(self, cog: "Support"):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel_select = discord.ui.ChannelSelect(
            placeholder="Selecciona el canal del Panel de Soporte",
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.channel_select.callback = self.select_channel
        self.add_item(self.channel_select)

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden configurar el Panel de Soporte.")
        return False

    async def save_channel(self, interaction: discord.Interaction, channel) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta configuración solo aplica dentro de un servidor.")
            return
        if not is_panel_text_channel(channel):
            await private_response(interaction, "Selecciona un canal de texto válido para el Panel de Soporte.")
            return
        missing = support_panel_permission_errors(channel, interaction.guild)
        if missing:
            await private_response(interaction, "No puedo publicar ahí. Faltan permisos: " + ", ".join(missing) + ".")
            return
        self.cog.db.set_setting(interaction.guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY, str(channel.id))
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar canal Panel de Soporte",
            system="Configuracion",
            observation=str(channel.id),
        )
        mention = getattr(channel, "mention", f"<#{channel.id}>")
        await private_response(interaction, f"✅ Canal del Panel de Soporte configurado correctamente: {mention}")

    async def select_channel(self, interaction: discord.Interaction) -> None:
        if await self.require_admin(interaction):
            await self.save_channel(interaction, self.channel_select.values[0])

    @discord.ui.button(label="Usar canal actual", emoji="📍", style=discord.ButtonStyle.primary, row=1)
    async def use_current(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await self.save_channel(interaction, interaction.channel)

    @discord.ui.button(label="Quitar canal", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def clear_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(interaction.guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY, "")
        await private_response(interaction, "🎫 Panel de Soporte\nCanal: No configurado")


class SupportBannerModal(discord.ui.Modal, title="Banner del Panel de Soporte"):
    def __init__(self, cog: "Support"):
        super().__init__(timeout=180)
        self.cog = cog
        self.image_url = discord.ui.TextInput(
            label="URL de imagen o vacío para quitar",
            required=False,
            max_length=500,
            placeholder="https://...",
        )
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden configurar el banner.")
            return
        value = str(self.image_url.value).strip()
        if value and not value.startswith(("http://", "https://")):
            await private_response(interaction, "La URL del banner debe empezar con http:// o https://.")
            return
        self.cog.db.set_setting(interaction.guild.id, SUPPORT_PANEL_BANNER_SETTING_KEY, value)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar banner Panel de Soporte",
            system="Configuracion",
            observation="configurado" if value else "quitado",
        )
        await private_response(
            interaction,
            "✅ Banner del Panel de Soporte configurado correctamente." if value else "✅ Banner del Panel de Soporte quitado.",
        )


class SupportAdminView(discord.ui.View):
    def __init__(self, cog: "Support"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden administrar el Panel de Soporte.")
        return False

    @discord.ui.button(label="Publicar panel", emoji="📌", style=discord.ButtonStyle.success, custom_id="support:admin:publish", row=0)
    async def publish_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            message = await self.cog.publish_support_panel(interaction.guild, interaction.user.id)
        except (ValueError, discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            await interaction.followup.send(f"No pude publicar el Panel de Soporte: {exc}", ephemeral=True)
            return
        await interaction.followup.send(f"✅ Panel de Soporte publicado en {message.channel.mention}.", ephemeral=True)

    @discord.ui.button(label="Actualizar panel", emoji="🔄", style=discord.ButtonStyle.primary, custom_id="support:admin:update", row=0)
    async def update_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            updated = await self.cog.update_support_panel(interaction.guild, interaction.user.id)
        except (ValueError, discord.Forbidden, discord.HTTPException, AttributeError) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "✅ Panel de Soporte actualizado." if updated else "No encontré un panel publicado para actualizar.",
            ephemeral=True,
        )

    @discord.ui.button(label="Configurar canal", emoji="📍", style=discord.ButtonStyle.secondary, custom_id="support:admin:channel", row=1)
    async def configure_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            current = self.cog.db.get_setting(interaction.guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY)
            await private_response(
                interaction,
                f"Canal actual del Panel de Soporte: {channel_setting_text(current)}",
                view=SupportPanelChannelConfigView(self.cog),
            )

    @discord.ui.button(label="Configurar imagen/banner", emoji="🖼️", style=discord.ButtonStyle.secondary, custom_id="support:admin:banner", row=1)
    async def configure_banner(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(SupportBannerModal(self.cog))

    @discord.ui.button(label="Ver configuración", emoji="👁️", style=discord.ButtonStyle.secondary, custom_id="support:admin:status", row=2)
    async def view_status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.support_panel_status_text(interaction.guild))


class Support(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(SupportPanelView(self))

    def build_support_panel_embed(self, guild_id: int) -> discord.Embed:
        embed = discord.Embed(
            title="🎫 PANEL DE SOPORTE G3NESYS",
            description=(
                "¿Necesitas ayuda, quieres realizar una reclamación o tienes dudas sobre cómo utilizar el bot?\n\n"
                "Desde este panel puedes abrir un ticket de soporte, consultar tus solicitudes anteriores "
                "o acceder a nuestras guías de usuario.\n\n"
                "💰 **Reclamaciones de pagos**\n\n"
                "Para solicitar cualquier auditoría de tus pagos, tienes un plazo máximo de 15 días naturales "
                "después de la actividad en cuestión para abrir una reclamación.\n\n"
                "Al abrir un ticket de reclamación de pago, por favor incluye toda la información que recuerdes.\n\n"
                "Si cuentas con el ID de la actividad, inclúyelo en tu solicitud para agilizar la auditoría.\n\n"
                "⏱️ **Tiempo de respuesta:** hasta 24 horas."
            ),
            color=discord.Color.gold(),
        )
        banner_url = self.db.get_setting(guild_id, SUPPORT_PANEL_BANNER_SETTING_KEY)
        if banner_url:
            embed.set_image(url=banner_url)
        return embed

    def user_tickets_embed(self, guild_id: int, user_id: int, *, page: int = 0) -> tuple[discord.Embed, list, int]:
        total = count_tickets_by_user(self.db, guild_id, user_id)
        offset = page * SUPPORT_TICKETS_PAGE_SIZE
        rows = search_tickets_by_user(
            self.db,
            guild_id,
            user_id,
            limit=SUPPORT_TICKETS_PAGE_SIZE,
            offset=offset,
        )
        embed = discord.Embed(
            title="📂 Mis tickets",
            description="Estos son tus tickets registrados en este servidor.",
            color=discord.Color.blurple(),
        )
        if not rows:
            embed.description = "No tienes tickets registrados en este servidor."
            return embed, rows, total
        for row in rows:
            status = SUPPORT_TICKET_STATUS_LABELS.get(str(row["status"]), str(row["status"]))
            thread_id = row["thread_id"]
            link = f"[Abrir hilo](https://discord.com/channels/{guild_id}/{thread_id})" if thread_id else "Sin hilo disponible"
            embed.add_field(
                name=f"{row['code']} · {status}",
                value=(
                    f"Asunto: {row['subject']}\n"
                    f"Fecha: {row['created_at']}\n"
                    f"Acceso: {link}"
                )[:1024],
                inline=False,
            )
        last_page = max(0, (total - 1) // SUPPORT_TICKETS_PAGE_SIZE)
        embed.set_footer(text=f"Página {page + 1}/{last_page + 1} · {total} ticket(s)")
        return embed, rows, total

    async def show_my_tickets(
        self,
        interaction: discord.Interaction,
        user_id: int,
        *,
        page: int = 0,
        edit: bool = False,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Los tickets se consultan desde el servidor.")
            return
        if interaction.user.id != user_id:
            await private_response(interaction, "Solo puedes consultar tus propios tickets.")
            return
        embed, _rows, total = self.user_tickets_embed(interaction.guild.id, user_id, page=page)
        view = UserTicketsView(self, user_id=user_id, page=page, total=total) if total > SUPPORT_TICKETS_PAGE_SIZE else None
        if view is not None:
            previous_button = next(item for item in view.children if getattr(item, "custom_id", "") == "support:my_tickets:prev")
            next_button = next(item for item in view.children if getattr(item, "custom_id", "") == "support:my_tickets:next")
            previous_button.disabled = page <= 0
            next_button.disabled = page >= max(0, (total - 1) // SUPPORT_TICKETS_PAGE_SIZE)
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
            return
        await private_response(interaction, embed=embed, view=view)

    def support_panel_status_text(self, guild: discord.Guild) -> str:
        channel = self.db.get_setting(guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY)
        banner = self.db.get_setting(guild.id, SUPPORT_PANEL_BANNER_SETTING_KEY)
        row = self.db.fetch_one(
            "SELECT channel_id, message_id FROM panel_messages WHERE guild_id = ? AND panel_type = ?",
            (guild.id, SUPPORT_PANEL_TYPE),
        )
        lines = [
            "🎫 **Panel de Soporte**",
            f"Canal configurado: {channel_setting_text(channel)}",
            f"Banner: {'Configurado' if banner else 'No configurado'}",
        ]
        if row is None:
            lines.append("Panel publicado: No")
        else:
            lines.append(f"Panel publicado: <#{row['channel_id']}> · mensaje `{row['message_id']}`")
        return "\n".join(lines)

    async def configured_support_channel(self, guild: discord.Guild):
        raw_channel_id = self.db.get_setting(guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY)
        if not raw_channel_id:
            raise ValueError("Configura primero el canal del Panel de Soporte.")
        try:
            channel_id = int(raw_channel_id)
        except ValueError as exc:
            raise ValueError("El canal configurado del Panel de Soporte es inválido.") from exc
        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
                channel = None
        if channel is None or not is_panel_text_channel(channel):
            raise ValueError("El canal configurado del Panel de Soporte no existe o no es un canal de texto.")
        missing = support_panel_permission_errors(channel, guild)
        if missing:
            raise ValueError("Faltan permisos en el canal del Panel de Soporte: " + ", ".join(missing) + ".")
        return channel

    async def publish_support_panel(self, guild: discord.Guild, admin_id: int, *, channel=None):
        target = channel or await self.configured_support_channel(guild)
        message = await target.send(embed=self.build_support_panel_embed(guild.id), view=SupportPanelView(self))
        self.db.execute(
            """
            INSERT INTO panel_messages (
                guild_id, panel_type, channel_id, message_id, created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, panel_type)
            DO UPDATE SET channel_id = excluded.channel_id,
                          message_id = excluded.message_id,
                          created_by = excluded.created_by,
                          created_at = excluded.created_at
            """,
            (guild.id, SUPPORT_PANEL_TYPE, target.id, message.id, admin_id, utc_now_iso()),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Publicar Panel de Soporte",
            system="Configuracion",
            observation=f"channel={target.id}; message={message.id}",
        )
        return message

    async def update_support_panel(self, guild: discord.Guild, admin_id: int) -> bool:
        row = self.db.fetch_one(
            "SELECT channel_id, message_id FROM panel_messages WHERE guild_id = ? AND panel_type = ?",
            (guild.id, SUPPORT_PANEL_TYPE),
        )
        if row is None:
            return False
        channel_id = int(row["channel_id"])
        message_id = int(row["message_id"])
        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
                channel = None
        if channel is None:
            raise ValueError("No encontré el canal donde estaba publicado el Panel de Soporte.")
        message = await channel.fetch_message(message_id)
        await message.edit(embed=self.build_support_panel_embed(guild.id), view=SupportPanelView(self))
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Actualizar Panel de Soporte",
            system="Configuracion",
            observation=f"channel={channel_id}; message={message_id}",
        )
        return True

    @commands.command(name="panel_soporte")
    async def panel_soporte(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        message = await self.publish_support_panel(ctx.guild, ctx.author.id, channel=ctx.channel)
        self.db.set_setting(ctx.guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY, str(ctx.channel.id))
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass
        LOGGER.info("Panel de Soporte publicado en %s/%s", ctx.guild.id, message.channel.id)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Support(bot))
