from __future__ import annotations

import re
from collections import defaultdict

import discord
from discord.ext import commands

from ..constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_IN_PROGRESS,
    ACTIVITY_NOTICE,
    ACTIVITY_OPEN,
    ACTIVITY_PAYOUT_CREATED,
    ADMIN_PANEL_IMAGE,
    ATTENDANCE_ABSENT,
    ATTENDANCE_CONFIRMED,
    ATTENDANCE_PENDING,
    PAYOUT_PENDING,
    PINGS_PANEL_IMAGE,
)
from ..permissions import (
    can_manage_activity,
    has_bank_access,
    is_admin_subject,
    is_caller_subject,
    require_admin_context,
    require_caller_context,
)
from ..services.audit import log_action
from ..services.fines import create_fine
from ..services.notifications import send_dm_safe
from ..utils import normalize_key, parse_channel_id, parse_int_amount, utc_now_iso


def parse_role_lines(raw: str) -> list[dict]:
    roles: list[dict] = []
    for position, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        emoji = ""
        if "|" in line:
            parts = [part.strip() for part in line.split("|")]
            name = parts[0]
            slots = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 1
            emoji = parts[2] if len(parts) > 2 else ""
        else:
            match = re.match(r"(.+?)(?::|-)\s*(\d+)$", line)
            if match:
                name = match.group(1).strip()
                slots = int(match.group(2))
            else:
                name = line
                slots = 1
        if slots <= 0:
            raise ValueError("Los cupos deben ser mayores que cero.")
        roles.append(
            {
                "key": normalize_key(name),
                "name": name[:80],
                "slots": slots,
                "emoji": emoji,
                "position": position,
            }
        )
    if not roles:
        raise ValueError("Debes agregar al menos un rol o arma.")
    return roles


def parse_percent(raw: str) -> float:
    cleaned = (raw or "0").replace("%", "").replace(",", ".").strip()
    value = float(cleaned or 0)
    if value < 0 or value > 100:
        raise ValueError("El porcentaje debe estar entre 0 y 100.")
    return value


async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    ephemeral = interaction.guild is not None
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)


class TemplateModal(discord.ui.Modal, title="Crear plantilla"):
    template_name = discord.ui.TextInput(label="Nombre de plantilla", max_length=80)
    activity_name = discord.ui.TextInput(label="Nombre base de actividad", max_length=100)
    default_time = discord.ui.TextInput(label="Horario base", max_length=40)
    roles = discord.ui.TextInput(
        label="Roles/armas",
        style=discord.TextStyle.paragraph,
        placeholder="Falce|1|<:falce:123>\nHealer|1\nDPS Rango|2",
        max_length=1800,
    )

    def __init__(self, cog: "Activities"):
        super().__init__(timeout=300)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_template_from_modal(interaction, self)


class ActivityModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "Activities",
        *,
        template_id: int | None,
        default_name: str = "",
        default_time: str = "",
    ):
        title = "Publicar actividad" if template_id else "Crear actividad"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.template_id = template_id
        self.activity_name = discord.ui.TextInput(
            label="Nombre de actividad",
            max_length=100,
            default=default_name,
        )
        self.horario = discord.ui.TextInput(
            label="Horario",
            max_length=40,
            default=default_time,
        )
        self.voice_channel = discord.ui.TextInput(
            label="Canal de voz (ID o mencion)",
            required=False,
            max_length=80,
        )
        self.notes = discord.ui.TextInput(
            label="Observaciones",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
        )
        self.add_item(self.activity_name)
        self.add_item(self.horario)
        self.add_item(self.voice_channel)
        self.add_item(self.notes)
        self.roles = None
        if template_id is None:
            self.roles = discord.ui.TextInput(
                label="Roles/armas",
                style=discord.TextStyle.paragraph,
                placeholder="Falce|1|<:falce:123>\nHealer|1\nDPS Rango|2",
                max_length=1800,
            )
            self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.publish_activity_from_modal(interaction, self)


class PayoutModal(discord.ui.Modal, title="Generar reparto"):
    gross_loot = discord.ui.TextInput(label="Loot bruto", placeholder="45000000")
    market_rate = discord.ui.TextInput(label="Tasa mercado %", placeholder="4", default="0")
    repairs = discord.ui.TextInput(label="Reparaciones", placeholder="6000000", default="0")
    expenses = discord.ui.TextInput(label="Otros gastos", placeholder="0", default="0")
    guild_percent = discord.ui.TextInput(label="Porcentaje gremial %", placeholder="10", default="10")

    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.activity_id = activity_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_payout_from_modal(interaction, self.activity_id, self)


class TemplateSelect(discord.ui.Select):
    def __init__(self, cog: "Activities", templates):
        self.cog = cog
        options = [
            discord.SelectOption(
                label=row["name"][:100],
                description=f"{row['activity_name']} - {row['default_time']}"[:100],
                value=str(row["id"]),
            )
            for row in templates[:25]
        ]
        super().__init__(
            placeholder="Selecciona una plantilla",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="g3n:pings:template_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        template_id = int(self.values[0])
        template = self.cog.db.fetch_one(
            "SELECT * FROM templates WHERE id = ? AND guild_id = ?",
            (template_id, interaction.guild.id),
        )
        if template is None:
            await private_response(interaction, "No encontre esa plantilla.")
            return
        await interaction.response.send_modal(
            ActivityModal(
                self.cog,
                template_id=template_id,
                default_name=template["activity_name"],
                default_time=template["default_time"],
            )
        )


class TemplateSelectView(discord.ui.View):
    def __init__(self, cog: "Activities", templates):
        super().__init__(timeout=180)
        self.add_item(TemplateSelect(cog, templates))


class PingsPanelView(discord.ui.View):
    def __init__(self, cog: "Activities"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Crear actividad",
        style=discord.ButtonStyle.primary,
        custom_id="g3n:pings:create_activity",
    )
    async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo callers autorizados o admins pueden crear actividades.")
            return
        await interaction.response.send_modal(ActivityModal(self.cog, template_id=None))

    @discord.ui.button(
        label="Crear plantilla",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:create_template",
    )
    async def create_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo callers autorizados o admins pueden crear plantillas.")
            return
        await interaction.response.send_modal(TemplateModal(self.cog))

    @discord.ui.button(
        label="Seleccionar plantilla",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:select_template",
    )
    async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo callers autorizados o admins pueden publicar actividades.")
            return
        templates = self.cog.db.fetch_all(
            "SELECT * FROM templates WHERE guild_id = ? ORDER BY created_at DESC LIMIT 25",
            (interaction.guild.id,),
        )
        if not templates:
            await private_response(interaction, "Aun no hay plantillas. Crea una con `Crear plantilla`.")
            return
        await private_response(
            interaction,
            "Elige la plantilla que quieres usar:",
            view=TemplateSelectView(self.cog, templates),
        )

    @discord.ui.button(
        label="Ver mis actividades",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:my_activities",
    )
    async def my_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        rows = self.cog.db.fetch_all(
            """
            SELECT code, name, horario, status
            FROM activities
            WHERE guild_id = ? AND caller_id = ?
            ORDER BY id DESC LIMIT 10
            """,
            (interaction.guild.id, interaction.user.id),
        )
        if not rows:
            await private_response(interaction, "No tienes actividades creadas.")
            return
        lines = ["**Tus ultimas actividades**"]
        for row in rows:
            lines.append(f"`{row['code']}` {row['name']} - {row['horario']} - {row['status']}")
        await private_response(interaction, "\n".join(lines))

    @discord.ui.button(
        label="Ver plantillas",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:view_templates",
    )
    async def view_templates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        rows = self.cog.db.fetch_all(
            """
            SELECT t.id, t.name, t.activity_name, t.default_time, COUNT(r.id) AS roles
            FROM templates t
            LEFT JOIN template_roles r ON r.template_id = t.id
            WHERE t.guild_id = ?
            GROUP BY t.id
            ORDER BY t.created_at DESC LIMIT 15
            """,
            (interaction.guild.id,),
        )
        if not rows:
            await private_response(interaction, "No hay plantillas guardadas.")
            return
        lines = ["**Plantillas guardadas**"]
        for row in rows:
            lines.append(
                f"`{row['id']}` {row['name']} - {row['activity_name']} "
                f"({row['roles']} roles, {row['default_time']})"
            )
        await private_response(interaction, "\n".join(lines))

    @discord.ui.button(
        label="Configuracion",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:configuration",
    )
    async def configuration(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await private_response(
            interaction,
            "Configuracion rapida: usa `!canal_pings_set`, `!caller_set @usuario` "
            "y `!economia_set absence_fine_amount 200000`.",
        )


class ActivityView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        roles = cog.get_activity_roles(activity_id)
        status = activity["status"] if activity else ACTIVITY_CANCELLED
        role_disabled = status not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}
        for index, row in enumerate(roles[:15]):
            button = discord.ui.Button(
                label=row["name"][:80],
                style=discord.ButtonStyle.secondary,
                custom_id=f"g3n:activity:role:{activity_id}:{row['id']}",
                row=index // 5,
                disabled=role_disabled,
            )
            if row["emoji"]:
                try:
                    button.emoji = discord.PartialEmoji.from_str(row["emoji"])
                except ValueError:
                    pass
            button.callback = self.role_button
            self.add_item(button)

        self.add_control_button("Salirme", "leave", discord.ButtonStyle.danger, 3, role_disabled)
        self.add_control_button(
            "Iniciar",
            "start",
            discord.ButtonStyle.success,
            3,
            status not in {ACTIVITY_OPEN, ACTIVITY_NOTICE},
        )
        self.add_control_button(
            "Aviso",
            "notice",
            discord.ButtonStyle.primary,
            3,
            status != ACTIVITY_OPEN,
        )
        self.add_control_button(
            "Check",
            "check",
            discord.ButtonStyle.primary,
            3,
            status != ACTIVITY_IN_PROGRESS,
        )
        self.add_control_button(
            "Finalizar",
            "finish",
            discord.ButtonStyle.success,
            3,
            status != ACTIVITY_IN_PROGRESS,
        )
        self.add_control_button(
            "Reparto",
            "payout",
            discord.ButtonStyle.primary,
            4,
            status != ACTIVITY_FINISHED,
        )
        self.add_control_button(
            "Cancelar",
            "cancel",
            discord.ButtonStyle.danger,
            4,
            status in {ACTIVITY_CANCELLED, ACTIVITY_FINISHED, ACTIVITY_PAYOUT_CREATED},
        )

    def add_control_button(
        self,
        label: str,
        action: str,
        style: discord.ButtonStyle,
        row: int,
        disabled: bool,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"g3n:activity:{action}:{self.activity_id}",
            row=row,
            disabled=disabled,
        )
        button.callback = self.control_button
        self.add_item(button)

    async def role_button(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data["custom_id"]
        _, _, _, activity_id, role_id = custom_id.split(":")
        await self.cog.join_role(interaction, int(activity_id), int(role_id))

    async def control_button(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data["custom_id"]
        _, _, action, activity_id = custom_id.split(":")
        await self.cog.handle_activity_action(interaction, action, int(activity_id))


class ConfirmAttendanceView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.activity_id = activity_id
        button = discord.ui.Button(
            label="Aqui estoy",
            style=discord.ButtonStyle.success,
            custom_id=f"g3n:attendance:confirm:{activity_id}",
        )
        button.callback = self.confirm
        self.add_item(button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.confirm_attendance(interaction, self.activity_id)


class Activities(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(PingsPanelView(self))
        active_rows = self.db.fetch_all(
            """
            SELECT id, status
            FROM activities
            WHERE status IN (?, ?, ?, ?) AND message_id IS NOT NULL
            """,
            (ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS, ACTIVITY_FINISHED),
        )
        for row in active_rows:
            self.bot.add_view(ActivityView(self, int(row["id"])))
            if row["status"] == ACTIVITY_IN_PROGRESS:
                self.bot.add_view(ConfirmAttendanceView(self, int(row["id"])))

    @commands.command(name="panel_pings")
    async def panel_pings(self, ctx: commands.Context) -> None:
        if not await require_caller_context(ctx, self.db):
            return
        embed = discord.Embed(
            title="Actividades G3NESYS",
            description=(
                "Crea plantillas, publica actividades y organiza composiciones "
                "sin saturar el canal."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_image(url=PINGS_PANEL_IMAGE)
        message = await ctx.send(embed=embed, view=PingsPanelView(self))
        self.db.execute(
            """
            INSERT INTO panel_messages (
                guild_id, panel_type, channel_id, message_id, created_by, created_at
            )
            VALUES (?, 'pings', ?, ?, ?, ?)
            ON CONFLICT(guild_id, panel_type)
            DO UPDATE SET channel_id = excluded.channel_id,
                          message_id = excluded.message_id,
                          created_by = excluded.created_by,
                          created_at = excluded.created_at
            """,
            (ctx.guild.id, ctx.channel.id, message.id, ctx.author.id, utc_now_iso()),
        )
        try:
            await ctx.message.delete()
        except discord.HTTPException:
            pass

    @commands.command(name="penalizacion_remove")
    async def penalizacion_remove(self, ctx: commands.Context, member: discord.Member, *, reason: str = "") -> None:
        if not await require_admin_context(ctx, self.db):
            return
        self.db.execute(
            """
            UPDATE penalizacion_actividades
            SET activo = 0, removido_por = ?, fecha_remocion = ?, observaciones = ?
            WHERE servidor_id = ? AND usuario_id = ? AND activo = 1
            """,
            (ctx.author.id, utc_now_iso(), reason, ctx.guild.id, member.id),
        )
        log_action(
            self.db,
            ctx.guild.id,
            admin_id=ctx.author.id,
            action="Quitar penalizacion de actividad",
            system="Actividades",
            affected_user_id=member.id,
            observation=reason,
        )
        await ctx.reply(f"{member.mention} fue retirado de penalizacion.", mention_author=False)

    @commands.command(name="penalizaciones")
    async def penalizaciones(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        rows = self.db.fetch_all(
            """
            SELECT usuario_id, motivo, fecha_ingreso
            FROM penalizacion_actividades
            WHERE servidor_id = ? AND activo = 1
            ORDER BY id DESC LIMIT 20
            """,
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.reply("No hay usuarios penalizados.", mention_author=False)
            return
        lines = ["**Penalizaciones activas**"]
        for row in rows:
            lines.append(f"<@{row['usuario_id']}> - {row['motivo']} - {row['fecha_ingreso']}")
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="reparto_participantes")
    async def reparto_participantes(self, ctx: commands.Context, code: str) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese reparto.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del reparto o un admin puede verlo.", mention_author=False)
            return
        rows = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (int(payout["id"]),),
        )
        if not rows:
            await ctx.reply("Ese reparto no tiene participantes.", mention_author=False)
            return
        lines = [f"**Participantes de {code}**"]
        for row in rows:
            amount = f"{int(row['amount']):,}".replace(",", ".")
            lines.append(f"<@{row['user_id']}> - {row['participation_percent']}% - {amount}")
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="reparto_participacion")
    async def reparto_participacion(
        self,
        ctx: commands.Context,
        code: str,
        member: discord.Member,
        percent_raw: str,
    ) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese reparto.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del reparto o un admin puede modificarlo.", mention_author=False)
            return
        if payout["status"] != PAYOUT_PENDING:
            await ctx.reply("Solo se pueden modificar repartos pendientes.", mention_author=False)
            return
        try:
            percent = parse_percent(percent_raw)
            self.set_payout_participation(int(payout["id"]), member.id, percent)
            self.recalculate_payout_amounts(int(payout["id"]))
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"Participacion de {member.mention} actualizada a {percent}%.", mention_author=False)

    @commands.command(name="reparto_agregar")
    async def reparto_agregar(
        self,
        ctx: commands.Context,
        code: str,
        member: discord.Member,
        percent_raw: str = "100",
    ) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese reparto.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del reparto o un admin puede modificarlo.", mention_author=False)
            return
        if payout["status"] != PAYOUT_PENDING:
            await ctx.reply("Solo se pueden modificar repartos pendientes.", mention_author=False)
            return
        try:
            percent = parse_percent(percent_raw)
            exists = self.db.fetch_one(
                "SELECT 1 FROM payout_participants WHERE payout_id = ? AND user_id = ?",
                (int(payout["id"]), member.id),
            )
            if exists:
                raise ValueError("Ese usuario ya esta en el reparto.")
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount
                )
                VALUES (?, ?, ?, 0)
                """,
                (int(payout["id"]), member.id, percent),
            )
            self.recalculate_payout_amounts(int(payout["id"]))
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"{member.mention} agregado al reparto con {percent}%.", mention_author=False)

    @commands.command(name="reparto_quitar")
    async def reparto_quitar(self, ctx: commands.Context, code: str, member: discord.Member) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese reparto.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del reparto o un admin puede modificarlo.", mention_author=False)
            return
        if payout["status"] != PAYOUT_PENDING:
            await ctx.reply("Solo se pueden modificar repartos pendientes.", mention_author=False)
            return
        self.db.execute(
            "DELETE FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (int(payout["id"]), member.id),
        )
        try:
            self.recalculate_payout_amounts(int(payout["id"]))
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"{member.mention} fue retirado del reparto.", mention_author=False)

    def get_activity(self, activity_id: int):
        return self.db.fetch_one("SELECT * FROM activities WHERE id = ?", (activity_id,))

    def get_activity_roles(self, activity_id: int):
        return self.db.fetch_all(
            "SELECT * FROM activity_roles WHERE activity_id = ? ORDER BY position ASC",
            (activity_id,),
        )

    def get_activity_participants(self, activity_id: int):
        return self.db.fetch_all(
            """
            SELECT p.*, r.name AS role_name, r.emoji AS role_emoji
            FROM activity_participants p
            JOIN activity_roles r ON r.id = p.role_id
            WHERE p.activity_id = ?
            ORDER BY r.position ASC, p.joined_at ASC
            """,
            (activity_id,),
        )

    async def create_template_from_modal(
        self,
        interaction: discord.Interaction,
        modal: TemplateModal,
    ) -> None:
        if not interaction.guild or not is_caller_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para crear plantillas.")
            return
        try:
            roles = parse_role_lines(str(modal.roles.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        template_id = self.db.execute(
            """
            INSERT INTO templates (guild_id, name, activity_name, default_time, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                str(modal.template_name.value).strip(),
                str(modal.activity_name.value).strip(),
                str(modal.default_time.value).strip(),
                interaction.user.id,
                utc_now_iso(),
            ),
        )
        for role in roles:
            self.db.execute(
                """
                INSERT INTO template_roles (template_id, key, name, slots, emoji, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    role["key"],
                    role["name"],
                    role["slots"],
                    role["emoji"],
                    role["position"],
                ),
            )
        await private_response(interaction, f"Plantilla guardada con {len(roles)} roles.")

    async def publish_activity_from_modal(
        self,
        interaction: discord.Interaction,
        modal: ActivityModal,
    ) -> None:
        if not interaction.guild or not is_caller_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para publicar actividades.")
            return
        channel_id_raw = self.db.get_setting(interaction.guild.id, "channel_pings_id")
        if not channel_id_raw:
            await private_response(interaction, "Primero configura el canal con `!canal_pings_set`.")
            return
        channel = interaction.guild.get_channel(int(channel_id_raw))
        if channel is None:
            await private_response(interaction, "El canal de pings configurado ya no existe.")
            return

        try:
            if modal.template_id is None:
                roles = parse_role_lines(str(modal.roles.value))
            else:
                template_roles = self.db.fetch_all(
                    "SELECT * FROM template_roles WHERE template_id = ? ORDER BY position ASC",
                    (modal.template_id,),
                )
                roles = [
                    {
                        "key": row["key"],
                        "name": row["name"],
                        "slots": int(row["slots"]),
                        "emoji": row["emoji"] or "",
                        "position": int(row["position"]),
                    }
                    for row in template_roles
                ]
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return

        voice_channel_id = parse_channel_id(str(modal.voice_channel.value))
        code = self.db.next_code(interaction.guild.id, "ACT")
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, template_id, name, caller_id, horario,
                voice_channel_id, notes, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                interaction.guild.id,
                modal.template_id,
                str(modal.activity_name.value).strip(),
                interaction.user.id,
                str(modal.horario.value).strip(),
                voice_channel_id,
                str(modal.notes.value).strip(),
                ACTIVITY_OPEN,
                utc_now_iso(),
            ),
        )
        for role in roles:
            self.db.execute(
                """
                INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    activity_id,
                    role["key"],
                    role["name"],
                    role["slots"],
                    role["emoji"],
                    role["position"],
                ),
            )

        embed = self.build_activity_embed(activity_id)
        view = ActivityView(self, activity_id)
        message = await channel.send(embed=embed, view=view)
        self.db.execute(
            "UPDATE activities SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel.id, message.id, activity_id),
        )
        self.bot.add_view(ActivityView(self, activity_id))
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Publicar actividad",
            system="Actividades",
            observation=code,
        )
        await private_response(interaction, f"Actividad publicada: `{code}`.")

    def build_activity_embed(self, activity_id: int) -> discord.Embed:
        activity = self.get_activity(activity_id)
        roles = self.get_activity_roles(activity_id)
        participants = self.get_activity_participants(activity_id)
        by_role: dict[int, list[str]] = defaultdict(list)
        for participant in participants:
            by_role[int(participant["role_id"])].append(participant["display_name"])

        color = discord.Color.green()
        if activity["status"] in {ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            color = discord.Color.gold()
        elif activity["status"] in {ACTIVITY_CANCELLED}:
            color = discord.Color.red()
        elif activity["status"] in {ACTIVITY_FINISHED, ACTIVITY_PAYOUT_CREATED}:
            color = discord.Color.blue()

        voice_text = "Sin canal"
        if activity["voice_channel_id"]:
            voice_text = f"<#{activity['voice_channel_id']}>"
        embed = discord.Embed(
            title=f"⚔️ {activity['name']}",
            description=activity["notes"] or None,
            color=color,
        )
        embed.add_field(name="Caller", value=f"<@{activity['caller_id']}>", inline=True)
        embed.add_field(name="Horario", value=activity["horario"], inline=True)
        embed.add_field(name="Canal de voz", value=voice_text, inline=True)
        embed.add_field(name="Estado", value=activity["status"], inline=True)
        embed.add_field(name="ID", value=activity["code"], inline=True)

        for role in roles:
            names = by_role.get(int(role["id"]), [])
            value = "\n".join(f"• {name}" for name in names) if names else "• Vacio"
            header = f"{role['emoji'] or ''} {role['name']} ({len(names)}/{role['slots']})".strip()
            embed.add_field(name=header[:256], value=value[:1024], inline=True)
        embed.set_footer(text="Los avisos y respuestas se envian por DM o mensajes privados.")
        return embed

    async def update_activity_message(self, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity or not activity["channel_id"] or not activity["message_id"]:
            return
        guild = self.bot.get_guild(int(activity["guild_id"]))
        if guild is None:
            return
        channel = guild.get_channel(int(activity["channel_id"]))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(activity["message_id"]))
            await message.edit(
                embed=self.build_activity_embed(activity_id),
                view=ActivityView(self, activity_id),
            )
        except discord.HTTPException:
            return

    async def join_role(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        role_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        activity = self.get_activity(activity_id)
        if not activity or activity["guild_id"] != interaction.guild.id:
            await interaction.followup.send("No encontre esta actividad.", ephemeral=True)
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await interaction.followup.send("Las inscripciones ya estan cerradas.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await interaction.followup.send(
                "Necesitas el rol MIEMBRO G3NESYS o INVITADO para anotarte.",
                ephemeral=True,
            )
            return
        penalty = self.ensure_penalty_for_user(interaction.guild.id, interaction.user.id)
        if penalty:
            await interaction.followup.send(
                "No puedes anotarte porque estas en lista de penalizacion. "
                f"Motivo: {penalty}",
                ephemeral=True,
            )
            return
        role = self.db.fetch_one(
            "SELECT * FROM activity_roles WHERE id = ? AND activity_id = ?",
            (role_id, activity_id),
        )
        if role is None:
            await interaction.followup.send("No encontre ese rol.", ephemeral=True)
            return
        current = self.db.fetch_one(
            "SELECT role_id FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        count_row = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM activity_participants
            WHERE activity_id = ? AND role_id = ? AND user_id != ?
            """,
            (activity_id, role_id, interaction.user.id),
        )
        if int(count_row["total"]) >= int(role["slots"]):
            await interaction.followup.send("Ese rol ya no tiene cupo.", ephemeral=True)
            return
        self.db.execute(
            """
            INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(activity_id, user_id)
            DO UPDATE SET role_id = excluded.role_id,
                          display_name = excluded.display_name,
                          joined_at = excluded.joined_at
            """,
            (
                activity_id,
                role_id,
                interaction.user.id,
                interaction.user.display_name,
                utc_now_iso(),
            ),
        )
        await self.update_activity_message(activity_id)
        if current and int(current["role_id"]) != role_id:
            await interaction.followup.send(f"Te movi a **{role['name']}**.", ephemeral=True)
        else:
            await interaction.followup.send(f"Quedaste anotado en **{role['name']}**.", ephemeral=True)

    async def leave_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity or activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await private_response(interaction, "No puedes salirte en este estado.")
            return
        self.db.execute(
            "DELETE FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        await self.update_activity_message(activity_id)
        await private_response(interaction, "Te quite de la actividad.")

    async def handle_activity_action(
        self,
        interaction: discord.Interaction,
        action: str,
        activity_id: int,
    ) -> None:
        activity = self.get_activity(activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad.")
            return
        if action == "leave":
            await self.leave_activity(interaction, activity_id)
            return
        if not can_manage_activity(self.db, interaction, int(activity["caller_id"])):
            await private_response(interaction, "Solo el caller creador o un admin puede controlar esta actividad.")
            return
        if action == "payout":
            await interaction.response.send_modal(PayoutModal(self, activity_id))
            return
        await interaction.response.defer(ephemeral=True)
        if action == "notice":
            await self.send_notice(interaction, activity_id)
        elif action == "start":
            await self.start_activity(interaction, activity_id)
        elif action == "check":
            await self.send_attendance_check(interaction, activity_id)
        elif action == "finish":
            await self.finish_activity(interaction, activity_id)
        elif action == "cancel":
            await self.cancel_activity(interaction, activity_id)
        else:
            await interaction.followup.send("Accion no reconocida.", ephemeral=True)

    async def send_notice(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] != ACTIVITY_OPEN:
            await interaction.followup.send("Solo puedes mandar aviso si la actividad esta abierta.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET status = ? WHERE id = ?",
            (ACTIVITY_NOTICE, activity_id),
        )
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            member = interaction.guild.get_member(int(participant["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="aviso_actividad",
                    content=(
                        f"La actividad **{activity['name']}** esta por iniciar. "
                        "Por favor entra al canal de voz y preparate."
                    ),
                )
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Aviso enviado por DM a los participantes.", ephemeral=True)

    async def start_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await interaction.followup.send("Esta actividad no puede iniciarse en su estado actual.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET status = ?, started_at = ? WHERE id = ?",
            (ACTIVITY_IN_PROGRESS, utc_now_iso(), activity_id),
        )
        for participant in self.get_activity_participants(activity_id):
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
                )
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(actividad_id, usuario_id) DO NOTHING
                """,
                (activity_id, int(participant["user_id"]), ATTENDANCE_PENDING),
            )
        self.bot.add_view(ConfirmAttendanceView(self, activity_id))
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Actividad iniciada. Inscripciones cerradas.", ephemeral=True)

    async def send_attendance_check(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] != ACTIVITY_IN_PROGRESS:
            await interaction.followup.send("El check solo aplica en actividades en curso.", ephemeral=True)
            return
        participants = self.get_activity_participants(activity_id)
        if not participants:
            await interaction.followup.send("No hay participantes anotados.", ephemeral=True)
            return
        for participant in participants:
            member = interaction.guild.get_member(int(participant["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="check_asistencia",
                    content=(
                        f"Confirma tu asistencia a **{activity['name']}**. "
                        "Si te anotaste y no participas, puedes recibir multa automatica."
                    ),
                    view=ConfirmAttendanceView(self, activity_id),
                )
        await interaction.followup.send("Check enviado por DM.", ephemeral=True)

    async def confirm_attendance(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad.")
            return
        guild = self.bot.get_guild(int(activity["guild_id"]))
        confirm_voice = 0
        if guild is not None and activity["voice_channel_id"]:
            member = guild.get_member(interaction.user.id)
            if member and member.voice and member.voice.channel:
                confirm_voice = 1 if member.voice.channel.id == int(activity["voice_channel_id"]) else 0
        has_voice_channel = bool(activity["voice_channel_id"])
        status = ATTENDANCE_CONFIRMED if confirm_voice or not has_voice_channel else ATTENDANCE_ABSENT
        self.db.execute(
            """
            INSERT INTO asistencia_actividades (
                actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz, fecha_check
            )
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(actividad_id, usuario_id)
            DO UPDATE SET estado = excluded.estado,
                          confirmo_boton = 1,
                          confirmo_voz = excluded.confirmo_voz,
                          fecha_check = excluded.fecha_check
            """,
            (activity_id, interaction.user.id, status, confirm_voice, utc_now_iso()),
        )
        if status == ATTENDANCE_CONFIRMED:
            await private_response(interaction, "Asistencia confirmada.")
        else:
            await private_response(
                interaction,
                "Recibi tu boton, pero no estas en el canal de voz configurado. "
                "La asistencia quedo como ausente.",
            )

    async def finish_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] != ACTIVITY_IN_PROGRESS:
            await interaction.followup.send("Solo puedes finalizar actividades en curso.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET status = ?, ended_at = ? WHERE id = ?",
            (ACTIVITY_FINISHED, utc_now_iso(), activity_id),
        )
        attendance_rows = self.db.fetch_all(
            "SELECT * FROM asistencia_actividades WHERE actividad_id = ?",
            (activity_id,),
        )
        known = {int(row["usuario_id"]): row for row in attendance_rows}
        participants = self.get_activity_participants(activity_id)
        absence_fine_enabled = self.db.get_int_setting(interaction.guild.id, "absence_fine_enabled", 0) == 1
        absence_fine_amount = self.db.get_int_setting(interaction.guild.id, "absence_fine_amount", 0)
        absences = []
        for participant in participants:
            user_id = int(participant["user_id"])
            row = known.get(user_id)
            if row is None or row["estado"] == ATTENDANCE_PENDING:
                self.db.execute(
                    """
                    INSERT INTO asistencia_actividades (
                        actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz, fecha_check
                    )
                    VALUES (?, ?, ?, 0, 0, ?)
                    ON CONFLICT(actividad_id, usuario_id)
                    DO UPDATE SET estado = excluded.estado,
                                  fecha_check = excluded.fecha_check
                    """,
                    (activity_id, user_id, ATTENDANCE_ABSENT, utc_now_iso()),
                )
                row = self.db.fetch_one(
                    """
                    SELECT * FROM asistencia_actividades
                    WHERE actividad_id = ? AND usuario_id = ?
                    """,
                    (activity_id, user_id),
                )
            if row and row["estado"] == ATTENDANCE_ABSENT and int(row["genero_multa"]) == 0:
                absences.append(user_id)
                member = interaction.guild.get_member(user_id)
                if member and absence_fine_enabled and absence_fine_amount > 0:
                    fine_code = await create_fine(
                        self.db,
                        guild_id=interaction.guild.id,
                        user=member,
                        amount=absence_fine_amount,
                        reason=f"Inasistencia a actividad {activity['code']}",
                        origin="Sistema de Ping Actividades",
                        created_by=self.bot.user.id if self.bot.user else interaction.user.id,
                    )
                    self.db.execute(
                        """
                        UPDATE asistencia_actividades
                        SET genero_multa = 1
                        WHERE actividad_id = ? AND usuario_id = ?
                        """,
                        (activity_id, user_id),
                    )
                    self.ensure_penalty_for_user(interaction.guild.id, user_id)
                    log_action(
                        self.db,
                        interaction.guild.id,
                        admin_id=interaction.user.id,
                        action="Multa automatica por inasistencia",
                        system="Actividades",
                        affected_user_id=user_id,
                        amount=absence_fine_amount,
                        observation=fine_code,
                    )
        await self.update_activity_message(activity_id)
        await interaction.followup.send(
            f"Actividad finalizada. Ausentes registrados: {len(absences)}.",
            ephemeral=True,
        )

    async def cancel_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] in {ACTIVITY_CANCELLED, ACTIVITY_FINISHED, ACTIVITY_PAYOUT_CREATED}:
            await interaction.followup.send("Esta actividad ya no puede cancelarse.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET status = ?, ended_at = ? WHERE id = ?",
            (ACTIVITY_CANCELLED, utc_now_iso(), activity_id),
        )
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            member = interaction.guild.get_member(int(participant["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="cancelar_actividad",
                    content=(
                        f"La actividad **{activity['name']}** fue cancelada por el caller. "
                        "No se aplicaran multas ni penalizaciones."
                    ),
                )
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Actividad cancelada y participantes notificados.", ephemeral=True)

    async def create_payout_from_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        modal: PayoutModal,
    ) -> None:
        activity = self.get_activity(activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad.")
            return
        if activity["status"] != ACTIVITY_FINISHED:
            await private_response(interaction, "Solo se puede generar reparto desde actividad finalizada.")
            return
        if not can_manage_activity(self.db, interaction, int(activity["caller_id"])):
            await private_response(interaction, "Solo el caller creador o un admin puede generar el reparto.")
            return
        try:
            gross = parse_int_amount(str(modal.gross_loot.value))
            market_rate = parse_percent(str(modal.market_rate.value))
            repairs = parse_int_amount(str(modal.repairs.value)) if str(modal.repairs.value).strip() != "0" else 0
            expenses = parse_int_amount(str(modal.expenses.value)) if str(modal.expenses.value).strip() != "0" else 0
            guild_percent = parse_percent(str(modal.guild_percent.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        after_market = gross - int(round(gross * (market_rate / 100)))
        after_expenses = after_market - repairs - expenses
        if after_expenses < 0:
            await private_response(interaction, "Los gastos superan el loot disponible.")
            return
        guild_amount = int(round(after_expenses * (guild_percent / 100)))
        distributable = after_expenses - guild_amount
        participants = self.db.fetch_all(
            """
            SELECT a.*
            FROM asistencia_actividades a
            WHERE a.actividad_id = ? AND a.estado = ?
            """,
            (activity_id, ATTENDANCE_CONFIRMED),
        )
        if not participants:
            await private_response(interaction, "No hay participantes confirmados para repartir.")
            return
        code = self.db.next_code(interaction.guild.id, "REP")
        payout_id = self.db.execute(
            """
            INSERT INTO payouts (
                code, guild_id, activity_id, caller_id, status, gross_loot,
                market_rate_percent, repairs, other_expenses, guild_percent,
                guild_amount, distributable, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                interaction.guild.id,
                activity_id,
                interaction.user.id,
                PAYOUT_PENDING,
                gross,
                market_rate,
                repairs,
                expenses,
                guild_percent,
                guild_amount,
                distributable,
                utc_now_iso(),
            ),
        )
        for participant in participants:
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount
                )
                VALUES (?, ?, 100, 0)
                """,
                (payout_id, int(participant["usuario_id"])),
            )
        self.recalculate_payout_amounts(payout_id)
        self.db.execute(
            "UPDATE activities SET status = ? WHERE id = ?",
            (ACTIVITY_PAYOUT_CREATED, activity_id),
        )
        await self.update_activity_message(activity_id)
        await self.send_payout_to_admins(interaction.guild, payout_id)
        await private_response(interaction, f"Reparto preliminar creado: `{code}`.")

    def get_payout_by_code(self, guild_id: int, code: str):
        return self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )

    def can_manage_payout(self, ctx: commands.Context, payout) -> bool:
        return int(payout["caller_id"]) == ctx.author.id or is_admin_subject(self.db, ctx)

    def set_payout_participation(self, payout_id: int, user_id: int, percent: float) -> None:
        row = self.db.fetch_one(
            "SELECT id FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (payout_id, user_id),
        )
        if row is None:
            raise ValueError("Ese usuario no esta en el reparto. Usa `!reparto_agregar`.")
        self.db.execute(
            """
            UPDATE payout_participants
            SET participation_percent = ?
            WHERE payout_id = ? AND user_id = ?
            """,
            (percent, payout_id, user_id),
        )

    def recalculate_payout_amounts(self, payout_id: int) -> None:
        payout = self.db.fetch_one("SELECT * FROM payouts WHERE id = ?", (payout_id,))
        rows = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (payout_id,),
        )
        if not rows:
            raise ValueError("El reparto debe tener al menos un participante.")
        total_percent = sum(float(row["participation_percent"]) for row in rows)
        if total_percent <= 0:
            raise ValueError("La participacion total debe ser mayor que cero.")
        distributable = int(payout["distributable"])
        assigned = 0
        for index, row in enumerate(rows):
            if index == len(rows) - 1:
                amount = distributable - assigned
            else:
                amount = int(round(distributable * (float(row["participation_percent"]) / total_percent)))
                assigned += amount
            self.db.execute(
                "UPDATE payout_participants SET amount = ? WHERE id = ?",
                (amount, int(row["id"])),
            )

    async def send_payout_to_admins(self, guild: discord.Guild, payout_id: int) -> None:
        payout = self.db.fetch_one("SELECT * FROM payouts WHERE id = ?", (payout_id,))
        channel_id = self.db.get_setting(guild.id, "channel_repartos_id") or self.db.get_setting(
            guild.id,
            "channel_admin_id",
        )
        if not channel_id:
            return
        channel = guild.get_channel(int(channel_id))
        if channel is None:
            return
        embed = discord.Embed(
            title=f"📋 Reparto pendiente {payout['code']}",
            description="Requiere revision y aprobacion admin antes de depositar saldos.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Loot bruto", value=f"{payout['gross_loot']:,}".replace(",", "."))
        embed.add_field(name="Aporte gremial", value=f"{payout['guild_amount']:,}".replace(",", "."))
        embed.add_field(name="Monto repartible", value=f"{payout['distributable']:,}".replace(",", "."))
        embed.set_image(url=ADMIN_PANEL_IMAGE)
        await channel.send(embed=embed)

    def ensure_penalty_for_user(self, guild_id: int, user_id: int) -> str | None:
        active = self.db.fetch_one(
            """
            SELECT motivo FROM penalizacion_actividades
            WHERE servidor_id = ? AND usuario_id = ? AND activo = 1
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )
        if active:
            return str(active["motivo"])

        pending_limit = self.db.get_int_setting(guild_id, "pending_fine_penalty_limit", 3)
        pending = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM fines
            WHERE guild_id = ? AND user_id = ? AND status = 'Pendiente'
            """,
            (guild_id, user_id),
        )
        if int(pending["total"]) >= pending_limit:
            return self.add_penalty(guild_id, user_id, "3 multas pendientes", "Sistema de Multas")

        total_limit = self.db.get_int_setting(guild_id, "total_absence_limit", 10)
        total_absent = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM asistencia_actividades a
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.usuario_id = ? AND a.estado = ?
            """,
            (guild_id, user_id, ATTENDANCE_ABSENT),
        )
        if int(total_absent["total"]) >= total_limit:
            return self.add_penalty(guild_id, user_id, "10 inasistencias acumuladas", "Actividades")

        consecutive_limit = self.db.get_int_setting(guild_id, "consecutive_absence_limit", 3)
        last_rows = self.db.fetch_all(
            """
            SELECT a.estado
            FROM asistencia_actividades a
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.usuario_id = ?
            ORDER BY a.id DESC LIMIT ?
            """,
            (guild_id, user_id, consecutive_limit),
        )
        if (
            len(last_rows) >= consecutive_limit
            and all(row["estado"] == ATTENDANCE_ABSENT for row in last_rows)
        ):
            return self.add_penalty(guild_id, user_id, "3 inasistencias seguidas", "Actividades")
        return None

    def add_penalty(self, guild_id: int, user_id: int, reason: str, origin: str) -> str:
        self.db.execute(
            """
            INSERT INTO penalizacion_actividades (
                servidor_id, usuario_id, motivo, origen, fecha_ingreso, activo
            )
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (guild_id, user_id, reason, origin, utc_now_iso()),
        )
        log_action(
            self.db,
            guild_id,
            admin_id=None,
            action="Penalizacion automatica",
            system="Actividades",
            affected_user_id=user_id,
            observation=reason,
        )
        return reason


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Activities(bot))
