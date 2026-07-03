from __future__ import annotations

import discord
from discord.ext import commands

from ..permissions import require_admin_context
from ..services.audit import log_action
from ..services.callers import (
    CallerRemovalNoticeView,
    authorize_caller,
    caller_welcome_embed,
    is_caller_penalized,
    revoke_caller,
)
from ..services.notifications import send_dm_safe
from ..utils import join_csv_ids, split_csv_ids


def format_percent_value(raw: str) -> str:
    cleaned = str(raw or "0").replace("%", "").replace(",", ".").strip()
    try:
        value = float(cleaned or 0)
    except ValueError as exc:
        raise ValueError("El porcentaje debe ser un numero valido.") from exc
    if value < 0 or value > 100:
        raise ValueError("El porcentaje debe estar entre 0 y 100.")
    return f"{value:.2f}".rstrip("0").rstrip(".") or "0"


class Settings(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @commands.command(name="ayuda_g3n")
    async def ayuda_g3n(self, ctx: commands.Context) -> None:
        await self.send_help(ctx)

    @commands.command(name="ayuda", aliases=["help", "comandos"])
    async def ayuda(self, ctx: commands.Context) -> None:
        await self.send_help(ctx)

    async def send_help(self, ctx: commands.Context) -> None:
        sections = [
            (
                "📌 Paneles",
                [
                    "`!panel_pings` - Publica Panel de Callers.",
                    "`!panel_banco` - Publica panel bancario.",
                    "`!panel_admin` - Publica panel administrativo.",
                ],
            ),
            (
                "⚙️ Configuracion",
                [
                    "`!config_ver` - Muestra configuracion basica.",
                    "`!canal_pings_set` - Canal oficial de actividades.",
                    "`!canal_admin_set` - Canal admin.",
                    "`!canal_cobros_set` - Canal de cobros.",
                    "`!canal_multas_set` - Canal de multas.",
                    "`!canal_historial_set` - Canal historial.",
                    "`!canal_splits_set` - Canal de Splits.",
                    "`!canal_notify_splits_set` - Avisos administrativos de Splits.",
                    "`!canal_notify_withdrawals_set` - Avisos administrativos de cobros.",
                    "`!canal_notify_registration_set` - Avisos de inscripciones.",
                    "`!canal_notify_activities_set` - Avisos de actividades.",
                    "`!canal_notify_fines_set` - Avisos de multas.",
                    "`!canal_notify_general_admin_set` - Avisos administrativos generales.",
                    "`!admin_role_set @rol` - Autoriza rol admin.",
                    "`!caller_set @usuario` - Autoriza caller.",
                    "`!caller_remove @usuario` - Quita caller.",
                    "`!economia_set clave valor` - Ajusta economia.",
                    "`!roles_banco_set \"MIEMBRO G3NESYS\" INVITADO` - Roles banco.",
                    "`!diagnostico` - Muestra ruta de base de datos y estado tecnico.",
                ],
            ),
            (
                "⚔️ Actividades",
                [
                    "`!penalizaciones` - Lista penalizados.",
                    "`!penalizacion_remove @usuario motivo` - Quita penalizacion.",
                    "`!split_participantes SPLIT-000001` - Lista participantes.",
                    "`!split_participacion SPLIT-000001 @usuario 10` - Edita porcentaje.",
                    "`!split_agregar SPLIT-000001 @usuario 100` - Agrega participante.",
                    "`!split_quitar SPLIT-000001 @usuario` - Quita participante.",
                ],
            ),
            (
                "🚨 Multas",
                [
                    "`!crear_multa @usuario monto motivo` - Crea multa manual.",
                    "`!cancelar_multa MULTA-000001 motivo` - Cancela multa.",
                    "`!mis_multas` - Consulta tus multas.",
                    "`!mis_multas @usuario` - Admin consulta multas de usuario.",
                    "`!pagar_multa MULTA-000001` - Paga multa con saldo.",
                ],
            ),
            (
                "🏦 Banco",
                [
                    "`!saldo` - Consulta saldo.",
                    "`!estado_cuenta` - Estado de cuenta.",
                    "`!transferir @usuario monto` - Transferencia miembro a miembro.",
                    "`!cobrar monto motivo` - Solicita cobro.",
                ],
            ),
            (
                "💰 Administracion",
                [
                    "`!tesoreria` - Ver tesoreria.",
                    "`!registrar_ingreso monto categoria descripcion` - Ingreso gremial.",
                    "`!registrar_egreso monto categoria descripcion` - Egreso gremial.",
                    "`!depositar_usuario @usuario monto disponible motivo` - Deposito admin.",
                    "`!aprobar_cobro COBRO-000001` - Aprueba cobro.",
                    "`!rechazar_cobro COBRO-000001 motivo` - Rechaza cobro.",
                    "`!liquidar_cobro COBRO-000001 monto` - Liquida cobro.",
                    "`!aprobar_split SPLIT-000001` - Aprueba Split.",
                    "`!rechazar_split SPLIT-000001 motivo` - Rechaza Split.",
                    "`!corregir_split SPLIT-000001 motivo` - Pide correccion.",
                    "`!auditoria_split SPLIT-000001` - Consulta cambios del Split.",
                    "`!reporte_excel` - Exporta reporte Excel.",
                ],
            ),
        ]
        chunks: list[str] = []
        current = ["**📖 Ayuda G3NESYS Bot**"]
        for title, lines in sections:
            block = ["", f"**{title}**", *lines]
            if len("\n".join(current + block)) > 1800:
                chunks.append("\n".join(current))
                current = [f"**📖 Ayuda G3NESYS Bot (cont.)**", *block]
            else:
                current.extend(block)
        chunks.append("\n".join(current))

        try:
            for chunk in chunks:
                await ctx.author.send(chunk)
            await ctx.reply("Te envie la ayuda completa por DM.", mention_author=False)
        except discord.HTTPException:
            for chunk in chunks:
                await ctx.reply(chunk, mention_author=False)

    @commands.command(name="diagnostico")
    async def diagnostico(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        db_path = self.db.path.resolve()
        exists = db_path.exists()
        size = db_path.stat().st_size if exists else 0
        await ctx.reply(
            "\n".join(
                [
                    "**🔍 Diagnostico G3NESYS**",
                    f"Base de datos: `{db_path}`",
                    f"Existe: `{exists}`",
                    f"Tamaño: `{size}` bytes",
                    f"Bot conectado como: `{self.bot.user}`",
                ]
            ),
            mention_author=False,
        )

    @commands.command(name="admin_role_set")
    async def admin_role_set(self, ctx: commands.Context, role: discord.Role) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        current = split_csv_ids(self.db.get_setting(ctx.guild.id, "admin_role_ids"))
        current.add(role.id)
        self.db.set_setting(ctx.guild.id, "admin_role_ids", join_csv_ids(current))
        await ctx.reply(f"Rol admin autorizado: {role.mention}", mention_author=False)

    @commands.command(name="caller_set")
    async def caller_set(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        if member.bot:
            await ctx.reply("Un bot no puede registrarse como caller.", mention_author=False)
            return
        if is_caller_penalized(self.db, ctx.guild.id, member.id):
            await ctx.reply(
                f"{member.mention} tiene una penalizacion activa. "
                "Retirala primero desde el menu `Callers` del Panel Administrativo.",
                mention_author=False,
            )
            return
        created = authorize_caller(
            self.db,
            ctx.guild.id,
            member.id,
            ctx.author.id,
        )
        if not created:
            await ctx.reply(f"{member.mention} ya es caller autorizado.", mention_author=False)
            return
        delivered = await send_dm_safe(
            self.db,
            guild_id=ctx.guild.id,
            user=member,
            action="bienvenida_caller",
            embed=caller_welcome_embed(ctx.guild.name),
        )
        log_action(
            self.db,
            ctx.guild.id,
            admin_id=ctx.author.id,
            action="Agregar caller",
            affected_user_id=member.id,
            system="Callers",
            observation="Caller autorizado con el comando caller_set.",
        )
        dm_status = "Bienvenida enviada por DM." if delivered else "No pude enviarle DM."
        await ctx.reply(
            f"📣 {member.mention} ahora es caller autorizado. {dm_status}",
            mention_author=False,
        )

    @commands.command(name="caller_remove")
    async def caller_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        removed = revoke_caller(self.db, ctx.guild.id, member.id)
        if not removed:
            await ctx.reply(f"{member.mention} no estaba registrado como caller.", mention_author=False)
            return
        log_action(
            self.db,
            ctx.guild.id,
            admin_id=ctx.author.id,
            action="Eliminar caller",
            affected_user_id=member.id,
            system="Callers",
            observation="Caller eliminado con caller_remove; aviso opcional pendiente.",
        )
        await ctx.reply(
            f"➖ {member.mention} ya no es caller autorizado. ¿Deseas enviarle un aviso amistoso?",
            view=CallerRemovalNoticeView(
                self.db,
                guild_id=ctx.guild.id,
                guild_name=ctx.guild.name,
                admin_id=ctx.author.id,
                member=member,
            ),
            mention_author=False,
        )

    @commands.command(name="config_ver")
    async def config_ver(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        keys = [
            ("Canal pings", "channel_pings_id"),
            ("Canal admins", "channel_admin_id"),
            ("Canal cobros", "channel_cobros_id"),
            ("Canal multas", "channel_multas_id"),
            ("Canal historial", "channel_historial_id"),
            ("Canal Splits", "channel_repartos_id"),
            ("Avisos Splits", "channel_notify_splits_id"),
            ("Avisos cobros", "channel_notify_withdrawals_id"),
            ("Avisos inscripciones", "channel_notify_registration_id"),
            ("Avisos actividades", "channel_notify_activities_id"),
            ("Avisos multas", "channel_notify_fines_id"),
            ("Avisos admin generales", "channel_notify_general_admin_id"),
            ("Rol miembro", "member_role_name"),
            ("Rol invitado", "guest_role_name"),
            ("Multa inasistencia", "absence_fine_amount"),
            ("Multa inasistencia activa", "absence_fine_enabled"),
            ("Comision transferencia %", "transfer_fee_percent"),
            ("Porcentaje gremial predeterminado", "guild_percentage_default"),
            ("Tasa mercado predeterminada", "market_rate_default"),
            ("Permanencia minima en voz %", "voice_minimum_percent"),
            ("Porcentaje caller predeterminado", "caller_percentage_default"),
        ]
        lines = ["**Configuracion G3NESYS**"]
        for label, key in keys:
            lines.append(f"{label}: `{self.db.get_setting(ctx.guild.id, key) or 'sin configurar'}`")
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="economia_set")
    async def economia_set(self, ctx: commands.Context, key: str, *, value: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        allowed = {
            "guild_percentage_default",
            "market_rate_default",
            "absence_fine_amount",
            "absence_fine_enabled",
            "minimum_withdrawal",
            "currency_name",
            "transfer_fee_percent",
            "require_voice_for_attendance",
            "voice_minimum_percent",
            "caller_percentage_default",
        }
        if key not in allowed:
            await ctx.reply(
                "Clave no permitida. Usa `!config_ver` para ver lo basico.",
                mention_author=False,
            )
            return
        percent_keys = {
            "guild_percentage_default",
            "market_rate_default",
            "transfer_fee_percent",
            "voice_minimum_percent",
            "caller_percentage_default",
        }
        if key in percent_keys:
            try:
                value = format_percent_value(value)
            except ValueError as exc:
                await ctx.reply(str(exc), mention_author=False)
                return
        self.db.set_setting(ctx.guild.id, key, value)
        await ctx.reply(f"Configuracion actualizada: `{key}` = `{value}`", mention_author=False)

    @commands.command(name="roles_banco_set")
    async def roles_banco_set(
        self,
        ctx: commands.Context,
        member_role_name: str,
        *,
        guest_role_name: str,
    ) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        self.db.set_setting(ctx.guild.id, "member_role_name", member_role_name)
        self.db.set_setting(ctx.guild.id, "guest_role_name", guest_role_name)
        await ctx.reply("Roles de banco actualizados.", mention_author=False)

    async def set_channel(self, ctx: commands.Context, key: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        self.db.set_setting(ctx.guild.id, key, str(ctx.channel.id))
        await ctx.reply(f"Canal configurado: {ctx.channel.mention}", mention_author=False)

    @commands.command(name="canal_pings_set")
    async def canal_pings_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_pings_id")

    @commands.command(name="canal_admin_set")
    async def canal_admin_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_admin_id")

    @commands.command(name="canal_cobros_set")
    async def canal_cobros_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_cobros_id")

    @commands.command(name="canal_multas_set")
    async def canal_multas_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_multas_id")

    @commands.command(name="canal_historial_set")
    async def canal_historial_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_historial_id")

    @commands.command(name="canal_repartos_set", aliases=["canal_splits_set"])
    async def canal_repartos_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_repartos_id")

    @commands.command(name="canal_notify_splits_set", aliases=["canal_notif_splits_set"])
    async def canal_notify_splits_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_splits_id")

    @commands.command(
        name="canal_notify_withdrawals_set",
        aliases=["canal_notif_cobros_set"],
    )
    async def canal_notify_withdrawals_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_withdrawals_id")

    @commands.command(
        name="canal_notify_registration_set",
        aliases=["canal_notif_registros_set"],
    )
    async def canal_notify_registration_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_registration_id")

    @commands.command(
        name="canal_notify_activities_set",
        aliases=["canal_notif_actividades_set"],
    )
    async def canal_notify_activities_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_activities_id")

    @commands.command(name="canal_notify_fines_set", aliases=["canal_notif_multas_set"])
    async def canal_notify_fines_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_fines_id")

    @commands.command(
        name="canal_notify_general_admin_set",
        aliases=["canal_notif_admin_set"],
    )
    async def canal_notify_general_admin_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_notify_general_admin_id")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
