from __future__ import annotations

import logging

import discord
from discord.ext import commands

from ..constants import (
    BANK_PANEL_IMAGE,
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_CANCELLED,
    WITHDRAWAL_DELEGATED,
    WITHDRAWAL_PAID,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REASSIGNMENT,
    WITHDRAWAL_REJECTED,
    WITHDRAWAL_UNPAID,
)
from ..permissions import has_bank_access, is_admin_subject, is_full_member, require_admin_context
from ..services.audit import log_action
from ..services.callers import is_caller_penalized
from ..services.economy import (
    create_withdrawal_request,
    format_percent,
    get_account,
    movement_history_line,
    pending_fines_total,
    transfer_between_members,
)
from ..services.notifications import send_admin_notification, send_dm_safe
from ..services.tickets import (
    OPEN_TICKET_STATUSES,
    TICKET_ADMIN_REPLY,
    TICKET_CLOSED,
    TICKET_IN_PROGRESS,
    TICKET_INTERNAL_NOTE,
    TICKET_PENDING,
    TICKET_RESOLVED,
    TICKET_STATUSES,
    TICKET_WAITING_USER,
    add_attachment,
    add_ticket_message,
    assign_ticket,
    change_ticket_status,
    create_ticket,
    find_ticket_for_attachment,
    get_ticket,
    list_tickets,
    search_tickets_by_user,
    set_ticket_notification,
    set_ticket_thread,
    ticket_attachments,
    ticket_messages,
    validate_ticket_status,
)
from ..services.ticket_channels import (
    TICKET_CHANNEL_SETTING_KEY,
    is_text_ticket_channel,
    ticket_channel_permission_errors,
)
from ..utils import format_amount, parse_channel_id, parse_int_amount, utc_now_iso


logger = logging.getLogger(__name__)


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


def parse_percent_setting(raw: str, default: float = 0) -> float:
    try:
        value = float(str(raw).replace(",", ".").strip())
    except (TypeError, ValueError):
        return default
    if value < 0 or value > 100:
        return default
    return value


def transfer_fee_amount(amount: int, fee_percent: float) -> int:
    return int(round(amount * (fee_percent / 100)))


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
    def __init__(self, cog: "Bank", guild_id: int | None):
        super().__init__(timeout=180)
        self.cog = cog
        fee_percent = cog.transfer_fee_percent(guild_id) if guild_id is not None else 3
        self.receiver = discord.ui.TextInput(label="Usuario destino (ID o mencion)")
        self.amount = discord.ui.TextInput(
            label=f"Monto (comision {format_percent(fee_percent)}%)"[:45],
            placeholder="100000",
        )
        self.add_item(self.receiver)
        self.add_item(self.amount)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.transfer_interaction(
            interaction,
            str(self.receiver.value),
            str(self.amount.value),
        )


class TransferConfirmationView(discord.ui.View):
    def __init__(
        self,
        cog: "Bank",
        *,
        guild_id: int,
        sender_id: int,
        receiver_id: int,
        amount: int,
        fee_percent: float,
    ):
        super().__init__(timeout=120)
        self.cog = cog
        self.guild_id = guild_id
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.amount = amount
        self.fee_percent = fee_percent

    async def require_sender(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.sender_id:
            return True
        await private_response(interaction, "Solo quien inicio la transferencia puede confirmar.")
        return False

    @discord.ui.button(label="Confirmar", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_sender(interaction):
            return
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Esta transferencia pertenece a otro servidor.")
            return
        sender = interaction.guild.get_member(self.sender_id)
        receiver = interaction.guild.get_member(self.receiver_id)
        if sender is None or receiver is None:
            await private_response(interaction, "No pude encontrar a uno de los usuarios en el servidor.")
            return
        await interaction.response.defer(ephemeral=True)
        try:
            movement = await self.cog.perform_member_transfer(
                interaction.guild,
                sender,
                receiver,
                self.amount,
                self.fee_percent,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.edit_original_response(
            content=f"Transferencia realizada.\n{movement_history_line(movement)}",
            view=None,
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_sender(interaction):
            await interaction.response.edit_message(content="Transferencia cancelada.", view=None)


class BankPanelView(discord.ui.View):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="Consultar mi saldo", emoji="💰", style=discord.ButtonStyle.primary, custom_id="g3n:bank:balance", row=0)
    async def balance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_balance_interaction(interaction)

    @discord.ui.button(label="Mis multas", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="g3n:bank:fines", row=0)
    async def fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_fines_interaction(interaction)

    @discord.ui.button(label="Pagar multa", emoji="✅", style=discord.ButtonStyle.success, custom_id="g3n:bank:pay_fine", row=0)
    async def pay_fine(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PayFineModal(self.cog))

    @discord.ui.button(label="Cobrar saldo", emoji="💳", style=discord.ButtonStyle.success, custom_id="g3n:bank:withdraw", row=0)
    async def withdraw(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(WithdrawalModal(self.cog))

    @discord.ui.button(label="Transferir plata", emoji="🔁", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:transfer", row=1)
    async def transfer(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(TransferModal(self.cog, interaction.guild.id if interaction.guild else None))

    @discord.ui.button(label="Estado de cuenta", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:statement", row=1)
    async def statement(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_statement_interaction(interaction)

    @discord.ui.button(label="Depositos", emoji="🪙", style=discord.ButtonStyle.secondary, custom_id="g3n:bank:deposits", row=1)
    async def deposits(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_deposits_interaction(interaction)


    @discord.ui.button(label="Crear ticket", emoji="\U0001F3AB", style=discord.ButtonStyle.primary, custom_id="g3n:bank:create_ticket", row=2)
    async def create_ticket(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.open_ticket_modal(interaction)

class ApproveWithdrawalReviewModal(discord.ui.Modal, title="Aprobar cobro"):
    admin_message = discord.ui.TextInput(
        label="Indicaciones para el usuario (opcional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Ej.: Te pago en la isla de Martlock a las 00 UTC.",
    )

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
            await private_response(interaction, "Solo admins autorizados pueden aprobar cobros.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            await admin_cog.approve_withdrawal(
                interaction.guild,
                self.code,
                interaction.user.id,
                str(self.admin_message.value).strip(),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(
            interaction,
            f"Solicitud `{self.code}` aprobada. Ya puede liquidarse.",
        )


class RejectWithdrawalReviewModal(discord.ui.Modal, title="No aprobar cobro"):
    reason = discord.ui.TextInput(
        label="Motivo del rechazo",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
    )

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
            await private_response(interaction, "Solo admins autorizados pueden rechazar cobros.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            result = await admin_cog.reject_withdrawal(
                interaction.guild,
                self.code,
                interaction.user.id,
                str(self.reason.value).strip(),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class LiquidateWithdrawalReviewModal(discord.ui.Modal, title="Liquidar cobro"):
    amount = discord.ui.TextInput(label="Monto a liquidar", placeholder="1000000")
    admin_message = discord.ui.TextInput(
        label="Indicaciones para el usuario (opcional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Ej.: Te pago en la isla de Martlock a las 00 UTC.",
    )

    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code

    async def on_submit(self, interaction: discord.Interaction) -> None:
        guild = interaction.guild or self.cog.bot.get_guild(self.guild_id)
        if guild is None or guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        if interaction.guild is not None and not is_admin_subject(self.cog.db, interaction):
            withdrawal = self.cog.db.fetch_one(
                "SELECT assigned_officer_id FROM withdrawals WHERE guild_id = ? AND code = ?",
                (self.guild_id, self.code),
            )
            if withdrawal is None or int(withdrawal["assigned_officer_id"] or 0) != interaction.user.id:
                await private_response(interaction, "Solo admins u oficiales asignados pueden registrar pagos.")
                return
        try:
            amount = parse_int_amount(str(self.amount.value))
            result = await admin_cog.liquidate_withdrawal(
                guild,
                self.code,
                amount,
                interaction.user.id,
                str(self.admin_message.value).strip(),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class WithdrawalNoPaidModal(discord.ui.Modal, title="No pagado"):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.note = discord.ui.TextInput(label="Nota opcional", required=False, style=discord.TextStyle.paragraph, max_length=600)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            result = await admin_cog.mark_withdrawal_unpaid(interaction.guild, self.code, interaction.user.id, str(self.note.value).strip())
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class WithdrawalDelegateDetailsModal(discord.ui.Modal, title="Delegar pago"):
    def __init__(self, cog: "Bank", guild_id: int, code: str, officer_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.officer_id = officer_id
        self.place = discord.ui.TextInput(label="Lugar de pago", placeholder="Banco de Bridgewatch", max_length=200)
        self.schedule = discord.ui.TextInput(label="Dia y horario", placeholder="30 de julio, 00:00 UTC", max_length=80)
        self.note = discord.ui.TextInput(label="Nota opcional", required=False, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.place)
        self.add_item(self.schedule)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        try:
            result = await admin_cog.delegate_withdrawal(
                interaction.guild,
                self.code,
                interaction.user.id,
                self.officer_id,
                str(self.place.value).strip(),
                str(self.schedule.value).strip(),
                str(self.note.value).strip(),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class WithdrawalOfficerSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(placeholder="Busca al oficial autorizado", min_values=1, max_values=1)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden delegar cobros.")
            return
        officer = self.values[0]
        member = interaction.guild.get_member(officer.id)
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        if member is None or member.bot:
            await private_response(interaction, "Selecciona un miembro valido del servidor.")
            return
        if is_caller_penalized(self.cog.db, interaction.guild.id, member.id):
            await private_response(interaction, "Este usuario esta bloqueado por una penalizacion activa.")
            return
        if not admin_cog.is_payment_delegate_active(interaction.guild.id, member.id):
            log_action(self.cog.db, interaction.guild.id, admin_id=interaction.user.id, action="Seleccion de delegado no autorizado", system="Banco", affected_user_id=member.id, observation=self.code)
            await private_response(interaction, "Este usuario no esta autorizado como delegado de pagos.")
            return
        await interaction.response.send_modal(WithdrawalDelegateDetailsModal(self.cog, self.guild_id, self.code, officer.id))


class WithdrawalDelegateView(discord.ui.View):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.add_item(WithdrawalOfficerSelect(cog, guild_id, code))


class OfficerReturnWithdrawalModal(discord.ui.Modal, title="Retornar cobro"):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.reason = discord.ui.TextInput(label="Motivo del retorno", style=discord.TextStyle.paragraph, max_length=600)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        guild = self.cog.bot.get_guild(self.guild_id)
        if guild is None:
            await private_response(interaction, "No pude encontrar el servidor.")
            return
        try:
            result = await admin_cog.return_delegated_withdrawal(guild, self.code, interaction.user.id, str(self.reason.value).strip())
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class OfficerWithdrawalView(discord.ui.View):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self._add("Marcar como pagado", "paid", "\U00002705", discord.ButtonStyle.success)
        self._add("Pago parcial", "partial", "\U0001F4B5", discord.ButtonStyle.primary)
        self._add("Retornar cobro", "return", "\U000021A9", discord.ButtonStyle.danger)

    def _add(self, label: str, action: str, emoji: str, style: discord.ButtonStyle) -> None:
        button = discord.ui.Button(label=label, emoji=emoji, style=style, custom_id=f"g3n:withdrawal_officer:{action}:{self.guild_id}:{self.code}")
        button.callback = self.handle
        self.add_item(button)

    async def handle(self, interaction: discord.Interaction) -> None:
        action = str(interaction.data.get("custom_id", "")).split(":")[2]
        admin_cog = self.cog.bot.get_cog("Admin")
        guild = self.cog.bot.get_guild(self.guild_id)
        if admin_cog is None or guild is None:
            await private_response(interaction, "No pude resolver esta solicitud.")
            return
        if action == "paid":
            try:
                result = await admin_cog.pay_withdrawal_full(guild, self.code, interaction.user.id)
            except ValueError as exc:
                await private_response(interaction, str(exc))
                return
            await private_response(interaction, result)
            return
        if action == "partial":
            await interaction.response.send_modal(LiquidateWithdrawalReviewModal(self.cog, self.guild_id, self.code))
            return
        await interaction.response.send_modal(OfficerReturnWithdrawalModal(self.cog, self.guild_id, self.code))


class WithdrawalReviewView(discord.ui.View):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        withdrawal = cog.db.fetch_one(
            "SELECT status FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        status = withdrawal["status"] if withdrawal is not None else WITHDRAWAL_PENDING
        if status == WITHDRAWAL_PENDING:
            self._add_button("Aprobar cobro", "approve", "✅", discord.ButtonStyle.success, row=0)
            self._add_button("Rechazar", "reject", "\U0000274C", discord.ButtonStyle.danger, row=0)
            self._add_button("Delegar pago", "delegate", "👤", discord.ButtonStyle.secondary, row=0)
        elif status in {WITHDRAWAL_APPROVED, WITHDRAWAL_PARTIAL, WITHDRAWAL_DELEGATED, WITHDRAWAL_REASSIGNMENT}:
            self._add_button("Pagado", "paid", "✅", discord.ButtonStyle.success, row=0)
            self._add_button("Pago parcial", "liquidate", "💵", discord.ButtonStyle.primary, row=0)
            self._add_button("No pagado", "unpaid", "↩", discord.ButtonStyle.danger, row=1)
            if status in {WITHDRAWAL_APPROVED, WITHDRAWAL_REASSIGNMENT}:
                self._add_button("Delegar pago", "delegate", "👤", discord.ButtonStyle.secondary, row=1)
    def _add_button(self, label: str, action: str, emoji: str, style: discord.ButtonStyle, *, row: int) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:withdrawal:{action}:{self.guild_id}:{self.code}",
            row=row,
        )
        button.callback = self.handle
        self.add_item(button)

    async def handle(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este cobro pertenece a otro servidor.")
            return
        action = str(interaction.data.get("custom_id", "")).split(":")[2]
        if action in {"approve", "reject", "delegate"} and not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden gestionar cobros.")
            return
        admin_cog = self.cog.bot.get_cog("Admin")
        if admin_cog is None:
            await private_response(interaction, "El panel administrativo no esta disponible.")
            return
        if action == "approve":
            await interaction.response.send_modal(ApproveWithdrawalReviewModal(self.cog, self.guild_id, self.code))
            return
        if action == "reject":
            await interaction.response.send_modal(RejectWithdrawalReviewModal(self.cog, self.guild_id, self.code))
            return
        if action == "liquidate":
            await interaction.response.send_modal(LiquidateWithdrawalReviewModal(self.cog, self.guild_id, self.code))
            return
        if action == "paid":
            try:
                result = await admin_cog.pay_withdrawal_full(interaction.guild, self.code, interaction.user.id)
            except ValueError as exc:
                await private_response(interaction, str(exc))
                return
            await private_response(interaction, result)
            return
        if action == "unpaid":
            await interaction.response.send_modal(WithdrawalNoPaidModal(self.cog, self.guild_id, self.code))
            return
        if action == "delegate":
            if admin_cog.active_payment_delegate_count(interaction.guild.id) == 0:
                await private_response(interaction, "No hay delegados de pago configurados. A\u00f1ade uno desde Panel de Admins > Delegados de pago.")
                return
            await private_response(interaction, "Selecciona el delegado que realizara el pago:", view=WithdrawalDelegateView(self.cog, self.guild_id, self.code))


class TicketCreateModal(discord.ui.Modal, title="Crear ticket"):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog
        self.subject = discord.ui.TextInput(
            label="Asunto",
            placeholder="Motivo principal del ticket",
            max_length=100,
        )
        self.description = discord.ui.TextInput(
            label="Descripcion",
            placeholder="Explica el problema, solicitud o duda",
            style=discord.TextStyle.paragraph,
            max_length=1800,
        )
        self.add_item(self.subject)
        self.add_item(self.description)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_ticket_interaction(
            interaction,
            str(self.subject.value),
            str(self.description.value),
        )


class TicketAdminReplyModal(discord.ui.Modal, title="Responder ticket"):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.response_text = discord.ui.TextInput(
            label="Respuesta para el usuario",
            style=discord.TextStyle.paragraph,
            max_length=1800,
        )
        self.add_item(self.response_text)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.reply_ticket_interaction(
            interaction,
            self.guild_id,
            self.code,
            str(self.response_text.value),
        )


class TicketFollowupModal(discord.ui.Modal, title="Dar seguimiento"):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.visibility = discord.ui.TextInput(
            label="Tipo: usuario o interna",
            placeholder="usuario / interna",
            default="interna",
            max_length=20,
        )
        self.new_status = discord.ui.TextInput(
            label="Estado nuevo opcional",
            placeholder="Pendiente, En seguimiento, Esperando respuesta del usuario, Resuelto, Cerrado",
            required=False,
            max_length=40,
        )
        self.content = discord.ui.TextInput(
            label="Contenido",
            style=discord.TextStyle.paragraph,
            max_length=1800,
        )
        self.add_item(self.visibility)
        self.add_item(self.new_status)
        self.add_item(self.content)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.followup_ticket_interaction(
            interaction,
            self.guild_id,
            self.code,
            visibility=str(self.visibility.value),
            content=str(self.content.value),
            new_status=str(self.new_status.value).strip() or None,
        )


class TicketSearchByIdModal(discord.ui.Modal, title="Buscar ticket"):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog
        self.code = discord.ui.TextInput(label="ID del ticket", placeholder="TKT-000001", max_length=20)
        self.add_item(self.code)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.show_ticket_detail_interaction(interaction, str(self.code.value).strip())


class TicketSearchByUserModal(discord.ui.Modal, title="Buscar tickets por usuario"):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=180)
        self.cog = cog
        self.user_id = discord.ui.TextInput(label="ID o mencion del usuario", max_length=40)
        self.add_item(self.user_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user_id = parse_channel_id(str(self.user_id.value))
        if user_id is None:
            await private_response(interaction, "No pude leer ese usuario.")
            return
        await self.cog.show_tickets_by_user(interaction, user_id)


class TicketAdminActionView(discord.ui.View):
    def __init__(self, cog: "Bank", guild_id: int, code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.guild_id = guild_id
        self.code = code
        self.add_button("Responder", "respond", "\U0001F4AC", discord.ButtonStyle.primary, row=0)
        self.add_button("Dar seguimiento", "follow", "\U0001F4DD", discord.ButtonStyle.secondary, row=0)
        self.add_button("Resolver", "resolve", "\U00002705", discord.ButtonStyle.success, row=0)
        self.add_button("Cerrar", "close", "\U0001F512", discord.ButtonStyle.danger, row=0)
        self.add_button("Reabrir", "reopen", "\U0001F513", discord.ButtonStyle.secondary, row=1)

    def add_button(self, label: str, action: str, emoji: str, style: discord.ButtonStyle, *, row: int) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:ticket:{action}:{self.guild_id}:{self.code}",
            row=row,
        )
        button.callback = self.handle_button
        self.add_item(button)

    async def handle_button(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or interaction.guild.id != self.guild_id:
            await private_response(interaction, "Este ticket pertenece a otro servidor.")
            return
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden gestionar tickets.")
            return
        action = str(interaction.data.get("custom_id", "")).split(":")[2]
        if action == "respond":
            await interaction.response.send_modal(TicketAdminReplyModal(self.cog, self.guild_id, self.code))
            return
        if action == "follow":
            await interaction.response.send_modal(TicketFollowupModal(self.cog, self.guild_id, self.code))
            return
        status = {
            "resolve": TICKET_RESOLVED,
            "close": TICKET_CLOSED,
            "reopen": TICKET_IN_PROGRESS,
        }.get(action)
        if status is None:
            await private_response(interaction, "Accion de ticket desconocida.")
            return
        await self.cog.change_ticket_status_interaction(interaction, self.guild_id, self.code, status)


class TicketListSelect(discord.ui.Select):
    def __init__(self, cog: "Bank", rows):
        options = []
        for row in rows:
            options.append(
                discord.SelectOption(
                    label=f"{row['code']} - {row['subject']}"[:100],
                    value=str(row["code"]),
                    description=(
                        f"{row['status']} | Usuario {row['user_id']} | "
                        f"Resp {row['message_count']} | Evid {row['attachment_count']}"
                    )[:100],
                )
            )
        super().__init__(placeholder="Selecciona un ticket", min_values=1, max_values=1, options=options)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.show_ticket_detail_interaction(interaction, str(self.values[0]))


class TicketListView(discord.ui.View):
    def __init__(self, cog: "Bank", rows):
        super().__init__(timeout=300)
        if rows:
            self.add_item(TicketListSelect(cog, rows))


class TicketsAdminMenuView(discord.ui.View):
    def __init__(self, cog: "Bank"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden consultar tickets.")
        return False

    async def show_status(self, interaction: discord.Interaction, statuses: tuple[str, ...]) -> None:
        if not await self.require_admin(interaction):
            return
        rows = list_tickets(self.cog.db, interaction.guild.id, statuses, limit=10)
        if not rows:
            await private_response(interaction, "No hay tickets en esa vista.")
            return
        await private_response(
            interaction,
            self.cog.ticket_list_text(rows),
            view=TicketListView(self.cog, rows),
        )

    @discord.ui.button(label="Tickets pendientes", style=discord.ButtonStyle.primary, row=0)
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_status(interaction, (TICKET_PENDING,))

    @discord.ui.button(label="En seguimiento", style=discord.ButtonStyle.secondary, row=0)
    async def progress(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_status(interaction, (TICKET_IN_PROGRESS,))

    @discord.ui.button(label="Esperando respuesta", style=discord.ButtonStyle.secondary, row=1)
    async def waiting(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_status(interaction, (TICKET_WAITING_USER,))

    @discord.ui.button(label="Resueltos", style=discord.ButtonStyle.success, row=1)
    async def resolved(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_status(interaction, (TICKET_RESOLVED,))

    @discord.ui.button(label="Cerrados", style=discord.ButtonStyle.danger, row=1)
    async def closed(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_status(interaction, (TICKET_CLOSED,))

    @discord.ui.button(label="Buscar por ID", style=discord.ButtonStyle.primary, row=2)
    async def search_id(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(TicketSearchByIdModal(self.cog))

    @discord.ui.button(label="Buscar por usuario", style=discord.ButtonStyle.primary, row=2)
    async def search_user(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(TicketSearchByUserModal(self.cog))


class Bank(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(BankPanelView(self))
        pending = self.db.fetch_all(
            """
            SELECT guild_id, code FROM withdrawals
            WHERE status IN (?, ?, ?, ?, ?)
            """,
            (WITHDRAWAL_PENDING, WITHDRAWAL_APPROVED, WITHDRAWAL_PARTIAL, WITHDRAWAL_DELEGATED, WITHDRAWAL_REASSIGNMENT),
        )
        for row in pending:
            self.bot.add_view(
                WithdrawalReviewView(self, int(row["guild_id"]), str(row["code"]))
            )
        tickets = self.db.fetch_all(
            "SELECT guild_id, code FROM tickets",
        )
        for row in tickets:
            self.bot.add_view(
                TicketAdminActionView(self, int(row["guild_id"]), str(row["code"]))
            )

    def withdrawal_admin_embed(self, guild: discord.Guild, withdrawal) -> discord.Embed:
        paid = int(withdrawal["amount_liquidated"] or 0)
        requested = int(withdrawal["amount_requested"] or 0)
        pending = 0 if withdrawal["status"] == WITHDRAWAL_REJECTED else max(0, requested - paid)
        embed = discord.Embed(
            title=f"💳 Solicitud de cobro {withdrawal['code']}",
            description=f"Estado: {withdrawal['status']}",
            color=discord.Color.gold(),
        )
        embed.add_field(name="Usuario", value=f"<@{withdrawal['user_id']}>", inline=True)
        embed.add_field(name="Solicitado", value=format_amount(requested), inline=True)
        embed.add_field(name="Pagado", value=format_amount(paid), inline=True)
        embed.add_field(name="Pendiente", value=format_amount(pending), inline=True)
        embed.add_field(
            name="Oficial asignado",
            value=f"<@{withdrawal['assigned_officer_id']}>" if withdrawal["assigned_officer_id"] else "Sin asignar",
            inline=True,
        )
        embed.add_field(name="Lugar", value=withdrawal["payment_place"] or "Sin lugar", inline=True)
        embed.add_field(name="Horario", value=withdrawal["payment_schedule"] or "Sin horario", inline=True)
        embed.add_field(name="Fecha de pago", value=withdrawal["liquidated_at"] or "Sin pago registrado", inline=True)
        embed.add_field(
            name="Registrado por",
            value=f"<@{withdrawal['liquidated_by']}>" if withdrawal["liquidated_by"] else "Sin registro de pago",
            inline=True,
        )
        embed.add_field(name="Ultima actualizacion", value=withdrawal["updated_at"] or withdrawal["created_at"], inline=True)
        if withdrawal["approved_by"]:
            embed.add_field(name="Aprobado por", value=f"<@{withdrawal['approved_by']}>", inline=True)
        if withdrawal["approved_at"]:
            embed.add_field(name="Fecha de aprobacion", value=withdrawal["approved_at"], inline=True)
        if withdrawal["rejected_by"]:
            embed.add_field(name="No aprobada por", value=f"<@{withdrawal['rejected_by']}>", inline=True)
        if withdrawal["rejected_at"]:
            embed.add_field(name="Fecha de rechazo", value=withdrawal["rejected_at"], inline=True)
        if withdrawal["rejection_reason"]:
            embed.add_field(name="Motivo del rechazo", value=str(withdrawal["rejection_reason"])[:1024], inline=False)
        embed.add_field(name="Nota", value=withdrawal["reason"] or "Sin nota", inline=False)
        if withdrawal["approval_admin_message"]:
            embed.add_field(name="Indicaciones de aprobacion", value=str(withdrawal["approval_admin_message"])[:1024], inline=False)
        if withdrawal["liquidation_admin_message"]:
            embed.add_field(name="Indicaciones de pago", value=str(withdrawal["liquidation_admin_message"])[:1024], inline=False)
        if withdrawal["return_reason"]:
            embed.add_field(name="Nota de cierre/retorno", value=str(withdrawal["return_reason"])[:1024], inline=False)
        return embed

    def withdrawal_admin_view(self, withdrawal) -> discord.ui.View | None:
        terminal_statuses = {WITHDRAWAL_PAID, WITHDRAWAL_UNPAID, WITHDRAWAL_REJECTED, WITHDRAWAL_CANCELLED}
        if withdrawal["status"] in terminal_statuses:
            return None
        return WithdrawalReviewView(self, int(withdrawal["guild_id"]), str(withdrawal["code"]))

    async def refresh_withdrawal_admin_message(self, guild: discord.Guild, code: str, *, actor_id: int | None = None) -> str:
        withdrawal = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild.id, code.strip().upper()),
        )
        if withdrawal is None:
            return ""
        channel_id = withdrawal["notification_channel_id"] if "notification_channel_id" in withdrawal.keys() else None
        message_id = withdrawal["notification_message_id"] if "notification_message_id" in withdrawal.keys() else None
        if not channel_id or not message_id:
            log_action(
                self.db,
                guild.id,
                admin_id=actor_id,
                action="Fallo actualizar embed cobro",
                system="Banco",
                affected_user_id=int(withdrawal["user_id"]),
                observation=f"{code}; sin channel_id/message_id guardado",
            )
            return " Advertencia: no encontre el mensaje original para actualizar el embed."
        channel = guild.get_channel(int(channel_id)) or self.bot.get_channel(int(channel_id))
        try:
            if channel is None:
                channel = await self.bot.fetch_channel(int(channel_id))
            message = await channel.fetch_message(int(message_id))
            await message.edit(
                embed=self.withdrawal_admin_embed(guild, withdrawal),
                view=self.withdrawal_admin_view(withdrawal),
            )
            return ""
        except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError) as exc:
            log_action(
                self.db,
                guild.id,
                admin_id=actor_id,
                action="Fallo actualizar embed cobro",
                system="Banco",
                affected_user_id=int(withdrawal["user_id"]),
                observation=f"{code}; channel={channel_id}; message={message_id}; error={exc}",
            )
            return " Advertencia: la operacion se registro, pero no pude actualizar el mensaje original del cobro."
    async def open_ticket_modal(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
            return
        await interaction.response.send_modal(TicketCreateModal(self))

    async def create_ticket_interaction(
        self,
        interaction: discord.Interaction,
        subject: str,
        description: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await private_response(interaction, "Los tickets se crean desde el servidor.")
            return
        if not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
            return
        try:
            ticket = create_ticket(self.db, interaction.guild.id, interaction.user.id, subject, description)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        ticket_channel = await self.resolve_ticket_destination_channel(interaction.guild, interaction.channel)
        thread = await self.try_create_ticket_thread(interaction, str(ticket["code"]), ticket_channel)
        if thread is not None:
            set_ticket_thread(self.db, int(ticket["id"]), thread.id)
            ticket = get_ticket(self.db, interaction.guild.id, str(ticket["code"]))
        dm_sent = await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=interaction.user,
            action="ticket_creado",
            embed=self.ticket_user_confirmation_embed(interaction.guild, ticket),
        )
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Creacion de ticket",
            system="Banco",
            affected_user_id=interaction.user.id,
            observation=f"Ticket {ticket['code']} creado. DM={'ok' if dm_sent else 'fallo'}.",
        )
        await self.notify_ticket_created(interaction.guild, ticket, ticket_channel)
        evidence_target = thread.mention if thread is not None else getattr(ticket_channel, "mention", "este canal mencionando el ID del ticket")
        dm_note = "" if dm_sent else " No pude enviarte la confirmacion por DM, pero el ticket fue creado."
        await private_response(
            interaction,
            (
                f"Ticket `{ticket['code']}` creado con estado `{ticket['status']}`.{dm_note}\n"
                f"Si deseas adjuntar evidencias, envia las imagenes en {evidence_target}."
            ),
        )

    def log_ticket_channel_warning(self, guild_id: int, reason: str) -> None:
        logger.warning("Canal de tickets no configurado o invalido en guild %s: %s", guild_id, reason)
        log_action(
            self.db,
            guild_id,
            admin_id=None,
            action="Advertencia canal de tickets",
            system="Banco",
            observation=reason[:1800],
        )

    async def resolve_ticket_destination_channel(self, guild: discord.Guild, fallback_channel):
        raw_channel_id = self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY)
        if not raw_channel_id:
            self.log_ticket_channel_warning(guild.id, "ticket_channel_id no configurado; usando canal del Panel de Banco")
            return fallback_channel
        try:
            channel_id = int(raw_channel_id)
        except (TypeError, ValueError):
            self.log_ticket_channel_warning(guild.id, f"ticket_channel_id invalido: {raw_channel_id}; usando canal del Panel de Banco")
            return fallback_channel
        channel = guild.get_channel(channel_id) or self.bot.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except (discord.Forbidden, discord.NotFound, discord.HTTPException, AttributeError):
                channel = None
        if channel is None:
            self.log_ticket_channel_warning(guild.id, f"canal de tickets {channel_id} no existe; usando canal del Panel de Banco")
            return fallback_channel
        if not is_text_ticket_channel(channel):
            self.log_ticket_channel_warning(guild.id, f"canal de tickets {channel_id} no es un canal de texto; usando canal del Panel de Banco")
            return fallback_channel
        missing = ticket_channel_permission_errors(channel, guild)
        if missing:
            self.log_ticket_channel_warning(
                guild.id,
                f"canal de tickets {channel_id} sin permisos: {', '.join(missing)}; usando canal del Panel de Banco",
            )
            return fallback_channel
        return channel

    async def try_create_ticket_thread(self, interaction: discord.Interaction, code: str, channel):
        if channel is None or not callable(getattr(channel, "create_thread", None)):
            return None
        try:
            if isinstance(channel, discord.ForumChannel):
                thread, _message = await channel.create_thread(
                    name=f"{code}-evidencias",
                    content=(
                        f"Ticket `{code}` creado por {interaction.user.mention}. "
                        "Envia aqui las imagenes o evidencias relacionadas."
                    ),
                    reason="Ticket Banco G3NESYS",
                )
                return thread
            thread = await channel.create_thread(
                name=f"{code}-evidencias",
                type=discord.ChannelType.private_thread,
                invitable=False,
                auto_archive_duration=1440,
                reason="Ticket Banco G3NESYS",
            )
            await thread.add_user(interaction.user)

            if callable(getattr(thread, "send", None)):
                await thread.send(
                    f"Ticket `{code}` creado por {interaction.user.mention}. "
                    "Envia aqui las imagenes o evidencias relacionadas."
                )
            return thread
        except (discord.Forbidden, discord.HTTPException):
            return None

    def ticket_user_confirmation_embed(
        self,
        guild: discord.Guild,
        ticket,
    ) -> discord.Embed:
        embed = discord.Embed(
            title=f"🎫 Ticket {ticket['code']}",
            color=discord.Color.gold(),
        )

        thread_id = ticket["thread_id"]

        if thread_id:
            thread_link = f"https://discord.com/channels/{guild.id}/{thread_id}"

            embed.description = (
                "Hemos recibido correctamente tu solicitud.\n\n"
                "Si deseas agregar evidencia, imágenes, videos o cualquier "
                "información adicional, puedes hacerlo directamente en el hilo "
                "de atención de tu ticket.\n\n"
                f"🔗 **[Abrir el hilo de mi ticket]({thread_link})**\n\n"
                "Nuestro equipo administrativo dará seguimiento a tu solicitud "
                "lo antes posible."
            )
        else:
            embed.description = (
                "Hemos recibido correctamente tu solicitud.\n\n"
                "Nuestro equipo administrativo dará seguimiento a tu solicitud "
                "lo antes posible.\n\n"
                "En este momento no fue posible generar el enlace directo al hilo."
            )

        embed.add_field(
            name="Asunto",
            value=str(ticket["subject"])[:1024],
            inline=False,
        )

        embed.add_field(
            name="Estado inicial",
            value=str(ticket["status"]),
            inline=True,
        )

        embed.add_field(
            name="Fecha de creación",
            value=str(ticket["created_at"]),
            inline=True,
        )

        embed.set_footer(
            text=(
                "No es necesario crear otro ticket por el mismo asunto. "
                "Agrega toda la información dentro del hilo."
            )
        )

        return embed

    async def notify_ticket_created(self, guild: discord.Guild, ticket, ticket_channel) -> None:
        view = TicketAdminActionView(self, guild.id, str(ticket["code"]))
        self.bot.add_view(view)
        message = None
        if ticket_channel is not None and callable(getattr(ticket_channel, "send", None)):
            try:
                message = await ticket_channel.send(embed=self.ticket_admin_embed(guild, ticket), view=view)
            except (discord.Forbidden, discord.HTTPException):
                message = None
        if message is None:
            self.log_ticket_channel_warning(
                guild.id,
                f"no se pudo publicar el mensaje principal del ticket {ticket['code']} en el canal de tickets",
            )
            message = await send_admin_notification(
                self.db,
                guild=guild,
                category="general_admin",
                embed=self.ticket_admin_embed(guild, ticket),
                view=view,
            )
        if message is not None:
            set_ticket_notification(self.db, int(ticket["id"]), message.id)

    def ticket_list_text(self, rows) -> str:
        lines = ["**Tickets**"]
        for row in rows:
            assigned = f"<@{row['assigned_admin_id']}>" if row["assigned_admin_id"] else "Sin asignar"
            lines.append(
                f"`{row['code']}` | <@{row['user_id']}> | {row['subject']} | "
                f"{row['created_at']} | {row['status']} | {assigned} | "
                f"Resp: {row['message_count']} | Evid: {row['attachment_count']}"
            )
        return "\n".join(lines)[:1900]

    async def show_admin_tickets_menu(self, interaction: discord.Interaction) -> None:
        await private_response(interaction, "Panel de tickets:", view=TicketsAdminMenuView(self))

    async def show_ticket_detail_interaction(self, interaction: discord.Interaction, code: str) -> None:
        if interaction.guild is None or not is_admin_subject(self.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden consultar tickets.")
            return
        ticket = get_ticket(self.db, interaction.guild.id, code)
        if ticket is None:
            await private_response(interaction, "No encontre ese ticket.")
            return
        view = TicketAdminActionView(self, interaction.guild.id, str(ticket["code"]))
        self.bot.add_view(view)
        await private_response(interaction, "Detalle del ticket:", embed=self.ticket_admin_embed(interaction.guild, ticket), view=view)

    async def show_tickets_by_user(self, interaction: discord.Interaction, user_id: int) -> None:
        if interaction.guild is None or not is_admin_subject(self.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden consultar tickets.")
            return
        rows = search_tickets_by_user(self.db, interaction.guild.id, user_id, limit=10)
        if not rows:
            await private_response(interaction, "No encontre tickets para ese usuario.")
            return
        await private_response(interaction, self.ticket_list_text(rows), view=TicketListView(self, rows))

    async def reply_ticket_interaction(self, interaction: discord.Interaction, guild_id: int, code: str, content: str) -> None:
        ticket = await self.require_ticket_admin_action(interaction, guild_id, code, allow_closed=False)
        if ticket is None:
            return
        member = interaction.guild.get_member(int(ticket["user_id"]))
        dm_sent = False
        dm_error = None
        if member is not None:
            dm_sent = await send_dm_safe(
                self.db,
                guild_id=guild_id,
                user=member,
                action="respuesta_ticket",
                content=f"Respuesta al ticket `{code}`:\n\n{content[:1800]}",
            )
            if not dm_sent:
                dm_error = "No se pudo enviar DM al usuario."
        else:
            dm_error = "Usuario no encontrado en el servidor."
        old_status = str(ticket["status"])
        new_status = TICKET_IN_PROGRESS if old_status == TICKET_PENDING else old_status
        assign_ticket(self.db, int(ticket["id"]), interaction.user.id)
        add_ticket_message(
            self.db,
            int(ticket["id"]),
            author_id=interaction.user.id,
            message_type=TICKET_ADMIN_REPLY,
            content=content,
            dm_sent=dm_sent,
            dm_error=dm_error,
            old_status=old_status,
            new_status=new_status if new_status != old_status else None,
        )
        log_action(self.db, guild_id, admin_id=interaction.user.id, action="Respuesta enviada a ticket", system="Banco", affected_user_id=int(ticket["user_id"]), observation=f"Ticket {code}. DM={'ok' if dm_sent else 'fallo'}.")
        await self.refresh_ticket_message(interaction, code)
        await private_response(interaction, f"Respuesta guardada para `{code}`." + (" Advertencia: no pude enviar DM." if not dm_sent else ""))

    async def followup_ticket_interaction(self, interaction: discord.Interaction, guild_id: int, code: str, *, visibility: str, content: str, new_status: str | None) -> None:
        ticket = await self.require_ticket_admin_action(interaction, guild_id, code, allow_closed=False)
        if ticket is None:
            return
        old_status = str(ticket["status"])
        target_status = new_status or old_status
        try:
            validate_ticket_status(target_status)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        visible = visibility.strip().casefold() in {"usuario", "visible", "user"}
        dm_sent = None
        dm_error = None
        message_type = TICKET_INTERNAL_NOTE
        if visible:
            message_type = TICKET_ADMIN_REPLY
            member = interaction.guild.get_member(int(ticket["user_id"]))
            if member is not None:
                dm_sent = await send_dm_safe(self.db, guild_id=guild_id, user=member, action="seguimiento_ticket", content=f"Actualizacion del ticket `{code}`:\n\n{content[:1800]}")
                if not dm_sent:
                    dm_error = "No se pudo enviar DM al usuario."
            else:
                dm_sent = False
                dm_error = "Usuario no encontrado en el servidor."
        assign_ticket(self.db, int(ticket["id"]), interaction.user.id)
        add_ticket_message(
            self.db,
            int(ticket["id"]),
            author_id=interaction.user.id,
            message_type=message_type,
            content=content,
            dm_sent=dm_sent,
            dm_error=dm_error,
            old_status=old_status if target_status != old_status else None,
            new_status=target_status if target_status != old_status else None,
        )
        log_action(self.db, guild_id, admin_id=interaction.user.id, action="Seguimiento de ticket", system="Banco", affected_user_id=int(ticket["user_id"]), observation=f"Ticket {code}; tipo={'visible' if visible else 'interno'}; estado={target_status}.")
        await self.refresh_ticket_message(interaction, code)
        warning = " Advertencia: no pude enviar DM." if visible and not dm_sent else ""
        await private_response(interaction, f"Seguimiento guardado para `{code}`.{warning}")

    async def change_ticket_status_interaction(self, interaction: discord.Interaction, guild_id: int, code: str, status: str) -> None:
        ticket = await self.require_ticket_admin_action(interaction, guild_id, code, allow_closed=(status == TICKET_IN_PROGRESS))
        if ticket is None:
            return
        change_ticket_status(self.db, int(ticket["id"]), admin_id=interaction.user.id, new_status=status)
        assign_ticket(self.db, int(ticket["id"]), interaction.user.id)
        log_action(self.db, guild_id, admin_id=interaction.user.id, action=f"Ticket actualizado a {status}", system="Banco", affected_user_id=int(ticket["user_id"]), observation=f"Ticket {code}.")
        await self.refresh_ticket_message(interaction, code)
        await private_response(interaction, f"Ticket `{code}` actualizado a `{status}`.")

    async def require_ticket_admin_action(self, interaction: discord.Interaction, guild_id: int, code: str, *, allow_closed: bool):
        if interaction.guild is None or interaction.guild.id != guild_id:
            await private_response(interaction, "Este ticket pertenece a otro servidor.")
            return None
        if not is_admin_subject(self.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden gestionar tickets.")
            return None
        ticket = get_ticket(self.db, guild_id, code)
        if ticket is None:
            await private_response(interaction, "No encontre ese ticket.")
            return None
        if ticket["status"] == TICKET_CLOSED and not allow_closed:
            await private_response(interaction, "El ticket esta cerrado. Reabrelo antes de responder o dar seguimiento.")
            return None
        return ticket

    async def refresh_ticket_message(self, interaction: discord.Interaction, code: str) -> None:
        ticket = get_ticket(self.db, interaction.guild.id, code)
        if ticket is None:
            return
        try:
            if interaction.message is not None:
                await interaction.message.edit(
                    embed=self.ticket_admin_embed(interaction.guild, ticket),
                    view=TicketAdminActionView(self, interaction.guild.id, code),
                )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            pass

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None or not message.attachments:
            return
        ticket = find_ticket_for_attachment(
            self.db,
            message.guild.id,
            message.author.id,
            message.channel.id,
            message.content,
        )
        if ticket is None:
            return
        for attachment in message.attachments:
            add_attachment(
                self.db,
                int(ticket["id"]),
                author_id=message.author.id,
                url=attachment.url,
                filename=attachment.filename,
                content_type=getattr(attachment, "content_type", None),
                message_id=message.id,
                channel_id=message.channel.id,
            )
        log_action(
            self.db,
            message.guild.id,
            admin_id=message.author.id,
            action="Archivos adjuntados a ticket",
            system="Banco",
            affected_user_id=message.author.id,
            observation=f"Ticket {ticket['code']}; adjuntos={len(message.attachments)}.",
        )

    async def send_delegated_withdrawal_dm(self, guild: discord.Guild, code: str, officer: discord.Member, note: str = "") -> None:
        row = self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?", (guild.id, code))
        if row is None:
            return
        user = guild.get_member(int(row["user_id"]))
        embed = discord.Embed(title="Pago delegado", color=discord.Color.gold())
        embed.add_field(name="Solicitud", value=str(code), inline=True)
        embed.add_field(name="Usuario", value=f"<@{row['user_id']}> ({row['user_id']})", inline=False)
        embed.add_field(name="Cantidad pendiente", value=format_amount(int(row["amount_requested"]) - int(row["amount_liquidated"] or 0)), inline=True)
        embed.add_field(name="Motivo", value=str(row["reason"] or "Sin motivo")[:1024], inline=False)
        embed.add_field(name="Lugar", value=str(row["payment_place"] or "Sin lugar"), inline=True)
        embed.add_field(name="Horario", value=str(row["payment_schedule"] or "Sin horario"), inline=True)
        embed.add_field(name="Admin", value=f"<@{row['delegated_by']}>" if row["delegated_by"] else "Sin admin", inline=True)
        if note:
            embed.add_field(name="Nota", value=note[:1024], inline=False)
        self.bot.add_view(OfficerWithdrawalView(self, guild.id, code))
        officer_sent = await send_dm_safe(self.db, guild_id=guild.id, user=officer, action="cobro_delegado_oficial", embed=embed, view=OfficerWithdrawalView(self, guild.id, code))
        if user is not None:
            user_sent = await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="cobro_delegado_usuario",
                content=(
                    f"Tu solicitud de cobro `{code}` fue asignada. Tu pago lo realizara {officer.mention} "
                    f"el dia/horario `{row['payment_schedule']}`, en {row['payment_place']}."
                    + (f"\nNota: {note}" if note else "")
                ),
            )
            if not user_sent:
                log_action(self.db, guild.id, admin_id=int(row["delegated_by"] or officer.id), action="Fallo DM usuario cobro delegado", system="Banco", affected_user_id=user.id, observation=code)
        if not officer_sent:
            log_action(self.db, guild.id, admin_id=int(row["delegated_by"] or officer.id), action="Fallo DM oficial cobro delegado", system="Banco", affected_user_id=officer.id, observation=code)

    def transfer_fee_percent(self, guild_id: int) -> float:
        return parse_percent_setting(
            self.db.get_setting(guild_id, "transfer_fee_percent", "3"),
            3,
        )

    def transfer_confirmation_text(
        self,
        receiver: discord.Member,
        amount: int,
        fee_percent: float,
    ) -> str:
        fee = transfer_fee_amount(amount, fee_percent)
        net_amount = amount - fee
        return "\n".join(
            [
                "Confirma la transferencia:",
                f"Destinatario: {receiver.mention}",
                f"Monto a transferir: **{format_amount(amount)}**",
                f"Comision aplicada ({format_percent(fee_percent)}%): **{format_amount(fee)}**",
                f"Total recibido por destinatario: **{format_amount(net_amount)}**",
                f"Total descontado de tu saldo: **{format_amount(amount)}**",
            ]
        )

    async def perform_member_transfer(
        self,
        guild: discord.Guild,
        sender: discord.Member,
        receiver: discord.Member,
        amount: int,
        fee_percent: float,
    ):
        movement_id = transfer_between_members(
            self.db,
            guild.id,
            sender_id=sender.id,
            receiver_id=receiver.id,
            amount=amount,
            fee_percent=fee_percent,
        )
        movement = self.db.fetch_one(
            "SELECT * FROM movements WHERE guild_id = ? AND id = ?",
            (guild.id, movement_id),
        )
        await send_dm_safe(
            self.db,
            guild_id=guild.id,
            user=receiver,
            action="transferencia_recibida",
            content=(
                f"Has recibido una transferencia de {sender.display_name}.\n\n"
                f"{movement_history_line(movement)}"
            ),
        )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="general_admin",
            content=f"Transferencia: {movement_history_line(movement)}",
        )
        return movement

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
            fee_percent = self.transfer_fee_percent(ctx.guild.id)
            movement = await self.perform_member_transfer(
                ctx.guild,
                ctx.author,
                member,
                amount,
                fee_percent,
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(
            f"Transferencia realizada.\n{movement_history_line(movement)}",
            mention_author=False,
        )

    @commands.command(name="cobrar")
    async def cobrar(self, ctx: commands.Context, amount_raw: str, *, reason: str = "") -> None:
        if not isinstance(ctx.author, discord.Member) or not has_bank_access(self.db, ctx.author):
            await ctx.reply("Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada para solicitar cobro.", mention_author=False)
            return
        await self.create_withdrawal_and_notify(ctx, ctx.author, amount_raw, reason)

    async def show_balance_interaction(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
            return
        await dm_or_private(
            self,
            interaction,
            self.balance_text(interaction.guild.id, interaction.user),
            "consultar_saldo",
        )

    async def show_statement_interaction(self, interaction: discord.Interaction) -> None:
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
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
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
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
            await private_response(interaction, "Necesitas rol MIEMBRO G3NESYS, INVITADO o alianza configurada.")
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
            fee_percent = self.transfer_fee_percent(interaction.guild.id)
            fee = transfer_fee_amount(amount, fee_percent)
            net_amount = amount - fee
            if net_amount <= 0:
                raise ValueError("La comision consume todo el monto.")
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(
            interaction,
            self.transfer_confirmation_text(receiver, amount, fee_percent),
            view=TransferConfirmationView(
                self,
                guild_id=interaction.guild.id,
                sender_id=interaction.user.id,
                receiver_id=receiver.id,
                amount=amount,
                fee_percent=fee_percent,
            ),
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
        view = self.withdrawal_admin_view(row)
        if view is not None:
            self.bot.add_view(view)
        message = await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            embed=self.withdrawal_admin_embed(guild, row),
            view=view,
        )
        if message is not None:
            self.db.execute(
                """
                UPDATE withdrawals
                SET notification_channel_id = ?, notification_message_id = ?, updated_at = COALESCE(updated_at, ?)
                WHERE guild_id = ? AND code = ?
                """,
                (message.channel.id, message.id, utc_now_iso(), guild.id, code),
            )
        else:
            log_action(
                self.db,
                guild.id,
                admin_id=None,
                action="Fallo publicar embed cobro",
                system="Banco",
                affected_user_id=int(row["user_id"]),
                observation=code,
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
