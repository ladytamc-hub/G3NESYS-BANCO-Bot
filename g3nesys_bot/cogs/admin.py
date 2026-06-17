from __future__ import annotations

from pathlib import Path

import discord
from discord.ext import commands
from openpyxl import Workbook

from ..constants import (
    ADMIN_PANEL_IMAGE,
    PAYOUT_DEPOSITED,
    PAYOUT_CORRECTION,
    PAYOUT_PENDING,
    PAYOUT_REJECTED,
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_LIQUIDATED,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
)
from ..permissions import is_admin_subject, require_admin_context
from ..services.audit import log_action
from ..services.economy import (
    adjust_user_balance,
    create_movement,
    deposit_to_user_from_treasury,
    ensure_treasury,
    get_account,
    pending_fines_total,
    register_guild_expense,
    register_guild_income,
)
from ..services.notifications import send_dm_safe
from ..utils import format_amount, parse_channel_id, parse_int_amount, utc_now_iso


async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


class ConfirmAdminActionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, action: str, payload: dict):
        super().__init__(timeout=120)
        self.cog = cog
        self.admin_id = admin_id
        self.action = action
        self.payload = payload

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Solo quien inicio la operacion puede confirmar.", ephemeral=True)
            return
        try:
            message = await self.cog.execute_confirmed_action(
                interaction,
                self.action,
                self.payload,
            )
        except ValueError as exc:
            message = str(exc)
        await interaction.response.edit_message(content=message, view=None)

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Solo quien inicio la operacion puede cancelar.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Operacion cancelada.", view=None)


class IncomeModal(discord.ui.Modal, title="Registrar ingreso"):
    amount = discord.ui.TextInput(label="Monto", placeholder="1000000")
    category = discord.ui.TextInput(label="Categoria", placeholder="Donacion")
    description = discord.ui.TextInput(label="Descripcion", style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_income_interaction(interaction, self)


class ExpenseModal(discord.ui.Modal, title="Registrar egreso"):
    amount = discord.ui.TextInput(label="Monto", placeholder="1000000")
    category = discord.ui.TextInput(label="Categoria", placeholder="Reparaciones")
    description = discord.ui.TextInput(label="Descripcion", style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.register_expense_interaction(interaction, self)


class DepositModal(discord.ui.Modal, title="Depositar a usuario"):
    user = discord.ui.TextInput(label="Usuario (ID o mencion)")
    amount = discord.ui.TextInput(label="Monto", placeholder="1000000")
    balance_type = discord.ui.TextInput(label="Tipo: disponible o retenido", default="disponible")
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.deposit_interaction(interaction, self)


class UserStatementModal(discord.ui.Modal, title="Estado de cuenta"):
    user = discord.ui.TextInput(label="Usuario (ID o mencion)")

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.user_statement_interaction(interaction, str(self.user.value))


class AdminPanelView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=None)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False

    @discord.ui.button(label="Ver Tesoreria", style=discord.ButtonStyle.primary, custom_id="g3n:admin:treasury", row=0)
    async def treasury(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.treasury_text(interaction.guild.id))

    @discord.ui.button(label="Registrar Ingreso", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:income", row=0)
    async def income(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(IncomeModal(self.cog))

    @discord.ui.button(label="Registrar Egreso", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:expense", row=0)
    async def expense(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(ExpenseModal(self.cog))

    @discord.ui.button(label="Depositar a Usuario", style=discord.ButtonStyle.success, custom_id="g3n:admin:deposit", row=0)
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(DepositModal(self.cog))

    @discord.ui.button(label="Revisar Repartos", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:payouts", row=1)
    async def payouts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.pending_payouts_text(interaction.guild.id))

    @discord.ui.button(label="Multas", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:fines", row=1)
    async def fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, "Usa `!crear_multa @usuario monto motivo` o `!cancelar_multa MULTA-000001 motivo`.")

    @discord.ui.button(label="Solicitudes Cobro", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawals", row=1)
    async def withdrawals(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.withdrawals_text(interaction.guild.id))

    @discord.ui.button(label="Estado de Cuenta", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:statement", row=1)
    async def statement(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(UserStatementModal(self.cog))

    @discord.ui.button(label="Historial", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:history", row=2)
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.history_text(interaction.guild.id))

    @discord.ui.button(label="Rankings", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:rankings", row=2)
    async def rankings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.rankings_text(interaction.guild.id))

    @discord.ui.button(label="Reportes", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:reports", row=2)
    async def reports(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            path = self.cog.create_report(interaction.guild.id)
            await interaction.response.send_message(
                "Reporte generado.",
                file=discord.File(path),
                ephemeral=True,
            )

    @discord.ui.button(label="Auditoria", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:audit", row=2)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.audit_text(interaction.guild.id))

    @discord.ui.button(label="Configuracion", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:config", row=3)
    async def config(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, "Usa `!config_ver`, comandos `!canal_*_set`, `!caller_set` y `!economia_set`.")


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(AdminPanelView(self))

    @commands.command(name="panel_admin")
    async def panel_admin(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        embed = discord.Embed(
            title="Panel Administrativo G3NESYS",
            description="Tesoreria, repartos, cobros, historial, rankings y configuracion.",
            color=discord.Color.blurple(),
        )
        embed.set_image(url=ADMIN_PANEL_IMAGE)
        message = await ctx.send(embed=embed, view=AdminPanelView(self))
        self.db.execute(
            """
            INSERT INTO panel_messages (
                guild_id, panel_type, channel_id, message_id, created_by, created_at
            )
            VALUES (?, 'admin', ?, ?, ?, ?)
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

    @commands.command(name="tesoreria")
    async def tesoreria(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        await ctx.reply(self.treasury_text(ctx.guild.id), mention_author=False)

    @commands.command(name="registrar_ingreso")
    async def registrar_ingreso(self, ctx: commands.Context, amount_raw: str, category: str, *, description: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
            register_guild_income(
                self.db,
                ctx.guild.id,
                amount=amount,
                category=category,
                description=description,
                admin_id=ctx.author.id,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply("Ingreso registrado.", mention_author=False)

    @commands.command(name="registrar_egreso")
    async def registrar_egreso(self, ctx: commands.Context, amount_raw: str, category: str, *, description: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            f"¿Confirmas esta operacion?\nRegistrar egreso de {format_amount(amount)} por {description}",
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="expense",
                payload={"amount": amount, "category": category, "description": description},
            ),
            mention_author=False,
        )

    @commands.command(name="depositar_usuario")
    async def depositar_usuario(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount_raw: str,
        balance_type: str,
        *,
        reason: str,
    ) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
            normalized_type = self.normalize_balance_type(balance_type)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            (
                "¿Confirmas esta operacion?\n"
                f"Depositar {format_amount(amount)} a {member.mention} como {balance_type}.\n"
                f"Motivo: {reason}"
            ),
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="deposit",
                payload={
                    "user_id": member.id,
                    "amount": amount,
                    "balance_type": normalized_type,
                    "reason": reason,
                },
            ),
            mention_author=False,
        )

    @commands.command(name="aprobar_cobro")
    async def aprobar_cobro(self, ctx: commands.Context, code: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            await self.approve_withdrawal(ctx.guild, code, ctx.author.id)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"Solicitud `{code}` aprobada. Queda pendiente por liquidar.", mention_author=False)

    @commands.command(name="rechazar_cobro")
    async def rechazar_cobro(self, ctx: commands.Context, code: str, *, reason: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        withdrawal = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (ctx.guild.id, code),
        )
        if withdrawal is None or withdrawal["status"] != WITHDRAWAL_PENDING:
            await ctx.reply("Solo se pueden rechazar solicitudes pendientes.", mention_author=False)
            return
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, rejected_by = ?, rejected_at = ?, rejection_reason = ?
            WHERE id = ?
            """,
            (WITHDRAWAL_REJECTED, ctx.author.id, utc_now_iso(), reason, int(withdrawal["id"])),
        )
        user = ctx.guild.get_member(int(withdrawal["user_id"]))
        if user:
            await send_dm_safe(
                self.db,
                guild_id=ctx.guild.id,
                user=user,
                action="rechazar_cobro",
                content=f"Tu solicitud de cobro `{code}` fue rechazada. Motivo: {reason}",
            )
        await ctx.reply(f"Solicitud `{code}` rechazada.", mention_author=False)

    @commands.command(name="liquidar_cobro")
    async def liquidar_cobro(self, ctx: commands.Context, code: str, amount_raw: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            f"¿Confirmas esta operacion?\nLiquidar `{code}` por {format_amount(amount)}.",
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="liquidate_withdrawal",
                payload={"code": code, "amount": amount},
            ),
            mention_author=False,
        )

    @commands.command(name="aprobar_reparto")
    async def aprobar_reparto(self, ctx: commands.Context, code: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        await ctx.reply(
            f"¿Confirmas esta operacion?\nAprobar reparto `{code}` y depositar saldos.",
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="approve_payout",
                payload={"code": code},
            ),
            mention_author=False,
        )

    @commands.command(name="rechazar_reparto")
    async def rechazar_reparto(self, ctx: commands.Context, code: str, *, reason: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            await self.update_payout_status(
                ctx.guild,
                code,
                PAYOUT_REJECTED,
                ctx.author.id,
                reason,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"Reparto `{code}` rechazado.", mention_author=False)

    @commands.command(name="corregir_reparto")
    async def corregir_reparto(self, ctx: commands.Context, code: str, *, reason: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            await self.update_payout_status(
                ctx.guild,
                code,
                PAYOUT_CORRECTION,
                ctx.author.id,
                reason,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(f"Correccion solicitada para `{code}`.", mention_author=False)

    @commands.command(name="reporte_excel")
    async def reporte_excel(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        path = self.create_report(ctx.guild.id)
        await ctx.reply("Reporte generado.", file=discord.File(path), mention_author=False)

    async def register_income_interaction(self, interaction: discord.Interaction, modal: IncomeModal) -> None:
        try:
            amount = parse_int_amount(str(modal.amount.value))
            register_guild_income(
                self.db,
                interaction.guild.id,
                amount=amount,
                category=str(modal.category.value),
                description=str(modal.description.value),
                admin_id=interaction.user.id,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, "Ingreso registrado.")

    async def register_expense_interaction(self, interaction: discord.Interaction, modal: ExpenseModal) -> None:
        try:
            amount = parse_int_amount(str(modal.amount.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(
            interaction,
            f"¿Confirmas esta operacion?\nRegistrar egreso de {format_amount(amount)}.",
            view=ConfirmAdminActionView(
                self,
                admin_id=interaction.user.id,
                action="expense",
                payload={
                    "amount": amount,
                    "category": str(modal.category.value),
                    "description": str(modal.description.value),
                },
            ),
        )

    async def deposit_interaction(self, interaction: discord.Interaction, modal: DepositModal) -> None:
        try:
            user_id = parse_channel_id(str(modal.user.value))
            if user_id is None:
                raise ValueError("No pude leer el usuario.")
            amount = parse_int_amount(str(modal.amount.value))
            balance_type = self.normalize_balance_type(str(modal.balance_type.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(
            interaction,
            f"¿Confirmas esta operacion?\nDepositar {format_amount(amount)} a <@{user_id}>.",
            view=ConfirmAdminActionView(
                self,
                admin_id=interaction.user.id,
                action="deposit",
                payload={
                    "user_id": user_id,
                    "amount": amount,
                    "balance_type": balance_type,
                    "reason": str(modal.reason.value),
                },
            ),
        )

    async def user_statement_interaction(self, interaction: discord.Interaction, user_raw: str) -> None:
        user_id = parse_channel_id(user_raw)
        if user_id is None:
            await private_response(interaction, "No pude leer el usuario.")
            return
        member = interaction.guild.get_member(user_id)
        if member is None:
            await private_response(interaction, "No encontre al usuario en el servidor.")
            return
        await private_response(interaction, self.user_statement_text(interaction.guild.id, member))

    async def execute_confirmed_action(
        self,
        interaction: discord.Interaction,
        action: str,
        payload: dict,
    ) -> str:
        if action == "expense":
            register_guild_expense(
                self.db,
                interaction.guild.id,
                amount=int(payload["amount"]),
                category=str(payload["category"]),
                description=str(payload["description"]),
                admin_id=interaction.user.id,
            )
            return "Egreso registrado."
        if action == "deposit":
            movement_id = deposit_to_user_from_treasury(
                self.db,
                interaction.guild.id,
                user_id=int(payload["user_id"]),
                amount=int(payload["amount"]),
                balance_type=str(payload["balance_type"]),
                reason=str(payload["reason"]),
                admin_id=interaction.user.id,
            )
            member = interaction.guild.get_member(int(payload["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="deposito_admin",
                    content=(
                        "💰 Has recibido un deposito.\n\n"
                        f"Cantidad: {format_amount(payload['amount'])}\n"
                        f"Tipo: {self.readable_balance_type(str(payload['balance_type']))}\n"
                        f"Motivo: {payload['reason']}\n"
                        f"Realizado por: {interaction.user.display_name}"
                    ),
                )
            return f"Deposito registrado. Movimiento #{movement_id}."
        if action == "liquidate_withdrawal":
            return await self.liquidate_withdrawal(
                interaction.guild,
                str(payload["code"]),
                int(payload["amount"]),
                interaction.user.id,
            )
        if action == "approve_payout":
            return await self.approve_payout(interaction.guild, str(payload["code"]), interaction.user.id)
        raise ValueError("Accion no reconocida.")

    async def approve_withdrawal(self, guild: discord.Guild, code: str, admin_id: int) -> None:
        withdrawal = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        if withdrawal["status"] != WITHDRAWAL_PENDING:
            raise ValueError("Solo se pueden aprobar solicitudes pendientes.")
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, approved_by = ?, approved_at = ?
            WHERE id = ?
            """,
            (WITHDRAWAL_APPROVED, admin_id, utc_now_iso(), int(withdrawal["id"])),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Aprobar solicitud de cobro",
            system="Banco",
            affected_user_id=int(withdrawal["user_id"]),
            amount=int(withdrawal["amount_requested"]),
            observation=code,
        )
        user = guild.get_member(int(withdrawal["user_id"]))
        if user:
            await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="aprobar_cobro",
                content=(
                    f"Tu solicitud `{code}` fue aprobada por "
                    f"{format_amount(withdrawal['amount_requested'])}. "
                    "Queda pendiente por liquidar."
                ),
            )

    async def liquidate_withdrawal(
        self,
        guild: discord.Guild,
        code: str,
        amount: int,
        admin_id: int,
    ) -> str:
        withdrawal = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        if withdrawal["status"] != WITHDRAWAL_APPROVED:
            raise ValueError("La solicitud debe estar aprobada antes de liquidar.")
        requested = int(withdrawal["amount_requested"])
        if amount > requested:
            raise ValueError("No puedes liquidar mas de lo solicitado.")
        account = get_account(self.db, guild.id, int(withdrawal["user_id"]))
        if int(account["available"]) < amount:
            raise ValueError("El usuario no tiene saldo disponible suficiente.")

        adjust_user_balance(self.db, guild.id, int(withdrawal["user_id"]), available_delta=-amount)
        status = WITHDRAWAL_LIQUIDATED if amount == requested else WITHDRAWAL_PARTIAL
        movement_id = create_movement(
            self.db,
            guild.id,
            movement_type="LIQUIDACION",
            category="Cobro de saldo",
            amount=amount,
            description=f"Liquidacion de {code}",
            created_by=admin_id,
            user_id=int(withdrawal["user_id"]),
            source_table="withdrawals",
            source_id=int(withdrawal["id"]),
        )
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, amount_liquidated = ?, liquidated_by = ?, liquidated_at = ?
            WHERE id = ?
            """,
            (status, amount, admin_id, utc_now_iso(), int(withdrawal["id"])),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Liquidar solicitud de cobro",
            system="Banco",
            affected_user_id=int(withdrawal["user_id"]),
            amount=amount,
            observation=code,
        )
        user = guild.get_member(int(withdrawal["user_id"]))
        if user:
            await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="liquidar_cobro",
                content=(
                    f"Tu solicitud `{code}` fue liquidada.\n"
                    f"Monto solicitado: {format_amount(requested)}\n"
                    f"Monto liquidado: {format_amount(amount)}\n"
                    f"Estado: {status}"
                ),
            )
        return f"Cobro `{code}` liquidado por {format_amount(amount)}. Movimiento #{movement_id}."

    async def approve_payout(self, guild: discord.Guild, code: str, admin_id: int) -> str:
        payout = self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if payout is None:
            raise ValueError("No encontre ese reparto.")
        if payout["status"] != PAYOUT_PENDING:
            raise ValueError("Ese reparto ya fue procesado o no esta pendiente.")

        if int(payout["guild_amount"]) > 0:
            register_guild_income(
                self.db,
                guild.id,
                amount=int(payout["guild_amount"]),
                category="Aporte por actividad",
                description=f"Aporte gremial de reparto {code}",
                admin_id=admin_id,
            )
        participants = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ?",
            (int(payout["id"]),),
        )
        for participant in participants:
            user_id = int(participant["user_id"])
            fine_count, _ = pending_fines_total(self.db, guild.id, user_id)
            amount = int(participant["amount"])
            balance_type = "retained" if fine_count > 0 else "available"
            if balance_type == "retained":
                adjust_user_balance(self.db, guild.id, user_id, retained_delta=amount)
            else:
                adjust_user_balance(self.db, guild.id, user_id, available_delta=amount)
            create_movement(
                self.db,
                guild.id,
                movement_type="DEPOSITO",
                category="Reparto de actividad",
                amount=amount,
                description=f"Deposito por reparto {code}",
                created_by=admin_id,
                user_id=user_id,
                source_table="payouts",
                source_id=int(payout["id"]),
            )
            self.db.execute(
                """
                UPDATE payout_participants
                SET balance_type = ?, deposited_at = ?
                WHERE id = ?
                """,
                (balance_type, utc_now_iso(), int(participant["id"])),
            )
            member = guild.get_member(user_id)
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
                    user=member,
                    action="deposito_reparto",
                    content=(
                        "💰 Has recibido un deposito por reparto.\n\n"
                        f"Cantidad: {format_amount(amount)}\n"
                        f"Tipo: {self.readable_balance_type(balance_type)}\n"
                        f"Reparto: {code}"
                    ),
                )
        self.db.execute(
            "UPDATE payouts SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (PAYOUT_DEPOSITED, admin_id, utc_now_iso(), int(payout["id"])),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Aprobar reparto",
            system="Repartos",
            amount=int(payout["distributable"]),
            observation=code,
        )
        return f"Reparto `{code}` aprobado y saldos depositados."

    async def update_payout_status(
        self,
        guild: discord.Guild,
        code: str,
        status: str,
        admin_id: int,
        reason: str,
    ) -> None:
        payout = self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if payout is None:
            raise ValueError("No encontre ese reparto.")
        if payout["status"] != PAYOUT_PENDING:
            raise ValueError("Solo se pueden cambiar repartos pendientes.")
        self.db.execute(
            "UPDATE payouts SET status = ?, reviewed_by = ?, reviewed_at = ?, notes = ? WHERE id = ?",
            (status, admin_id, utc_now_iso(), reason, int(payout["id"])),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action=f"Actualizar reparto a {status}",
            system="Repartos",
            amount=int(payout["distributable"]),
            observation=f"{code}: {reason}",
        )
        caller = guild.get_member(int(payout["caller_id"]))
        if caller:
            await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=caller,
                action="estado_reparto",
                content=f"El reparto `{code}` cambio a `{status}`. Motivo: {reason}",
            )

    def treasury_text(self, guild_id: int) -> str:
        ensure_treasury(self.db, guild_id)
        treasury = self.db.fetch_one("SELECT * FROM treasury WHERE guild_id = ?", (guild_id,))
        rows = self.db.fetch_all(
            """
            SELECT type, COALESCE(SUM(amount), 0) AS total
            FROM movements
            WHERE guild_id = ?
            GROUP BY type
            """,
            (guild_id,),
        )
        totals = {row["type"]: int(row["total"]) for row in rows}
        return "\n".join(
            [
                "**Tesoreria G3NESYS**",
                f"Saldo total: {format_amount(treasury['balance'])}",
                f"Ingresos: {format_amount(totals.get('INGRESO', 0))}",
                f"Egresos: {format_amount(totals.get('EGRESO', 0))}",
                f"Depositos internos: {format_amount(totals.get('DEPOSITO', 0))}",
                f"Liquidaciones: {format_amount(totals.get('LIQUIDACION', 0))}",
            ]
        )

    def withdrawals_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT code, user_id, amount_requested, status
            FROM withdrawals
            WHERE guild_id = ? AND status IN (?, ?)
            ORDER BY id DESC LIMIT 15
            """,
            (guild_id, WITHDRAWAL_PENDING, WITHDRAWAL_APPROVED),
        )
        if not rows:
            return "No hay solicitudes de cobro pendientes o aprobadas."
        lines = ["**Solicitudes de cobro**"]
        for row in rows:
            lines.append(
                f"`{row['code']}` <@{row['user_id']}> {format_amount(row['amount_requested'])} - {row['status']}"
            )
        lines.append("Comandos: `!aprobar_cobro CODIGO`, `!liquidar_cobro CODIGO monto`.")
        return "\n".join(lines)

    def pending_payouts_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT code, caller_id, distributable, guild_amount, status
            FROM payouts
            WHERE guild_id = ? AND status = ?
            ORDER BY id DESC LIMIT 15
            """,
            (guild_id, PAYOUT_PENDING),
        )
        if not rows:
            return "No hay repartos pendientes."
        lines = ["**Repartos pendientes**"]
        for row in rows:
            lines.append(
                f"`{row['code']}` Caller <@{row['caller_id']}> "
                f"Repartible {format_amount(row['distributable'])} "
                f"Aporte {format_amount(row['guild_amount'])}"
            )
        lines.append("Comandos: `!aprobar_reparto CODIGO`, `!rechazar_reparto CODIGO motivo`, `!corregir_reparto CODIGO motivo`.")
        return "\n".join(lines)

    def history_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT code, type, category, amount, description
            FROM movements
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT 15
            """,
            (guild_id,),
        )
        if not rows:
            return "No hay movimientos registrados."
        lines = ["**Historial gremial**"]
        for row in rows:
            lines.append(f"`{row['code']}` {row['type']} {format_amount(row['amount'])} - {row['description']}")
        return "\n".join(lines)

    def audit_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT admin_id, action, affected_user_id, amount, system, observation, created_at
            FROM audit_logs
            WHERE guild_id = ?
            ORDER BY id DESC LIMIT 15
            """,
            (guild_id,),
        )
        if not rows:
            return "No hay auditoria registrada."
        lines = ["**Auditoria**"]
        for row in rows:
            affected = f" -> <@{row['affected_user_id']}>" if row["affected_user_id"] else ""
            amount = f" {format_amount(row['amount'])}" if row["amount"] else ""
            lines.append(f"{row['action']}{affected}{amount} [{row['system']}] {row['observation'] or ''}")
        return "\n".join(lines)

    def rankings_text(self, guild_id: int) -> str:
        economy = self.db.fetch_all(
            """
            SELECT user_id, available + retained + seized AS total
            FROM accounts
            WHERE guild_id = ?
            ORDER BY total DESC LIMIT 5
            """,
            (guild_id,),
        )
        attendance = self.db.fetch_all(
            """
            SELECT a.usuario_id, COUNT(*) AS total
            FROM asistencia_actividades a
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.estado = 'Confirmado'
            GROUP BY a.usuario_id
            ORDER BY total DESC LIMIT 5
            """,
            (guild_id,),
        )
        lines = ["**Rankings**", "**Top Economia**"]
        if not economy:
            lines.append("Sin datos.")
        for idx, row in enumerate(economy, start=1):
            lines.append(f"{idx}. <@{row['user_id']}> - {format_amount(row['total'])}")
        lines.append("**Top Asistencia**")
        if not attendance:
            lines.append("Sin datos.")
        for idx, row in enumerate(attendance, start=1):
            lines.append(f"{idx}. <@{row['usuario_id']}> - {row['total']} asistencias")
        return "\n".join(lines)

    def user_statement_text(self, guild_id: int, member: discord.Member) -> str:
        account = get_account(self.db, guild_id, member.id)
        fine_count, fine_total = pending_fines_total(self.db, guild_id, member.id)
        return "\n".join(
            [
                f"**Estado de cuenta de {member.display_name}**",
                f"Disponible: {format_amount(account['available'])}",
                f"Retenido: {format_amount(account['retained'])}",
                f"Decomisado: {format_amount(account['seized'])}",
                f"Multas pendientes: {fine_count} ({format_amount(fine_total)})",
            ]
        )

    def create_report(self, guild_id: int) -> Path:
        reports_dir = Path("data/reports")
        reports_dir.mkdir(parents=True, exist_ok=True)
        path = reports_dir / f"reporte-g3nesys-{guild_id}.xlsx"
        wb = Workbook()
        ws = wb.active
        ws.title = "Movimientos"
        ws.append(["Codigo", "Tipo", "Categoria", "Usuario", "Monto", "Descripcion", "Fecha"])
        rows = self.db.fetch_all(
            """
            SELECT code, type, category, user_id, amount, description, created_at
            FROM movements
            WHERE guild_id = ?
            ORDER BY id DESC
            """,
            (guild_id,),
        )
        for row in rows:
            ws.append([
                row["code"],
                row["type"],
                row["category"],
                row["user_id"],
                row["amount"],
                row["description"],
                row["created_at"],
            ])
        ws2 = wb.create_sheet("Multas")
        ws2.append(["Codigo", "Usuario", "Monto", "Estado", "Motivo", "Origen", "Fecha"])
        fines = self.db.fetch_all(
            """
            SELECT code, user_id, amount, status, reason, origin, created_at
            FROM fines
            WHERE guild_id = ?
            ORDER BY id DESC
            """,
            (guild_id,),
        )
        for row in fines:
            ws2.append([
                row["code"],
                row["user_id"],
                row["amount"],
                row["status"],
                row["reason"],
                row["origin"],
                row["created_at"],
            ])
        wb.save(path)
        return path

    def normalize_balance_type(self, raw: str) -> str:
        value = raw.strip().lower()
        if value in {"disponible", "available"}:
            return "available"
        if value in {"retenido", "retained"}:
            return "retained"
        raise ValueError("Tipo de saldo invalido. Usa disponible o retenido.")

    def readable_balance_type(self, raw: str) -> str:
        return "Saldo retenido" if raw == "retained" else "Saldo disponible"


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
