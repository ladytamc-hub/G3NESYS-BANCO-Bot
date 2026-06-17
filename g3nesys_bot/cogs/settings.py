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
        await ctx.reply(
            "\n".join(
                [
                    "**G3NESYS Bot**",
                    "`!panel_pings` publica el panel de actividades.",
                    "`!panel_banco` publica el panel bancario.",
                    "`!panel_admin` publica el panel administrativo.",
                    "`!caller_set @usuario` autoriza callers.",
                    "`!canal_pings_set` configura el canal actual como canal de pings.",
                    "`!config_ver` muestra configuracion basica.",
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
