from __future__ import annotations

from pathlib import Path

import discord
from discord.ext import commands

from ..constants import (
    ADMIN_PANEL_IMAGE,
    PAYOUT_APPROVED,
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
from ..services.callers import (
    CallerRemovalNoticeView,
    authorize_caller,
    caller_ranking,
    caller_welcome_embed,
    is_caller_penalized,
    remove_caller_penalty,
    revoke_caller,
)
from ..services.economy import (
    adjust_user_balance,
    create_movement,
    deposit_to_user_from_treasury,
    ensure_treasury,
    get_account,
    movement_history_line,
    pending_fines_total,
    register_guild_expense,
    register_guild_income,
)
from ..services.fines import cancel_fine, create_fine
from ..services.notifications import (
    ADMIN_CHANNEL_SETTINGS,
    send_admin_notification,
    send_dm_safe,
)
from ..services.payout_audit import log_payout_action, payout_audit_text
from ..services.reports import create_admin_report
from ..utils import format_amount, parse_channel_id, parse_int_amount, utc_now_iso


NOTIFICATION_CHANNEL_CATEGORIES = (
    ("splits", "Splits pendientes por aprobar", "📋"),
    ("withdrawals", "Solicitudes de cobro", "💳"),
    ("registration", "Registro", "📝"),
    ("activities", "Actividades con validación admin", "⚔️"),
    ("fines", "Multas o sanciones", "🚨"),
    ("general_admin", "Otras notificaciones admin", "🔔"),
)
NOTIFICATION_CATEGORY_MAP = {
    category: (label, emoji)
    for category, label, emoji in NOTIFICATION_CHANNEL_CATEGORIES
}


def normalize_admin_message(value: str | None) -> str:
    return (value or "").strip()[:600]


def admin_message_block(message: str) -> str:
    return f"\n\n**Indicaciones del admin:**\n{message}" if message else ""


async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


async def dm_or_private(cog: "Admin", interaction: discord.Interaction, content: str, action: str) -> None:
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


class ConfirmAdminActionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, action: str, payload: dict):
        super().__init__(timeout=120)
        self.cog = cog
        self.admin_id = admin_id
        self.action = action
        self.payload = payload

    @discord.ui.button(label="Confirmar", emoji="✅", style=discord.ButtonStyle.danger)
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

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary)
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


class ApproveWithdrawalModal(discord.ui.Modal, title="Aprobar cobro"):
    code = discord.ui.TextInput(label="Codigo de cobro", placeholder="COBRO-000001")
    admin_message = discord.ui.TextInput(
        label="Indicaciones para el usuario (opcional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Ej.: Te pago en la isla de Martlock a las 00 UTC.",
    )

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden aprobar cobros.")
            return
        code = str(self.code.value).strip().upper()
        try:
            await self.cog.approve_withdrawal(
                interaction.guild,
                code,
                interaction.user.id,
                normalize_admin_message(str(self.admin_message.value)),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, f"Solicitud `{code}` aprobada. Ya puede liquidarse.")


class LiquidateWithdrawalModal(discord.ui.Modal, title="Liquidar cobro"):
    code = discord.ui.TextInput(label="Codigo de cobro", placeholder="COBRO-000001")
    amount = discord.ui.TextInput(label="Monto a liquidar", placeholder="1000000")
    admin_message = discord.ui.TextInput(
        label="Indicaciones para el usuario (opcional)",
        required=False,
        style=discord.TextStyle.paragraph,
        max_length=600,
        placeholder="Ej.: Te pago en la isla de Martlock a las 00 UTC.",
    )

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden liquidar cobros.")
            return
        try:
            amount = parse_int_amount(str(self.amount.value))
            result = await self.cog.liquidate_withdrawal(
                interaction.guild,
                str(self.code.value).strip().upper(),
                amount,
                interaction.user.id,
                normalize_admin_message(str(self.admin_message.value)),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(interaction, result)


class WithdrawalAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=600)
        self.cog = cog

    @discord.ui.button(label="Aprobar cobro", emoji="✅", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden aprobar cobros.")
            return
        await interaction.response.send_modal(ApproveWithdrawalModal(self.cog))

    @discord.ui.button(label="Liquidar cobro", emoji="💵", style=discord.ButtonStyle.primary)
    async def liquidate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden liquidar cobros.")
            return
        await interaction.response.send_modal(LiquidateWithdrawalModal(self.cog))


class CreateFineModal(discord.ui.Modal, title="Crear multa"):
    user = discord.ui.TextInput(label="Usuario (ID o mencion)")
    amount = discord.ui.TextInput(label="Monto", placeholder="200000")
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden crear multas.")
            return
        try:
            user_id = parse_channel_id(str(self.user.value))
            if user_id is None:
                raise ValueError("No pude leer el usuario.")
            member = interaction.guild.get_member(user_id)
            if member is None:
                raise ValueError("No encontre al usuario en el servidor.")
            amount = parse_int_amount(str(self.amount.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await private_response(
            interaction,
            (
                "¿Confirmas esta operacion?\n"
                f"Crear multa a {member.mention} por {format_amount(amount)}.\n"
                f"Motivo: {self.reason.value}"
            ),
            view=ConfirmAdminActionView(
                self.cog,
                admin_id=interaction.user.id,
                action="create_fine",
                payload={
                    "user_id": member.id,
                    "amount": amount,
                    "reason": str(self.reason.value),
                },
            ),
        )


class CancelFineModal(discord.ui.Modal, title="Cancelar multa"):
    fine_code = discord.ui.TextInput(label="ID de multa", placeholder="MULTA-000001")
    reason = discord.ui.TextInput(label="Motivo de cancelacion", style=discord.TextStyle.paragraph)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden cancelar multas.")
            return
        fine_code = str(self.fine_code.value).strip().upper()
        fine = self.cog.db.fetch_one(
            "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
            (interaction.guild.id, fine_code),
        )
        if fine is None:
            await private_response(interaction, "No encontre esa multa.")
            return
        await private_response(
            interaction,
            (
                "¿Confirmas esta operacion?\n"
                f"Cancelar multa `{fine_code}` de <@{fine['user_id']}>.\n"
                f"Motivo: {self.reason.value}"
            ),
            view=ConfirmAdminActionView(
                self.cog,
                admin_id=interaction.user.id,
                action="cancel_fine",
                payload={
                    "fine_code": fine_code,
                    "reason": str(self.reason.value),
                },
            ),
        )


class FineAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar multas.")
        return False

    @discord.ui.button(label="Crear multa", emoji="🚨", style=discord.ButtonStyle.danger)
    async def create_fine_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(CreateFineModal(self.cog))

    @discord.ui.button(label="Cancelar multa", emoji="🟢", style=discord.ButtonStyle.success)
    async def cancel_fine_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(CancelFineModal(self.cog))

    @discord.ui.button(label="Pendientes", emoji="📋", style=discord.ButtonStyle.secondary)
    async def pending_fines_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.pending_fines_text(interaction.guild.id))


class CallerMemberSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        labels = {
            "add": "agregar como caller",
            "remove": "eliminar como caller",
            "unpenalize": "quitar de penalizacion",
        }
        super().__init__(
            placeholder=f"Selecciona a quien quieres {labels[action]}",
            min_values=1,
            max_values=1,
        )
        self.cog = cog
        self.action = action
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        if interaction.guild is None:
            await private_response(interaction, "Este menu solo funciona dentro del servidor.")
            return
        selected = self.values[0]
        member = selected if isinstance(selected, discord.Member) else interaction.guild.get_member(selected.id)
        if member is None:
            try:
                member = await interaction.guild.fetch_member(selected.id)
            except discord.HTTPException:
                member = None
        if member is None:
            await private_response(interaction, "No encontre a ese usuario dentro del servidor.")
            return
        if member.bot and self.action == "add":
            await private_response(interaction, "Un bot no puede registrarse como caller.")
            return
        if self.action == "add":
            await self.cog.add_caller_interaction(interaction, member)
        elif self.action == "remove":
            await self.cog.remove_caller_interaction(interaction, member)
        else:
            await self.cog.remove_caller_penalty_interaction(interaction, member)


class CallerSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(timeout=180)
        self.add_item(CallerMemberSelect(cog, action=action, admin_id=admin_id))


class CallersAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden gestionar callers.")
        return False

    @discord.ui.button(label="Lista de callers", emoji="🏆", style=discord.ButtonStyle.primary)
    async def list_callers(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        embeds = self.cog.caller_ranking_embeds(interaction.guild)
        sent = True
        for embed in embeds:
            delivered = await send_dm_safe(
                self.cog.db,
                guild_id=interaction.guild.id,
                user=interaction.user,
                action="ranking_callers",
                embed=embed,
            )
            if not delivered:
                sent = False
                break
        if sent:
            await private_response(interaction, "Te envie la lista y el ranking de callers por DM.")
        else:
            await private_response(
                interaction,
                "No pude enviarte un DM. Te muestro la primera pagina aqui.",
                embed=embeds[0],
            )

    @discord.ui.button(label="Agregar caller", emoji="➕", style=discord.ButtonStyle.success)
    async def add_caller(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al nuevo caller:",
                view=CallerSelectionView(self.cog, action="add", admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Eliminar caller", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_caller(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al caller que quieres eliminar. Despues podras elegir si envias un aviso:",
                view=CallerSelectionView(self.cog, action="remove", admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Penalizados", emoji="⚠️", style=discord.ButtonStyle.secondary)
    async def penalties(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.caller_penalties_text(interaction.guild.id),
                "penalizaciones_callers",
            )

    @discord.ui.button(label="Quitar penalizacion", emoji="🟢", style=discord.ButtonStyle.success)
    async def remove_penalty(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al caller cuya penalizacion quieres retirar:",
                view=CallerSelectionView(
                    self.cog,
                    action="unpenalize",
                    admin_id=interaction.user.id,
                ),
            )


class PayoutReasonModal(discord.ui.Modal):
    reason = discord.ui.TextInput(label="Motivo", style=discord.TextStyle.paragraph, max_length=600)

    def __init__(self, cog: "Admin", code: str, target_status: str):
        title = "Rechazar Split" if target_status == PAYOUT_REJECTED else "Solicitar correccion"
        super().__init__(title=title, timeout=180)
        self.cog = cog
        self.code = code
        self.target_status = target_status

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden hacer esto.")
            return
        try:
            await self.cog.update_payout_status(
                interaction.guild,
                self.code,
                self.target_status,
                interaction.user.id,
                str(self.reason.value),
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        label = "rechazado" if self.target_status == PAYOUT_REJECTED else "marcado para correccion"
        await private_response(interaction, f"Split `{self.code}` {label}.")


class PayoutReviewView(discord.ui.View):
    def __init__(self, cog: "Admin", code: str):
        super().__init__(timeout=None)
        self.cog = cog
        self.code = code
        self.add_button("Aprobar", "approve", "✅", discord.ButtonStyle.success)
        self.add_button("Rechazar", "reject", "❌", discord.ButtonStyle.danger)
        self.add_button("Corregir", "correction", "🛠️", discord.ButtonStyle.secondary)
        self.add_button("Ver detalle", "detail", "📋", discord.ButtonStyle.primary)
        self.add_button("Auditoria", "audit", "🧾", discord.ButtonStyle.secondary)

    def add_button(self, label: str, action: str, emoji: str, style: discord.ButtonStyle) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:admin:payout:{action}:{self.code}",
        )
        button.callback = self.handle_button
        self.add_item(button)

    async def handle_button(self, interaction: discord.Interaction) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden revisar Splits.")
            return
        custom_id = str(interaction.data["custom_id"])
        action = custom_id.split(":")[3]
        if action == "approve":
            await private_response(
                interaction,
                f"¿Confirmas esta operacion?\nAprobar Split `{self.code}` y depositar saldos.",
                view=ConfirmAdminActionView(
                    self.cog,
                    admin_id=interaction.user.id,
                    action="approve_payout",
                    payload={"code": self.code},
                ),
            )
            return
        if action == "reject":
            await interaction.response.send_modal(PayoutReasonModal(self.cog, self.code, PAYOUT_REJECTED))
            return
        if action == "correction":
            await interaction.response.send_modal(PayoutReasonModal(self.cog, self.code, PAYOUT_CORRECTION))
            return
        if action == "detail":
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.payout_detail_text(interaction.guild.id, self.code),
                "detalle_reparto_admin",
            )
            return
        if action == "audit":
            payout = self.cog.db.fetch_one(
                "SELECT id FROM payouts WHERE guild_id = ? AND code = ?",
                (interaction.guild.id, self.code),
            )
            if payout is None:
                await private_response(interaction, "No encontre ese Split.")
                return
            await dm_or_private(
                self.cog,
                interaction,
                payout_audit_text(
                    self.cog.db,
                    interaction.guild.id,
                    int(payout["id"]),
                ),
                "auditoria_split_admin",
            )


class SplitsAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden revisar Splits.")
        return False

    @discord.ui.button(
        label="Pendientes de aprobación",
        emoji="⏳",
        style=discord.ButtonStyle.primary,
        custom_id="g3n:admin:splits:pending",
    )
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.pending_payouts_text(interaction.guild.id),
                "splits_pendientes_admin",
            )

    @discord.ui.button(
        label="Aprobados",
        emoji="✅",
        style=discord.ButtonStyle.success,
        custom_id="g3n:admin:splits:approved",
    )
    async def approved(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.approved_payouts_text(interaction.guild.id),
                "splits_aprobados_admin",
            )

    @discord.ui.button(
        label="Lista general",
        emoji="📚",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:admin:splits:all",
    )
    async def all_splits(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.all_payouts_text(interaction.guild.id),
                "splits_lista_general_admin",
            )


class NotificationChannelConfigView(discord.ui.View):
    def __init__(self, cog: "Admin", category: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.category = category
        self.label = NOTIFICATION_CATEGORY_MAP[category][0]
        self.setting_key = ADMIN_CHANNEL_SETTINGS[category][0]
        self.channel_select = discord.ui.ChannelSelect(
            placeholder=f"Selecciona canal para {self.label}"[:150],
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
        await private_response(
            interaction,
            "Solo admins autorizados pueden configurar notificaciones.",
        )
        return False

    async def save_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> None:
        self.cog.db.set_setting(
            interaction.guild.id,
            self.setting_key,
            str(channel_id),
        )
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar canal de notificaciones",
            system="Configuracion",
            observation=f"{self.category}: {channel_id}",
        )
        await private_response(
            interaction,
            (
                f"Canal de **{self.label}** actualizado a <#{channel_id}>.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )

    async def select_channel(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        channel = self.channel_select.values[0]
        await self.save_channel(interaction, int(channel.id))

    @discord.ui.button(
        label="Usar canal actual",
        emoji="📍",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def use_current(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        channel = interaction.channel
        if channel is None or not callable(getattr(channel, "send", None)):
            await private_response(interaction, "Este canal no admite notificaciones.")
            return
        await self.save_channel(interaction, int(channel.id))

    @discord.ui.button(
        label="Usar respaldo",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def clear_specific(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(interaction.guild.id, self.setting_key, "")
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Restaurar respaldo de notificaciones",
            system="Configuracion",
            observation=self.category,
        )
        await private_response(
            interaction,
            (
                f"**{self.label}** volverá a usar su canal de respaldo.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )


class NotificationCategorySelect(discord.ui.Select):
    def __init__(self, cog: "Admin"):
        self.cog = cog
        super().__init__(
            placeholder="Selecciona el tipo de notificación",
            min_values=1,
            max_values=1,
            options=[
                discord.SelectOption(
                    label=label,
                    value=category,
                    emoji=emoji,
                )
                for category, label, emoji in NOTIFICATION_CHANNEL_CATEGORIES
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar notificaciones.",
            )
            return
        category = self.values[0]
        label = NOTIFICATION_CATEGORY_MAP[category][0]
        current = self.cog.db.get_setting(
            interaction.guild.id,
            ADMIN_CHANNEL_SETTINGS[category][0],
        )
        current_text = f"<#{current}>" if current else "sin canal específico"
        await private_response(
            interaction,
            (
                f"Configura **{label}**. Actualmente: {current_text}.\n"
                "Selecciona un canal, usa el canal actual o restaura el respaldo."
            ),
            view=NotificationChannelConfigView(self.cog, category),
        )


class NotificationsAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog
        self.add_item(NotificationCategorySelect(cog))

    @discord.ui.button(
        label="Ver configuración",
        emoji="👁️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def show_configuration(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden ver las notificaciones.",
            )
            return
        await private_response(
            interaction,
            self.cog.notification_settings_text(interaction.guild.id),
        )


class AdminPanelView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=None)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False

    @discord.ui.button(label="Ver Tesoreria", emoji="💰", style=discord.ButtonStyle.primary, custom_id="g3n:admin:treasury", row=0)
    async def treasury(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.treasury_text(interaction.guild.id), "tesoreria_panel")

    @discord.ui.button(label="Registrar Ingreso", emoji="📥", style=discord.ButtonStyle.success, custom_id="g3n:admin:income", row=0)
    async def income(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(IncomeModal(self.cog))

    @discord.ui.button(label="Registrar Egreso", emoji="📤", style=discord.ButtonStyle.danger, custom_id="g3n:admin:expense", row=0)
    async def expense(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(ExpenseModal(self.cog))

    @discord.ui.button(label="Depositar a Usuario", emoji="🪙", style=discord.ButtonStyle.success, custom_id="g3n:admin:deposit", row=0)
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(DepositModal(self.cog))

    @discord.ui.button(label="Revisar Splits", emoji="📋", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:payouts", row=1)
    async def payouts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona la lista de Splits que deseas consultar:",
                view=SplitsAdminView(self.cog),
            )

    @discord.ui.button(label="Multas", emoji="🚨", style=discord.ButtonStyle.danger, custom_id="g3n:admin:fines", row=1)
    async def fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Panel de multas:",
                view=FineAdminView(self.cog),
            )

    @discord.ui.button(label="Solicitudes Cobro", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawals", row=1)
    async def withdrawals(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.withdrawals_text(interaction.guild.id),
                view=WithdrawalAdminView(self.cog),
            )

    @discord.ui.button(label="Estado de Cuenta", emoji="👤", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:statement", row=1)
    async def statement(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(UserStatementModal(self.cog))

    @discord.ui.button(label="Edo. Cta. Gremio", emoji="📜", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:history", row=2)
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.history_text(interaction.guild.id), "historial_panel")

    @discord.ui.button(label="Historial de liquidaciones", emoji="🧾", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:liquidation_history", row=2)
    async def liquidation_history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.liquidation_history_text(interaction.guild.id),
                "historial_liquidaciones_admin",
            )

    @discord.ui.button(label="Rankings", emoji="🏆", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:rankings", row=2)
    async def rankings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.rankings_text(interaction.guild.id), "rankings_panel")

    @discord.ui.button(label="Reportes", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:reports", row=2)
    async def reports(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.defer(ephemeral=True)
            path = self.cog.create_report(interaction.guild.id)
            await interaction.followup.send(
                "Reporte administrativo integral generado.",
                file=discord.File(path),
                ephemeral=True,
            )

    @discord.ui.button(label="Auditoria", emoji="🔍", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:audit", row=2)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.audit_text(interaction.guild.id), "auditoria_panel")

    @discord.ui.button(label="Callers", emoji="📣", style=discord.ButtonStyle.primary, custom_id="g3n:admin:callers", row=3)
    async def callers(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            embed = discord.Embed(
                title="📣 Gestion de Callers G3NESYS",
                description=(
                    "Consulta el ranking o administra quienes pueden dirigir actividades.\n\n"
                    "**Puntuacion:** +10 por actividad completada, +2 por asistencia, "
                    "-4 por cancelacion con composicion completa y -6 por ausencia. "
                    "Las cancelaciones por cupos incompletos no restan. Al llegar a -14, "
                    "el acceso de caller queda suspendido."
                ),
                color=discord.Color.magenta(),
            )
            await private_response(interaction, "Menu de callers:", embed=embed, view=CallersAdminView(self.cog))

    @discord.ui.button(label="Configuracion", emoji="⚙️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:config", row=3)
    async def config(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, "Usa `!config_ver`, comandos `!canal_*_set`, `!caller_set` y `!economia_set`.")

    @discord.ui.button(label="Notificaciones", emoji="🔔", style=discord.ButtonStyle.primary, custom_id="g3n:admin:notifications", row=3)
    async def notifications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.notification_settings_text(interaction.guild.id),
                view=NotificationsAdminView(self.cog),
            )


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db

    async def cog_load(self) -> None:
        self.bot.add_view(AdminPanelView(self))
        rows = self.db.fetch_all(
            """
            SELECT DISTINCT code
            FROM payouts
            WHERE status = ? AND sent_to_admin_at IS NOT NULL
            """,
            (PAYOUT_PENDING,),
        )
        for row in rows:
            self.bot.add_view(PayoutReviewView(self, row["code"]))

    def build_payout_review_view(self, code: str) -> PayoutReviewView:
        return PayoutReviewView(self, code)

    async def add_caller_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        if is_caller_penalized(self.db, interaction.guild.id, member.id):
            await private_response(
                interaction,
                f"{member.mention} tiene una penalizacion activa. "
                "Retirala primero desde `Quitar penalizacion`.",
            )
            return
        created = authorize_caller(
            self.db,
            interaction.guild.id,
            member.id,
            interaction.user.id,
        )
        if not created:
            await private_response(interaction, f"{member.mention} ya es caller autorizado.")
            return
        delivered = await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=member,
            action="bienvenida_caller",
            embed=caller_welcome_embed(interaction.guild.name),
        )
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Agregar caller",
            affected_user_id=member.id,
            system="Callers",
            observation="Caller autorizado desde el panel administrativo.",
        )
        dm_status = "Le envie la bienvenida formal por DM." if delivered else "No pude enviarle DM, pero el acceso quedo activo."
        await private_response(interaction, f"📣 {member.mention} ahora es caller autorizado. {dm_status}")

    async def remove_caller_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        removed = revoke_caller(self.db, interaction.guild.id, member.id)
        if not removed:
            await private_response(interaction, f"{member.mention} no estaba registrado como caller.")
            return
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Eliminar caller",
            affected_user_id=member.id,
            system="Callers",
            observation="Acceso de caller retirado desde el panel administrativo; aviso opcional pendiente.",
        )
        await private_response(
            interaction,
            f"➖ {member.mention} ya no es caller autorizado. ¿Deseas enviarle un aviso amistoso?",
            view=CallerRemovalNoticeView(
                self.db,
                guild_id=interaction.guild.id,
                guild_name=interaction.guild.name,
                admin_id=interaction.user.id,
                member=member,
            ),
        )

    async def remove_caller_penalty_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        removed = remove_caller_penalty(
            self.db,
            interaction.guild.id,
            member.id,
            interaction.user.id,
        )
        if not removed:
            await private_response(interaction, f"{member.mention} no tiene una penalizacion activa.")
            return
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Retirar penalizacion de caller",
            affected_user_id=member.id,
            system="Callers",
            observation="Acceso de caller rehabilitado por un administrador.",
        )
        authorized = self.db.fetch_one(
            "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, member.id),
        )
        result = (
            f"🟢 Se retiro la penalizacion de {member.mention}. Ya puede volver a usar las funciones de caller."
            if authorized is not None
            else f"🟢 Se retiro la penalizacion de {member.mention}. Debes agregarlo nuevamente si volvera a ser caller."
        )
        await private_response(
            interaction,
            result,
        )

    def caller_penalties_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT user_id, score_at_penalty, reason, penalized_at
            FROM caller_penalties
            WHERE guild_id = ? AND active = 1
            ORDER BY score_at_penalty ASC, penalized_at ASC
            LIMIT 30
            """,
            (guild_id,),
        )
        if not rows:
            return "🟢 **Callers penalizados**\nNo hay penalizaciones activas."
        lines = ["⚠️ **Callers penalizados**"]
        for index, row in enumerate(rows, start=1):
            lines.append(
                f"{index}. <@{row['user_id']}> • **{row['score_at_penalty']} puntos** • {row['reason']}"
            )
        lines.append("Usa `🟢 Quitar penalizacion` en el menu de Callers para rehabilitar a alguien.")
        return "\n".join(lines)

    def caller_ranking_embeds(self, guild: discord.Guild) -> list[discord.Embed]:
        rows = caller_ranking(self.db, guild.id)
        if not rows:
            return [
                discord.Embed(
                    title="📣 Callers de G3NESYS",
                    description="Todavia no hay callers autorizados.",
                    color=discord.Color.magenta(),
                )
            ]
        pages: list[discord.Embed] = []
        page_size = 5
        total_pages = (len(rows) + page_size - 1) // page_size
        medals = {1: "🥇", 2: "🥈", 3: "🥉"}
        for page_index in range(total_pages):
            embed = discord.Embed(
                title="📣 Ranking de Callers G3NESYS",
                description=(
                    "Clasificacion calculada con actividades, asistencia y cumplimiento.\n"
                    "La plata incluye Splits aprobados y depositados."
                ),
                color=discord.Color.gold(),
            )
            start = page_index * page_size
            for index, row in enumerate(rows[start : start + page_size], start=start + 1):
                member = guild.get_member(int(row["user_id"]))
                name = member.display_name if member else f"Usuario {row['user_id']}"
                badge = medals.get(index, f"#{index}")
                status = " • ⛔ Penalizado" if int(row["penalized"]) else ""
                embed.add_field(
                    name=f"{badge} {name} • {row['score']} puntos{status}",
                    value=(
                        f"💰 Repartido: **{format_amount(row['distributed'])}**\n"
                        f"⚔️ Creadas: **{row['activities_created']}** • "
                        f"✅ Completadas: **{row['activities_completed']}**\n"
                        f"❌ Canceladas: **{row['activities_cancelled']}** • "
                        f"🛡️ Justificadas: **{row['cancellations_exempt']}**\n"
                        f"🙋 Asistencias: **{row['attendances']}** • "
                        f"🚫 Ausencias: **{row['absences']}**"
                    ),
                    inline=False,
                )
            embed.set_footer(text=f"Pagina {page_index + 1}/{total_pages} • {len(rows)} callers autorizados")
            pages.append(embed)
        return pages

    def build_payout_review_embed(self, guild_id: int, code: str) -> discord.Embed:
        payout = self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        embed = discord.Embed(
            title=f"📋 Split pendiente {code}",
            description="Revisa el Split y usa los botones para aprobar, rechazar o pedir correccion.",
            color=discord.Color.gold(),
        )
        if payout is None:
            embed.description = "No encontre los datos de este Split."
            return embed
        embed.add_field(name="Caller", value=f"<@{payout['caller_id']}>", inline=True)
        embed.add_field(name="Loot bruto", value=format_amount(payout["gross_loot"]), inline=True)
        embed.add_field(name="Aporte gremial", value=format_amount(payout["guild_amount"]), inline=True)
        embed.add_field(
            name="Porcentaje caller",
            value=f"{float(payout['caller_percent'] or 0):.1f}% — {format_amount(payout['caller_amount'])}",
            inline=True,
        )
        embed.add_field(name="Monto repartible", value=format_amount(payout["distributable"]), inline=True)
        embed.add_field(name="Estado", value=payout["status"], inline=True)
        embed.add_field(
            name="Participantes",
            value=self.payout_detail_text(guild_id, code, compact=True)[:1024],
            inline=False,
        )
        embed.set_image(url=ADMIN_PANEL_IMAGE)
        return embed

    @commands.command(name="panel_admin")
    async def panel_admin(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        embed = discord.Embed(
            title="Panel Administrativo G3NESYS",
            description="Tesoreria, Splits, cobros, historial, rankings y configuracion.",
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
        await send_admin_notification(
            self.db,
            guild=ctx.guild,
            category="general_admin",
            content=(
                f"📈 Ingreso registrado por <@{ctx.author.id}>: {format_amount(amount)} · "
                f"{category} · {description}"
            ),
        )
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
    async def aprobar_cobro(
        self,
        ctx: commands.Context,
        code: str,
        *,
        admin_message: str = "",
    ) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            await self.approve_withdrawal(
                ctx.guild,
                code,
                ctx.author.id,
                normalize_admin_message(admin_message),
            )
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
        await send_admin_notification(
            self.db,
            guild=ctx.guild,
            category="withdrawals",
            content=(
                f"❌ Cobro `{code}` rechazado por <@{ctx.author.id}> para "
                f"<@{withdrawal['user_id']}>. Motivo: {reason}"
            ),
        )
        await ctx.reply(f"Solicitud `{code}` rechazada.", mention_author=False)

    @commands.command(name="liquidar_cobro")
    async def liquidar_cobro(
        self,
        ctx: commands.Context,
        code: str,
        amount_raw: str,
        *,
        admin_message: str = "",
    ) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            amount = parse_int_amount(amount_raw)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        message = normalize_admin_message(admin_message)
        await ctx.reply(
            (
                f"¿Confirmas esta operacion?\nLiquidar `{code}` por {format_amount(amount)}."
                f"{admin_message_block(message)}"
            ),
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="liquidate_withdrawal",
                payload={"code": code, "amount": amount, "admin_message": message},
            ),
            mention_author=False,
        )

    @commands.command(name="aprobar_reparto", aliases=["aprobar_split"])
    async def aprobar_reparto(self, ctx: commands.Context, code: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        await ctx.reply(
            f"¿Confirmas esta operacion?\nAprobar Split `{code}` y depositar saldos.",
            view=ConfirmAdminActionView(
                self,
                admin_id=ctx.author.id,
                action="approve_payout",
                payload={"code": code},
            ),
            mention_author=False,
        )

    @commands.command(name="rechazar_reparto", aliases=["rechazar_split"])
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
        await ctx.reply(f"Split `{code}` rechazado.", mention_author=False)

    @commands.command(name="corregir_reparto", aliases=["corregir_split"])
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

    @commands.command(name="auditoria_split", aliases=["auditoria_reparto"])
    async def auditoria_split(self, ctx: commands.Context, code: str) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        payout = self.db.fetch_one(
            "SELECT id FROM payouts WHERE guild_id = ? AND code = ?",
            (ctx.guild.id, code.upper()),
        )
        if payout is None:
            await ctx.reply("No encontre ese Split.", mention_author=False)
            return
        await ctx.reply(
            payout_audit_text(self.db, ctx.guild.id, int(payout["id"])),
            mention_author=False,
        )

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
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="general_admin",
            content=(
                f"📈 Ingreso registrado por <@{interaction.user.id}>: {format_amount(amount)} · "
                f"{modal.category.value} · {modal.description.value}"
            ),
        )
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
        guild = interaction.guild
        if guild is None:
            raise ValueError("Esta accion debe confirmarse dentro del servidor.")
        if action == "expense":
            register_guild_expense(
                self.db,
                guild.id,
                amount=int(payload["amount"]),
                category=str(payload["category"]),
                description=str(payload["description"]),
                admin_id=interaction.user.id,
            )
            await send_admin_notification(
                self.db,
                guild=guild,
                category="general_admin",
                content=(
                    f"📉 Egreso registrado por <@{interaction.user.id}>: "
                    f"{format_amount(payload['amount'])} · {payload['category']} · "
                    f"{payload['description']}"
                ),
            )
            return "Egreso registrado."
        if action == "deposit":
            movement_id = deposit_to_user_from_treasury(
                self.db,
                guild.id,
                user_id=int(payload["user_id"]),
                amount=int(payload["amount"]),
                balance_type=str(payload["balance_type"]),
                reason=str(payload["reason"]),
                admin_id=interaction.user.id,
            )
            member = guild.get_member(int(payload["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
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
            await send_admin_notification(
                self.db,
                guild=guild,
                category="general_admin",
                content=(
                    f"💰 Deposito administrativo por <@{interaction.user.id}> a "
                    f"<@{payload['user_id']}>: {format_amount(payload['amount'])} · "
                    f"{payload['reason']} · Movimiento #{movement_id}."
                ),
            )
            return f"Deposito registrado. Movimiento #{movement_id}."
        if action == "create_fine":
            member = guild.get_member(int(payload["user_id"]))
            if member is None:
                raise ValueError("No encontre al usuario en el servidor.")
            code = await create_fine(
                self.db,
                guild_id=guild.id,
                user=member,
                amount=int(payload["amount"]),
                reason=str(payload["reason"]),
                origin="Manual",
                created_by=interaction.user.id,
            )
            return f"Multa creada: `{code}`."
        if action == "cancel_fine":
            await cancel_fine(
                self.db,
                guild=guild,
                fine_code=str(payload["fine_code"]),
                admin_id=interaction.user.id,
                reason=str(payload["reason"]),
            )
            return f"Multa cancelada: `{payload['fine_code']}`."
        if action == "liquidate_withdrawal":
            return await self.liquidate_withdrawal(
                guild,
                str(payload["code"]),
                int(payload["amount"]),
                interaction.user.id,
                normalize_admin_message(str(payload.get("admin_message", ""))),
            )
        if action == "approve_payout":
            return await self.approve_payout(guild, str(payload["code"]), interaction.user.id)
        raise ValueError("Accion no reconocida.")

    async def approve_withdrawal(
        self,
        guild: discord.Guild,
        code: str,
        admin_id: int,
        admin_message: str = "",
    ) -> None:
        code = code.strip().upper()
        admin_message = normalize_admin_message(admin_message)
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
            SET status = ?, approved_by = ?, approved_at = ?,
                approval_admin_message = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                WITHDRAWAL_APPROVED,
                admin_id,
                utc_now_iso(),
                admin_message or None,
                guild.id,
                int(withdrawal["id"]),
            ),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Aprobar solicitud de cobro",
            system="Banco",
            affected_user_id=int(withdrawal["user_id"]),
            amount=int(withdrawal["amount_requested"]),
            observation=(
                f"{code} · Indicaciones: {admin_message}"
                if admin_message
                else code
            ),
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
                    f"Queda pendiente por liquidar.{admin_message_block(admin_message)}"
                ),
            )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            content=(
                f"✅ Cobro `{code}` aprobado por <@{admin_id}> para "
                f"<@{withdrawal['user_id']}> por {format_amount(withdrawal['amount_requested'])}."
                f"{admin_message_block(admin_message)}"
            ),
        )

    async def liquidate_withdrawal(
        self,
        guild: discord.Guild,
        code: str,
        amount: int,
        admin_id: int,
        admin_message: str = "",
    ) -> str:
        code = code.strip().upper()
        admin_message = normalize_admin_message(admin_message)
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
            SET status = ?, amount_liquidated = ?, liquidated_by = ?, liquidated_at = ?,
                liquidation_admin_message = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                status,
                amount,
                admin_id,
                utc_now_iso(),
                admin_message or None,
                guild.id,
                int(withdrawal["id"]),
            ),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Liquidar solicitud de cobro",
            system="Banco",
            affected_user_id=int(withdrawal["user_id"]),
            amount=amount,
            observation=(
                f"{code} · Indicaciones: {admin_message}"
                if admin_message
                else code
            ),
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
                    f"Estado: {status}{admin_message_block(admin_message)}"
                ),
            )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            content=(
                f"💵 Cobro `{code}` liquidado por <@{admin_id}>. "
                f"Usuario: <@{withdrawal['user_id']}> · "
                f"Monto: {format_amount(amount)} · Estado: {status} · "
                f"Movimiento #{movement_id}.{admin_message_block(admin_message)}"
            ),
        )
        return f"Cobro `{code}` liquidado por {format_amount(amount)}. Movimiento #{movement_id}."

    async def approve_payout(self, guild: discord.Guild, code: str, admin_id: int) -> str:
        payout = self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if payout is None:
            raise ValueError("No encontre ese Split.")
        if payout["status"] != PAYOUT_PENDING:
            raise ValueError("Ese Split ya fue procesado o no esta pendiente.")

        if int(payout["guild_amount"]) > 0:
            register_guild_income(
                self.db,
                guild.id,
                amount=int(payout["guild_amount"]),
                category="Aporte por actividad",
                description=f"Aporte gremial de Split {code}",
                admin_id=admin_id,
            )
        caller_amount = int(payout["caller_amount"] or 0)
        if caller_amount > 0:
            caller_id = int(payout["caller_id"])
            fine_count, _ = pending_fines_total(self.db, guild.id, caller_id)
            caller_balance_type = "retained" if fine_count > 0 else "available"
            if caller_balance_type == "retained":
                adjust_user_balance(self.db, guild.id, caller_id, retained_delta=caller_amount)
            else:
                adjust_user_balance(self.db, guild.id, caller_id, available_delta=caller_amount)
            create_movement(
                self.db,
                guild.id,
                movement_type="DEPOSITO",
                category="Porcentaje de caller",
                amount=caller_amount,
                description=f"Porcentaje de caller del Split {code}",
                created_by=admin_id,
                user_id=caller_id,
                source_table="payouts",
                source_id=int(payout["id"]),
            )
            caller = guild.get_member(caller_id)
            if caller is not None:
                await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
                    user=caller,
                    action="deposito_porcentaje_caller",
                    content=(
                        f"📣 Recibiste {format_amount(caller_amount)} por tu porcentaje de caller "
                        f"en el Split `{code}`."
                    ),
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
                category="Split de actividad",
                amount=amount,
                description=f"Deposito por Split {code}",
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
                    action="deposito_split",
                    content=(
                        "💰 Has recibido un deposito por Split.\n\n"
                        f"Cantidad: {format_amount(amount)}\n"
                        f"Tipo: {self.readable_balance_type(balance_type)}\n"
                        f"Split: {code}"
                    ),
                )
        self.db.execute(
            "UPDATE payouts SET status = ?, reviewed_by = ?, reviewed_at = ? WHERE id = ?",
            (PAYOUT_DEPOSITED, admin_id, utc_now_iso(), int(payout["id"])),
        )
        log_payout_action(
            self.db,
            guild.id,
            int(payout["id"]),
            actor_id=admin_id,
            action="Split aprobado",
            details=f"Monto repartible: {int(payout['distributable'])}",
        )
        log_payout_action(
            self.db,
            guild.id,
            int(payout["id"]),
            actor_id=admin_id,
            action="Depositos del Split realizados",
            details=(
                f"Participantes: {len(participants)}; repartible: {int(payout['distributable'])}; "
                f"caller: {caller_amount}; gremio: {int(payout['guild_amount'])}"
            ),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Aprobar Split",
            system="Splits",
            amount=int(payout["distributable"]) + caller_amount,
            observation=code,
        )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="splits",
            content=(
                f"✅ Split `{code}` aprobado y depositado por <@{admin_id}>. "
                f"Participantes: {len(participants)} · "
                f"Repartible: {format_amount(payout['distributable'])} · "
                f"Caller: {format_amount(caller_amount)} · "
                f"Gremio: {format_amount(payout['guild_amount'])}."
            ),
        )
        return f"Split `{code}` aprobado y saldos depositados."

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
            raise ValueError("No encontre ese Split.")
        if payout["status"] != PAYOUT_PENDING:
            raise ValueError("Solo se pueden cambiar Splits pendientes.")
        self.db.execute(
            "UPDATE payouts SET status = ?, reviewed_by = ?, reviewed_at = ?, notes = ? WHERE id = ?",
            (status, admin_id, utc_now_iso(), reason, int(payout["id"])),
        )
        audit_action = (
            "Split rechazado"
            if status == PAYOUT_REJECTED
            else "Correccion solicitada"
            if status == PAYOUT_CORRECTION
            else f"Estado actualizado a {status}"
        )
        log_payout_action(
            self.db,
            guild.id,
            int(payout["id"]),
            actor_id=admin_id,
            action=audit_action,
            details=reason,
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action=f"Actualizar Split a {status}",
            system="Splits",
            amount=int(payout["distributable"]),
            observation=f"{code}: {reason}",
        )
        caller = guild.get_member(int(payout["caller_id"]))
        if caller:
            await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=caller,
                action="estado_split",
                content=f"El Split `{code}` cambio a `{status}`. Motivo: {reason}",
            )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="splits",
            content=(
                f"📋 Split `{code}` actualizado a **{status}** por <@{admin_id}>. "
                f"Motivo: {reason}"
            ),
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

    def notification_settings_text(self, guild_id: int) -> str:
        lines = [
            "🔔 **Canales de notificaciones administrativas**",
            "Los avisos privados para usuarios continúan enviándose por DM.",
            "",
        ]
        for category, label, emoji in NOTIFICATION_CHANNEL_CATEGORIES:
            route = ADMIN_CHANNEL_SETTINGS[category]
            specific = self.db.get_setting(guild_id, route[0])
            if specific:
                destination = (
                    f"<#{specific}>"
                    if specific.isdigit()
                    else f"ID inválido: `{specific}`"
                )
            else:
                fallback = next(
                    (
                        self.db.get_setting(guild_id, key)
                        for key in route[1:]
                        if self.db.get_setting(guild_id, key)
                    ),
                    "",
                )
                destination = (
                    f"Respaldo <#{fallback}>"
                    if fallback and fallback.isdigit()
                    else "Sin configurar"
                )
            lines.append(f"{emoji} **{label}:** {destination}")
        lines.extend(
            [
                "",
                "Selecciona una categoría para establecer o cambiar su canal.",
            ]
        )
        return "\n".join(lines)

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

    def liquidation_history_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT code, user_id, amount_requested, amount_liquidated, status,
                   liquidated_by, liquidated_at, approval_admin_message,
                   liquidation_admin_message
            FROM withdrawals
            WHERE guild_id = ? AND status IN (?, ?) AND liquidated_at IS NOT NULL
            ORDER BY liquidated_at DESC, id DESC LIMIT 15
            """,
            (guild_id, WITHDRAWAL_LIQUIDATED, WITHDRAWAL_PARTIAL),
        )
        if not rows:
            return "No hay liquidaciones registradas."
        lines = ["🧾 **Historial de liquidaciones**"]
        for row in rows:
            liquidator = (
                f"<@{row['liquidated_by']}>"
                if row["liquidated_by"] is not None
                else "Sistema"
            )
            lines.append(
                f"`{row['code']}` <@{row['user_id']}> · "
                f"{format_amount(row['amount_liquidated'] or 0)} de "
                f"{format_amount(row['amount_requested'])} · {row['status']} · "
                f"Por {liquidator} · {row['liquidated_at']}"
            )
            if row["approval_admin_message"]:
                lines.append(
                    f"↳ Indicaciones al aprobar: {row['approval_admin_message']}"
                )
            if row["liquidation_admin_message"]:
                lines.append(
                    f"↳ Indicaciones al liquidar: {row['liquidation_admin_message']}"
                )
        return "\n".join(lines)[:1900]

    def pending_fines_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT code, user_id, amount, reason, created_at
            FROM fines
            WHERE guild_id = ? AND status = 'Pendiente'
            ORDER BY id DESC LIMIT 15
            """,
            (guild_id,),
        )
        if not rows:
            return "No hay multas pendientes."
        lines = ["🚨 **Multas pendientes**"]
        for row in rows:
            lines.append(
                f"`{row['code']}` <@{row['user_id']}> {format_amount(row['amount'])} - {row['reason']}"
            )
        return "\n".join(lines)

    def pending_payouts_text(self, guild_id: int) -> str:
        return self.payouts_list_text(
            guild_id,
            mode="pending",
        )

    def approved_payouts_text(self, guild_id: int) -> str:
        return self.payouts_list_text(
            guild_id,
            mode="approved",
        )

    def all_payouts_text(self, guild_id: int) -> str:
        return self.payouts_list_text(
            guild_id,
            mode="all",
        )

    def payouts_list_text(self, guild_id: int, *, mode: str) -> str:
        if mode == "pending":
            title = "⏳ **Splits pendientes de aprobación**"
            empty = "No hay Splits pendientes de aprobación."
            query = """
                SELECT code, caller_id, distributable, guild_amount,
                       caller_amount, status, created_at, reviewed_at
                FROM payouts
                WHERE guild_id = ? AND status = ? AND sent_to_admin_at IS NOT NULL
                ORDER BY id DESC LIMIT 15
            """
            params = (guild_id, PAYOUT_PENDING)
        elif mode == "approved":
            title = "✅ **Splits aprobados**"
            empty = "No hay Splits aprobados."
            query = """
                SELECT code, caller_id, distributable, guild_amount,
                       caller_amount, status, created_at, reviewed_at
                FROM payouts
                WHERE guild_id = ? AND status IN (?, ?)
                ORDER BY COALESCE(reviewed_at, created_at) DESC, id DESC LIMIT 15
            """
            params = (guild_id, PAYOUT_APPROVED, PAYOUT_DEPOSITED)
        elif mode == "all":
            title = "📚 **Lista general de Splits**"
            empty = "No hay Splits registrados."
            query = """
                SELECT code, caller_id, distributable, guild_amount,
                       caller_amount, status, created_at, reviewed_at
                FROM payouts
                WHERE guild_id = ?
                ORDER BY id DESC LIMIT 20
            """
            params = (guild_id,)
        else:
            raise ValueError("Vista de Splits no reconocida.")

        rows = self.db.fetch_all(query, params)
        if not rows:
            return empty
        lines = [title]
        for row in rows:
            lines.append(
                f"`{row['code']}` · **{row['status']}** · Caller <@{row['caller_id']}> · "
                f"Repartible {format_amount(row['distributable'])} · "
                f"Gremio {format_amount(row['guild_amount'])} · "
                f"Caller {format_amount(row['caller_amount'])}"
            )
        if mode == "pending":
            lines.append(
                "Usa los botones del mensaje de revisión para aprobar, rechazar o pedir corrección."
            )
        return "\n".join(lines)[:1900]

    def payout_detail_text(self, guild_id: int, code: str, *, compact: bool = False) -> str:
        payout = self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        if payout is None:
            return "No encontre ese Split."
        rows = self.db.fetch_all(
            """
            SELECT user_id, participation_percent, amount
            FROM payout_participants
            WHERE payout_id = ?
            ORDER BY id ASC
            """,
            (int(payout["id"]),),
        )
        if compact:
            if not rows:
                return "Sin participantes."
            return "\n".join(
                f"• <@{row['user_id']}> - {row['participation_percent']}% - {format_amount(row['amount'])}"
                for row in rows
            )
        lines = [
            f"📋 **Detalle de Split {code}**",
            f"Caller: <@{payout['caller_id']}>",
            f"Loot bruto: {format_amount(payout['gross_loot'])}",
            f"Aporte gremial: {format_amount(payout['guild_amount'])}",
            f"Pago caller: {float(payout['caller_percent'] or 0):.1f}% — {format_amount(payout['caller_amount'])}",
            f"Monto repartible: {format_amount(payout['distributable'])}",
            "",
            "**Participantes**",
        ]
        if not rows:
            lines.append("Sin participantes.")
        for row in rows:
            lines.append(
                f"• <@{row['user_id']}> - {row['participation_percent']}% - {format_amount(row['amount'])}"
            )
        return "\n".join(lines)

    def history_text(self, guild_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT *
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
            lines.append(movement_history_line(row))
        return "\n".join(lines)[:1900]

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
        movements = self.db.fetch_all(
            """
            SELECT * FROM movements
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
        lines.extend(movement_history_line(row) for row in movements)
        if not movements:
            lines.append("Sin movimientos.")
        return "\n".join(lines)

    def create_report(self, guild_id: int) -> Path:
        return create_admin_report(
            self.db,
            guild_id,
            self.bot.get_guild(guild_id),
        )

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
