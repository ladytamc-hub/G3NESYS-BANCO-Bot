from __future__ import annotations

import discord
from discord.ext import commands

from ..constants import BANK_PANEL_IMAGE, WITHDRAWAL_APPROVED, WITHDRAWAL_PENDING
from ..permissions import has_bank_access, is_admin_subject, is_full_member, require_admin_context
from ..services.economy import (
    create_withdrawal_request,
    get_account,
    movement_history_line,
    pending_fines_total,
    transfer_between_members,
)
from ..services.notifications import send_admin_notification, send_dm_safe
from ..utils import format_amount, parse_channel_id, parse_int_amount, utc_now_iso


async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


async def dm_or_private(cog: "Bank", interaction: discord.Interaction, content: str, action: str) -> None:
    sent = await send_dm_safe(
        cog.db,
        guild_id=interaction.guild.id if interaction.guild else None,
        user=interaction.user,
        action=action,
        content=content[:1900],
    )
    if sent:
        await private_response(interaction, "Te envie la informacion por DM.")
    else:
        await private_response(interaction, content[:1900])


class PayFineModal(discord.ui.Modal, title="Pagar multa"):
    fine_code = discord.ui.TextInput(label="ID de multa", placeholder="MULTA-000001")

    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.pay_fine_interaction(interaction, str(self.fine_code.value).strip())


class WithdrawalModal(discord.ui.Modal, title="Cobrar saldo"):
    amount = discord.ui.TextInput(label="Monto solicitado", placeholder="300000")
    reason = discord.ui.TextInput(
        label="Nota",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=300,
    )

    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.withdraw_interaction(
            interaction,
            str(self.amount.value),
            str(self.reason.value).strip(),
        )


class TransferModal(discord.ui.Modal, title="Transferir plata"):
    receiver = discord.ui.TextInput(label="Usuario destino (ID o mencion)")
    amount = discord.ui.TextInput(label="Monto", placeholder="100000")

    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.transfer_interaction(
            interaction,
            str(self.receiver.value),
            str(self.amount.value),
        )


class BankPanelView(discord.ui.View):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Consultar saldo", emoji="💰", style=discord.ButtonStyle.primary, custom_id="g3n:bank:balance")
    async def balance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_balance_interaction(interaction)

    @discord.ui.button(label="Mis multas", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="g3n:bank:fines")
    async def fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_fines_interaction(interaction)

    @discord.ui.button(label="Pagar multa", emoji="✅", style=discord.ButtonStyle.success, custom_id="g3n:bank:pay_fine")
    async def pay_fine(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PayFineModal(self.cog))

    @discord.ui.button(label="Cobrar saldo", emoji="💳", style=discord.ButtonStyle.success, custom_id="g3n:bank:withdraw")
    async def withdraw(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(WithdrawalModal(self.cog))

    @discord.ui.button(label="Transferir plata", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:transfer")
    async def transfer(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(TransferModal(self.cog))

    @discord.ui.button(label="Estado de cuenta", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:statement")
    async def statement(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_statement_interaction(interaction)

    @discord.ui.button(label="Depositos", emoji="🪙", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:deposits")
    async def deposits(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_deposits_interaction(interaction)


class LiquidateWithdrawalReviewModal(discord.ui.Modal, title="Liquidar cobro"):
    amount = discord.ui.TextInput(label="Monto a liquidar", placeholder="1000000")

    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden liquidar cobros.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            amount = parse_int_amount(str(self.amount.value))
            result = await admin_cog.liquidate_withdrawal(
                interaction.guild,
                self.code,
                amount,
                interaction.user.id,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class WithdrawalReviewView(discord.ui.View):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        approve = discord.ui.Button(
            label="Aprobar cobro",
            emoji="✅",
            style=discord.ButtonStyle.success,
            custom_id=f"g3n:withdrawal:approve:{guild_id}:{code}",
        )
        liquidate = discord.ui.Button(
            label="Liquidar cobro",
            emoji="💵",
            style=discord.ButtonStyle.primary,
            custom_id=f"g3n:withdrawal:liquidate:{guild_id}:{code}",
        )
        approve.callback = self.approve
        liquidate.callback = self.liquidate
        self.add_item(approve)
        self.add_item(liquidate)

    async def approve(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden aprobar cobros.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            await admin_cog.approve_withdrawal(interaction.guild, self.code, interaction.user.id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, f"Solicitud `{self.code}` aprobada. Ya puede liquidarse.")

    async def liquidate(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden liquidar cobros.")
            return
        await interaction.response.send_modal(
            LiquidateWithdrawalReviewModal(self.cog, self.guild_id, self.code)
        )


class Bank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(BankPanelView(self))
        pending = self.db.fetch_all(
            """
            SELECT guild_id, code FROM withdrawals
            WHERE status IN (?, ?)
            """,
            (WITHDRAWAL_PENDING, WITHDRAWAL_APPROVED),
        )
        for row in pending:
            self.bot.add_view(
                WithdrawalReviewView(self, int(row["guild_id"]), str(row["code"]))
            )

    @commands.command(name="panel_banco")
    async def panel_banco(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        embed = discord.Embed(
            title="Banco G3NESYS",
            description="Consulta saldos, multas, cobros y transferencias.",
            color=discord.Color.green(),
        )
        embed.set_image(url=BANK_PANEL_IMAGE)
        message = await ctx.send(embed=embed, view=BankPanelView(self))
        self.db.execute(
            """
            INSERT INTO panel_messages (
                guild_id, panel_type, channel_id, message_id, created_by, created_at
            )
            VALUES (?, 'banco', ?, ?, ?, ?)
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

    @commands.command(name="saldo")
    async def saldo(self, ctx: commands.Context) -> None:
        await ctx.reply(self.balance_text(ctx.guild.id, ctx.author), mention_author=False)

    @commands.command(name="estado_cuenta")
    async def estado_cuenta(self, ctx: commands.Context) -> None:
        await ctx.reply(self.statement_text(ctx.guild.id, ctx.author), mention_author=False)

    @commands.command(name="transferir")
    async def transferir(self, ctx: commands.Context, member: discord.Member, amount_raw: str) -> None:
        if not isinstance(ctx.author, discord.Member) or not is_full_member(self.db, ctx.author):
            await ctx.reply("Solo MIEMBRO G3NESYS puede transferir.", mention_author=False)
            return
        if not is_full_member(self.db, member):
            await ctx.reply("Solo puedes transferir a otro MIEMBRO G3NESYS.", mention_author=False)
            return
        try:
            amount = parse_int_amount(amount_raw)
            fee_percent = self.db.get_int_setting(ctx.guild.id, "transfer_fee_percent", 3)
            movement_id = transfer_between_members(
                self.db,
                ctx.guild.id,
                sender_id=ctx.author.id,
                receiver_id=member.id,
                amount=amount,
                fee_percent=fee_percent,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        movement = self.db.fetch_one(
            "SELECT * FROM movements WHERE guild_id = ? AND id = ?",
            (ctx.guild.id, movement_id),
        )
        await send_dm_safe(
            self.db,
            guild_id=ctx.guild.id,
            user=member,
            action="transferencia_recibida",
            content=(
                f"Has recibido una transferencia de {ctx.author.display_name}.\n\n"
                f"{movement_history_line(movement)}"
            ),
        )
        await send_admin_notification(
            self.db,
            guild=ctx.guild,
            category="general_admin",
            content=f"🔁 {movement_history_line(movement)}",
        )
        await ctx.reply(
            f"Transferencia realizada.\n{movement_history_line(movement)}",
            mention_author=False,
        )

    @commands.command(name="cobrar")
    async def cobrar(self, ctx: commands.Context, amount_raw: str, *, reason: str = "") -> None:
        if not isinstance(ctx.author, discord.Member) or not has_bank_access(self.db, ctx.author):
            await ctx.reply("Necesitas rol MIEMBRO G3NESYS o INVITADO para solicitar cobro.", mention_author=False)
            return
        await self.create_withdrawal_and_notify(ctx, ctx.author, amount_raw, reason)

    async def show_balance_interaction(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS o INVITADO.")
            return
        await dm_or_private(
            self,
            interaction,
            self.balance_text(interaction.guild.id, interaction.user),
            "consultar_saldo",
        )

    async def show_statement_interaction(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS o INVITADO.")
            return
        await dm_or_private(
            self,
            interaction,
            self.statement_text(interaction.guild.id, interaction.user),
            "estado_cuenta",
        )

    async def show_fines_interaction(self, interaction: discord.Interaction) -> None:
        rows = self.db.fetch_all(
            """
            SELECT code, amount, reason, status
            FROM fines
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 10
            """,
            (interaction.guild.id, interaction.user.id),
        )
        if not rows:
            await private_response(interaction, "No tienes multas registradas.")
            return
        lines = ["**Tus multas**"]
        for row in rows:
            lines.append(f"`{row['code']}` {format_amount(row['amount'])} - {row['status']} - {row['reason']}")
        await dm_or_private(self, interaction, "\n".join(lines), "mis_multas_panel")

    async def show_deposits_interaction(self, interaction: discord.Interaction) -> None:
        rows = self.db.fetch_all(
            """
            SELECT code, amount, description, created_at
            FROM movements
            WHERE guild_id = ? AND user_id = ? AND type = 'DEPOSITO'
            ORDER BY id DESC LIMIT 10
            """,
            (interaction.guild.id, interaction.user.id),
        )
        if not rows:
            await private_response(interaction, "No tienes depositos registrados.")
            return
        lines = ["**Depositos recientes**"]
        for row in rows:
            lines.append(f"`{row['code']}` {format_amount(row['amount'])} - {row['description']}")
        await dm_or_private(self, interaction, "\n".join(lines), "depositos_panel")

    async def pay_fine_interaction(self, interaction: discord.Interaction, fine_code: str) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS o INVITADO.")
            return
        fine = self.db.fetch_one(
            "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
            (interaction.guild.id, fine_code),
        )
        if fine is None:
            await private_response(interaction, "No encontre esa multa.")
            return
        try:
            from ..services.economy import pay_fine_from_balance

            pay_fine_from_balance(
                self.db,
                interaction.guild.id,
                fine_code=fine_code,
                payer_id=interaction.user.id,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="fines",
            content=(
                f"✅ Multa `{fine_code}` pagada por <@{interaction.user.id}> para "
                f"<@{fine['user_id']}>. Monto: {format_amount(fine['amount'])}."
            ),
        )
        await private_response(interaction, f"Multa `{fine_code}` pagada.")

    async def withdraw_interaction(
        self,
        interaction: discord.Interaction,
        amount_raw: str,
        reason: str,
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS o INVITADO.")
            return
        try:
            amount = parse_int_amount(amount_raw)
            minimum = self.db.get_int_setting(interaction.guild.id, "minimum_withdrawal", 0)
            if minimum and amount < minimum:
                raise ValueError(f"El cobro minimo es {format_amount(minimum)}.")
            code = create_withdrawal_request(
                self.db,
                interaction.guild.id,
                user_id=interaction.user.id,
                amount=amount,
                reason=reason,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await self.send_withdrawal_to_admins(interaction.guild, code)
        await private_response(interaction, f"Solicitud de cobro creada: `{code}`.")

    async def transfer_interaction(
        self,
        interaction: discord.Interaction,
        receiver_raw: str,
        amount_raw: str,
    ) -> None:
        if not isinstance(interaction.user, discord.Member) or not is_full_member(self.db, interaction.user):
            await private_response(interaction, "Solo MIEMBRO G3NESYS puede transferir.")
            return
        receiver_id = parse_channel_id(receiver_raw)
        if receiver_id is None:
            await private_response(interaction, "No pude leer el usuario destino.")
            return
        receiver = interaction.guild.get_member(receiver_id)
        if receiver is None or not is_full_member(self.db, receiver):
            await private_response(interaction, "Solo puedes transferir a otro MIEMBRO G3NESYS.")
            return
        try:
            amount = parse_int_amount(amount_raw)
            fee_percent = self.db.get_int_setting(interaction.guild.id, "transfer_fee_percent", 3)
            movement_id = transfer_between_members(
                self.db,
                interaction.guild.id,
                sender_id=interaction.user.id,
                receiver_id=receiver.id,
                amount=amount,
                fee_percent=fee_percent,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        movement = self.db.fetch_one(
            "SELECT * FROM movements WHERE guild_id = ? AND id = ?",
            (interaction.guild.id, movement_id),
        )
        await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=receiver,
            action="transferencia_recibida",
            content=(
                f"Has recibido una transferencia de {interaction.user.display_name}.\n\n"
                f"{movement_history_line(movement)}"
            ),
        )
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="general_admin",
            content=f"🔁 {movement_history_line(movement)}",
        )
        await private_response(
            interaction,
            f"Transferencia realizada.\n{movement_history_line(movement)}",
        )

    async def create_withdrawal_and_notify(
        self,
        ctx: commands.Context,
        member: discord.Member,
        amount_raw: str,
        reason: str,
    ) -> None:
        try:
            amount = parse_int_amount(amount_raw)
            minimum = self.db.get_int_setting(ctx.guild.id, "minimum_withdrawal", 0)
            if minimum and amount < minimum:
                raise ValueError(f"El cobro minimo es {format_amount(minimum)}.")
            code = create_withdrawal_request(
                self.db,
                ctx.guild.id,
                user_id=member.id,
                amount=amount,
                reason=reason,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await self.send_withdrawal_to_admins(ctx.guild, code)
        await ctx.reply(f"Solicitud de cobro creada: `{code}`.", mention_author=False)

    async def send_withdrawal_to_admins(self, guild: discord.Guild, code: str) -> None:
        row = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if row is None:
            return
        embed = discord.Embed(
            title=f"💳 Solicitud de cobro {code}",
            description="Estado: Pendiente. Admin debe aprobar antes de liquidar.",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Usuario", value=f"<@{row['user_id']}>", inline=True)
        embed.add_field(name="Monto solicitado", value=format_amount(row["amount_requested"]), inline=True)
        embed.add_field(name="Nota", value=row["reason"] or "Sin nota", inline=False)
        view = WithdrawalReviewView(self, guild.id, code)
        self.bot.add_view(view)
        await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            embed=embed,
            view=view,
        )

    def balance_text(self, guild_id: int, member: discord.Member) -> str:
        account = get_account(self.db, guild_id, member.id)
        fine_count, fine_total = pending_fines_total(self.db, guild_id, member.id)
        return "\n".join(
            [
                f"**Saldo de {member.display_name}**",
                f"Disponible: {format_amount(account['available'])}",
                f"Retenido: {format_amount(account['retained'])}",
                f"Decomisado: {format_amount(account['seized'])}",
                f"Multas pendientes: {fine_count} ({format_amount(fine_total)})",
            ]
        )

    def statement_text(self, guild_id: int, member: discord.Member) -> str:
        account = get_account(self.db, guild_id, member.id)
        fine_count, fine_total = pending_fines_total(self.db, guild_id, member.id)
        movements = self.db.fetch_all(
            """
            SELECT *
            FROM movements
            WHERE guild_id = ? AND (user_id = ? OR counterparty_id = ?)
            ORDER BY id DESC LIMIT 8
            """,
            (guild_id, member.id, member.id),
        )
        lines = [
            f"**Estado de cuenta de {member.display_name}**",
            f"Disponible: {format_amount(account['available'])}",
            f"Retenido: {format_amount(account['retained'])}",
            f"Decomisado: {format_amount(account['seized'])}",
            f"Multas pendientes: {fine_count} ({format_amount(fine_total)})",
            "",
            "**Movimientos recientes**",
        ]
        if not movements:
            lines.append("Sin movimientos.")
        for row in movements:
            lines.append(movement_history_line(row))
        return "\n".join(lines)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Bank(bot))
