from __future__ import annotations

import discord
from discord.ext import commands

from ..constants import FINE_PENDING
from ..permissions import is_admin_subject, require_admin_context
from ..services.economy import pay_fine_from_balance
from ..services.fines import cancel_fine, create_fine
from ..services.notifications import send_dm_safe
from ..utils import format_amount, parse_int_amount


class ConfirmFineView(discord.ui.View):
    def __init__(
        self,
        cog: "Fines",
        *,
        admin_id: int,
        member_id: int,
        amount: int,
        reason: str,
        action: str,
        fine_code: str | None = None,
    ):
        super().__init__(timeout=120)
        self.cog = cog
        self.admin_id = admin_id
        self.member_id = member_id
        self.amount = amount
        self.reason = reason
        self.action = action
        self.fine_code = fine_code

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Solo quien inicio la operacion puede confirmar.", ephemeral=True)
            return
        member = interaction.guild.get_member(self.member_id)
        if member is None:
            await interaction.response.send_message("No encontre al usuario.", ephemeral=True)
            return
        try:
            if self.action == "create":
                code = await create_fine(
                    self.cog.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    amount=self.amount,
                    reason=self.reason,
                    origin="Manual",
                    created_by=interaction.user.id,
                )
                await interaction.response.edit_message(content=f"Multa creada: `{code}`.", view=None)
            elif self.action == "cancel" and self.fine_code:
                await cancel_fine(
                    self.cog.db,
                    guild=interaction.guild,
                    fine_code=self.fine_code,
                    admin_id=interaction.user.id,
                    reason=self.reason,
                )
                await interaction.response.edit_message(content=f"Multa cancelada: `{self.fine_code}`.", view=None)
        except ValueError as exc:
            await interaction.response.edit_message(content=str(exc), view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Solo quien inicio la operacion puede cancelar.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Operacion cancelada.", view=None)


class Fines(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    @commands.command(name="crear_multa")
    async def crear_multa(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount_raw: str,
        *,
        reason: str,
    ) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            (
                "¿Confirmas esta operacion?\n"
                f"Crear multa a {member.mention} por {format_amount(amount)}.\n"
                f"Motivo: {reason}"
            ),
            view=ConfirmFineView(
                self,
                admin_id=ctx.author.id,
                member_id=member.id,
                amount=amount,
                reason=reason,
                action="create",
            ),
            mention_author=False,
        )

    @commands.command(name="cancelar_multa")
    async def cancelar_multa(self, ctx: commands.Context, fine_code: str, *, reason: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        fine = self.db.fetch_one(
            "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
            (ctx.guild.id, fine_code),
        )
        if fine is None:
            await ctx.reply("No encontre esa multa.", mention_author=False)
            return
        await ctx.reply(
            (
                "¿Confirmas esta operacion?\n"
                f"Cancelar multa `{fine_code}` de <@{fine['user_id']}>.\n"
                f"Motivo: {reason}"
            ),
            view=ConfirmFineView(
                self,
                admin_id=ctx.author.id,
                member_id=int(fine["user_id"]),
                amount=int(fine["amount"]),
                reason=reason,
                action="cancel",
                fine_code=fine_code,
            ),
            mention_author=False,
        )

    @commands.command(name="mis_multas")
    async def mis_multas(self, ctx: commands.Context, member: discord.Member | None = None) -> None:
        target = member or ctx.author
        if member is not None and not is_admin_subject(self.db, ctx):
            await ctx.reply("Solo admins pueden consultar multas de otros usuarios.", mention_author=False)
            return
        rows = self.db.fetch_all(
            """
            SELECT code, amount, reason, status, origin, created_at
            FROM fines
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 15
            """,
            (ctx.guild.id, target.id),
        )
        if not rows:
            await ctx.reply(f"{target.display_name} no tiene multas registradas.", mention_author=False)
            return
        lines = [f"**Multas de {target.display_name}**"]
        for row in rows:
            lines.append(
                f"`{row['code']}` {format_amount(row['amount'])} - {row['status']} - {row['reason']}"
            )
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="pagar_multa")
    async def pagar_multa(self, ctx: commands.Context, fine_code: str) -> None:
        fine = self.db.fetch_one(
            "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
            (ctx.guild.id, fine_code),
        )
        if fine is None:
            await ctx.reply("No encontre esa multa.", mention_author=False)
            return
        if fine["status"] != FINE_PENDING:
            await ctx.reply("Esa multa no esta pendiente.", mention_author=False)
            return
        try:
            pay_fine_from_balance(
                self.db,
                ctx.guild.id,
                fine_code=fine_code,
                payer_id=ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return

        fined_user = ctx.guild.get_member(int(fine["user_id"]))
        if fined_user:
            await send_dm_safe(
                self.db,
                guild_id=ctx.guild.id,
                user=fined_user,
                action="pagar_multa",
                content=(
                    "✅ Tu multa ha sido pagada.\n\n"
                    f"ID: {fine_code}\n"
                    f"Monto: {format_amount(fine['amount'])}\n"
                    f"Pagada por: {ctx.author.display_name}\n"
                    "Estado: Pagada"
                ),
            )
        if ctx.author.id != int(fine["user_id"]):
            await send_dm_safe(
                self.db,
                guild_id=ctx.guild.id,
                user=ctx.author,
                action="pagar_multa_tercero",
                content=(
                    "✅ Has pagado una multa.\n\n"
                    f"ID: {fine_code}\n"
                    f"Usuario beneficiado: <@{fine['user_id']}>\n"
                    f"Monto: {format_amount(fine['amount'])}"
                ),
            )
        await ctx.reply(f"Multa `{fine_code}` pagada.", mention_author=False)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Fines(bot))
