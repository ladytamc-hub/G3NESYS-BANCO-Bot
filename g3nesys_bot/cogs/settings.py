from __future__ import annotations

import discord
from discord.ext import commands

from ..permissions import require_admin_context
from ..utils import join_csv_ids, split_csv_ids, utc_now_iso


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
                    "`!panel_pings` - Publica panel de actividades.",
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
                    "`!canal_repartos_set` - Canal de repartos.",
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
                    "`!reparto_participantes REP-000001` - Lista participantes.",
                    "`!reparto_participacion REP-000001 @usuario 10` - Edita porcentaje.",
                    "`!reparto_agregar REP-000001 @usuario 100` - Agrega participante.",
                    "`!reparto_quitar REP-000001 @usuario` - Quita participante.",
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
                    "`!aprobar_reparto REP-000001` - Aprueba reparto.",
                    "`!rechazar_reparto REP-000001 motivo` - Rechaza reparto.",
                    "`!corregir_reparto REP-000001 motivo` - Pide correccion.",
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
        self.db.execute(
            """
            INSERT INTO callers (guild_id, user_id, added_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET added_by = excluded.added_by, created_at = excluded.created_at
            """,
            (ctx.guild.id, member.id, ctx.author.id, utc_now_iso()),
        )
        await ctx.reply(f"{member.mention} ahora es caller autorizado.", mention_author=False)

    @commands.command(name="caller_remove")
    async def caller_remove(self, ctx: commands.Context, member: discord.Member) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        self.db.execute(
            "DELETE FROM callers WHERE guild_id = ? AND user_id = ?",
            (ctx.guild.id, member.id),
        )
        await ctx.reply(f"{member.mention} ya no es caller autorizado.", mention_author=False)

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
            ("Canal repartos", "channel_repartos_id"),
            ("Rol miembro", "member_role_name"),
            ("Rol invitado", "guest_role_name"),
            ("Multa inasistencia", "absence_fine_amount"),
            ("Multa inasistencia activa", "absence_fine_enabled"),
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
        }
        if key not in allowed:
            await ctx.reply(
                "Clave no permitida. Usa `!config_ver` para ver lo basico.",
                mention_author=False,
            )
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

    @commands.command(name="canal_repartos_set")
    async def canal_repartos_set(self, ctx: commands.Context) -> None:
        await self.set_channel(ctx, "channel_repartos_id")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Settings(bot))
