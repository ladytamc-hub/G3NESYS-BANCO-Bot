from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
from pathlib import Path
import traceback

import discord
from discord.ext import commands

from ..constants import (
    ADMIN_PANEL_IMAGE,
    ACTIVITY_FINISHED,
    ACTIVITY_TYPE_MANDATORY,
    OFFICIAL_MEMBER_ROLE_ID,
    PAYOUT_APPROVED,
    PAYOUT_DEPOSITED,
    PAYOUT_CORRECTION,
    PAYOUT_PENDING,
    PAYOUT_REJECTED,
    RECRUITERS_PANEL_IMAGE,
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_LIQUIDATED,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PAID,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
    WITHDRAWAL_UNPAID,
    WITHDRAWAL_DELEGATED,
    WITHDRAWAL_REASSIGNMENT,
    WITHDRAWAL_CANCELLED,
)
from ..permissions import (
    CALLER_PANEL_ROLE_NAMES,
    CALLER_PANEL_ROLE_SETTING_KEY,
    add_member_access_role,
    configured_member_role_ids,
    has_any_configured_role,
    is_admin_subject,
    is_caller_panel_subject,
    is_split_admin_subject,
    remove_member_access_role,
    require_admin_context,
)
from ..services.activity_audit import (
    AUDIT_NO_SPLIT,
    AUDIT_PENDING,
    AUDIT_SPLIT,
    ActivityAuditRecord,
    build_activity_audit_report_files,
    get_activity_audit_dataset,
    normalize_activity_code,
    pending_days,
)
from ..services.audit import log_action
from ..services.balance_control import (
    known_user_name,
    list_outside_users_with_balance,
    mark_member_alerted,
    record_member_departure,
    record_member_join,
    seize_user_balance,
)
from ..services.liquidation_expedient import (
    ActivityNotFoundError,
    ActivityWithoutLiquidationError,
    build_liquidation_expedient_file,
    liquidation_expedient_tempfile,
)
from ..services.voice_monitoring import format_duration, get_persisted_activity_voice_stats, summarize_voice_stats
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
    format_percent,
    get_account,
    movement_history_line,
    pending_fines_total,
    register_guild_expense,
    register_guild_income,
)
from ..services.fines import cancel_fine, create_fine
from ..services.guild_economy import (
    GuildEconomySummary,
    build_guild_economy_csv_report,
    get_guild_economy_summary,
    guild_economy_report_tempfile,
)
from ..services.notifications import (
    ADMIN_CHANNEL_SETTINGS,
    send_admin_notification,
    send_dm_safe,
)
from ..services.ticket_channels import (
    TICKET_CONVERSATION_CHANNEL_LABEL,
    TICKET_CONVERSATION_CHANNEL_SETTING_KEY,
    TICKET_CHANNEL_LABEL,
    TICKET_CHANNEL_SETTING_KEY,
    is_normal_text_ticket_channel,
    is_text_ticket_channel,
    ticket_channel_permission_errors,
)
from .support import SupportAdminView
from ..services.payout_audit import log_payout_action, payout_audit_text
from ..services.quick_liquidations import (
    get_liquidatable_participants,
    get_liquidatable_payout,
    liquidate_payout,
    recent_liquidatable_payouts,
)
from ..services.reports import create_admin_report
from ..utils import format_amount, format_money, join_csv_ids, parse_channel_id, parse_int_amount, split_csv_ids, utc_now_iso
from .withdrawal_audit_views import WithdrawalAuditHomeView, build_withdrawal_audit_home_embed



NOTIFICATION_CHANNEL_CATEGORIES = (
    ("splits", "Splits pendientes por aprobar", "📋"),
    ("withdrawals", "Solicitudes de cobro", "💳"),
    ("registration", "Registro", "📝"),
    ("activities", "Actividades con validación admin", "⚔️"),
    ("fines", "Multas o sanciones", "🚨"),
    ("general_admin", "Otras notificaciones admin", "🔔"),
)
PING_PUBLICATIONS_LABEL = "Canal de publicaciones de pings"
PING_PUBLICATIONS_SETTING_KEY = "channel_pings_id"
APPROVED_PING_CHANNELS_SETTING_KEY = "approved_ping_channel_ids"
REGEAR_CHANNEL_LABEL = "Canal de Requips"
REGEAR_CHANNEL_SETTING_KEY = "channel_requips_id"
REGEAR_NOTIFICATION_CHANNEL_LABEL = "Notificaciones de Requips"
REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY = "channel_notify_requips_id"
NOTIFICATION_CATEGORY_MAP = {
    category: (label, emoji)
    for category, label, emoji in NOTIFICATION_CHANNEL_CATEGORIES
}
RECRUITER_ROLE_NAMES = {"reclutador", "reclutadores"}
ADMIN_ROLE_NAMES = {
    "admin",
    "admins",
    "administrador",
    "administradores",
    "admin g3nesys",
    "administrador g3nesys",
}


REGEAR_STATUS_LABELS = {
    "pending": ("⏳", "Pendiente de revisión"),
    "paid": ("✅", "Pagado"),
    "pending_payment": ("🕒", "Pendiente de pago"),
    "rejected": ("❌", "Rechazado"),
}
REGEAR_RANKING_FILTERS = (
    ("general", "General", None),
    ("weekly", "Semanal", 7),
    ("monthly", "Mensual", 30),
)
REGEAR_RANKING_FILTER_MAP = {
    key: (label, days)
    for key, label, days in REGEAR_RANKING_FILTERS
}
def normalize_admin_message(value: str | None) -> str:
    return (value or "").strip()[:600]


def admin_message_block(message: str) -> str:
    return f"\n\n**Indicaciones del admin:**\n{message}" if message else ""


def regear_status_display(status: str) -> str:
    emoji, label = REGEAR_STATUS_LABELS.get(status, REGEAR_STATUS_LABELS["pending"])
    return f"{emoji} {label}"


def regear_filter_label(filter_key: str) -> str:
    return REGEAR_RANKING_FILTER_MAP.get(filter_key, REGEAR_RANKING_FILTER_MAP["general"])[0]


def regear_filter_cutoff(filter_key: str) -> str | None:
    _label, days = REGEAR_RANKING_FILTER_MAP.get(filter_key, REGEAR_RANKING_FILTER_MAP["general"])
    if days is None:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")


def discord_date(value: str | None, style: str = "d") -> str:
    if not value:
        return "Sin fecha"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return str(value)
    return f"<t:{int(parsed.timestamp())}:{style}>"


def short_member_label(guild: discord.Guild, user_id: int) -> str:
    member = guild.get_member(user_id)
    if member is not None:
        return member.display_name[:80]
    return f"Usuario {user_id}"


DEFAULT_RATE_SETTINGS = {
    "transfer_fee_percent": (
        "Comision por transferencia",
        "Comision transferencia %",
        "3",
    ),
    "guild_percentage_default": (
        "Porcentaje gremial",
        "Porcentaje gremial %",
        "10",
    ),
    "market_rate_default": (
        "Tasa de mercado",
        "Tasa mercado %",
        "0",
    ),
    "caller_percentage_default": (
        "Pago del caller",
        "Porcentaje caller %",
        "0",
    ),
}


def parse_admin_percent(raw: str) -> str:
    cleaned = str(raw or "0").replace("%", "").replace(",", ".").strip()
    try:
        value = float(cleaned or 0)
    except ValueError as exc:
        raise ValueError("El porcentaje debe ser un numero valido.") from exc
    if value < 0 or value > 100:
        raise ValueError("El porcentaje debe estar entre 0 y 100.")
    return format_percent(value)


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
        if not is_admin_subject(self.cog.db, interaction):
            await interaction.response.send_message(
                "Ya no tienes autorizacion de admin para confirmar esta operacion.",
                ephemeral=True,
            )
            return
        try:
            message = await self.cog.execute_confirmed_action(
                interaction,
                self.action,
                self.payload,
            )
        except ValueError as exc:
            message = str(exc)
        await interaction.response.edit_message(content=message, embed=None, view=None)

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await interaction.response.send_message("Solo quien inicio la operacion puede cancelar.", ephemeral=True)
            return
        await interaction.response.edit_message(content="Operacion cancelada.", embed=None, view=None)


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


class AdminIdModal(discord.ui.Modal):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        title = "Agregar admin por ID" if action == "add" else "Eliminar admin por ID"
        super().__init__(title=title, timeout=180)
        self.cog = cog
        self.action = action
        self.admin_id = admin_id
        self.user_id_input = discord.ui.TextInput(
            label="ID o mencion del usuario",
            placeholder="123456789012345678",
            max_length=40,
        )
        self.add_item(self.user_id_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        user_id = parse_channel_id(str(self.user_id_input.value))
        if user_id is None:
            await private_response(interaction, "No pude leer ese ID de Discord.")
            return
        await self.cog.prompt_admin_change(interaction, self.action, user_id)


class AdminUserSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        verb = "agregar" if action == "add" else "eliminar"
        super().__init__(
            placeholder=f"Selecciona el usuario que deseas {verb}",
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
        await self.cog.prompt_admin_change(interaction, self.action, self.values[0].id)


class AdminSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.action = action
        self.admin_id = admin_id
        self.add_item(AdminUserSelect(cog, action=action, admin_id=admin_id))

    @discord.ui.button(label="Ingresar ID manualmente", emoji="⌨️", style=discord.ButtonStyle.secondary)
    async def manual_id(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        await interaction.response.send_modal(
            AdminIdModal(self.cog, action=self.action, admin_id=self.admin_id)
        )


class DepositOptionsView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_id = admin_id

    async def require_owner_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
        return False

    @discord.ui.button(label="Deposito individual", emoji="\U0001FA99", style=discord.ButtonStyle.success)
    async def manual_deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner_admin(interaction):
            await interaction.response.send_modal(DepositModal(self.cog))

    @discord.ui.button(label="Deposito masivo", emoji="\U0001F4B0", style=discord.ButtonStyle.primary)
    async def bulk_deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner_admin(interaction):
            view = BulkDepositSelectionView(self.cog, interaction.user.id)
            await private_response(interaction, view.text(interaction.guild), view=view)


class BulkDepositAmountModal(discord.ui.Modal, title="Deposito masivo"):
    def __init__(self, view: "BulkDepositSelectionView"):
        super().__init__(timeout=180)
        self.parent_view = view
        self.amount = discord.ui.TextInput(label="Cantidad por usuario", placeholder="500000")
        self.concept = discord.ui.TextInput(label="Concepto", placeholder="Pago actividad Avalonian", max_length=120)
        self.note = discord.ui.TextInput(label="Nota opcional", required=False, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.amount)
        self.add_item(self.concept)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            amount = parse_int_amount(str(self.amount.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        amounts = {user_id: amount for user_id in self.parent_view.user_ids}
        await self.parent_view.cog.show_bulk_deposit_preview(
            interaction,
            admin_id=self.parent_view.admin_id,
            amounts=amounts,
            concept=str(self.concept.value).strip(),
            note=str(self.note.value).strip(),
            mode="Misma cantidad",
        )


class BulkDepositDifferentModal(discord.ui.Modal, title="Deposito masivo por importes"):
    def __init__(self, cog: "Admin", admin_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.lines = discord.ui.TextInput(
            label="Usuario | cantidad por linea",
            style=discord.TextStyle.paragraph,
            placeholder="123456789012345678 | 500000\n234567890123456789 | 750000",
            max_length=1800,
        )
        self.concept = discord.ui.TextInput(label="Concepto", max_length=120)
        self.note = discord.ui.TextInput(label="Nota opcional", required=False, style=discord.TextStyle.paragraph, max_length=500)
        self.add_item(self.lines)
        self.add_item(self.concept)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este flujo puede usarlo.")
            return
        amounts: dict[int, int] = {}
        invalid: list[str] = []
        for index, line in enumerate(str(self.lines.value).splitlines(), start=1):
            if not line.strip():
                continue
            parts = [part.strip() for part in line.replace(",", "|").split("|")]
            if len(parts) < 2:
                invalid.append(f"Linea {index}: formato invalido")
                continue
            user_id = parse_channel_id(parts[0])
            try:
                amount = parse_int_amount(parts[1])
            except ValueError:
                amount = 0
            if user_id is None or amount <= 0:
                invalid.append(f"Linea {index}: usuario o cantidad invalida")
                continue
            amounts[user_id] = amount
        if invalid:
            await private_response(interaction, "No pude procesar:\n" + "\n".join(invalid[:8]))
            return
        await self.cog.show_bulk_deposit_preview(
            interaction,
            admin_id=self.admin_id,
            amounts=amounts,
            concept=str(self.concept.value).strip(),
            note=str(self.note.value).strip(),
            mode="Cantidad diferente",
        )


class BulkDepositUserSelect(discord.ui.UserSelect):
    def __init__(self, parent_view: "BulkDepositSelectionView"):
        super().__init__(placeholder="Busca miembros para agregar", min_values=1, max_values=25)
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.parent_view.require_admin(interaction):
            return
        added = []
        for user in self.values:
            member = interaction.guild.get_member(user.id) if interaction.guild else None
            if member is not None and not member.bot:
                self.parent_view.user_ids.add(user.id)
                added.append(member.mention)
        await interaction.response.edit_message(
            content=self.parent_view.text(interaction.guild),
            view=self.parent_view,
        )


class BulkDepositSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", admin_id: int):
        super().__init__(timeout=600)
        self.cog = cog
        self.admin_id = admin_id
        self.user_ids: set[int] = set()
        self.add_item(BulkDepositUserSelect(self))

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este deposito masivo puede usarlo.")
        return False

    def text(self, guild: discord.Guild | None) -> str:
        if not self.user_ids:
            return "Selecciona miembros por bloques. Luego elige la modalidad de deposito."
        mentions = [f"<@{user_id}>" for user_id in sorted(self.user_ids)]
        return f"Seleccionados: {len(self.user_ids)}\n" + ", ".join(mentions[:30])

    @discord.ui.button(label="Misma cantidad", emoji="\U0001F4B0", style=discord.ButtonStyle.success, row=1)
    async def same_amount(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        if not self.user_ids:
            await private_response(interaction, "Selecciona al menos un usuario.")
            return
        await interaction.response.send_modal(BulkDepositAmountModal(self))

    @discord.ui.button(label="Cantidades distintas", emoji="\U0001F4DD", style=discord.ButtonStyle.primary, row=1)
    async def different_amounts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(BulkDepositDifferentModal(self.cog, self.admin_id))

    @discord.ui.button(label="Vaciar", emoji="\U0001F5D1", style=discord.ButtonStyle.secondary, row=2)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            self.user_ids.clear()
            await interaction.response.edit_message(content=self.text(interaction.guild), view=self)

    @discord.ui.button(label="Cancelar", emoji="\U0000274C", style=discord.ButtonStyle.danger, row=2)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.edit_message(content="Deposito masivo cancelado.", view=None)


class BulkDepositConfirmView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, operation_id: str, amounts: dict[int, int], concept: str, note: str, mode: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_id = admin_id
        self.operation_id = operation_id
        self.amounts = dict(amounts)
        self.concept = concept
        self.note = note
        self.mode = mode
        self.executed = False

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin responsable puede confirmar este deposito masivo.")
        return False

    @discord.ui.button(label="Confirmar depositos", emoji="\U00002705", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        if self.executed:
            await private_response(interaction, "Esta operacion ya fue ejecutada.")
            return
        self.executed = True
        await interaction.response.defer(ephemeral=True)
        result = await self.cog.execute_bulk_deposit(
            interaction.guild,
            admin_id=self.admin_id,
            operation_id=self.operation_id,
            amounts=self.amounts,
            concept=self.concept,
            note=self.note,
            mode=self.mode,
        )
        await interaction.edit_original_response(content=result, embed=None, view=None)

    @discord.ui.button(label="Cancelar", emoji="\U0000274C", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.edit_message(content="Deposito masivo cancelado.", embed=None, view=None)


class QuickLiquidationSplitSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", *, admin_id: int, payouts):
        options = []
        for payout in payouts:
            options.append(
                discord.SelectOption(
                    label=f"{payout['code']} · {payout['activity_name']}"[:100],
                    value=str(payout["id"]),
                    description=(
                        f"{payout['pending_members']} miembros · "
                        f"{format_amount(payout['pending_total'])} pendientes"
                    )[:100],
                    emoji="⚡",
                )
            )
        super().__init__(
            placeholder="Selecciona un split reciente",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        payout_id = int(self.values[0])
        payout = get_liquidatable_payout(self.cog.db, interaction.guild.id, payout_id)
        participants = get_liquidatable_participants(self.cog.db, payout_id)
        if payout is None or not participants:
            await interaction.response.edit_message(
                content="Ese split ya no tiene miembros pendientes de liquidar.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                f"Split `{payout['code']}` · **{payout['activity_name']}**\n"
                f"Pendientes: {len(participants)} miembros · "
                f"{format_amount(sum(int(row['amount']) for row in participants))}\n\n"
                "Elige si deseas liquidar la actividad completa o a un solo miembro."
            ),
            view=QuickLiquidationModeView(
                self.cog,
                payout_id=payout_id,
                admin_id=self.admin_id,
            ),
        )


class QuickLiquidationSplitSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, payouts):
        super().__init__(timeout=300)
        self.add_item(QuickLiquidationSplitSelect(cog, admin_id=admin_id, payouts=payouts))


class QuickLiquidationModeView(discord.ui.View):
    def __init__(self, cog: "Admin", *, payout_id: int, admin_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.payout_id = payout_id
        self.admin_id = admin_id

    async def require_owner_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
        return False

    @discord.ui.button(label="Actividad completa", emoji="👥", style=discord.ButtonStyle.danger)
    async def complete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = get_liquidatable_payout(self.cog.db, interaction.guild.id, self.payout_id)
        participants = get_liquidatable_participants(self.cog.db, self.payout_id)
        if payout is None or not participants:
            await private_response(interaction, "Ese split ya fue liquidado por completo.")
            return
        embed = self.cog.quick_liquidation_confirmation_embed(
            interaction.guild,
            payout,
            participants,
            interaction.user,
            mode="Completa",
        )
        await private_response(
            interaction,
            "Confirma la liquidacion rapida de la actividad completa.",
            embed=embed,
            view=ConfirmAdminActionView(
                self.cog,
                admin_id=self.admin_id,
                action="quick_liquidate_full",
                payload={"payout_id": self.payout_id},
            ),
        )

    @discord.ui.button(label="Un solo miembro", emoji="👤", style=discord.ButtonStyle.primary)
    async def individual(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        participants = get_liquidatable_participants(self.cog.db, self.payout_id)
        if not participants:
            await private_response(interaction, "Ese split ya fue liquidado por completo.")
            return
        await private_response(
            interaction,
            "Selecciona un miembro del split o ingresa su ID manualmente:",
            view=QuickLiquidationMemberSelectionView(
                self.cog,
                payout_id=self.payout_id,
                admin_id=self.admin_id,
                guild=interaction.guild,
                participants=participants,
            ),
        )


class QuickLiquidationMemberSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", *, payout_id: int, admin_id: int, guild: discord.Guild, participants):
        options = []
        for participant in participants[:25]:
            user_id = int(participant["user_id"])
            member = guild.get_member(user_id)
            name = member.display_name if member else f"Usuario {user_id}"
            options.append(
                discord.SelectOption(
                    label=name[:100],
                    value=str(user_id),
                    description=f"ID {user_id} · {format_amount(participant['amount'])}"[:100],
                    emoji="👤",
                )
            )
        super().__init__(
            placeholder="Selecciona un miembro pendiente",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.cog = cog
        self.payout_id = payout_id
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        await self.cog.prompt_quick_liquidation_individual(
            interaction,
            self.payout_id,
            int(self.values[0]),
        )


class QuickLiquidationMemberIdModal(discord.ui.Modal, title="Liquidar miembro por ID"):
    user_id_input = discord.ui.TextInput(
        label="ID o mencion del usuario",
        placeholder="123456789012345678",
        max_length=40,
    )

    def __init__(self, cog: "Admin", *, payout_id: int, admin_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.payout_id = payout_id
        self.admin_id = admin_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        user_id = parse_channel_id(str(self.user_id_input.value))
        if user_id is None:
            await private_response(interaction, "No pude leer ese ID de Discord.")
            return
        await self.cog.prompt_quick_liquidation_individual(
            interaction,
            self.payout_id,
            user_id,
        )


class QuickLiquidationMemberSelectionView(discord.ui.View):
    def __init__(
        self,
        cog: "Admin",
        *,
        payout_id: int,
        admin_id: int,
        guild: discord.Guild,
        participants,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.payout_id = payout_id
        self.admin_id = admin_id
        self.add_item(
            QuickLiquidationMemberSelect(
                cog,
                payout_id=payout_id,
                admin_id=admin_id,
                guild=guild,
                participants=participants,
            )
        )

    @discord.ui.button(label="Ingresar ID manualmente", emoji="⌨️", style=discord.ButtonStyle.secondary)
    async def manual_id(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        await interaction.response.send_modal(
            QuickLiquidationMemberIdModal(
                self.cog,
                payout_id=self.payout_id,
                admin_id=self.admin_id,
            )
        )


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

    @discord.ui.button(label="Aprobar cobro", emoji="\U00002705", style=discord.ButtonStyle.success)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden aprobar cobros.")
            return
        await interaction.response.send_modal(ApproveWithdrawalModal(self.cog))

    @discord.ui.button(label="Liquidar cobro", emoji="\U0001F4B5", style=discord.ButtonStyle.primary)
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
            "add_pcall": "agregar como creador PCALL",
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
        if member.bot and self.action in {"add", "add_pcall"}:
            await private_response(interaction, "Un bot no puede recibir acceso de caller o PCALL.")
            return
        if self.action == "add":
            await self.cog.add_caller_interaction(interaction, member)
        elif self.action == "add_pcall":
            await self.cog.add_pcall_interaction(interaction, member)
        elif self.action == "remove":
            await self.cog.remove_caller_interaction(interaction, member)
        else:
            await self.cog.remove_caller_penalty_interaction(interaction, member)


class CallerSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(timeout=180)
        self.add_item(CallerMemberSelect(cog, action=action, admin_id=admin_id))


class CallerAddOptionsView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id

    async def require_owner_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
        return False

    @discord.ui.button(label="Agregar Caller", style=discord.ButtonStyle.success)
    async def add_official_caller(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al nuevo caller oficial:",
                view=CallerSelectionView(self.cog, action="add", admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Agregar creador PCALL", style=discord.ButtonStyle.primary)
    async def add_pcall_creator(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_owner_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al creador de contenido que recibira acceso PCALL:",
                view=CallerSelectionView(self.cog, action="add_pcall", admin_id=interaction.user.id),
            )

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

    @discord.ui.button(label="Agregar persona", emoji="➕", style=discord.ButtonStyle.success)
    async def add_caller(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Elige el tipo de alta:",
                view=CallerAddOptionsView(self.cog, admin_id=interaction.user.id),
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


class RecruiterMemberSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        verb = "agregar como reclutador" if action == "add" else "eliminar como reclutador"
        super().__init__(
            placeholder=f"Selecciona a quien quieres {verb}",
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
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None:
            await private_response(interaction, "No encontre a ese usuario dentro del servidor.")
            return
        if member.bot and self.action == "add":
            await private_response(interaction, "Un bot no puede registrarse como reclutador.")
            return
        if self.action == "add":
            await self.cog.add_recruiter_interaction(interaction, member)
        else:
            await self.cog.remove_recruiter_interaction(interaction, member)


class RecruiterSelectionView(discord.ui.View):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(timeout=180)
        self.add_item(RecruiterMemberSelect(cog, action=action, admin_id=admin_id))


class RecruitersAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden gestionar reclutadores.")
        return False

    @discord.ui.button(label="Ver reclutadores actuales", emoji="👥", style=discord.ButtonStyle.primary)
    async def list_recruiters(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.recruiters_text(interaction.guild),
                "lista_reclutadores",
            )

    @discord.ui.button(label="Agregar reclutador", emoji="➕", style=discord.ButtonStyle.success)
    async def add_recruiter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al nuevo reclutador:",
                view=RecruiterSelectionView(
                    self.cog,
                    action="add",
                    admin_id=interaction.user.id,
                ),
            )

    @discord.ui.button(label="Eliminar reclutador", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_recruiter(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al usuario al que deseas quitar el rol de Reclutador:",
                view=RecruiterSelectionView(
                    self.cog,
                    action="remove",
                    admin_id=interaction.user.id,
                ),
            )



class PaymentDelegateAddSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(placeholder="Busca el miembro que recibira pagos delegados", min_values=1, max_values=1)
        self.cog = cog
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            if interaction.guild is not None:
                log_action(self.cog.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento sin permisos delegados de pago", system="Banco", observation="add")
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        member = interaction.guild.get_member(self.values[0].id) if interaction.guild else None
        await self.cog.add_payment_delegate_interaction(interaction, member)


class PaymentDelegateAddView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(timeout=300)
        self.add_item(PaymentDelegateAddSelect(cog, admin_id=admin_id))


class PaymentDelegateRemoveSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", *, admin_id: int, guild: discord.Guild, rows):
        options = []
        for row in list(rows)[:25]:
            user_id = int(row["user_id"])
            active_count = cog.active_delegated_withdrawals_count(int(row["guild_id"]), user_id)
            options.append(
                discord.SelectOption(
                    label=short_member_label(guild, user_id)[:100],
                    value=str(user_id),
                    description=f"ID {user_id} | Pagos activos: {active_count}"[:100],
                    emoji="\U0001F464",
                )
            )
        super().__init__(placeholder="Selecciona el delegado que deseas quitar", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            if interaction.guild is not None:
                log_action(self.cog.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento sin permisos delegados de pago", system="Banco", observation="remove")
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        await self.cog.remove_payment_delegate_interaction(interaction, int(self.values[0]))


class PaymentDelegateRemoveView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, guild: discord.Guild, rows):
        super().__init__(timeout=300)
        self.add_item(PaymentDelegateRemoveSelect(cog, admin_id=admin_id, guild=guild, rows=rows))


class PaymentDelegatesAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        if interaction.guild is not None:
            log_action(self.cog.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento sin permisos delegados de pago", system="Banco")
        await private_response(interaction, "Solo admins autorizados pueden gestionar delegados de pago.")
        return False

    @discord.ui.button(label="A\u00f1adir delegado", emoji="\U00002795", style=discord.ButtonStyle.success)
    async def add_delegate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona el miembro autorizado para recibir pagos delegados:",
                view=PaymentDelegateAddView(self.cog, admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Quitar delegado", emoji="\U00002796", style=discord.ButtonStyle.danger)
    async def remove_delegate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        rows = self.cog.payment_delegate_rows(interaction.guild.id, active_only=True)
        if not rows:
            await private_response(interaction, "No hay delegados de pago activos para quitar.")
            return
        await private_response(
            interaction,
            "Selecciona el delegado que deseas desactivar. Las solicitudes historicas no se modifican.",
            view=PaymentDelegateRemoveView(self.cog, admin_id=interaction.user.id, guild=interaction.guild, rows=rows),
        )

    @discord.ui.button(label="Ver delegados", emoji="\U0001F4CB", style=discord.ButtonStyle.secondary)
    async def list_delegates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.payment_delegates_text(interaction.guild), "delegados_pago")


class AdminsAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden gestionar administradores.")
        return False

    @discord.ui.button(label="Ver admins actuales", emoji="👥", style=discord.ButtonStyle.primary)
    async def list_admins(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.admins_text(interaction.guild),
                "lista_admins",
            )

    @discord.ui.button(label="Agregar admin", emoji="➕", style=discord.ButtonStyle.success)
    async def add_admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al usuario que deseas autorizar como admin o ingresa su ID:",
                view=AdminSelectionView(
                    self.cog,
                    action="add",
                    admin_id=interaction.user.id,
                ),
            )

    @discord.ui.button(label="Eliminar admin", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al admin que deseas retirar o ingresa su ID:",
                view=AdminSelectionView(
                    self.cog,
                    action="remove",
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
        if not is_split_admin_subject(self.cog.db, interaction):
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
        self.add_button("Aprobar", "approve", "✅", discord.ButtonStyle.success, row=0)
        self.add_button("Rechazar", "reject", "❌", discord.ButtonStyle.danger, row=0)
        self.add_button("Corregir Split", "edit", "🛠️", discord.ButtonStyle.secondary, row=0)
        self.add_button("Pedir Corrección", "correction", "🔁", discord.ButtonStyle.secondary, row=1)
        self.add_button("Ver Detalle", "detail", "🔍", discord.ButtonStyle.primary, row=1)
        self.add_button("Auditoría", "audit", "📋", discord.ButtonStyle.secondary, row=1)

    def add_button(
        self,
        label: str,
        action: str,
        emoji: str,
        style: discord.ButtonStyle,
        *,
        row: int,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:admin:payout:{action}:{self.code}",
            row=row,
        )
        button.callback = self.handle_button
        self.add_item(button)

    async def handle_button(self, interaction: discord.Interaction) -> None:
        custom_id = str(interaction.data["custom_id"])
        action = custom_id.split(":")[3]
        if action == "edit":
            activities_cog = self.cog.bot.get_cog("Activities")
            if activities_cog is None or not hasattr(activities_cog, "prompt_correct_payout_interaction"):
                await private_response(interaction, "El panel de actividades no esta disponible.")
                return
            await activities_cog.prompt_correct_payout_interaction(
                interaction,
                interaction.guild.id,
                self.code,
                source_message=interaction.message,
            )
            return
        if not is_split_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden revisar Splits.")
            return
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

class PendingPayoutSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", payouts):
        options = []
        for payout in list(payouts)[:25]:
            status_label = "Requiere corrección" if payout["status"] == PAYOUT_CORRECTION else "Pendiente"
            options.append(
                discord.SelectOption(
                    label=f"{payout['code']} · {status_label}"[:100],
                    value=str(payout["code"]),
                    description=(
                        f"Caller {payout['caller_id']} · "
                        f"Repartible {format_amount(payout['distributable'])}"
                    )[:100],
                    emoji="🔁" if payout["status"] == PAYOUT_CORRECTION else "📋",
                )
            )
        super().__init__(
            placeholder="Selecciona un Split pendiente",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if not isinstance(parent, PendingPayoutManagementView):
            await private_response(interaction, "No pude actualizar esta seleccion.")
            return
        if interaction.user.id != parent.admin_id or not is_split_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        parent.selected_code = self.values[0]
        await interaction.response.edit_message(
            content=parent.message_text(interaction.guild.id),
            view=parent,
        )


class PendingPayoutManagementView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, payouts):
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_id = admin_id
        self.payouts = list(payouts)
        self.selected_code: str | None = None
        self.add_item(PendingPayoutSelect(cog, self.payouts))

    async def require_owner_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_split_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
        return False

    def selected_payout(self):
        if self.selected_code is None:
            return None
        return next((row for row in self.payouts if str(row["code"]) == self.selected_code), None)

    def message_text(self, guild_id: int) -> str:
        selected = self.selected_payout()
        extra = ""
        if selected is not None:
            status_label = "🔁 Requiere corrección" if selected["status"] == PAYOUT_CORRECTION else "⏳ Pendiente"
            extra = (
                f"\n\nSeleccionado: `{selected['code']}` · {status_label} · "
                f"Caller <@{selected['caller_id']}> · "
                f"Repartible {format_amount(selected['distributable'])}"
            )
        return (self.cog.pending_payouts_text(guild_id) + extra)[:1900]

    async def require_selected_payout(self, interaction: discord.Interaction):
        payout = self.selected_payout()
        if payout is None:
            await private_response(interaction, "Selecciona un Split primero.")
            return None
        current = self.cog.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (interaction.guild.id, payout["code"]),
        )
        if current is None:
            await private_response(interaction, "No encontre ese Split.")
            return None
        if current["status"] not in {PAYOUT_PENDING, PAYOUT_CORRECTION}:
            await private_response(interaction, "Ese Split ya no está pendiente; ya fue procesado.")
            return None
        return current

    async def require_pending_approval(self, interaction: discord.Interaction):
        payout = await self.require_selected_payout(interaction)
        if payout is None:
            return None
        if payout["status"] != PAYOUT_PENDING:
            await private_response(interaction, "Ese Split ya requiere corrección y no está pendiente de aprobación.")
            return None
        return payout

    @discord.ui.button(label="Aprobar", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_pending_approval(interaction)
        if payout is None:
            return
        await private_response(
            interaction,
            f"¿Confirmas esta operacion?\nAprobar Split `{payout['code']}` y depositar saldos.",
            view=ConfirmAdminActionView(
                self.cog,
                admin_id=interaction.user.id,
                action="approve_payout",
                payload={"code": str(payout["code"])},
            ),
        )

    @discord.ui.button(label="Rechazar", emoji="❌", style=discord.ButtonStyle.danger, row=1)
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_pending_approval(interaction)
        if payout is None:
            return
        await interaction.response.send_modal(PayoutReasonModal(self.cog, str(payout["code"]), PAYOUT_REJECTED))

    @discord.ui.button(label="Corregir Split", emoji="🛠️", style=discord.ButtonStyle.secondary, row=1)
    async def correct(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_selected_payout(interaction)
        if payout is None:
            return
        activities_cog = self.cog.bot.get_cog("Activities")
        if activities_cog is None or not hasattr(activities_cog, "prompt_correct_payout_interaction"):
            await private_response(interaction, "El panel de actividades no esta disponible.")
            return
        await activities_cog.prompt_correct_payout_interaction(
            interaction,
            interaction.guild.id,
            str(payout["code"]),
        )

    @discord.ui.button(label="Pedir Corrección", emoji="🔁", style=discord.ButtonStyle.secondary, row=2)
    async def request_correction(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_selected_payout(interaction)
        if payout is None:
            return
        if payout["status"] == PAYOUT_CORRECTION:
            await private_response(interaction, "Ese Split ya requiere corrección.")
            return
        await interaction.response.send_modal(PayoutReasonModal(self.cog, str(payout["code"]), PAYOUT_CORRECTION))

    @discord.ui.button(label="Ver Detalle", emoji="🔍", style=discord.ButtonStyle.primary, row=2)
    async def detail(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_selected_payout(interaction)
        if payout is None:
            return
        await dm_or_private(
            self.cog,
            interaction,
            self.cog.payout_detail_text(interaction.guild.id, str(payout["code"])),
            "detalle_split_pendiente_admin",
        )

    @discord.ui.button(label="Auditoría", emoji="📋", style=discord.ButtonStyle.secondary, row=2)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        payout = await self.require_selected_payout(interaction)
        if payout is None:
            return
        await dm_or_private(
            self.cog,
            interaction,
            payout_audit_text(self.cog.db, interaction.guild.id, int(payout["id"])),
            "auditoria_split_pendiente_admin",
        )

class SplitsAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_split_admin_subject(self.cog.db, interaction):
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
        if not await self.require_admin(interaction):
            return
        rows = self.cog.pending_payout_rows(interaction.guild.id)
        if not rows:
            await private_response(interaction, "No hay Splits pendientes de aprobación.")
            return
        await private_response(
            interaction,
            self.cog.pending_payouts_text(interaction.guild.id),
            view=PendingPayoutManagementView(
                self.cog,
                admin_id=interaction.user.id,
                payouts=rows,
            ),
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
        label="Actividades pendientes de split",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="g3n:admin:splits:pending_activities",
    )
    async def pending_split_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        rows = self.cog.pending_split_activities(interaction.guild.id)
        if not rows:
            await private_response(interaction, "No hay actividades pendientes de split.")
            return
        await private_response(
            interaction,
            self.cog.pending_split_activities_text(interaction.guild.id, rows=rows),
            view=PendingSplitActivitiesView(
                self.cog,
                admin_id=interaction.user.id,
                activities=rows,
            ),
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


class PendingSplitActivitySelect(discord.ui.Select):
    def __init__(self, cog: "Admin", *, activities):
        options = []
        for activity in activities[:25]:
            options.append(
                discord.SelectOption(
                    label=f"{activity['code']} · {activity['name']}"[:100],
                    value=str(activity["id"]),
                    description=(
                        f"Caller {activity['caller_id']} · "
                        f"{activity['confirmed']} asist. · {activity['horario'] or activity['ended_at']}"
                    )[:100],
                    emoji="🔴",
                )
            )
        super().__init__(
            placeholder="Selecciona una actividad pendiente de split",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if not isinstance(parent, PendingSplitActivitiesView):
            await private_response(interaction, "No pude actualizar esta selección.")
            return
        if interaction.user.id != parent.admin_id or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        parent.selected_activity_id = int(self.values[0])
        activity = parent.selected_activity()
        selected = f"\n\nSeleccionada: `{activity['code']}` **{activity['name']}**" if activity else ""
        await interaction.response.edit_message(
            content=self.cog.pending_split_activities_text(interaction.guild.id, rows=parent.activities) + selected,
            view=parent,
        )


class PendingSplitActivitiesView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, activities):
        super().__init__(timeout=300)
        self.cog = cog
        self.admin_id = admin_id
        self.activities = list(activities)
        self.selected_activity_id: int | None = None
        self.add_item(PendingSplitActivitySelect(cog, activities=activities))

    async def require_owner_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
        return False

    def selected_activity(self):
        if self.selected_activity_id is None:
            return None
        return next(
            (row for row in self.activities if int(row["id"]) == self.selected_activity_id),
            None,
        )

    async def require_selected_activity(self, interaction: discord.Interaction):
        activity = self.selected_activity()
        if activity is None:
            await private_response(interaction, "Selecciona una actividad primero.")
        return activity

    @discord.ui.button(label="Revisar detalles", emoji="🔎", style=discord.ButtonStyle.secondary, row=1)
    async def details(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        activity = await self.require_selected_activity(interaction)
        if activity is None:
            return
        await private_response(
            interaction,
            self.cog.pending_split_activity_detail_text(interaction.guild, int(activity["id"])),
        )

    @discord.ui.button(label="Recordar caller", emoji="🔔", style=discord.ButtonStyle.primary, row=1)
    async def remind(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        activity = await self.require_selected_activity(interaction)
        if activity is None:
            return
        await self.cog.remind_pending_split_caller(interaction, int(activity["id"]))

    @discord.ui.button(label="Crear split", emoji="💰", style=discord.ButtonStyle.success, row=1)
    async def create_split(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_owner_admin(interaction):
            return
        activity = await self.require_selected_activity(interaction)
        if activity is None:
            return
        activities_cog = self.cog.bot.get_cog("Activities")
        if activities_cog is None or not hasattr(activities_cog, "build_payout_modal"):
            await private_response(interaction, "El panel de actividades no esta disponible.")
            return
        current = activities_cog.get_guild_activity(interaction.guild.id, int(activity["id"]))
        if current is None or current["status"] != ACTIVITY_FINISHED:
            await private_response(interaction, "Esta actividad ya no esta pendiente de split.")
            return
        await interaction.response.send_modal(activities_cog.build_payout_modal(int(activity["id"])))


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


class ApprovedPingChannelRemoveSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", guild: discord.Guild):
        self.cog = cog
        options = []
        for channel_id in sorted(cog.approved_ping_channel_ids(guild.id))[:25]:
            channel = guild.get_channel(channel_id)
            label = f"#{channel.name}" if channel is not None and hasattr(channel, "name") else str(channel_id)
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(channel_id),
                    description=f"ID {channel_id}"[:100],
                )
            )
        super().__init__(
            placeholder="Selecciona el canal aprobado a quitar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden administrar canales aprobados.")
            return
        channel_id = int(self.values[0])
        approved = self.cog.approved_ping_channel_ids(interaction.guild.id)
        if channel_id not in approved:
            await private_response(interaction, "Ese canal ya no esta en la lista aprobada.")
            return
        approved.discard(channel_id)
        self.cog.set_approved_ping_channel_ids(interaction.guild.id, approved)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Quitar canal aprobado para pings",
            system="Configuracion",
            observation=str(channel_id),
        )
        await private_response(
            interaction,
            f"Canal <#{channel_id}> quitado de aprobados.\n\n{self.cog.approved_ping_channels_text(interaction.guild.id)}",
        )


class ApprovedPingChannelRemoveView(discord.ui.View):
    def __init__(self, cog: "Admin", guild: discord.Guild):
        super().__init__(timeout=180)
        self.add_item(ApprovedPingChannelRemoveSelect(cog, guild))

def channel_setting_text(value: str | None) -> str:
    if value:
        return f"<#{value}>" if value.isdigit() else f"ID inválido: `{value}`"
    return "Sin configurar"


class PingPublicationChannelConfigView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel_select = discord.ui.ChannelSelect(
            placeholder=f"Selecciona {PING_PUBLICATIONS_LABEL}"[:150],
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.channel_select.callback = self.select_channel
        self.add_item(self.channel_select)
        self.approved_channel_select = discord.ui.ChannelSelect(
            placeholder="Selecciona canal para aprobar pings"[:150],
            channel_types=[discord.ChannelType.text, discord.ChannelType.news],
            min_values=1,
            max_values=1,
            row=2,
        )
        self.approved_channel_select.callback = self.approve_selected_channel
        self.add_item(self.approved_channel_select)

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(
            interaction,
            "Solo admins autorizados pueden configurar publicaciones de pings.",
        )
        return False

    async def save_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> None:
        self.cog.db.set_setting(
            interaction.guild.id,
            PING_PUBLICATIONS_SETTING_KEY,
            str(channel_id),
        )
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar canal de publicaciones de pings",
            system="Configuracion",
            observation=str(channel_id),
        )
        await private_response(
            interaction,
            (
                f"**{PING_PUBLICATIONS_LABEL}** actualizado a <#{channel_id}>.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )

    async def select_channel(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        channel = self.channel_select.values[0]
        await self.save_channel(interaction, int(channel.id))

    async def approve_selected_channel(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        channel = self.approved_channel_select.values[0]
        channel_id = int(channel.id)
        approved = self.cog.approved_ping_channel_ids(interaction.guild.id)
        approved.add(channel_id)
        self.cog.set_approved_ping_channel_ids(interaction.guild.id, approved)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Agregar canal aprobado para pings",
            system="Configuracion",
            observation=str(channel_id),
        )
        await private_response(
            interaction,
            f"Canal <#{channel_id}> aprobado para pings.\n\n{self.cog.approved_ping_channels_text(interaction.guild.id)}",
        )

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
            await private_response(interaction, "Este canal no admite publicaciones de pings.")
            return
        await self.save_channel(interaction, int(channel.id))

    @discord.ui.button(
        label="Quitar canal",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def clear_channel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(
            interaction.guild.id,
            PING_PUBLICATIONS_SETTING_KEY,
            "",
        )
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Quitar canal de publicaciones de pings",
            system="Configuracion",
            observation=PING_PUBLICATIONS_SETTING_KEY,
        )
        await private_response(
            interaction,
            (
                f"**{PING_PUBLICATIONS_LABEL}** quedó sin canal configurado.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )



    @discord.ui.button(
        label="Ver aprobados",
        style=discord.ButtonStyle.secondary,
        row=3,
    )
    async def show_approved_channels(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.approved_ping_channels_text(interaction.guild.id))

    @discord.ui.button(
        label="Quitar aprobado",
        style=discord.ButtonStyle.danger,
        row=3,
    )
    async def remove_approved_channel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        if not self.cog.approved_ping_channel_ids(interaction.guild.id):
            await private_response(interaction, "No hay canales aprobados para quitar.")
            return
        await private_response(
            interaction,
            "Elige el canal aprobado que quieres quitar:",
            view=ApprovedPingChannelRemoveView(self.cog, interaction.guild),
        )


class RegearChannelConfigView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel_select = discord.ui.ChannelSelect(
            placeholder=f"Selecciona {REGEAR_CHANNEL_LABEL}"[:150],
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
            "Solo admins autorizados pueden configurar el canal de Requips.",
        )
        return False

    async def save_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> None:
        self.cog.db.set_setting(
            interaction.guild.id,
            REGEAR_CHANNEL_SETTING_KEY,
            str(channel_id),
        )
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar canal de Requips",
            system="Configuracion",
            observation=str(channel_id),
        )
        await private_response(
            interaction,
            (
                f"✅ Canal de Requips configurado correctamente:\n<#{channel_id}>\n\n"
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
            await private_response(interaction, "Este canal no admite solicitudes de Requips.")
            return
        await self.save_channel(interaction, int(channel.id))

    @discord.ui.button(
        label="Quitar canal",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def clear_channel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(interaction.guild.id, REGEAR_CHANNEL_SETTING_KEY, "")
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Quitar canal de Requips",
            system="Configuracion",
            observation=REGEAR_CHANNEL_SETTING_KEY,
        )
        await private_response(
            interaction,
            (
                f"**{REGEAR_CHANNEL_LABEL}** quedó sin canal configurado.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )


class RegearNotificationChannelConfigView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog
        self.channel_select = discord.ui.ChannelSelect(
            placeholder=f"Selecciona {REGEAR_NOTIFICATION_CHANNEL_LABEL}"[:150],
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
            "Solo admins autorizados pueden configurar las notificaciones de Requips.",
        )
        return False

    async def save_channel(
        self,
        interaction: discord.Interaction,
        channel_id: int,
    ) -> None:
        self.cog.db.set_setting(
            interaction.guild.id,
            REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY,
            str(channel_id),
        )
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar notificaciones de Requips",
            system="Configuracion",
            observation=str(channel_id),
        )
        await private_response(
            interaction,
            (
                f"✅ Notificaciones de Requips configuradas correctamente:\n<#{channel_id}>\n\n"
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
            await private_response(interaction, "Este canal no admite notificaciones de Requips.")
            return
        await self.save_channel(interaction, int(channel.id))

    @discord.ui.button(
        label="Quitar canal",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def clear_channel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(interaction.guild.id, REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY, "")
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Quitar notificaciones de Requips",
            system="Configuracion",
            observation=REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY,
        )
        await private_response(
            interaction,
            (
                f"**{REGEAR_NOTIFICATION_CHANNEL_LABEL}** quedó sin canal configurado.\n\n"
                f"{self.cog.notification_settings_text(interaction.guild.id)}"
            ),
        )

class TicketChannelConfigView(discord.ui.View):
    def __init__(
        self,
        cog: "Admin",
        *,
        setting_key: str,
        label: str,
        conversation: bool = False,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.setting_key = setting_key
        self.label = label
        self.conversation = conversation
        channel_types = [discord.ChannelType.text] if conversation else [discord.ChannelType.text, discord.ChannelType.news]
        self.channel_select = discord.ui.ChannelSelect(
            placeholder=f"Selecciona {label}"[:150],
            channel_types=channel_types,
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
            f"Solo admins autorizados pueden configurar {self.label}.",
        )
        return False

    async def save_channel(self, interaction: discord.Interaction, channel) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta configuracion solo aplica dentro de un servidor.")
            return
        is_valid_channel = (
            is_normal_text_ticket_channel(channel)
            if self.conversation
            else is_text_ticket_channel(channel)
        )
        if not is_valid_channel:
            await private_response(interaction, f"Selecciona un canal de texto valido para {self.label}.")
            return
        missing = ticket_channel_permission_errors(
            channel,
            interaction.guild,
            conversation=self.conversation,
        )
        if missing:
            await private_response(
                interaction,
                "No puedo usar ese canal para tickets. Faltan permisos: " + ", ".join(missing) + ".",
            )
            return
        channel_id = int(channel.id)
        self.cog.db.set_setting(interaction.guild.id, self.setting_key, str(channel_id))
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action=f"Configurar {self.label}",
            system="Configuracion",
            observation=str(channel_id),
        )
        mention = getattr(channel, "mention", f"<#{channel_id}>")
        success_label = (
            "Canal de conversaciones de tickets"
            if self.conversation
            else "Canal de notificaciones de tickets"
        )
        await private_response(
            interaction,
            f"✅ {success_label} configurado correctamente: {mention}",
        )

    async def select_channel(self, interaction: discord.Interaction) -> None:
        if not await self.require_admin(interaction):
            return
        await self.save_channel(interaction, self.channel_select.values[0])

    @discord.ui.button(
        label="Usar canal actual",
        emoji="📍",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def use_current(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        channel = interaction.channel
        if channel is None:
            await private_response(interaction, "No pude identificar el canal actual.")
            return
        await self.save_channel(interaction, channel)

    @discord.ui.button(
        label="Quitar canal",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def clear_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        self.cog.db.set_setting(interaction.guild.id, self.setting_key, "")
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action=f"Quitar {self.label}",
            system="Configuracion",
            observation=self.setting_key,
        )
        await private_response(interaction, f"🎫 {self.label}\nNo configurado")

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
        label=PING_PUBLICATIONS_LABEL,
        emoji="📣",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def pings_publications(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar publicaciones de pings.",
            )
            return
        current = self.cog.db.get_setting(
            interaction.guild.id,
            PING_PUBLICATIONS_SETTING_KEY,
        )
        await private_response(
            interaction,
            (
                f"Configura **{PING_PUBLICATIONS_LABEL}**. "
                f"Actualmente: {channel_setting_text(current)}.\n"
                f"Aprobados para callers: {self.cog.approved_ping_channels_summary(interaction.guild.id)}.\n"
                "El primer selector cambia el canal predeterminado; el segundo aprueba canales para callers."
            ),
            view=PingPublicationChannelConfigView(self.cog),
        )

    @discord.ui.button(
        label=REGEAR_CHANNEL_LABEL,
        emoji="🛡️",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def regear_channel(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar el canal de Requips.",
            )
            return
        current = self.cog.db.get_setting(interaction.guild.id, REGEAR_CHANNEL_SETTING_KEY)
        await private_response(
            interaction,
            (
                f"Configura **{REGEAR_CHANNEL_LABEL}**. "
                f"Actualmente: {channel_setting_text(current)}.\n"
                "Selecciona un canal, usa el canal actual o quita el canal configurado."
            ),
            view=RegearChannelConfigView(self.cog),
        )

    @discord.ui.button(
        label=REGEAR_NOTIFICATION_CHANNEL_LABEL,
        emoji="🛡️",
        style=discord.ButtonStyle.primary,
        row=1,
    )
    async def regear_notifications(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar las notificaciones de Requips.",
            )
            return
        current = self.cog.db.get_setting(interaction.guild.id, REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY)
        await private_response(
            interaction,
            (
                f"Configura **{REGEAR_NOTIFICATION_CHANNEL_LABEL}**. "
                f"Actualmente: {channel_setting_text(current)}.\n"
                "Selecciona un canal, usa el canal actual o quita el canal configurado."
            ),
            view=RegearNotificationChannelConfigView(self.cog),
        )

    @discord.ui.button(
        label="Establecer canal de notificaciones de tickets",
        emoji="📢",
        style=discord.ButtonStyle.primary,
        row=2,
    )
    async def ticket_notification_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar el canal de notificaciones de tickets.",
            )
            return
        await private_response(
            interaction,
            (
                f"{self.cog.ticket_channel_status_text(interaction.guild.id)}\n\n"
                "Selecciona un canal de texto para recibir los embeds administrativos de tickets."
            ),
            view=TicketChannelConfigView(
                self.cog,
                setting_key=TICKET_CHANNEL_SETTING_KEY,
                label=TICKET_CHANNEL_LABEL,
            ),
        )

    @discord.ui.button(
        label="Establecer canal de conversaciones de tickets",
        emoji="🔒",
        style=discord.ButtonStyle.primary,
        row=2,
    )
    async def ticket_conversation_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(
                interaction,
                "Solo admins autorizados pueden configurar el canal de conversaciones de tickets.",
            )
            return
        await private_response(
            interaction,
            (
                f"{self.cog.ticket_channel_status_text(interaction.guild.id)}\n\n"
                "Selecciona un canal de texto normal para crear hilos privados de tickets."
            ),
            view=TicketChannelConfigView(
                self.cog,
                setting_key=TICKET_CONVERSATION_CHANNEL_SETTING_KEY,
                label=TICKET_CONVERSATION_CHANNEL_LABEL,
                conversation=True,
            ),
        )

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


class RegearRankingUserSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "Admin",
        guild: discord.Guild,
        filter_key: str,
        rows: list,
    ):
        self.cog = cog
        self.guild_id = guild.id
        self.filter_key = filter_key
        options: list[discord.SelectOption] = []
        for index, row in enumerate(rows[:25], start=1):
            user_id = int(row["user_id"])
            label = f"{index}. {short_member_label(guild, user_id)}"[:100]
            description = (
                f"Total {row['total_requests']} | Pagados {row['paid_count']} | "
                f"Ult. {discord_date(row['last_created'], 'd')}"
            )[:100]
            options.append(
                discord.SelectOption(
                    label=label,
                    value=str(user_id),
                    description=description,
                    emoji="🛡️",
                )
            )
        super().__init__(
            placeholder="Selecciona un jugador para ver su historial de requips",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden consultar este ranking.")
            return
        user_id = int(self.values[0])
        chunks = self.cog.regear_user_history_chunks(interaction.guild, user_id)
        await interaction.response.send_message(
            chunks[0],
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        for chunk in chunks[1:]:
            await interaction.followup.send(
                chunk,
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )


class RegearRankingView(discord.ui.View):
    def __init__(self, cog: "Admin", guild: discord.Guild, filter_key: str = "general"):
        super().__init__(timeout=300)
        self.cog = cog
        self.guild_id = guild.id
        self.filter_key = filter_key if filter_key in REGEAR_RANKING_FILTER_MAP else "general"
        rows = cog.regear_ranking_rows(guild.id, self.filter_key, limit=25)
        if rows:
            self.add_item(RegearRankingUserSelect(cog, guild, self.filter_key, rows))
        for key, label, _days in REGEAR_RANKING_FILTERS:
            button = discord.ui.Button(
                label=label,
                style=(
                    discord.ButtonStyle.primary
                    if key == self.filter_key
                    else discord.ButtonStyle.secondary
                ),
                row=1,
            )
            button.callback = self._filter_callback(key)
            self.add_item(button)

    def _filter_callback(self, filter_key: str):
        async def callback(interaction: discord.Interaction) -> None:
            if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
                await private_response(interaction, "Solo admins autorizados pueden consultar este ranking.")
                return
            await interaction.response.edit_message(
                content="Ranking de Requips:",
                embed=self.cog.regear_ranking_embed(interaction.guild, filter_key),
                view=RegearRankingView(self.cog, interaction.guild, filter_key),
            )

        return callback


class RankingsAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden consultar rankings.")
        return False

    @discord.ui.button(label="Resumen", emoji="🏆", style=discord.ButtonStyle.secondary, row=0)
    async def summary(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.edit_message(
                content=self.cog.rankings_text(interaction.guild.id),
                embed=None,
                view=RankingsAdminView(self.cog),
            )

    @discord.ui.button(label="Ranking de Requips", emoji="🛡️", style=discord.ButtonStyle.primary, row=0)
    async def regear_ranking(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.edit_message(
                content="Ranking de Requips:",
                embed=self.cog.regear_ranking_embed(interaction.guild, "general"),
                view=RegearRankingView(self.cog, interaction.guild, "general"),
            )


class ExtraAdminOptionsView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar estas opciones.")
        return False

    @discord.ui.button(label="Rankings", emoji="🏆", style=discord.ButtonStyle.secondary)
    async def rankings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.rankings_text(interaction.guild.id),
                view=RankingsAdminView(self.cog),
            )


class RateSettingModal(discord.ui.Modal):
    def __init__(self, cog: "Admin", *, key: str, current_value: str):
        title, label, placeholder = DEFAULT_RATE_SETTINGS[key]
        super().__init__(title=title, timeout=180)
        self.cog = cog
        self.key = key
        self.value_input = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            default=current_value[:40],
            max_length=40,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden modificar tasas.")
            return
        try:
            value = parse_admin_percent(str(self.value_input.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        self.cog.db.set_setting(interaction.guild.id, self.key, value)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar tasas predeterminadas",
            system="Configuracion",
            observation=f"{self.key}={value}",
        )
        await private_response(
            interaction,
            f"Tasa actualizada: `{self.key}` = `{value}%`.\n\n"
            f"{self.cog.default_rates_text(interaction.guild.id)}",
            view=DefaultRatesAdminView(self.cog),
        )


class DefaultRatesAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden modificar tasas.")
        return False

    async def open_rate_modal(self, interaction: discord.Interaction, key: str) -> None:
        if not await self.require_admin(interaction):
            return
        _title, _label, fallback = DEFAULT_RATE_SETTINGS[key]
        current = self.cog.db.get_setting(interaction.guild.id, key, fallback)
        await interaction.response.send_modal(
            RateSettingModal(self.cog, key=key, current_value=current)
        )

    @discord.ui.button(label="Ver tasas", style=discord.ButtonStyle.primary, row=0)
    async def show_rates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.default_rates_text(interaction.guild.id), view=self)

    @discord.ui.button(label="Comision transferencia", style=discord.ButtonStyle.secondary, row=0)
    async def transfer_fee(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_rate_modal(interaction, "transfer_fee_percent")

    @discord.ui.button(label="Gremio", style=discord.ButtonStyle.secondary, row=1)
    async def guild_rate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_rate_modal(interaction, "guild_percentage_default")

    @discord.ui.button(label="Mercado", style=discord.ButtonStyle.secondary, row=1)
    async def market_rate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_rate_modal(interaction, "market_rate_default")

    @discord.ui.button(label="Caller", style=discord.ButtonStyle.secondary, row=1)
    async def caller_rate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_rate_modal(interaction, "caller_percentage_default")


class ConfigAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar estas opciones.")
        return False

    @discord.ui.button(label="Notificaciones", emoji="🔔", style=discord.ButtonStyle.primary)
    async def notifications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.notification_settings_text(interaction.guild.id),
                view=NotificationsAdminView(self.cog),
            )

    @discord.ui.button(label="Panel de Soporte", emoji="🎫", style=discord.ButtonStyle.primary)
    async def support_panel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        support_cog = self.cog.bot.get_cog("Support")
        if support_cog is None:
            await private_response(interaction, "El módulo de soporte no está disponible.")
            return
        await private_response(
            interaction,
            support_cog.support_panel_status_text(interaction.guild),
            view=SupportAdminView(support_cog),
        )

    @discord.ui.button(label="Tasas predeterminadas", style=discord.ButtonStyle.secondary)
    async def default_rates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.default_rates_text(interaction.guild.id),
                view=DefaultRatesAdminView(self.cog),
            )


class LegacyAdminPanelCallbacksView(discord.ui.View):
    """Keeps buttons from already-published admin panels working after the redesign."""

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=None)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False

    @discord.ui.button(label="Rankings", custom_id="g3n:admin:rankings")
    async def rankings(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.rankings_text(interaction.guild.id),
                view=RankingsAdminView(self.cog),
            )

    @discord.ui.button(label="Notificaciones", custom_id="g3n:admin:notifications")
    async def notifications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.notification_settings_text(interaction.guild.id),
                view=NotificationsAdminView(self.cog),
            )

    @discord.ui.button(label="Agregar admin", custom_id="g3n:admin:add_admin")
    async def add_admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al usuario que deseas autorizar como admin o ingresa su ID:",
                view=AdminSelectionView(
                    self.cog,
                    action="add",
                    admin_id=interaction.user.id,
                ),
            )

    @discord.ui.button(label="Eliminar admin", custom_id="g3n:admin:remove_admin")
    async def remove_admin(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona al admin que deseas retirar o ingresa su ID:",
                view=AdminSelectionView(
                    self.cog,
                    action="remove",
                    admin_id=interaction.user.id,
                ),
            )

    @discord.ui.button(label="Callers", custom_id="g3n:admin:callers")
    async def callers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).callers(interaction, button)

    @discord.ui.button(label="Reclutadores", custom_id="g3n:admin:recruiters")
    async def recruiters(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).recruiters(interaction, button)

    @discord.ui.button(label="Delegados de pago", custom_id="g3n:admin:payment_delegates")
    async def payment_delegates(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).payment_delegates(interaction, button)

    @discord.ui.button(label="Admins", custom_id="g3n:admin:admins")
    async def admins(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).admins(interaction, button)


class MemberPenaltyUserSelect(discord.ui.UserSelect):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(
            placeholder="Selecciona un miembro",
            min_values=1,
            max_values=1,
            custom_id=f"g3n:admin:members:penalties:{action}:user",
        )
        self.cog = cog
        self.action = action
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        parent = self.view
        if not isinstance(parent, MemberPenaltyUserSelectView):
            await private_response(interaction, "No pude procesar esta seleccion.")
            return
        if interaction.user.id != self.admin_id or not parent.can_use(interaction):
            await private_response(interaction, "\u274c No tienes permisos para gestionar penalizaciones.")
            return
        member = self.values[0]
        if self.action == "view":
            await private_response(
                interaction,
                self.cog.activity_penalties_text(interaction.guild.id, member),
            )
            return
        penalties = self.cog.active_activity_penalties(interaction.guild.id, member.id)
        if not penalties:
            await private_response(interaction, "\u2705 Este miembro no tiene penalizaciones activas.")
            return
        await private_response(
            interaction,
            f"Selecciona la penalizacion activa de {member.mention} que deseas eliminar:",
            view=MemberPenaltyRemoveSelectView(
                self.cog,
                admin_id=interaction.user.id,
                member_id=member.id,
                member_mention=member.mention,
                penalties=penalties,
            ),
        )


class MemberPenaltyUserSelectView(discord.ui.View):
    def __init__(self, cog: "Admin", *, action: str, admin_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.add_item(MemberPenaltyUserSelect(cog, action=action, admin_id=admin_id))

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and (
            is_admin_subject(self.cog.db, interaction)
            or is_caller_panel_subject(self.cog.db, interaction)
        )


class MemberPenaltyRemoveSelect(discord.ui.Select):
    def __init__(self, parent_view: "MemberPenaltyRemoveSelectView"):
        options = []
        for row in parent_view.penalties[:25]:
            penalty_id = int(row["id"])
            reason = str(row["motivo"])
            origin = str(row["origen"])
            date = str(row["fecha_ingreso"])
            options.append(
                discord.SelectOption(
                    label=f"ID {penalty_id} - {reason}"[:100],
                    value=str(penalty_id),
                    description=f"{origin} - {date}"[:100],
                    emoji="\u26a0\ufe0f",
                )
            )
        super().__init__(
            placeholder="Selecciona una penalizacion activa",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="g3n:admin:members:penalties:remove:select",
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.parent_view.admin_id or not self.parent_view.can_use(interaction):
            await private_response(interaction, "\u274c No tienes permisos para gestionar penalizaciones.")
            return
        penalty = self.parent_view.penalty_by_id(int(self.values[0]))
        if penalty is None:
            await private_response(interaction, "No encontre esa penalizacion activa.")
            return
        content = self.parent_view.confirmation_text(penalty)
        await interaction.response.edit_message(
            content=content,
            view=MemberPenaltyConfirmRemovalView(
                self.parent_view.cog,
                admin_id=interaction.user.id,
                member_id=self.parent_view.member_id,
                member_mention=self.parent_view.member_mention,
                penalty_id=int(penalty["id"]),
            ),
        )


class MemberPenaltyRemoveSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "Admin",
        *,
        admin_id: int,
        member_id: int,
        member_mention: str,
        penalties,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.member_id = member_id
        self.member_mention = member_mention
        self.penalties = list(penalties)
        self.add_item(MemberPenaltyRemoveSelect(self))

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and (
            is_admin_subject(self.cog.db, interaction)
            or is_caller_panel_subject(self.cog.db, interaction)
        )

    def penalty_by_id(self, penalty_id: int):
        return next((row for row in self.penalties if int(row["id"]) == penalty_id), None)

    def confirmation_text(self, penalty) -> str:
        return (
            "⚠️ ¿Confirmas que deseas eliminar esta penalizacion?\n\n"
            f"Usuario: {self.member_mention}\n"
            f"Motivo: {penalty['motivo']}\n"
            f"Fecha: {penalty['fecha_ingreso']}\n"
            f"ID: {penalty['id']}"
        )

class MemberPenaltyConfirmRemovalView(discord.ui.View):
    def __init__(
        self,
        cog: "Admin",
        *,
        admin_id: int,
        member_id: int,
        member_mention: str,
        penalty_id: int,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.member_id = member_id
        self.member_mention = member_mention
        self.penalty_id = penalty_id

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and interaction.user.id == self.admin_id and (
            is_admin_subject(self.cog.db, interaction)
            or is_caller_panel_subject(self.cog.db, interaction)
        )

    @discord.ui.button(label="Confirmar", emoji="\u2705", style=discord.ButtonStyle.danger, custom_id="g3n:admin:members:penalties:remove:confirm")
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.can_use(interaction):
            await private_response(interaction, "\u274c No tienes permisos para gestionar penalizaciones.")
            return
        penalty = self.cog.remove_activity_penalty(
            interaction.guild.id,
            penalty_id=self.penalty_id,
            user_id=self.member_id,
            removed_by=interaction.user.id,
            observation="Eliminada desde Panel de Admins > Gestion de usuarios > Miembros",
        )
        if penalty is None:
            await interaction.response.edit_message(
                content="No encontre esa penalizacion activa o ya fue eliminada.",
                view=None,
            )
            return
        await interaction.response.edit_message(
            content=(
                "✅ Penalizacion eliminada correctamente.\n\n"
                f"Usuario: {self.member_mention}\n"
                f"Motivo: {penalty['motivo']}\n"
                f"Eliminada por: <@{interaction.user.id}>"
            ),
            view=None,
        )

    @discord.ui.button(label="Cancelar", emoji="\u274c", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:members:penalties:remove:cancel")
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await private_response(interaction, "Solo quien abrio esta confirmacion puede cancelarla.")
            return
        await interaction.response.edit_message(content="Operacion cancelada.", view=None)


class MemberAccessRoleAddSelect(discord.ui.RoleSelect):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(placeholder="Selecciona el rol equivalente a miembro", min_values=1, max_values=1)
        self.cog = cog
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            log_action(
                self.cog.db,
                interaction.guild.id,
                admin_id=interaction.user.id,
                action="Intento sin permisos roles de miembro",
                system="Permisos",
                observation="add",
            )
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        role = self.values[0]
        added = add_member_access_role(self.cog.db, interaction.guild.id, role.id)
        if not added:
            await private_response(interaction, f"{role.mention} ya esta configurado como rol de miembro.")
            return
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Agregar rol equivalente de miembro",
            system="Permisos",
            observation=str(role.id),
        )
        await private_response(interaction, f"✅ Rol equivalente de miembro añadido: {role.mention}")


class MemberAccessRoleAddView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int):
        super().__init__(timeout=300)
        self.add_item(MemberAccessRoleAddSelect(cog, admin_id=admin_id))


class MemberAccessRoleRemoveSelect(discord.ui.Select):
    def __init__(self, cog: "Admin", *, admin_id: int, guild: discord.Guild):
        configured_roles = sorted(configured_member_role_ids(cog.db, guild.id) - {OFFICIAL_MEMBER_ROLE_ID})
        options = []
        for role_id in configured_roles[:25]:
            role = guild.get_role(role_id)
            label = role.name if role is not None else f"Rol no disponible {role_id}"
            description = f"ID {role_id}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(role_id),
                    description=description[:100],
                    emoji="👥",
                )
            )
        super().__init__(placeholder="Selecciona el rol equivalente que deseas quitar", min_values=1, max_values=1, options=options)
        self.cog = cog
        self.admin_id = admin_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        if interaction.user.id != self.admin_id or not is_admin_subject(self.cog.db, interaction):
            log_action(
                self.cog.db,
                interaction.guild.id,
                admin_id=interaction.user.id,
                action="Intento sin permisos roles de miembro",
                system="Permisos",
                observation="remove",
            )
            await private_response(interaction, "Solo el admin que abrio este menu puede usarlo.")
            return
        role_id = int(self.values[0])
        try:
            removed = remove_member_access_role(self.cog.db, interaction.guild.id, role_id)
        except ValueError as exc:
            await private_response(interaction, f"❌ {exc}")
            return
        role = interaction.guild.get_role(role_id)
        mention = role.mention if role is not None else f"`{role_id}`"
        if not removed:
            await private_response(interaction, f"{mention} no estaba configurado como rol equivalente.")
            return
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Quitar rol equivalente de miembro",
            system="Permisos",
            observation=str(role_id),
        )
        await private_response(interaction, f"✅ Rol equivalente de miembro quitado: {mention}")


class MemberAccessRoleRemoveView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, guild: discord.Guild):
        super().__init__(timeout=300)
        self.add_item(MemberAccessRoleRemoveSelect(cog, admin_id=admin_id, guild=guild))


class MemberAccessRolesView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden configurar roles de miembro.")
        return False

    @discord.ui.button(label="Añadir rol de miembro", emoji="➕", style=discord.ButtonStyle.success, custom_id="g3n:admin:members:roles:add", row=0)
    async def add_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona el rol que tendra permisos funcionales de miembro:",
                view=MemberAccessRoleAddView(self.cog, admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Quitar rol de miembro", emoji="➖", style=discord.ButtonStyle.danger, custom_id="g3n:admin:members:roles:remove", row=0)
    async def remove_role(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        configured_roles = configured_member_role_ids(self.cog.db, interaction.guild.id) - {OFFICIAL_MEMBER_ROLE_ID}
        if not configured_roles:
            await private_response(interaction, "No hay roles equivalentes adicionales configurados.")
            return
        await private_response(
            interaction,
            "Selecciona el rol equivalente que deseas quitar:",
            view=MemberAccessRoleRemoveView(self.cog, admin_id=interaction.user.id, guild=interaction.guild),
        )

    @discord.ui.button(label="Ver roles de miembro", emoji="📋", style=discord.ButtonStyle.primary, custom_id="g3n:admin:members:roles:view", row=0)
    async def view_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.member_access_roles_text(interaction.guild))

    @discord.ui.button(label="Volver", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:members:roles:back", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Gestión de miembros:",
            embed=None,
            view=MembersAdminView(self.cog),
        )


class MembersAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_penalty_manager(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and (
            is_admin_subject(self.cog.db, interaction)
            or is_caller_panel_subject(self.cog.db, interaction)
        ):
            return True
        await private_response(interaction, "\u274c No tienes permisos para gestionar penalizaciones.")
        return False

    @discord.ui.button(label="Ver penalizaciones", emoji="\U0001f50e", style=discord.ButtonStyle.primary, custom_id="g3n:admin:members:penalties:view", row=0)
    async def view_penalties(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_penalty_manager(interaction):
            await private_response(
                interaction,
                "Selecciona el miembro que deseas consultar:",
                view=MemberPenaltyUserSelectView(self.cog, action="view", admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Eliminar penalizaci\u00f3n", emoji="\U0001f5d1\ufe0f", style=discord.ButtonStyle.danger, custom_id="g3n:admin:members:penalties:remove", row=0)
    async def remove_penalty(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_penalty_manager(interaction):
            await private_response(
                interaction,
                "Selecciona el miembro al que deseas eliminarle una penalizacion activa:",
                view=MemberPenaltyUserSelectView(self.cog, action="remove", admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Roles de miembro", emoji="👥", style=discord.ButtonStyle.primary, custom_id="g3n:admin:members:roles", row=1)
    async def member_roles(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            await interaction.response.edit_message(
                content=self.cog.member_access_roles_text(interaction.guild),
                embed=None,
                view=MemberAccessRolesView(self.cog),
            )
            return
        await private_response(interaction, "Solo admins autorizados pueden configurar roles de miembro.")

    @discord.ui.button(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:members:back", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Gesti\u00f3n de usuarios:",
            embed=None,
            view=UserManagementAdminView(self.cog),
        )


class UserManagementAdminView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False

    @discord.ui.button(label="Callers", emoji="\U0001f4e3", style=discord.ButtonStyle.primary, custom_id="g3n:admin:user_management:callers", row=0)
    async def callers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).callers(interaction, button)

    @discord.ui.button(label="Reclutadores", emoji="\U0001f6e1\ufe0f", style=discord.ButtonStyle.primary, custom_id="g3n:admin:user_management:recruiters", row=0)
    async def recruiters(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).recruiters(interaction, button)

    @discord.ui.button(label="Delegados de pago", emoji="\U0001f465", style=discord.ButtonStyle.primary, custom_id="g3n:admin:user_management:payment_delegates", row=0)
    async def payment_delegates(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).payment_delegates(interaction, button)

    @discord.ui.button(label="Admins", emoji="\U0001f510", style=discord.ButtonStyle.primary, custom_id="g3n:admin:user_management:admins", row=0)
    async def admins(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await AdminPanelView(self.cog).admins(interaction, button)

    @discord.ui.button(label="Miembros", emoji="\U0001f464", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:user_management:members", row=1)
    async def members(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.edit_message(
                content="Gesti\u00f3n de miembros:",
                embed=None,
                view=MembersAdminView(self.cog),
            )

    @discord.ui.button(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:user_management:back", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content="Panel administrativo:",
            embed=None,
            view=AdminPanelView(self.cog),
        )



ACTIVITY_AUDIT_PAGE_SIZE = 4
ACTIVITY_AUDIT_DETAIL_PAGE_SIZE = 8


def activity_audit_short_date(value: str | None) -> str:
    return str(value or "Sin fecha")[:10]


def activity_audit_user_label(guild: discord.Guild | None, user_id: int | None) -> str:
    if user_id is None:
        return "Sin registro"
    member = guild.get_member(int(user_id)) if guild is not None else None
    if member is None:
        return f"Usuario fuera del servidor \u00b7 ID {int(user_id)}"
    return f"{member.mention} \u00b7 {member.display_name}"


def activity_audit_plain_user_name(guild: discord.Guild | None, user_id: int | None) -> str:
    if user_id is None:
        return ""
    member = guild.get_member(int(user_id)) if guild is not None else None
    if member is None:
        return f"Usuario fuera del servidor ID {int(user_id)}"
    return member.display_name


def activity_audit_clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "\u2026"


def activity_audit_channel_text(guild: discord.Guild | None, channel_id: int | None) -> str:
    if channel_id is None:
        return "Publicado en: Sin canal registrado"
    get_channel = getattr(guild, "get_channel", None) if guild is not None else None
    channel = get_channel(int(channel_id)) if callable(get_channel) else None
    if channel is None:
        return f"Publicado en: Canal no disponible \u00b7 ID {int(channel_id)}"
    mention = getattr(channel, "mention", None)
    if mention:
        return f"Publicado en: {mention}"
    name = getattr(channel, "name", None)
    return f"Publicado en: #{name}" if name else f"Publicado en: Canal ID {int(channel_id)}"


def activity_audit_ping_url(record: ActivityAuditRecord) -> str | None:
    if record.guild_id and record.channel_id and record.message_id:
        return f"https://discord.com/channels/{record.guild_id}/{record.channel_id}/{record.message_id}"
    return None


def activity_audit_thread_url(record: ActivityAuditRecord) -> str | None:
    if not (record.guild_id and record.thread_id):
        return None
    if record.thread_panel_message_id:
        return f"https://discord.com/channels/{record.guild_id}/{record.thread_id}/{record.thread_panel_message_id}"
    return f"https://discord.com/channels/{record.guild_id}/{record.thread_id}"


def activity_audit_publication_text(guild: discord.Guild | None, record: ActivityAuditRecord) -> str:
    lines = [activity_audit_channel_text(guild, record.channel_id)]
    if activity_audit_ping_url(record) is None:
        lines.append("Enlace al ping no disponible para esta actividad hist\u00f3rica")
    return "\n".join(lines)


def add_activity_audit_link_buttons(view: discord.ui.View, record: ActivityAuditRecord | None, *, row: int) -> None:
    if record is None:
        return
    ping_url = activity_audit_ping_url(record)
    if ping_url is not None:
        view.add_item(
            discord.ui.Button(
                label="Abrir ping",
                emoji="\U0001f517",
                style=discord.ButtonStyle.link,
                url=ping_url,
                row=row,
            )
        )
    thread_url = activity_audit_thread_url(record)
    if thread_url is not None:
        view.add_item(
            discord.ui.Button(
                label="Abrir hilo",
                emoji="\U0001f9f5",
                style=discord.ButtonStyle.link,
                url=thread_url,
                row=row,
            )
        )


async def edit_activity_audit_message(
    interaction: discord.Interaction,
    *,
    content: str,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.edit_message(content=content, embed=embed, view=view)


def build_activity_audit_home_embed(cog: "Admin", guild: discord.Guild) -> discord.Embed:
    dataset = get_activity_audit_dataset(cog.db, guild.id)
    summary = dataset.summary
    embed = discord.Embed(
        title="📊 Auditoría de actividades",
        description="Consulta solo lectura de actividades desde `ACT-000050`.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Total actividades", value=str(summary.total), inline=True)
    embed.add_field(name="Spliteadas", value=str(summary.split), inline=True)
    embed.add_field(name="Pendientes", value=str(summary.pending), inline=True)
    embed.add_field(name="Sin split por diseño", value=str(summary.no_split), inline=True)
    embed.add_field(name="Canceladas", value=str(summary.cancelled), inline=True)
    embed.add_field(name="Plata depositada", value=format_amount(summary.total_deposited), inline=True)
    embed.add_field(name="Actividad más antigua", value=activity_audit_short_date(summary.oldest_activity_date), inline=True)
    embed.add_field(name="Actividad más reciente", value=activity_audit_short_date(summary.newest_activity_date), inline=True)
    return embed


def build_activity_audit_list_embed(
    cog: "Admin",
    guild: discord.Guild,
    mode: str,
    page: int,
) -> tuple[discord.Embed, list[ActivityAuditRecord], int]:
    dataset = get_activity_audit_dataset(cog.db, guild.id)
    rows = dataset.filter_records(mode)
    title_by_mode = {
        "all": "📋 Todas las actividades",
        "split": "✅ Actividades spliteadas",
        "pending": "⏳ Actividades pendientes",
    }
    empty_by_mode = {
        "all": "No hay actividades desde ACT-000050.",
        "split": "No hay actividades spliteadas desde ACT-000050.",
        "pending": "No hay actividades pendientes desde ACT-000050.",
    }
    total_pages = max(1, (len(rows) + ACTIVITY_AUDIT_PAGE_SIZE - 1) // ACTIVITY_AUDIT_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * ACTIVITY_AUDIT_PAGE_SIZE
    page_rows = rows[start : start + ACTIVITY_AUDIT_PAGE_SIZE]
    embed = discord.Embed(title=title_by_mode.get(mode, title_by_mode["all"]), color=discord.Color.teal())
    if not page_rows:
        embed.description = empty_by_mode.get(mode, empty_by_mode["all"])
    else:
        lines: list[str] = []
        for index, record in enumerate(page_rows, start=start + 1):
            pinged_by = activity_audit_user_label(guild, record.pinged_by_id)
            caller = activity_audit_user_label(guild, record.caller_id) if record.caller_id else "Sin caller"
            elapsed = pending_days(record.created_at) if record.is_pending else None
            lines.extend(
                [
                    f"**{index}. `{record.code}` · {activity_audit_clip(record.name, 42)}**",
                    f"Fecha: `{activity_audit_short_date(record.created_at)}` · Pingueada por: {pinged_by}",
                    f"Discord ID: `{record.pinged_by_id or 'Sin registro'}` · Caller asignado: {caller}",
                    f"Estado real: `{record.real_status}` · Auditoría: **{record.audit_label}**",
                    f"Depositado: **{format_amount(record.total_deposited)}** · Beneficiarios: `{record.beneficiaries}`",
                    f"Primer depósito: `{activity_audit_short_date(record.first_deposit_at)}` · Último depósito: `{activity_audit_short_date(record.last_deposit_at)}`",
                ]
            )
            if record.is_pending:
                lines.append(f"Tiempo transcurrido: `{elapsed if elapsed is not None else 'N/D'} días` · Motivo: {record.observations}")
            lines.append("")
        embed.description = "\n".join(lines)[:3900]
    embed.set_footer(text=f"Página {page + 1} de {total_pages} · {len(rows)} actividades")
    return embed, page_rows, total_pages


def activity_audit_voice_summary(cog: "Admin", record: ActivityAuditRecord) -> str:
    stats = get_persisted_activity_voice_stats(cog.db, record.guild_id, record.internal_id)
    if not stats:
        return "Sin estad\u00edsticas de voz guardadas."
    summary = summarize_voice_stats(stats)
    return (
        f"Duraci\u00f3n: `{format_duration(summary.monitoring_duration_seconds)}` \u00b7 "
        f"Participantes: **{summary.total_participants}** \u00b7 "
        f"Promedio: **{summary.average_attendance_percentage:.1f}%**\n"
        f"Hasta el final: `{summary.stayed_until_end}` \u00b7 "
        f"Se retiraron: `{summary.left_before_end}` \u00b7 "
        f"Nunca entraron: `{summary.never_joined}`"
    )


def build_activity_audit_record_embed(cog: "Admin", guild: discord.Guild, record: ActivityAuditRecord) -> discord.Embed:
    embed = discord.Embed(
        title=f"{record.code} · {record.name}",
        description="Consulta individual de auditoría de actividad.",
        color=discord.Color.gold() if record.is_pending else discord.Color.green() if record.is_split else discord.Color.dark_gray(),
    )
    embed.add_field(name="Fecha", value=activity_audit_short_date(record.created_at), inline=True)
    embed.add_field(name="Pingueada por", value=activity_audit_user_label(guild, record.pinged_by_id), inline=False)
    embed.add_field(name="Discord ID", value=str(record.pinged_by_id or "Sin registro"), inline=True)
    embed.add_field(name="Caller asignado", value=activity_audit_user_label(guild, record.caller_id) if record.caller_id else "Sin caller", inline=False)
    embed.add_field(name="Caller Discord ID", value=str(record.caller_id or "Sin caller"), inline=True)
    embed.add_field(name="Tipo", value=record.activity_type, inline=True)
    embed.add_field(name="Estado real", value=record.real_status, inline=True)
    embed.add_field(name="Estado auditoría", value=record.audit_label, inline=True)
    embed.add_field(name="Total depositado", value=format_amount(record.total_deposited), inline=True)
    embed.add_field(name="Beneficiarios", value=str(record.beneficiaries), inline=True)
    embed.add_field(name="Movimientos", value=str(record.movement_count), inline=True)
    embed.add_field(name="Primer depósito", value=activity_audit_short_date(record.first_deposit_at), inline=True)
    embed.add_field(name="Último depósito", value=activity_audit_short_date(record.last_deposit_at), inline=True)
    if record.is_pending:
        elapsed = pending_days(record.created_at)
        embed.add_field(name="Tiempo pendiente", value=f"{elapsed if elapsed is not None else 'N/D'} días", inline=True)
    embed.add_field(name="Observaciones", value=record.observations or "Sin observaciones", inline=False)
    embed.add_field(name="Publicaci\u00f3n", value=activity_audit_publication_text(guild, record), inline=False)
    embed.add_field(name="Estad\u00edsticas de voz", value=activity_audit_voice_summary(cog, record), inline=False)
    if not record.has_split_details:
        embed.set_footer(text="Esta actividad no tiene depósitos asociados.")
    return embed


def build_activity_audit_details_embed(
    cog: "Admin",
    guild: discord.Guild,
    code: str,
    page: int,
) -> tuple[discord.Embed, int]:
    dataset = get_activity_audit_dataset(cog.db, guild.id)
    record = dataset.get_record(code)
    movements = list(dataset.movements_for(code))
    total_pages = max(1, (len(movements) + ACTIVITY_AUDIT_DETAIL_PAGE_SIZE - 1) // ACTIVITY_AUDIT_DETAIL_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    embed = discord.Embed(title=f"📋 Detalles del split · {normalize_activity_code(code) or code}", color=discord.Color.green())
    if record is None:
        embed.description = "No encontré esa actividad."
        return embed, total_pages
    if not movements:
        embed.description = "⏳ Esta actividad no tiene depósitos asociados."
        return embed, total_pages
    start = page * ACTIVITY_AUDIT_DETAIL_PAGE_SIZE
    page_movements = movements[start : start + ACTIVITY_AUDIT_DETAIL_PAGE_SIZE]
    rows = [f"{'FECHA':<10} {'CONCEPTO':<24} {'USUARIO':<18} {'CANTIDAD':>12}"]
    for movement in page_movements:
        rows.append(
            f"{activity_audit_short_date(movement.date):<10} "
            f"{activity_audit_clip(movement.concept, 24):<24} "
            f"{activity_audit_clip(activity_audit_plain_user_name(guild, movement.user_id), 18):<18} "
            f"{format_money(movement.amount):>12}"
        )
    embed.description = "```\n" + "\n".join(rows)[:3600] + "\n```"
    embed.add_field(name="Total general", value=format_amount(sum(item.amount for item in movements)), inline=True)
    embed.add_field(name="Beneficiarios únicos", value=str(len({item.user_id for item in movements if item.user_id is not None})), inline=True)
    embed.add_field(name="Total movimientos", value=str(len(movements)), inline=True)
    embed.add_field(name="Primer depósito", value=activity_audit_short_date(record.first_deposit_at), inline=True)
    embed.add_field(name="Último depósito", value=activity_audit_short_date(record.last_deposit_at), inline=True)
    embed.add_field(name="Publicaci\u00f3n", value=activity_audit_publication_text(guild, record), inline=False)
    embed.add_field(name="Estad\u00edsticas de voz", value=activity_audit_voice_summary(cog, record), inline=False)
    embed.set_footer(text=f"Página {page + 1} de {total_pages}")
    return embed, total_pages


class ActivityAuditSearchModal(discord.ui.Modal, title="Buscar actividad"):
    activity = discord.ui.TextInput(label="Actividad", placeholder="ACT-000060, 000060 o 60", max_length=20)

    def __init__(self, cog: "Admin"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
            return
        code = normalize_activity_code(str(self.activity.value))
        if code is None:
            await private_response(interaction, "No pude interpretar ese código de actividad.")
            return
        dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(code)
        if record is None:
            await private_response(interaction, f"No encontré `{code}` desde ACT-000050.")
            return
        await private_response(
            interaction,
            f"Resultado de búsqueda `{code}`:",
            embed=build_activity_audit_record_embed(self.cog, interaction.guild, record),
            view=ActivityAuditRecordView(self.cog, code, has_details=record.has_split_details, record=record),
        )


class ActivityAuditBaseView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False


class ActivityAuditHomeView(ActivityAuditBaseView):
    @discord.ui.button(label="Todas las actividades", emoji="📋", style=discord.ButtonStyle.primary, custom_id="g3n:admin:activity_audit:all", row=0)
    async def all_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await edit_activity_audit_message(
            interaction,
            content="Todas las actividades desde ACT-000050:",
            embed=build_activity_audit_list_embed(self.cog, interaction.guild, "all", 0)[0],
            view=ActivityAuditListView.from_records(self.cog, "all", 0, build_activity_audit_list_embed(self.cog, interaction.guild, "all", 0)[1]),
        )

    @discord.ui.button(label="Spliteadas", emoji="✅", style=discord.ButtonStyle.success, custom_id="g3n:admin:activity_audit:split", row=0)
    async def split_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await edit_activity_audit_message(
            interaction,
            content="Actividades spliteadas desde ACT-000050:",
            embed=build_activity_audit_list_embed(self.cog, interaction.guild, "split", 0)[0],
            view=ActivityAuditListView.from_records(self.cog, "split", 0, build_activity_audit_list_embed(self.cog, interaction.guild, "split", 0)[1]),
        )

    @discord.ui.button(label="Pendientes", emoji="⏳", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:pending", row=0)
    async def pending_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await edit_activity_audit_message(
            interaction,
            content="Actividades pendientes desde ACT-000050:",
            embed=build_activity_audit_list_embed(self.cog, interaction.guild, "pending", 0)[0],
            view=ActivityAuditListView.from_records(self.cog, "pending", 0, build_activity_audit_list_embed(self.cog, interaction.guild, "pending", 0)[1]),
        )

    @discord.ui.button(label="Buscar actividad", emoji="🔍", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:search", row=1)
    async def search(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await interaction.response.send_modal(ActivityAuditSearchModal(self.cog))

    @discord.ui.button(label="Descargar reporte", emoji="📥", style=discord.ButtonStyle.primary, custom_id="g3n:admin:activity_audit:download", row=1)
    async def download(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await interaction.response.defer(ephemeral=True)
        name_resolver = lambda user_id: activity_audit_plain_user_name(interaction.guild, user_id)
        report_files = build_activity_audit_report_files(
            self.cog.db,
            interaction.guild.id,
            name_resolver=name_resolver,
        )
        for index in range(0, len(report_files), 10):
            batch = report_files[index : index + 10]
            files = [discord.File(io.BytesIO(item.data), filename=item.filename) for item in batch]
            await interaction.followup.send(
                "Reporte de auditoría de actividades generado." if index == 0 else "Reporte de auditoría de actividades, continuación.",
                files=files,
                ephemeral=True,
            )

    @discord.ui.button(label="Volver", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:back_admin", row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await edit_activity_audit_message(
            interaction,
            content="Panel Administrativo G3NESYS",
            embed=None,
            view=AdminPanelView(self.cog),
        )


class ActivityAuditDetailButton(discord.ui.Button):
    def __init__(self, cog: "Admin", code: str, mode: str, page: int, index: int):
        super().__init__(
            label=f"Detalles {index}",
            emoji="📋",
            style=discord.ButtonStyle.secondary,
            custom_id=f"g3n:admin:activity_audit:detail:{code}:{mode}:{page}",
            row=2,
        )
        self.cog = cog
        self.code = code
        self.mode = mode
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        gate = ActivityAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        view = ActivityAuditDetailsView(self.cog, self.code, 0, back_mode=self.mode, back_page=self.page, record=record)
        embed, _ = build_activity_audit_details_embed(self.cog, interaction.guild, self.code, 0)
        await edit_activity_audit_message(interaction, content=f"Detalles del split `{self.code}`:", embed=embed, view=view)


class ActivityAuditListView(ActivityAuditBaseView):
    def __init__(self, cog: "Admin", mode: str, page: int):
        super().__init__(cog)
        self.mode = mode
        self.page = page
        self._add_detail_buttons()

    def _page_records(self, guild_id: int) -> list[ActivityAuditRecord]:
        dataset = get_activity_audit_dataset(self.cog.db, guild_id)
        rows = dataset.filter_records(self.mode)
        start = self.page * ACTIVITY_AUDIT_PAGE_SIZE
        return rows[start : start + ACTIVITY_AUDIT_PAGE_SIZE]

    def _add_detail_buttons(self) -> None:
        return

    async def _refresh(self, interaction: discord.Interaction, page: int) -> None:
        embed, page_rows, _ = build_activity_audit_list_embed(self.cog, interaction.guild, self.mode, page)
        view = ActivityAuditListView.from_records(self.cog, self.mode, page, page_rows)
        await edit_activity_audit_message(interaction, content="Auditoría de actividades:", embed=embed, view=view)

    @classmethod
    def from_records(cls, cog: "Admin", mode: str, page: int, records: list[ActivityAuditRecord]) -> "ActivityAuditListView":
        view = cls.__new__(cls)
        ActivityAuditBaseView.__init__(view, cog)
        view.mode = mode
        view.page = page
        for offset, record in enumerate(records, start=1 + page * ACTIVITY_AUDIT_PAGE_SIZE):
            if record.has_split_details:
                view.add_item(ActivityAuditDetailButton(cog, record.code, mode, page, offset))
        view.add_item(ActivityAuditPreviousButton(cog, mode, page))
        view.add_item(ActivityAuditNextButton(cog, mode, page))
        view.add_item(ActivityAuditBackHomeButton(cog))
        return view


class ActivityAuditPreviousButton(discord.ui.Button):
    def __init__(self, cog: "Admin", mode: str, page: int):
        super().__init__(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:activity_audit:prev:{mode}:{page}", row=4, disabled=page <= 0)
        self.cog = cog
        self.mode = mode
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view = ActivityAuditListView.from_records(self.cog, self.mode, max(0, self.page - 1), [])
        if not await view.require_admin(interaction):
            return
        embed, page_rows, _ = build_activity_audit_list_embed(self.cog, interaction.guild, self.mode, max(0, self.page - 1))
        await edit_activity_audit_message(
            interaction,
            content="Auditoría de actividades:",
            embed=embed,
            view=ActivityAuditListView.from_records(self.cog, self.mode, max(0, self.page - 1), page_rows),
        )


class ActivityAuditNextButton(discord.ui.Button):
    def __init__(self, cog: "Admin", mode: str, page: int):
        super().__init__(label="Siguiente", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:activity_audit:next:{mode}:{page}", row=4)
        self.cog = cog
        self.mode = mode
        self.page = page

    async def callback(self, interaction: discord.Interaction) -> None:
        view = ActivityAuditListView.from_records(self.cog, self.mode, self.page, [])
        if not await view.require_admin(interaction):
            return
        dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
        rows = dataset.filter_records(self.mode)
        total_pages = max(1, (len(rows) + ACTIVITY_AUDIT_PAGE_SIZE - 1) // ACTIVITY_AUDIT_PAGE_SIZE)
        next_page = min(total_pages - 1, self.page + 1)
        embed, page_rows, _ = build_activity_audit_list_embed(self.cog, interaction.guild, self.mode, next_page)
        await edit_activity_audit_message(
            interaction,
            content="Auditoría de actividades:",
            embed=embed,
            view=ActivityAuditListView.from_records(self.cog, self.mode, next_page, page_rows),
        )


class ActivityAuditBackHomeButton(discord.ui.Button):
    def __init__(self, cog: "Admin"):
        super().__init__(label="Volver", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:back_home", row=4)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        view = ActivityAuditHomeView(self.cog)
        if not await view.require_admin(interaction):
            return
        await edit_activity_audit_message(
            interaction,
            content="Auditoría de actividades:",
            embed=build_activity_audit_home_embed(self.cog, interaction.guild),
            view=view,
        )


class ActivityAuditRecordView(ActivityAuditBaseView):
    def __init__(
        self,
        cog: "Admin",
        code: str,
        *,
        has_details: bool = True,
        record: ActivityAuditRecord | None = None,
    ):
        super().__init__(cog)
        self.code = normalize_activity_code(code) or code
        if has_details:
            self.add_item(ActivityAuditRecordDetailsButton(cog, self.code))
            self.add_item(ActivityLiquidationExpedientButton(cog, self.code, row=0))
        add_activity_audit_link_buttons(self, record, row=1)
        self.add_item(ActivityAuditBackHomeButton(cog))


class ActivityAuditRecordDetailsButton(discord.ui.Button):
    def __init__(self, cog: "Admin", code: str):
        super().__init__(label="Detalles del split", emoji="📋", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:activity_audit:record_detail:{code}", row=0)
        self.cog = cog
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        gate = ActivityAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        if record is None or not record.has_split_details:
            await private_response(interaction, "⏳ Esta actividad no tiene depósitos asociados.")
            return
        embed, _ = build_activity_audit_details_embed(self.cog, interaction.guild, self.code, 0)
        view = ActivityAuditDetailsView(self.cog, self.code, 0, back_mode="record", back_page=0, record=record)
        await edit_activity_audit_message(interaction, content=f"Detalles del split `{self.code}`:", embed=embed, view=view)

class ActivityLiquidationExpedientButton(discord.ui.Button):
    def __init__(self, cog: "Admin", code: str, *, row: int):
        super().__init__(
            label="Expediente de Liquidacion",
            emoji="\U0001F4C1",
            style=discord.ButtonStyle.primary,
            custom_id=f"g3n:admin:activity_audit:expedient:{code}",
            row=row,
        )
        self.cog = cog
        self.code = normalize_activity_code(code) or code

    async def callback(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)
        gate = ActivityAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        name_resolver = lambda user_id: activity_audit_plain_user_name(interaction.guild, user_id)
        try:
            expedient = build_liquidation_expedient_file(
                self.cog.db,
                interaction.guild.id,
                self.code,
                name_resolver=name_resolver,
            )
        except ActivityNotFoundError:
            await interaction.followup.send("No encontre esa actividad.", ephemeral=True)
            return
        except ActivityWithoutLiquidationError:
            await interaction.followup.send(
                "Esta actividad todavia no tiene una liquidacion registrada.",
                ephemeral=True,
            )
            return
        content = (
            "\U0001F4C1 Expediente de liquidacion generado\n\n"
            f"Actividad: {expedient.activity_code}\n"
            f"Participantes: {expedient.participant_count:02d}\n"
            f"Solicitudes relacionadas: {expedient.request_count:02d}\n"
            f"Estado: {expedient.liquidation_status}"
        )
        with liquidation_expedient_tempfile(expedient) as path:
            file = discord.File(path, filename=expedient.filename)
            try:
                await interaction.followup.send(content, file=file, ephemeral=True)
            finally:
                close = getattr(file, "close", None)
                if callable(close):
                    close()
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Descargar expediente de liquidacion",
            system="Auditoria",
            observation=f"{expedient.activity_code}; tipo=EXPEDIENTE_LIQUIDACION",
        )

class ActivityAuditDetailsView(ActivityAuditBaseView):
    def __init__(
        self,
        cog: "Admin",
        code: str,
        page: int,
        *,
        back_mode: str,
        back_page: int,
        record: ActivityAuditRecord | None = None,
    ):
        super().__init__(cog)
        self.code = normalize_activity_code(code) or code
        self.page = page
        self.back_mode = back_mode
        self.back_page = back_page
        self.record = record
        if record is not None and (record.has_split_details or record.payout_ids):
            self.add_item(ActivityLiquidationExpedientButton(cog, self.code, row=3))
        add_activity_audit_link_buttons(self, record, row=3)

    async def _show_page(self, interaction: discord.Interaction, page: int) -> None:
        embed, _ = build_activity_audit_details_embed(self.cog, interaction.guild, self.code, page)
        await edit_activity_audit_message(
            interaction,
            content=f"Detalles del split `{self.code}`:",
            embed=embed,
            view=ActivityAuditDetailsView(self.cog, self.code, page, back_mode=self.back_mode, back_page=self.back_page, record=self.record),
        )

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:detail_prev", row=4)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        await self._show_page(interaction, max(0, self.page - 1))

    @discord.ui.button(label="Siguiente", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:detail_next", row=4)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
        movements = dataset.movements_for(self.code)
        total_pages = max(1, (len(movements) + ACTIVITY_AUDIT_DETAIL_PAGE_SIZE - 1) // ACTIVITY_AUDIT_DETAIL_PAGE_SIZE)
        await self._show_page(interaction, min(total_pages - 1, self.page + 1))

    @discord.ui.button(label="Volver", emoji="↩️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit:detail_back", row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        if self.back_mode == "record":
            dataset = get_activity_audit_dataset(self.cog.db, interaction.guild.id)
            record = dataset.get_record(self.code)
            if record is None:
                await edit_activity_audit_message(
                    interaction,
                    content="Auditoría de actividades:",
                    embed=build_activity_audit_home_embed(self.cog, interaction.guild),
                    view=ActivityAuditHomeView(self.cog),
                )
                return
            await edit_activity_audit_message(
                interaction,
                content=f"Resultado de búsqueda `{self.code}`:",
                embed=build_activity_audit_record_embed(self.cog, interaction.guild, record),
                view=ActivityAuditRecordView(self.cog, self.code, has_details=record.has_split_details, record=record),
            )
            return
        embed, page_rows, _ = build_activity_audit_list_embed(self.cog, interaction.guild, self.back_mode, self.back_page)
        await edit_activity_audit_message(
            interaction,
            content="Auditoría de actividades:",
            embed=embed,
            view=ActivityAuditListView.from_records(self.cog, self.back_mode, self.back_page, page_rows),
        )


def guild_economy_name_resolver(guild: discord.Guild):
    def resolve(user_id: int | None) -> str:
        if user_id is None:
            return ""
        member = guild.get_member(int(user_id)) if guild is not None else None
        return member.display_name if member is not None else ""

    return resolve


def build_guild_economy_embed(summary: GuildEconomySummary) -> discord.Embed:
    embed = discord.Embed(
        title="\U0001F4B0 Econom\u00eda Gremial",
        color=discord.Color.gold(),
    )
    embed.description = (
        f"\U0001F4B0 Total depositado en balances: {format_amount(summary.total_deposited)}\n"
        f"\u2705 Total ya pagado: {format_amount(summary.total_paid)}\n"
        f"\U0001F7E1 Total pendiente por pagar: {format_amount(summary.total_pending)}\n\n"
        f"\U0001F465 Usuarios con saldo pendiente: {summary.users_with_pending_balance}\n"
        f"\U0001F552 \u00daltima actualizaci\u00f3n: {summary.generated_at}"
    )
    return embed


class OutsideBalancesView(discord.ui.View):
    def __init__(self, cog: "Admin", *, page: int = 0, total: int = 0):
        super().__init__(timeout=300)
        self.cog = cog
        self.page = page
        self.total = total

    async def show_page(self, interaction: discord.Interaction, page: int) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden consultar estos saldos.")
            return
        text, total = self.cog.outside_balances_text(interaction.guild, page=page)
        await interaction.response.edit_message(content=text, view=OutsideBalancesView(self.cog, page=page, total=total))

    @discord.ui.button(label="Anterior", emoji="⬅️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:outside_balances:prev", row=0)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.show_page(interaction, max(0, self.page - 1))

    @discord.ui.button(label="Siguiente", emoji="➡️", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:outside_balances:next", row=0)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        last_page = max(0, (self.total - 1) // 8)
        await self.show_page(interaction, min(last_page, self.page + 1))


class BalanceSeizureTargetModal(discord.ui.Modal, title="Decomisar balance"):
    user_id = discord.ui.TextInput(label="Discord ID o mención del usuario", max_length=40)

    def __init__(self, cog: "Admin"):
        super().__init__(timeout=180)
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden decomisar balance.")
            return
        target_id = parse_channel_id(str(self.user_id.value))
        if target_id is None:
            await private_response(interaction, "No pude leer ese Discord ID.")
            return
        account = self.cog.db.fetch_one(
            "SELECT * FROM accounts WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, target_id),
        )
        if account is None:
            await private_response(interaction, "Ese usuario no tiene cuenta de balance registrada.")
            return
        await private_response(
            interaction,
            self.cog.balance_seizure_target_text(interaction.guild, target_id),
            view=BalanceSeizureTargetView(self.cog, admin_id=interaction.user.id, user_id=target_id),
        )


class BalanceSeizureSpecificModal(discord.ui.Modal, title="Cantidad a decomisar"):
    amount = discord.ui.TextInput(label="Cantidad", placeholder="4000000", max_length=30)
    reason = discord.ui.TextInput(label="Razón obligatoria", style=discord.TextStyle.paragraph, max_length=900)
    origin = discord.ui.TextInput(label="Origen/tipo", placeholder="pago manual / alianza / corrección / otro", max_length=80, default="otro")

    def __init__(self, cog: "Admin", *, admin_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que inició el decomiso puede confirmarlo.")
            return
        try:
            amount = parse_int_amount(str(self.amount.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        reason = str(self.reason.value).strip()
        if not reason:
            await private_response(interaction, "La razón del decomiso es obligatoria.")
            return
        origin = str(self.origin.value).strip()
        await private_response(
            interaction,
            self.cog.balance_seizure_confirmation_text(
                interaction.guild,
                self.user_id,
                amount=amount,
                reason=reason,
                origin=origin,
            ),
            view=BalanceSeizureConfirmView(
                self.cog,
                admin_id=self.admin_id,
                user_id=self.user_id,
                amount=amount,
                reason=reason,
                origin=origin,
            ),
        )


class BalanceSeizureAllReasonModal(discord.ui.Modal, title="Decomisar todo el balance"):
    reason = discord.ui.TextInput(label="Razón obligatoria", style=discord.TextStyle.paragraph, max_length=900)
    origin = discord.ui.TextInput(label="Origen/tipo", placeholder="abandono Discord / alianza / pago manual / corrección / otro", max_length=80, default="otro")

    def __init__(self, cog: "Admin", *, admin_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.user_id = user_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.user.id != self.admin_id or interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo el admin que inició el decomiso puede confirmarlo.")
            return
        account = get_account(self.cog.db, interaction.guild.id, self.user_id)
        amount = int(account["available"])
        if amount <= 0:
            await private_response(interaction, "Ese usuario no tiene balance disponible para decomisar.")
            return
        reason = str(self.reason.value).strip()
        if not reason:
            await private_response(interaction, "La razón del decomiso es obligatoria.")
            return
        origin = str(self.origin.value).strip()
        await private_response(
            interaction,
            self.cog.balance_seizure_confirmation_text(
                interaction.guild,
                self.user_id,
                amount=amount,
                reason=reason,
                origin=origin,
                all_balance=True,
            ),
            view=BalanceSeizureConfirmView(
                self.cog,
                admin_id=self.admin_id,
                user_id=self.user_id,
                amount=amount,
                reason=reason,
                origin=origin,
            ),
        )


class BalanceSeizureTargetView(discord.ui.View):
    def __init__(self, cog: "Admin", *, admin_id: int, user_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.user_id = user_id

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que inició el decomiso puede usar esta confirmación.")
        return False

    @discord.ui.button(label="TODO EL BALANCE", emoji="💰", style=discord.ButtonStyle.danger, custom_id="g3n:admin:balance_seizure:all", row=0)
    async def all_balance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(BalanceSeizureAllReasonModal(self.cog, admin_id=self.admin_id, user_id=self.user_id))

    @discord.ui.button(label="CANTIDAD ESPECÍFICA", emoji="✏️", style=discord.ButtonStyle.primary, custom_id="g3n:admin:balance_seizure:specific", row=0)
    async def specific_amount(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(BalanceSeizureSpecificModal(self.cog, admin_id=self.admin_id, user_id=self.user_id))

    @discord.ui.button(label="Cancelar", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:balance_seizure:cancel", row=1)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await private_response(interaction, "Solo el admin que inició esta operación puede cancelarla.")
            return
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


class BalanceSeizureConfirmView(discord.ui.View):
    def __init__(
        self,
        cog: "Admin",
        *,
        admin_id: int,
        user_id: int,
        amount: int,
        reason: str,
        origin: str,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.admin_id = admin_id
        self.user_id = user_id
        self.amount = amount
        self.reason = reason
        self.origin = origin

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id and interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo el admin que inició el decomiso puede confirmarlo.")
        return False

    @discord.ui.button(label="CONFIRMAR DECOMISO", emoji="⚠️", style=discord.ButtonStyle.danger, custom_id="g3n:admin:balance_seizure:confirm", row=0)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        try:
            result = await self.cog.execute_balance_seizure(
                interaction.guild,
                user_id=self.user_id,
                amount=self.amount,
                admin_id=interaction.user.id,
                reason=self.reason,
                origin=self.origin,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await interaction.response.edit_message(content=result, view=None)

    @discord.ui.button(label="CANCELAR", emoji="❌", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:balance_seizure:confirm_cancel", row=0)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.user.id != self.admin_id:
            await private_response(interaction, "Solo el admin que inició esta operación puede cancelarla.")
            return
        await interaction.response.edit_message(content="Operación cancelada.", view=None)


async def edit_guild_economy_message(interaction: discord.Interaction, **kwargs) -> None:
    edit_original = getattr(interaction, "edit_original_response", None)
    if callable(edit_original):
        await edit_original(**kwargs)
        return
    if not interaction.response.is_done() and hasattr(interaction.response, "edit_message"):
        await interaction.response.edit_message(**kwargs)
        return
    content = kwargs.pop("content", "") or ""
    await interaction.followup.send(content, ephemeral=True, **kwargs)


class GuildEconomyView(discord.ui.View):
    def __init__(self, cog: "Admin"):
        super().__init__(timeout=300)
        self.cog = cog

    async def _defer(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True)

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await interaction.followup.send("\u26d4 No tienes permisos para consultar Econom\u00eda Gremial.", ephemeral=True)
        return False

    async def send_summary(self, interaction: discord.Interaction) -> None:
        await self._defer(interaction)
        if not await self.require_admin(interaction):
            return
        try:
            summary = get_guild_economy_summary(self.cog.db, interaction.guild.id)
            await interaction.followup.send(
                embed=build_guild_economy_embed(summary),
                view=GuildEconomyView(self.cog),
                ephemeral=True,
            )
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("No fue posible consultar la Econom\u00eda Gremial.", ephemeral=True)

    @discord.ui.button(label="Descargar reporte", emoji="\U0001F4E5", style=discord.ButtonStyle.primary, custom_id="g3n:admin:guild_economy:download", row=0)
    async def download(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._defer(interaction)
        if not await self.require_admin(interaction):
            return
        try:
            report = build_guild_economy_csv_report(
                self.cog.db,
                interaction.guild.id,
                guild_name=getattr(interaction.guild, "name", "G3NESYS"),
                name_resolver=guild_economy_name_resolver(interaction.guild),
            )
            with guild_economy_report_tempfile(report) as path:
                file = discord.File(path, filename=report.filename)
                try:
                    await interaction.followup.send(
                        "Reporte de Econom\u00eda Gremial generado.",
                        file=file,
                        ephemeral=True,
                    )
                finally:
                    close = getattr(file, "close", None)
                    if callable(close):
                        close()
            try:
                log_action(
                    self.cog.db,
                    interaction.guild.id,
                    admin_id=interaction.user.id,
                    action="Descargar reporte Economia Gremial",
                    system="Banco",
                    observation="ECONOMIA_GREMIAL",
                )
            except Exception:
                traceback.print_exc()
        except Exception:
            traceback.print_exc()
            try:
                await interaction.followup.send("No fue posible generar el reporte de Econom\u00eda Gremial.", ephemeral=True)
            except Exception:
                pass

    @discord.ui.button(label="Actualizar", emoji="\U0001F504", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:guild_economy:refresh", row=0)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._defer(interaction)
        if not await self.require_admin(interaction):
            return
        try:
            summary = get_guild_economy_summary(self.cog.db, interaction.guild.id)
            await edit_guild_economy_message(
                interaction,
                embed=build_guild_economy_embed(summary),
                view=GuildEconomyView(self.cog),
            )
        except Exception:
            traceback.print_exc()
            await interaction.followup.send("No fue posible consultar la Econom\u00eda Gremial.", ephemeral=True)

    @discord.ui.button(label="Saldos de usuarios fuera", emoji="👥", style=discord.ButtonStyle.primary, custom_id="g3n:admin:guild_economy:outside_balances", row=1)
    async def outside_balances(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._defer(interaction)
        if not await self.require_admin(interaction):
            return
        text, total = self.cog.outside_balances_text(interaction.guild, page=0)
        await interaction.followup.send(
            text,
            view=OutsideBalancesView(self.cog, page=0, total=total) if total > 8 else None,
            ephemeral=True,
        )

    @discord.ui.button(label="Decomisar balance", emoji="💰", style=discord.ButtonStyle.danger, custom_id="g3n:admin:guild_economy:seize_balance", row=1)
    async def seize_balance(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "⛔ No tienes permisos para decomisar balances.")
            return
        await interaction.response.send_modal(BalanceSeizureTargetModal(self.cog))

    @discord.ui.button(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:guild_economy:back", row=0)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._defer(interaction)
        if not await self.require_admin(interaction):
            return
        await edit_guild_economy_message(
            interaction,
            content="Panel Administrativo G3NESYS",
            embed=None,
            view=AdminPanelView(self.cog),
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

    @discord.ui.button(label="Ver Plata Gremial", emoji="\U0001F4B0", style=discord.ButtonStyle.primary, custom_id="g3n:admin:treasury", row=0)
    async def treasury(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.treasury_text(interaction.guild.id), "tesoreria_panel")

    @discord.ui.button(label="Registrar Ingreso", emoji="\U0001F4E5", style=discord.ButtonStyle.success, custom_id="g3n:admin:income", row=0)
    async def income(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(IncomeModal(self.cog))

    @discord.ui.button(label="Registrar Egreso", emoji="\U0001F4E4", style=discord.ButtonStyle.danger, custom_id="g3n:admin:expense", row=0)
    async def expense(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(ExpenseModal(self.cog))

    @discord.ui.button(label="Depositar a Usuario", emoji="\U0001F4B0", style=discord.ButtonStyle.success, custom_id="g3n:admin:deposit", row=1)
    async def deposit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona el tipo de operacion:",
                view=DepositOptionsView(self.cog, admin_id=interaction.user.id),
            )

    @discord.ui.button(label="Solicitudes de Cobro", emoji="\U0001F4B3", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawals", row=1)
    async def withdrawals(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                self.cog.withdrawals_text(interaction.guild.id),
                view=WithdrawalAdminView(self.cog),
            )

    @discord.ui.button(label="Edo.Cta.Usuario", emoji="\U0001F464", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:statement", row=1)
    async def statement(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.send_modal(UserStatementModal(self.cog))

    @discord.ui.button(label="Revisar Splits", emoji="\U0001F4CB", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:payouts", row=2)
    async def payouts(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Selecciona la lista de Splits que deseas consultar:",
                view=SplitsAdminView(self.cog),
            )

    @discord.ui.button(label="Historial Liq.", emoji="\U0001F9FE", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:liquidation_history", row=2)
    async def liquidation_history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(
                self.cog,
                interaction,
                self.cog.liquidation_history_text(interaction.guild.id),
                "historial_liquidaciones_admin",
            )

    @discord.ui.button(label="Tickets", emoji="\U0001F3AB", style=discord.ButtonStyle.primary, custom_id="g3n:admin:tickets", row=2)
    async def tickets(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        bank_cog = self.cog.bot.get_cog("Bank")
        if bank_cog is None or not hasattr(bank_cog, "show_admin_tickets_menu"):
            await private_response(interaction, "El modulo de tickets no esta disponible.")
            return
        await bank_cog.show_admin_tickets_menu(interaction)

    @discord.ui.button(label="Edo.Cta.Gremio", emoji="\U0001F4DC", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:history", row=2)
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.history_text(interaction.guild.id), "historial_panel")

    @discord.ui.button(label="Gesti\u00f3n de usuarios", emoji="\U0001f465", style=discord.ButtonStyle.primary, custom_id="g3n:admin:user_management", row=3)
    async def user_management(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Gesti\u00f3n de usuarios:",
                view=UserManagementAdminView(self.cog),
            )

    async def callers(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            embed = discord.Embed(
                title="📣 Gestion de Callers G3NESYS",
                description=(
                    "Consulta el ranking o administra quienes pueden dirigir actividades.\n\n"
                    "PCALL da acceso al Panel de Callers sin registrar al usuario como caller oficial.\n\n"
                    "**Puntuacion:** +10 por actividad completada, +2 por asistencia, "
                    "-4 por cancelacion con composicion completa y -6 por ausencia. "
                    "Las cancelaciones por cupos incompletos no restan. Al llegar a -14, "
                    "el acceso de caller queda suspendido."
                ),
                color=discord.Color.magenta(),
            )
            await private_response(interaction, "Menu de callers:", embed=embed, view=CallersAdminView(self.cog))

    async def recruiters(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            embed = discord.Embed(
                title="🛡️ Gestion de Reclutadores G3NESYS",
                description=(
                    "Agrega, elimina o consulta a quienes tienen el rol de Reclutador. "
                    "Si el rol no existe, se creara al agregar al primer reclutador."
                ),
                color=discord.Color.blurple(),
            )
            embed.set_image(url=RECRUITERS_PANEL_IMAGE)
            await private_response(
                interaction,
                "Menu de reclutadores:",
                embed=embed,
                view=RecruitersAdminView(self.cog),
            )

    async def payment_delegates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Administra quienes pueden recibir pagos delegados:",
                view=PaymentDelegatesAdminView(self.cog),
            )

    async def admins(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Menu de administradores:",
                view=AdminsAdminView(self.cog),
            )

    @discord.ui.button(label="Reportes", emoji="\U0001F4CA", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:reports", row=4)
    async def reports(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await interaction.response.defer(ephemeral=True)
            path = self.cog.create_report(interaction.guild.id)
            await interaction.followup.send(
                "Reporte administrativo integral generado.",
                file=discord.File(path),
                ephemeral=True,
            )

    @discord.ui.button(label="Auditoria", emoji="\U0001F50D", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:audit", row=4)
    async def audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await dm_or_private(self.cog, interaction, self.cog.audit_text(interaction.guild.id), "auditoria_panel")

    @discord.ui.button(label="Multas", emoji="\U0001F6A8", style=discord.ButtonStyle.danger, custom_id="g3n:admin:fines", row=3)
    async def fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Panel de multas:",
                view=FineAdminView(self.cog),
            )

    @discord.ui.button(label="Auditoría de actividades", emoji="📊", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:activity_audit", row=3)
    async def activity_audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Auditoría de actividades:",
                embed=build_activity_audit_home_embed(self.cog, interaction.guild),
                view=ActivityAuditHomeView(self.cog),
            )

    @discord.ui.button(label="Econom\u00eda Gremial", emoji="\U0001F4B0", style=discord.ButtonStyle.primary, custom_id="g3n:admin:guild_economy", row=3)
    async def guild_economy(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await GuildEconomyView(self.cog).send_summary(interaction)

    @discord.ui.button(label="Auditoría pagos/cobros", emoji="💳", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit", row=3)
    async def withdrawal_audit(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        try:
            await interaction.response.defer(ephemeral=True)
            if not is_admin_subject(self.cog.db, interaction):
                await interaction.followup.send("Solo admins autorizados pueden usar este panel.", ephemeral=True)
                return
            embed = discord.Embed(
                title="💳 Auditoría de pagos y cobros",
                description="Selecciona una categoría para consultar las solicitudes de cobro.",
                color=discord.Color.blurple(),
            )
            view = WithdrawalAuditHomeView(self.cog, admin_panel_view_cls=AdminPanelView)
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        except Exception:
            traceback.print_exc()
            await interaction.followup.send(
                "❌ No se pudo abrir la Auditoría de pagos y cobros.",
                ephemeral=True,
            )

    @discord.ui.button(label="Más", emoji="\U0001F9ED", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:more", row=4)
    async def more(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Opciones adicionales:",
                view=ExtraAdminOptionsView(self.cog),
            )

    @discord.ui.button(label="Config.", emoji="\u2699\uFE0F", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:config", row=4)
    async def config(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(
                interaction,
                "Usa `!config_ver`, comandos `!canal_*_set`, `!caller_set` y `!economia_set`.",
                view=ConfigAdminView(self.cog),
            )



class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._withdrawal_processing: set[tuple[int, str]] = set()

    async def cog_load(self) -> None:
        self.bot.add_view(AdminPanelView(self))
        self.bot.add_view(LegacyAdminPanelCallbacksView(self))
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

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member) -> None:
        record_member_join(
            self.db,
            member.guild.id,
            user_id=member.id,
            display_name=member.display_name,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member) -> None:
        account = self.db.fetch_one(
            "SELECT available FROM accounts WHERE guild_id = ? AND user_id = ?",
            (member.guild.id, member.id),
        )
        should_alert = record_member_departure(
            self.db,
            member.guild.id,
            user_id=member.id,
            display_name=member.display_name,
        )
        if account is None or int(account["available"] or 0) <= 0 or not should_alert:
            return
        left_at = utc_now_iso()
        await send_admin_notification(
            self.db,
            guild=member.guild,
            category="general_admin",
            content=(
                "⚠️ **USUARIO FUERA DEL SERVIDOR CON SALDO**\n\n"
                f"Usuario: {member.mention}\n"
                f"Discord ID: `{member.id}`\n"
                "Albion: No registrado\n"
                f"Balance disponible: {format_amount(account['available'])}\n"
                f"Fecha de salida: {left_at}\n\n"
                "Este usuario salió del servidor manteniendo saldo a favor.\n"
                "El balance NO debe decomisarse automáticamente."
            ),
        )
        mark_member_alerted(self.db, member.guild.id, member.id)

    def build_payout_review_view(self, code: str) -> PayoutReviewView:
        return PayoutReviewView(self, code)

    def member_has_admin_access(self, guild: discord.Guild, member: discord.Member) -> bool:
        override = self.db.fetch_one(
            "SELECT authorized FROM admin_access WHERE guild_id = ? AND user_id = ?",
            (guild.id, member.id),
        )
        if override is not None and bool(override["authorized"]):
            return True
        if member.guild_permissions.administrator:
            return True
        configured_roles = self.db.get_setting(guild.id, "admin_role_ids")
        if has_any_configured_role(member, configured_roles):
            return True
        return not split_csv_ids(configured_roles) and any(
            role.name.strip().casefold() in ADMIN_ROLE_NAMES for role in member.roles
        )

    def is_payment_delegate_active(self, guild_id: int, user_id: int) -> bool:
        row = self.db.fetch_one(
            "SELECT active FROM payment_delegates WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return row is not None and bool(row["active"])

    def active_payment_delegate_count(self, guild_id: int) -> int:
        row = self.db.fetch_one(
            "SELECT COUNT(*) AS total FROM payment_delegates WHERE guild_id = ? AND active = 1",
            (guild_id,),
        )
        return int(row["total"] if row is not None else 0)

    def payment_delegate_rows(self, guild_id: int, *, active_only: bool = False):
        where = "WHERE guild_id = ?"
        params: tuple = (guild_id,)
        if active_only:
            where += " AND active = 1"
        return self.db.fetch_all(
            f"""
            SELECT *
            FROM payment_delegates
            {where}
            ORDER BY active DESC, added_at ASC
            """,
            params,
        )

    def active_delegated_withdrawals_count(self, guild_id: int, user_id: int) -> int:
        row = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM withdrawals
            WHERE guild_id = ?
              AND assigned_officer_id = ?
              AND status IN (?, ?, ?)
            """,
            (guild_id, user_id, WITHDRAWAL_DELEGATED, WITHDRAWAL_PARTIAL, WITHDRAWAL_REASSIGNMENT),
        )
        return int(row["total"] if row is not None else 0)

    def payment_delegates_text(self, guild: discord.Guild) -> str:
        rows = self.payment_delegate_rows(guild.id)
        if not rows:
            return "**Delegados de pago**\nNo hay delegados configurados."
        lines = ["**Delegados de pago**"]
        for row in rows:
            user_id = int(row["user_id"])
            member = guild.get_member(user_id)
            name = member.display_name if member is not None else "Usuario no disponible"
            status = "Activo" if int(row["active"]) else "Inactivo"
            active_payments = self.active_delegated_withdrawals_count(guild.id, user_id)
            lines.append(
                f"- **{name}** | ID `{user_id}` | {status} | "
                f"Alta: `{row['added_at']}` por <@{row['added_by']}> | "
                f"Pagos activos: {active_payments}"
            )
        return "\n".join(lines)[:1900]

    def member_access_roles_text(self, guild: discord.Guild) -> str:
        official_role = guild.get_role(OFFICIAL_MEMBER_ROLE_ID)
        official_label = official_role.mention if official_role is not None else f"<@&{OFFICIAL_MEMBER_ROLE_ID}>"
        configured_roles = sorted(configured_member_role_ids(self.db, guild.id) - {OFFICIAL_MEMBER_ROLE_ID})
        lines = [
            "👥 **ROLES CON ACCESO DE MIEMBRO**",
            "",
            "Rol principal:",
            f"• {official_label}",
            "",
            "Roles equivalentes:",
        ]
        if not configured_roles:
            lines.append("No hay roles equivalentes adicionales configurados.")
        else:
            for role_id in configured_roles:
                role = guild.get_role(role_id)
                label = role.mention if role is not None else f"`{role_id}` (rol no disponible)"
                lines.append(f"• {label}")
        return "\n".join(lines)[:1900]

    async def add_payment_delegate_interaction(self, interaction: discord.Interaction, member: discord.Member | None) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        if not is_admin_subject(self.db, interaction):
            log_action(self.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento sin permisos delegados de pago", system="Banco", observation="add")
            await private_response(interaction, "Solo admins autorizados pueden gestionar delegados de pago.")
            return
        if member is None or member.guild.id != interaction.guild.id:
            await private_response(interaction, "El usuario debe pertenecer a este servidor.")
            return
        if member.bot:
            await private_response(interaction, "No se pueden configurar bots como delegados de pago.")
            return
        if is_caller_penalized(self.db, interaction.guild.id, member.id):
            await private_response(interaction, "Este usuario esta bloqueado por una penalizacion activa.")
            return
        existing = self.db.fetch_one(
            "SELECT * FROM payment_delegates WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, member.id),
        )
        now = utc_now_iso()
        if existing is not None and int(existing["active"]):
            log_action(self.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento duplicado delegado de pago", system="Banco", affected_user_id=member.id)
            await private_response(interaction, f"{member.mention} ya esta registrado como delegado de pago activo.")
            return
        if existing is None:
            self.db.execute(
                """
                INSERT INTO payment_delegates (guild_id, user_id, active, added_by, added_at)
                VALUES (?, ?, 1, ?, ?)
                """,
                (interaction.guild.id, member.id, interaction.user.id, now),
            )
            action = "Alta de delegado de pago"
            message = f"{member.mention} fue agregado como delegado de pago."
        else:
            self.db.execute(
                """
                UPDATE payment_delegates
                SET active = 1, added_by = ?, added_at = ?, removed_by = NULL, removed_at = NULL
                WHERE guild_id = ? AND user_id = ?
                """,
                (interaction.user.id, now, interaction.guild.id, member.id),
            )
            action = "Reactivacion de delegado de pago"
            message = f"{member.mention} fue reactivado como delegado de pago."
        log_action(self.db, interaction.guild.id, admin_id=interaction.user.id, action=action, system="Banco", affected_user_id=member.id)
        await private_response(interaction, message)

    async def remove_payment_delegate_interaction(self, interaction: discord.Interaction, user_id: int) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        if not is_admin_subject(self.db, interaction):
            log_action(self.db, interaction.guild.id, admin_id=interaction.user.id, action="Intento sin permisos delegados de pago", system="Banco", observation="remove")
            await private_response(interaction, "Solo admins autorizados pueden gestionar delegados de pago.")
            return
        row = self.db.fetch_one(
            "SELECT * FROM payment_delegates WHERE guild_id = ? AND user_id = ? AND active = 1",
            (interaction.guild.id, user_id),
        )
        if row is None:
            await private_response(interaction, "Ese usuario no esta registrado como delegado activo.")
            return
        active_payments = self.active_delegated_withdrawals_count(interaction.guild.id, user_id)
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE payment_delegates
            SET active = 0, removed_by = ?, removed_at = ?
            WHERE guild_id = ? AND user_id = ?
            """,
            (interaction.user.id, now, interaction.guild.id, user_id),
        )
        log_action(self.db, interaction.guild.id, admin_id=interaction.user.id, action="Baja de delegado de pago", system="Banco", affected_user_id=user_id, observation=f"pagos_activos={active_payments}")
        warning = f"\nAdvertencia: conserva {active_payments} pago(s) activo(s) ya asignado(s)." if active_payments else ""
        await private_response(interaction, f"<@{user_id}> ya no aparecera para nuevas delegaciones.{warning}")

    def configured_admin_roles(self, guild: discord.Guild) -> list[discord.Role]:
        role_ids = split_csv_ids(self.db.get_setting(guild.id, "admin_role_ids"))
        roles = [role for role_id in role_ids if (role := guild.get_role(role_id)) is not None]
        if not roles:
            roles = [
                role
                for role in guild.roles
                if role.name.strip().casefold() in ADMIN_ROLE_NAMES
            ]
            if roles:
                self.db.set_setting(
                    guild.id,
                    "admin_role_ids",
                    ",".join(str(role.id) for role in roles),
                )
        return sorted(roles, key=lambda role: role.position, reverse=True)

    def member_has_configured_admin_role(self, guild: discord.Guild, member: discord.Member) -> bool:
        return any(role in member.roles for role in self.configured_admin_roles(guild))

    def configured_caller_panel_roles(self, guild: discord.Guild) -> list[discord.Role]:
        role_ids = split_csv_ids(self.db.get_setting(guild.id, CALLER_PANEL_ROLE_SETTING_KEY))
        return sorted(
            [role for role_id in role_ids if (role := guild.get_role(role_id)) is not None],
            key=lambda role: role.position,
            reverse=True,
        )

    @staticmethod
    def named_caller_panel_roles(guild: discord.Guild) -> list[discord.Role]:
        return sorted(
            [
                role
                for role in guild.roles
                if role.name.strip().casefold() in CALLER_PANEL_ROLE_NAMES
            ],
            key=lambda role: (role.name.strip().casefold() != "pcall", -role.position),
        )

    def caller_panel_roles(self, guild: discord.Guild) -> list[discord.Role]:
        roles_by_id = {
            role.id: role
            for role in [*self.configured_caller_panel_roles(guild), *self.named_caller_panel_roles(guild)]
        }
        return sorted(roles_by_id.values(), key=lambda role: role.position, reverse=True)

    def register_caller_panel_role(self, guild_id: int, role_id: int) -> None:
        current = split_csv_ids(self.db.get_setting(guild_id, CALLER_PANEL_ROLE_SETTING_KEY))
        if role_id not in current:
            current.add(role_id)
            self.db.set_setting(guild_id, CALLER_PANEL_ROLE_SETTING_KEY, join_csv_ids(current))

    async def ensure_caller_panel_role(
        self,
        guild: discord.Guild,
        actor: discord.Member | discord.User,
    ) -> discord.Role | None:
        roles = self.configured_caller_panel_roles(guild) or self.named_caller_panel_roles(guild)
        if roles:
            role = roles[0]
            self.register_caller_panel_role(guild.id, role.id)
            return role
        try:
            role = await guild.create_role(
                name="PCALL",
                reason=f"Creado desde el Panel Administrativo por {actor}",
            )
        except discord.Forbidden:
            return None
        except discord.HTTPException:
            return None
        self.register_caller_panel_role(guild.id, role.id)
        return role

    def has_admin_after_removal(self, guild: discord.Guild, removed_user_id: int) -> bool:
        for member in guild.members:
            if member.bot or member.id == removed_user_id:
                continue
            if self.member_has_admin_access(guild, member):
                return True
        return False

    @staticmethod
    def recruiter_roles(guild: discord.Guild) -> list[discord.Role]:
        roles = [
            role
            for role in guild.roles
            if role.name.strip().casefold() in RECRUITER_ROLE_NAMES
        ]
        return sorted(
            roles,
            key=lambda role: (role.name.strip().casefold() != "reclutador", role.position),
        )

    def recruiters_text(self, guild: discord.Guild) -> str:
        roles = self.recruiter_roles(guild)
        if not roles:
            return "🛡️ **Reclutadores actuales**\nEl rol Reclutador todavia no existe."
        role_ids = {role.id for role in roles}
        members = sorted(
            (
                member
                for member in guild.members
                if not member.bot and any(role.id in role_ids for role in member.roles)
            ),
            key=lambda member: member.display_name.casefold(),
        )
        if not members:
            return "🛡️ **Reclutadores actuales**\nNingun usuario tiene el rol de Reclutador."
        lines = ["🛡️ **Reclutadores actuales**"]
        lines.extend(f"{index}. {member.mention}" for index, member in enumerate(members[:50], start=1))
        if len(members) > 50:
            lines.append(f"… y {len(members) - 50} mas.")
        return "\n".join(lines)

    def admins_text(self, guild: discord.Guild) -> str:
        members = sorted(
            (
                member
                for member in guild.members
                if not member.bot and self.member_has_admin_access(guild, member)
            ),
            key=lambda member: member.display_name.casefold(),
        )
        if not members:
            return "🔐 **Admins actuales**\nNo encontre administradores autorizados."
        lines = ["🔐 **Admins actuales**"]
        lines.extend(f"{index}. {member.mention}" for index, member in enumerate(members[:50], start=1))
        if len(members) > 50:
            lines.append(f"… y {len(members) - 50} mas.")
        return "\n".join(lines)

    async def add_recruiter_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Este menu solo funciona dentro del servidor.")
            return
        roles = self.recruiter_roles(guild)
        if any(role in member.roles for role in roles):
            await private_response(interaction, f"{member.mention} ya tiene el rol de Reclutador.")
            return
        role = roles[0] if roles else None
        if role is None:
            try:
                role = await guild.create_role(
                    name="Reclutador",
                    reason=f"Creado desde el Panel Administrativo por {interaction.user}",
                )
            except discord.Forbidden:
                await private_response(
                    interaction,
                    "No pude crear el rol Reclutador. Revisa que el bot tenga permiso para gestionar roles.",
                )
                return
            except discord.HTTPException:
                await private_response(interaction, "Discord no permitio crear el rol Reclutador. Intenta de nuevo.")
                return
        try:
            await member.add_roles(
                role,
                reason=f"Asignado desde el Panel Administrativo por {interaction.user}",
            )
        except discord.Forbidden:
            await private_response(
                interaction,
                "No pude asignar el rol. Coloca el rol del bot por encima de Reclutador y permite gestionar roles.",
            )
            return
        except discord.HTTPException:
            await private_response(interaction, "Discord no permitio asignar el rol Reclutador. Intenta de nuevo.")
            return
        log_action(
            self.db,
            guild.id,
            admin_id=interaction.user.id,
            action="Agregar reclutador",
            affected_user_id=member.id,
            system="Reclutadores",
            observation=f"Rol {role.name} ({role.id}) asignado desde el panel administrativo.",
        )
        await private_response(
            interaction,
            f"🛡️ {member.mention} ahora tiene el rol {role.mention}.",
        )

    async def remove_recruiter_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Este menu solo funciona dentro del servidor.")
            return
        recruiter_roles = self.recruiter_roles(guild)
        member_roles = [role for role in recruiter_roles if role in member.roles]
        if not member_roles:
            await private_response(interaction, f"{member.mention} no tiene el rol de Reclutador.")
            return
        try:
            await member.remove_roles(
                *member_roles,
                reason=f"Retirado desde el Panel Administrativo por {interaction.user}",
            )
        except discord.Forbidden:
            await private_response(
                interaction,
                "No pude quitar el rol. Coloca el rol del bot por encima de Reclutador y permite gestionar roles.",
            )
            return
        except discord.HTTPException:
            await private_response(interaction, "Discord no permitio quitar el rol Reclutador. Intenta de nuevo.")
            return
        role_names = ", ".join(role.name for role in member_roles)
        log_action(
            self.db,
            guild.id,
            admin_id=interaction.user.id,
            action="Eliminar reclutador",
            affected_user_id=member.id,
            system="Reclutadores",
            observation=f"Rol(es) {role_names} retirado(s) desde el panel administrativo.",
        )
        await private_response(
            interaction,
            f"➖ {member.mention} ya no tiene el rol de Reclutador.",
        )

    async def prompt_admin_change(
        self,
        interaction: discord.Interaction,
        action: str,
        user_id: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None:
            await private_response(interaction, "No encontre ese usuario dentro del servidor.")
            return
        if member.bot:
            await private_response(interaction, "No puedes autorizar un bot como administrador.")
            return
        currently_authorized = self.member_has_admin_access(guild, member)
        has_admin_role = self.member_has_configured_admin_role(guild, member)
        if action == "add":
            if not self.configured_admin_roles(guild):
                await private_response(interaction, "❌ Primero debes configurar el rol de Admin.")
                return
            if currently_authorized and has_admin_role:
                await private_response(interaction, f"{member.mention} ya tiene acceso administrativo.")
                return
        if action == "remove" and not currently_authorized:
            await private_response(interaction, f"{member.mention} no tiene acceso administrativo.")
            return
        verb = "autorizar como admin" if action == "add" else "retirar como admin"
        warning = (
            ""
            if action == "add"
            else "\nSe retirara el rol de Admin configurado si el usuario lo tiene."
        )
        await private_response(
            interaction,
            f"¿Confirmas {verb} a {member.mention}?{warning}",
            view=ConfirmAdminActionView(
                self,
                admin_id=interaction.user.id,
                action="add_admin" if action == "add" else "remove_admin",
                payload={"user_id": member.id},
            ),
        )

    async def change_admin_access(
        self,
        guild: discord.Guild,
        *,
        user_id: int,
        authorized: bool,
        changed_by: int,
    ) -> str:
        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                member = None
        if member is None:
            raise ValueError("No encontre ese usuario dentro del servidor.")
        if member.bot:
            raise ValueError("No puedes gestionar un bot como administrador.")
        current_access = self.db.fetch_one(
            "SELECT authorized FROM admin_access WHERE guild_id = ? AND user_id = ?",
            (guild.id, user_id),
        )
        if authorized and current_access is not None and bool(current_access["authorized"]):
            log_action(
                self.db,
                guild.id,
                admin_id=changed_by,
                action="Intento duplicado de admin",
                affected_user_id=user_id,
                system="Administracion",
                observation="Este usuario ya esta autorizado como admin.",
            )
            return "Este usuario ya esta autorizado."
        if not authorized and not self.has_admin_after_removal(guild, user_id):
            raise ValueError(
                "No puedes eliminar al ultimo admin disponible. Agrega otro admin primero."
            )
        admin_roles = self.configured_admin_roles(guild)
        role_note = ""
        if authorized:
            if not admin_roles:
                raise ValueError("❌ Primero debes configurar el rol de Admin.")
            role = admin_roles[0]
            if role not in member.roles:
                try:
                    await member.add_roles(
                        role,
                        reason=f"Asignado desde el Panel Administrativo por {changed_by}",
                    )
                except discord.Forbidden as exc:
                    raise ValueError(
                        "No pude asignar el rol de Admin. Coloca el rol del bot por encima del rol Admin y permite gestionar roles."
                    ) from exc
                except discord.HTTPException as exc:
                    raise ValueError("Discord no permitio asignar el rol de Admin. Intenta de nuevo.") from exc
            role_note = f"Rol {role.name} ({role.id}) asignado."
        else:
            member_admin_roles = [role for role in admin_roles if role in member.roles]
            if member_admin_roles:
                try:
                    await member.remove_roles(
                        *member_admin_roles,
                        reason=f"Retirado desde el Panel Administrativo por {changed_by}",
                    )
                except discord.Forbidden as exc:
                    raise ValueError(
                        "No pude quitar el rol de Admin. Coloca el rol del bot por encima del rol Admin y permite gestionar roles."
                    ) from exc
                except discord.HTTPException as exc:
                    raise ValueError("Discord no permitio quitar el rol de Admin. Intenta de nuevo.") from exc
                role_note = "Rol(es) Admin retirado(s): " + ", ".join(role.name for role in member_admin_roles)
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET authorized = excluded.authorized,
                          updated_by = excluded.updated_by,
                          updated_at = excluded.updated_at
            """,
            (guild.id, user_id, 1 if authorized else 0, changed_by, utc_now_iso()),
        )
        action = "Agregar admin" if authorized else "Eliminar admin"
        log_action(
            self.db,
            guild.id,
            admin_id=changed_by,
            action=action,
            affected_user_id=user_id,
            system="Administracion",
            observation=(
                f"Acceso administrativo autorizado desde el panel. {role_note}".strip()
                if authorized
                else f"Acceso administrativo denegado desde el panel. {role_note}".strip()
            ),
        )
        await send_dm_safe(
            self.db,
            guild_id=guild.id,
            user=member,
            action="cambio_acceso_admin",
            content=(
                f"Ahora tienes acceso a las funciones administrativas del bot en {guild.name}."
                if authorized
                else f"Tu acceso a las funciones administrativas del bot en {guild.name} fue retirado."
            ),
        )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="general_admin",
            content=(
                f"{'➕' if authorized else '➖'} <@{user_id}> "
                f"{'fue agregado como admin' if authorized else 'fue eliminado como admin'} "
                f"por <@{changed_by}>."
            ),
        )
        return (
            "✅ Admin agregado correctamente.\n"
            "Se le asignó el rol de Admin en Discord y ya puede usar el Panel de Admins."
            if authorized
            else f"Se retiro el acceso administrativo de {member.mention}."
        )

    def active_activity_penalties(self, guild_id: int, user_id: int):
        return self.db.fetch_all(
            """
            SELECT id, guild_id, usuario_id, motivo, origen, fecha_ingreso, activo,
                   removido_por, fecha_remocion, observaciones
            FROM penalizacion_actividades
            WHERE guild_id = ? AND usuario_id = ? AND activo = 1
            ORDER BY id DESC
            """,
            (guild_id, user_id),
        )

    def activity_penalties_text(self, guild_id: int, member: discord.abc.User) -> str:
        rows = self.active_activity_penalties(guild_id, member.id)
        if not rows:
            return "\u2705 Este miembro no tiene penalizaciones activas."
        lines = [
            f"\u26a0\ufe0f **Penalizaciones activas de {member.mention}**",
            f"ID de Discord: `{member.id}`",
        ]
        for row in rows:
            lines.extend(
                [
                    "",
                    f"ID: `{row['id']}`",
                    f"Motivo: {row['motivo']}",
                    f"Origen: {row['origen']}",
                    f"Fecha: `{row['fecha_ingreso']}`",
                    "Estado: `Activa`",
                ]
            )
        return "\n".join(lines)[:1900]

    def remove_activity_penalty(
        self,
        guild_id: int,
        *,
        penalty_id: int,
        user_id: int,
        removed_by: int,
        observation: str,
    ):
        penalty = self.db.fetch_one(
            """
            SELECT * FROM penalizacion_actividades
            WHERE guild_id = ? AND id = ? AND usuario_id = ? AND activo = 1
            """,
            (guild_id, penalty_id, user_id),
        )
        if penalty is None:
            return None
        removal_note = (
            f"{observation}; penalty_id={penalty_id}; motivo={penalty['motivo']}; "
            f"origen={penalty['origen']}"
        )
        self.db.execute(
            """
            UPDATE penalizacion_actividades
            SET activo = 0, removido_por = ?, fecha_remocion = ?, observaciones = ?
            WHERE guild_id = ? AND id = ? AND usuario_id = ? AND activo = 1
            """,
            (removed_by, utc_now_iso(), removal_note, guild_id, penalty_id, user_id),
        )
        log_action(
            self.db,
            guild_id,
            admin_id=removed_by,
            action="Quitar penalizacion de actividad",
            system="Actividades",
            affected_user_id=user_id,
            observation=removal_note,
        )
        return penalty

    def get_activity_payout_for_quick_liquidation(self, guild_id: int, activity_id: int):
        return self.db.fetch_one(
            """
            SELECT p.*, COALESCE(a.name, a.code, 'Actividad sin nombre') AS activity_name,
                   a.code AS activity_code
            FROM payouts p
            LEFT JOIN activities a ON a.id = p.activity_id
            WHERE p.guild_id = ? AND p.activity_id = ?
            ORDER BY p.id DESC LIMIT 1
            """,
            (guild_id, activity_id),
        )

    async def prompt_quick_liquidation_for_activity(
        self,
        interaction: discord.Interaction,
        activity_id: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        if not is_admin_subject(self.db, interaction):
            await private_response(interaction, "❌ Solo los administradores pueden usar liquidación rápida.")
            return
        payout = self.get_activity_payout_for_quick_liquidation(guild.id, activity_id)
        if payout is None:
            await private_response(interaction, "❌ Debes splitear actividad primero.")
            return
        if payout["status"] != PAYOUT_DEPOSITED:
            await private_response(interaction, "El Split debe estar aprobado y depositado antes de liquidarlo.")
            return
        participants = get_liquidatable_participants(self.db, int(payout["id"]))
        if not participants:
            await private_response(interaction, "Ese split ya fue liquidado por completo.")
            return
        await private_response(
            interaction,
            (
                f"Split `{payout['code']}` · **{payout['activity_name']}**\n"
                f"Pendientes: {len(participants)} miembros · "
                f"{format_amount(sum(int(row['amount']) for row in participants))}\n\n"
                "Elige si deseas liquidar la actividad completa o a un solo miembro."
            ),
            view=QuickLiquidationModeView(
                self,
                payout_id=int(payout["id"]),
                admin_id=interaction.user.id,
            ),
        )

    def quick_liquidation_confirmation_embed(
        self,
        guild: discord.Guild,
        payout,
        participants,
        admin: discord.Member | discord.User,
        *,
        mode: str,
    ) -> discord.Embed:
        total = sum(int(row["amount"]) for row in participants)
        activity_reference = payout["activity_code"] or f"ID {payout['activity_id']}"
        embed = discord.Embed(
            title="⚡ Confirmar liquidacion rapida",
            description=(
                f"**Split:** {payout['code']}\n"
                f"**Actividad:** {payout['activity_name']} ({activity_reference})\n"
                f"**Modalidad:** {mode}\n"
                f"**Admin:** {admin.mention}\n"
                f"**Total a liquidar:** {format_amount(total)}"
            ),
            color=discord.Color.orange(),
        )
        lines = []
        for row in participants:
            user_id = int(row["user_id"])
            member = guild.get_member(user_id)
            name = member.display_name if member else f"Usuario {user_id}"
            lines.append(f"• {name} (<@{user_id}>) — {format_amount(row['amount'])}")
        chunks: list[str] = []
        current = ""
        for line in lines:
            candidate = f"{current}\n{line}".strip()
            if len(candidate) > 1000 and current:
                chunks.append(current)
                current = line
            else:
                current = candidate
        if current:
            chunks.append(current)
        for index, chunk in enumerate(chunks[:5], start=1):
            title = "Miembros y cantidades" if index == 1 else f"Miembros y cantidades ({index})"
            embed.add_field(name=title, value=chunk, inline=False)
        if len(chunks) > 5:
            embed.add_field(
                name="Aviso",
                value="La lista es demasiado extensa para Discord; todos los miembros pendientes siguen incluidos.",
                inline=False,
            )
        embed.set_footer(text="El saldo se restara al confirmar. Esta operacion no puede duplicarse.")
        return embed

    async def prompt_quick_liquidation_individual(
        self,
        interaction: discord.Interaction,
        payout_id: int,
        user_id: int,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Esta operacion debe realizarse dentro del servidor.")
            return
        payout = get_liquidatable_payout(self.db, guild.id, payout_id)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        participant = self.db.fetch_one(
            "SELECT * FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (payout_id, user_id),
        )
        if participant is None:
            await private_response(
                interaction,
                "El ID ingresado no corresponde a ningun miembro del split.",
            )
            return
        if participant["liquidated_at"] is not None:
            await private_response(interaction, "Ese miembro ya fue liquidado en este split.")
            return
        if participant["deposited_at"] is None:
            await private_response(interaction, "Ese miembro aun no tiene acreditado el saldo del split.")
            return
        embed = self.quick_liquidation_confirmation_embed(
            guild,
            payout,
            [participant],
            interaction.user,
            mode="Individual",
        )
        await private_response(
            interaction,
            "Confirma la liquidacion rapida del miembro seleccionado.",
            embed=embed,
            view=ConfirmAdminActionView(
                self,
                admin_id=interaction.user.id,
                action="quick_liquidate_individual",
                payload={"payout_id": payout_id, "user_id": user_id},
            ),
        )

    async def execute_quick_liquidation(
        self,
        guild: discord.Guild,
        *,
        payout_id: int,
        admin_id: int,
        user_id: int | None = None,
    ) -> str:
        result = liquidate_payout(
            self.db,
            guild.id,
            payout_id=payout_id,
            admin_id=admin_id,
            user_id=user_id,
        )
        for item in result.items:
            member = guild.get_member(item.user_id)
            if member is None:
                continue
            await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=member,
                action="liquidacion_rapida",
                content=(
                    "⚡ Tu saldo fue liquidado directamente por un administrador.\n\n"
                    f"Split: {result.payout_code}\n"
                    f"Actividad: {result.activity_name}\n"
                    f"Cantidad liquidada: {format_amount(item.amount)}\n"
                    f"Realizado por: <@{admin_id}>\n"
                    f"Registro: {result.code}"
                ),
            )
        members = ", ".join(
            f"<@{item.user_id}> ({format_amount(item.amount)})"
            for item in result.items
        )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="splits",
            content=(
                f"⚡ Liquidacion rapida **{result.mode}** {result.code} realizada "
                f"por <@{admin_id}> sobre el Split {result.payout_code}. "
                f"Total: {format_amount(result.total_amount)}. Miembros: {members}"
            )[:1900],
        )
        return (
            f"Liquidacion rapida {result.code} completada: "
            f"{len(result.items)} miembro(s), {format_amount(result.total_amount)}."
        )

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

    async def add_pcall_interaction(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
    ) -> None:
        guild = interaction.guild
        if guild is None:
            await private_response(interaction, "Este menu solo funciona dentro del servidor.")
            return
        if member.bot:
            await private_response(interaction, "Un bot no puede recibir acceso PCALL.")
            return
        if is_caller_penalized(self.db, guild.id, member.id):
            await private_response(
                interaction,
                f"{member.mention} tiene una penalizacion activa. "
                "Retirala primero desde `Quitar penalizacion`.",
            )
            return
        role = await self.ensure_caller_panel_role(guild, interaction.user)
        if role is None:
            await private_response(
                interaction,
                "No pude crear o encontrar el rol PCALL. Revisa que el bot pueda gestionar roles.",
            )
            return
        if role in member.roles:
            await private_response(
                interaction,
                f"{member.mention} ya tiene acceso PCALL al Panel de Callers con {role.mention}.",
            )
            return
        try:
            await member.add_roles(
                role,
                reason=f"PCALL asignado desde el Panel Administrativo por {interaction.user}",
            )
        except discord.Forbidden:
            await private_response(
                interaction,
                "No pude asignar el rol PCALL. Coloca el rol del bot por encima de PCALL y permite gestionar roles.",
            )
            return
        except discord.HTTPException:
            await private_response(interaction, "Discord no permitio asignar el rol PCALL. Intenta de nuevo.")
            return
        delivered = await send_dm_safe(
            self.db,
            guild_id=guild.id,
            user=member,
            action="bienvenida_pcall",
            content=(
                f"Ahora tienes acceso al Panel de Callers de {guild.name} como creador de contenido PCALL. "
                "Puedes crear pings, actividades, plantillas y splits desde el panel. "
                "Este rol no te registra como caller oficial ni te suma al ranking de callers."
            ),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=interaction.user.id,
            action="Agregar creador PCALL",
            affected_user_id=member.id,
            system="Callers",
            observation=f"Rol {role.name} ({role.id}) asignado para acceso al Panel de Callers.",
        )
        dm_status = "Le envie el aviso por DM." if delivered else "No pude enviarle DM, pero el acceso quedo activo."
        await private_response(
            interaction,
            f"{member.mention} ahora tiene acceso PCALL al Panel de Callers con {role.mention}. {dm_status}",
        )

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

    @commands.command(name="rechazar_cobro", aliases=["no_aprobar_cobro", "noaprobar_cobro"])
    async def rechazar_cobro(self, ctx: commands.Context, code: str, *, reason: str = "") -> None:
        if not await require_admin_context(ctx, self.db):
            return
        try:
            result = await self.reject_withdrawal(
                ctx.guild,
                code,
                ctx.author.id,
                reason.strip(),
            )
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await ctx.reply(result, mention_author=False)

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

    async def show_bulk_deposit_preview(
        self,
        interaction: discord.Interaction,
        *,
        admin_id: int,
        amounts: dict[int, int],
        concept: str,
        note: str,
        mode: str,
    ) -> None:
        if interaction.guild is None or interaction.user.id != admin_id or not is_admin_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para este deposito masivo.")
            return
        if not concept:
            await private_response(interaction, "El concepto es obligatorio.")
            return
        invalid = []
        resolved: dict[int, int] = {}
        for user_id, amount in amounts.items():
            member = interaction.guild.get_member(int(user_id))
            if member is None or member.bot:
                invalid.append(str(user_id))
                continue
            get_account(self.db, interaction.guild.id, member.id)
            resolved[member.id] = int(amount)
        if invalid:
            await private_response(interaction, "No pude resolver estos usuarios en el servidor: " + ", ".join(invalid[:10]))
            return
        if not resolved:
            await private_response(interaction, "No hay usuarios validos para depositar.")
            return
        operation_id = self.db.next_code(interaction.guild.id, "DM")
        total = sum(resolved.values())
        lines = [
            f"**Operacion:** `{operation_id}`",
            f"Modalidad: {mode}",
            f"Concepto: {concept}",
            f"Usuarios: {len(resolved)}",
            f"Total: {format_amount(total)}",
            "",
        ]
        for user_id, amount in list(resolved.items())[:20]:
            lines.append(f"<@{user_id}> - {format_amount(amount)}")
        if len(resolved) > 20:
            lines.append(f"... y {len(resolved) - 20} mas")
        await private_response(
            interaction,
            "Vista previa de deposito masivo:",
            embed=discord.Embed(description="\n".join(lines)[:4000], color=discord.Color.gold()),
            view=BulkDepositConfirmView(
                self,
                admin_id=admin_id,
                operation_id=operation_id,
                amounts=resolved,
                concept=concept,
                note=note,
                mode=mode,
            ),
        )

    async def execute_bulk_deposit(
        self,
        guild: discord.Guild,
        *,
        admin_id: int,
        operation_id: str,
        amounts: dict[int, int],
        concept: str,
        note: str,
        mode: str,
    ) -> str:
        successes: list[str] = []
        failures: list[str] = []
        total = 0
        for user_id, amount in amounts.items():
            member = guild.get_member(int(user_id))
            if member is None:
                failures.append(f"{user_id}: no esta en el servidor")
                continue
            try:
                movement_id = deposit_to_user_from_treasury(
                    self.db,
                    guild.id,
                    user_id=member.id,
                    amount=int(amount),
                    balance_type="available",
                    reason=f"{concept} | Operacion masiva {operation_id}" + (f" | {note}" if note else ""),
                    admin_id=admin_id,
                )
                total += int(amount)
                successes.append(f"{member.mention}: {format_amount(amount)} (mov #{movement_id})")
                account = get_account(self.db, guild.id, member.id)
                dm_sent = await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
                    user=member,
                    action="deposito_masivo",
                    content=(
                        f"Recibiste {format_amount(amount)} por `{concept}`.\n"
                        f"Operacion: `{operation_id}`\nNuevo saldo disponible: {format_amount(account['available'])}"
                    ),
                )
                if not dm_sent:
                    log_action(self.db, guild.id, admin_id=admin_id, action="Fallo DM deposito masivo", system="Banco", affected_user_id=member.id, amount=int(amount), observation=operation_id)
            except ValueError as exc:
                failures.append(f"{member.mention}: {exc}")
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Deposito masivo",
            system="Banco",
            amount=total,
            observation=f"{operation_id}; modo={mode}; usuarios_ok={len(successes)}; fallos={len(failures)}; concepto={concept}",
        )
        return "\n".join([
            f"Deposito masivo `{operation_id}` finalizado.",
            f"Exitosos: {len(successes)} | Fallidos: {len(failures)} | Total: {format_amount(total)}",
            "",
            *successes[:12],
            *( ["", "Fallos:", *failures[:8]] if failures else [] ),
        ])[:1900]

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
        if action == "add_admin":
            return await self.change_admin_access(
                guild,
                user_id=int(payload["user_id"]),
                authorized=True,
                changed_by=interaction.user.id,
            )
        if action == "remove_admin":
            return await self.change_admin_access(
                guild,
                user_id=int(payload["user_id"]),
                authorized=False,
                changed_by=interaction.user.id,
            )
        if action == "quick_liquidate_full":
            return await self.execute_quick_liquidation(
                guild,
                payout_id=int(payload["payout_id"]),
                admin_id=interaction.user.id,
            )
        if action == "quick_liquidate_individual":
            return await self.execute_quick_liquidation(
                guild,
                payout_id=int(payload["payout_id"]),
                admin_id=interaction.user.id,
                user_id=int(payload["user_id"]),
            )
        raise ValueError("Accion no reconocida.")

    def withdrawal_paid_amount(self, withdrawal) -> int:
        return int(withdrawal["amount_liquidated"] or 0)

    def withdrawal_pending_amount(self, withdrawal) -> int:
        return max(0, int(withdrawal["amount_requested"]) - self.withdrawal_paid_amount(withdrawal))

    def log_withdrawal_action(
        self,
        withdrawal_id: int,
        *,
        action_type: str,
        author_id: int,
        amount: int | None = None,
        old_status: str | None = None,
        new_status: str | None = None,
        note: str | None = None,
    ) -> None:
        self.db.execute(
            """
            INSERT INTO withdrawal_action_logs (
                withdrawal_id, action_type, author_id, amount,
                old_status, new_status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (withdrawal_id, action_type, author_id, amount, old_status, new_status, note, utc_now_iso()),
        )

    def is_withdrawal_operator(self, guild: discord.Guild, user_id: int, withdrawal=None) -> bool:
        member = guild.get_member(user_id)
        if member is None:
            return False
        if self.member_has_admin_access(guild, member):
            return True
        return withdrawal is not None and withdrawal["assigned_officer_id"] and int(withdrawal["assigned_officer_id"]) == user_id

    async def refresh_withdrawal_admin_message(self, guild: discord.Guild, code: str, admin_id: int | None = None) -> str:
        bank_cog = self.bot.get_cog("Bank")
        if bank_cog is None or not hasattr(bank_cog, "refresh_withdrawal_admin_message"):
            return ""
        return await bank_cog.refresh_withdrawal_admin_message(guild, code, actor_id=admin_id)

    async def reject_withdrawal(
        self,
        guild: discord.Guild,
        code: str,
        admin_id: int,
        reason: str = "",
    ) -> str:
        code = code.strip().upper()
        reason = str(reason or "").strip()[:600]
        key = (guild.id, code)
        if key in self._withdrawal_processing:
            raise ValueError("Esta solicitud ya se esta procesando. Espera unos segundos antes de reintentar.")
        self._withdrawal_processing.add(key)
        try:
            return await self._reject_withdrawal_locked(guild, code, admin_id, reason)
        finally:
            self._withdrawal_processing.discard(key)

    async def _reject_withdrawal_locked(
        self,
        guild: discord.Guild,
        code: str,
        admin_id: int,
        reason: str = "",
    ) -> str:
        now = utc_now_iso()
        with self.db.transaction() as cursor:
            withdrawal = cursor.execute(
                "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
                (guild.id, code),
            ).fetchone()
            if withdrawal is None:
                raise ValueError("No encontre esa solicitud.")
            if withdrawal["status"] != WITHDRAWAL_PENDING:
                raise ValueError("La solicitud ya no esta pendiente.")

            cursor.execute(
                """
                UPDATE withdrawals
                SET status = ?, rejected_by = ?, rejected_at = ?, rejection_reason = ?,
                    amount_liquidated = NULL,
                    approved_by = NULL, approved_at = NULL,
                    liquidated_by = NULL, liquidated_at = NULL,
                    assigned_officer_id = NULL, delegated_by = NULL,
                    payment_place = NULL, payment_schedule = NULL,
                    delegated_at = NULL, returned_at = NULL,
                    closed_at = ?, updated_at = ?
                WHERE guild_id = ? AND id = ? AND status = ?
                """,
                (
                    WITHDRAWAL_REJECTED,
                    admin_id,
                    now,
                    reason or None,
                    now,
                    now,
                    guild.id,
                    int(withdrawal["id"]),
                    WITHDRAWAL_PENDING,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("La solicitud ya no esta pendiente.")
            cursor.execute(
                """
                INSERT INTO withdrawal_action_logs (
                    withdrawal_id, action_type, author_id, amount,
                    old_status, new_status, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(withdrawal["id"]),
                    "no_aprobada",
                    admin_id,
                    int(withdrawal["amount_requested"]),
                    str(withdrawal["status"]),
                    WITHDRAWAL_REJECTED,
                    reason or None,
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit_logs (
                    guild_id, admin_id, action, affected_user_id, amount,
                    system, observation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild.id,
                    admin_id,
                    "Solicitud de cobro no aprobada",
                    int(withdrawal["user_id"]),
                    int(withdrawal["amount_requested"]),
                    "Banco",
                    f"{code}; motivo={reason}" if reason else code,
                    now,
                ),
            )
            user_id = int(withdrawal["user_id"])

        warning = ""
        user = guild.get_member(user_id)
        if user:
            dm_sent = await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="rechazar_cobro",
                content=(
                    f"Tu solicitud de cobro {code} no fue aprobada."
                    + (f"\nMotivo: {reason}" if reason else "")
                ),
            )
            if not dm_sent:
                log_action(
                    self.db,
                    guild.id,
                    admin_id=admin_id,
                    action="Fallo DM cobro no aprobado",
                    system="Banco",
                    affected_user_id=user_id,
                    observation=code,
                )
                warning = " Advertencia: no pude enviar DM al usuario."
        await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            content=(
                f"\U0000274C Cobro `{code}` no aprobado por <@{admin_id}> para <@{user_id}>."
                + (f" Motivo: {reason}" if reason else "")
            ),
        )
        warning += await self.refresh_withdrawal_admin_message(guild, code, admin_id)
        return f"Solicitud `{code}` no aprobada.{warning}"

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
                approval_admin_message = ?, updated_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                WITHDRAWAL_APPROVED,
                admin_id,
                utc_now_iso(),
                admin_message or None,
                utc_now_iso(),
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
        await self.refresh_withdrawal_admin_message(guild, code, admin_id)

    async def liquidate_withdrawal(
        self,
        guild: discord.Guild,
        code: str,
        amount: int,
        admin_id: int,
        admin_message: str = "",
    ) -> str:
        code = code.strip().upper()
        key = (guild.id, code)
        if key in self._withdrawal_processing:
            raise ValueError("Esta solicitud ya se esta procesando. Espera unos segundos antes de reintentar.")
        self._withdrawal_processing.add(key)
        try:
            return await self._liquidate_withdrawal_locked(guild, code, amount, admin_id, admin_message)
        finally:
            self._withdrawal_processing.discard(key)

    async def _liquidate_withdrawal_locked(
        self,
        guild: discord.Guild,
        code: str,
        amount: int,
        admin_id: int,
        admin_message: str = "",
    ) -> str:
        admin_message = normalize_admin_message(admin_message)
        withdrawal = self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (guild.id, code),
        )
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        if withdrawal["status"] not in {WITHDRAWAL_APPROVED, WITHDRAWAL_PARTIAL, WITHDRAWAL_DELEGATED, WITHDRAWAL_REASSIGNMENT}:
            raise ValueError("La solicitud debe estar aprobada, delegada o en pago parcial.")
        if not self.is_withdrawal_operator(guild, admin_id, withdrawal):
            raise ValueError("No tienes permiso para operar este cobro.")
        requested = int(withdrawal["amount_requested"])
        already_paid = self.withdrawal_paid_amount(withdrawal)
        pending = requested - already_paid
        if pending <= 0:
            raise ValueError("Esta solicitud ya no tiene cantidad pendiente.")
        if amount > pending:
            raise ValueError("No puedes pagar mas de la cantidad pendiente.")
        account = get_account(self.db, guild.id, int(withdrawal["user_id"]))
        if int(account["available"]) < amount:
            raise ValueError(
                "El usuario ya no tiene saldo disponible suficiente para pagar esta solicitud. "
                f"Saldo actual: {format_amount(account['available'])}. "
                f"Monto a pagar: {format_amount(amount)}."
            )

        adjust_user_balance(self.db, guild.id, int(withdrawal["user_id"]), available_delta=-amount)
        total_paid = already_paid + amount
        status = WITHDRAWAL_PAID if total_paid >= requested else WITHDRAWAL_PARTIAL
        movement_id = create_movement(
            self.db,
            guild.id,
            movement_type="LIQUIDACION",
            category="Cobro de saldo",
            amount=amount,
            description=f"Pago de {code}",
            created_by=admin_id,
            user_id=int(withdrawal["user_id"]),
            source_table="withdrawals",
            source_id=int(withdrawal["id"]),
        )
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, amount_liquidated = ?, liquidated_by = ?, liquidated_at = ?,
                liquidation_admin_message = ?, updated_at = ?, closed_at = ?
            WHERE guild_id = ? AND id = ? AND amount_liquidated IS ?
            """,
            (
                status,
                total_paid,
                admin_id,
                now,
                admin_message or None,
                now,
                now if status == WITHDRAWAL_PAID else None,
                guild.id,
                int(withdrawal["id"]),
                withdrawal["amount_liquidated"],
            ),
        )
        self.log_withdrawal_action(
            int(withdrawal["id"]),
            action_type="pago_completo" if status == WITHDRAWAL_PAID else "pago_parcial",
            author_id=admin_id,
            amount=amount,
            old_status=str(withdrawal["status"]),
            new_status=status,
            note=admin_message,
        )
        log_action(
            self.db,
            guild.id,
            admin_id=admin_id,
            action="Pago de solicitud de cobro",
            system="Banco",
            affected_user_id=int(withdrawal["user_id"]),
            amount=amount,
            observation=f"{code}; pagado={total_paid}; pendiente={requested-total_paid}; movimiento={movement_id}; {admin_message}",
        )
        user = guild.get_member(int(withdrawal["user_id"]))
        dm_sent = True
        if user:
            dm_sent = await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="pago_cobro",
                content=(
                    f"Tu solicitud `{code}` fue pagada.\n"
                    f"Monto solicitado: {format_amount(requested)}\n"
                    f"Monto pagado ahora: {format_amount(amount)}\n"
                    f"Total pagado: {format_amount(total_paid)}\n"
                    f"Pendiente: {format_amount(max(0, requested-total_paid))}\n"
                    f"Registrado por: <@{admin_id}>\nEstado: {status}{admin_message_block(admin_message)}"
                ),
            )
            if not dm_sent:
                log_action(self.db, guild.id, admin_id=admin_id, action="Fallo DM pago cobro", system="Banco", affected_user_id=int(withdrawal["user_id"]), amount=amount, observation=code)
        await send_admin_notification(
            self.db,
            guild=guild,
            category="withdrawals",
            content=(
                f"?? Cobro `{code}` pagado por <@{admin_id}>. "
                f"Usuario: <@{withdrawal['user_id']}> ? Monto: {format_amount(amount)} ? "
                f"Total pagado: {format_amount(total_paid)} ? Estado: {status} ? Movimiento #{movement_id}."
            ),
        )
        warning = " Advertencia: no pude enviar DM al usuario." if not dm_sent else ""
        warning += await self.refresh_withdrawal_admin_message(guild, code, admin_id)
        return f"Cobro `{code}` registrado por {format_amount(amount)}. Movimiento #{movement_id}.{warning}"

    async def pay_withdrawal_full(self, guild: discord.Guild, code: str, admin_id: int) -> str:
        withdrawal = self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?", (guild.id, code.strip().upper()))
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        return await self.liquidate_withdrawal(guild, code, self.withdrawal_pending_amount(withdrawal), admin_id, "Pago completo registrado.")

    async def mark_withdrawal_unpaid(self, guild: discord.Guild, code: str, admin_id: int, note: str = "") -> str:
        code = code.strip().upper()
        withdrawal = self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?", (guild.id, code))
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        if withdrawal["status"] in {WITHDRAWAL_PAID, WITHDRAWAL_UNPAID, WITHDRAWAL_REJECTED, WITHDRAWAL_CANCELLED}:
            raise ValueError("Esta solicitud ya esta cerrada.")
        if not self.is_withdrawal_operator(guild, admin_id, withdrawal):
            raise ValueError("No tienes permiso para cerrar este cobro.")
        pending = self.withdrawal_pending_amount(withdrawal)
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, closed_at = ?, updated_at = ?, return_reason = ?
            WHERE guild_id = ? AND id = ? AND status NOT IN (?, ?, ?, ?)
            """,
            (WITHDRAWAL_UNPAID, now, now, note or None, guild.id, int(withdrawal["id"]), WITHDRAWAL_PAID, WITHDRAWAL_UNPAID, WITHDRAWAL_REJECTED, WITHDRAWAL_CANCELLED),
        )
        self.log_withdrawal_action(int(withdrawal["id"]), action_type="no_pagado", author_id=admin_id, amount=pending, old_status=str(withdrawal["status"]), new_status=WITHDRAWAL_UNPAID, note=note)
        log_action(self.db, guild.id, admin_id=admin_id, action="Solicitud marcada no pagada", system="Banco", affected_user_id=int(withdrawal["user_id"]), amount=pending, observation=f"{code}; {note}")
        user = guild.get_member(int(withdrawal["user_id"]))
        if user:
            account = get_account(self.db, guild.id, user.id)
            sent = await send_dm_safe(
                self.db,
                guild_id=guild.id,
                user=user,
                action="cobro_no_pagado",
                content=(
                    "No asististe a cobrar tu solicitud. Tu plata se retorno a tu balance y deberas solicitar nuevamente tu pago.\n\n"
                    f"Solicitud: `{code}`\nCantidad retornada: {format_amount(pending)}\n"
                    f"Nuevo saldo disponible: {format_amount(account['available'])}\nFecha: {now}"
                ),
            )
            if not sent:
                log_action(self.db, guild.id, admin_id=admin_id, action="Fallo DM cobro no pagado", system="Banco", affected_user_id=user.id, amount=pending, observation=code)
        warning = await self.refresh_withdrawal_admin_message(guild, code, admin_id)
        return f"Cobro `{code}` cerrado como `{WITHDRAWAL_UNPAID}`. Pendiente conservado en balance: {format_amount(pending)}.{warning}"

    async def delegate_withdrawal(self, guild: discord.Guild, code: str, admin_id: int, officer_id: int, place: str, schedule: str, note: str = "") -> str:
        code = code.strip().upper()
        place = place.strip()
        schedule = schedule.strip()
        note = note.strip()
        if not place:
            raise ValueError("El lugar de pago es obligatorio.")
        if not schedule:
            raise ValueError("El dia y horario de pago son obligatorios.")
        withdrawal = self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?", (guild.id, code))
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        allowed_statuses = {WITHDRAWAL_PENDING, WITHDRAWAL_APPROVED, WITHDRAWAL_REASSIGNMENT}
        old_status = str(withdrawal["status"])
        if old_status not in allowed_statuses:
            raise ValueError("Solo se pueden delegar solicitudes pendientes, aprobadas o pendientes de reasignacion.")
        officer = guild.get_member(officer_id)
        if officer is None or officer.bot:
            raise ValueError("El delegado debe pertenecer al servidor y no puede ser bot.")
        if is_caller_penalized(self.db, guild.id, officer.id):
            raise ValueError("Este usuario esta bloqueado por una penalizacion activa.")
        if not self.is_payment_delegate_active(guild.id, officer.id):
            log_action(self.db, guild.id, admin_id=admin_id, action="Seleccion de delegado no autorizado", system="Banco", affected_user_id=officer.id, observation=code)
            raise ValueError("Este usuario no esta autorizado como delegado de pagos.")

        now = utc_now_iso()
        with self.db.transaction() as cursor:
            current = cursor.execute(
                "SELECT * FROM withdrawals WHERE guild_id = ? AND id = ?",
                (guild.id, int(withdrawal["id"])),
            ).fetchone()
            if current is None:
                raise ValueError("No encontre esa solicitud.")
            current_status = str(current["status"])
            if current_status not in allowed_statuses:
                raise ValueError("Solo se pueden delegar solicitudes pendientes, aprobadas o pendientes de reasignacion.")
            pending_amount = self.withdrawal_pending_amount(current)
            action_type = "aprobacion_delegacion" if current_status == WITHDRAWAL_PENDING else "delegacion"
            audit_action = "Solicitud aprobada y delegada" if current_status == WITHDRAWAL_PENDING else "Delegar cobro"
            cursor.execute(
                """
                UPDATE withdrawals
                SET status = ?,
                    approved_by = CASE WHEN status = ? THEN ? ELSE approved_by END,
                    approved_at = CASE WHEN status = ? THEN ? ELSE approved_at END,
                    assigned_officer_id = ?, delegated_by = ?, payment_place = ?,
                    payment_schedule = ?, delegated_at = ?, updated_at = ?
                WHERE guild_id = ? AND id = ? AND status IN (?, ?, ?)
                """,
                (
                    WITHDRAWAL_DELEGATED,
                    WITHDRAWAL_PENDING,
                    admin_id,
                    WITHDRAWAL_PENDING,
                    now,
                    officer_id,
                    admin_id,
                    place[:200],
                    schedule[:80],
                    now,
                    now,
                    guild.id,
                    int(withdrawal["id"]),
                    WITHDRAWAL_PENDING,
                    WITHDRAWAL_APPROVED,
                    WITHDRAWAL_REASSIGNMENT,
                ),
            )
            cursor.execute(
                """
                INSERT INTO withdrawal_action_logs (
                    withdrawal_id, action_type, author_id, amount,
                    old_status, new_status, note, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(withdrawal["id"]),
                    action_type,
                    admin_id,
                    pending_amount,
                    current_status,
                    WITHDRAWAL_DELEGATED,
                    f"delegado={officer_id}; lugar={place}; horario={schedule}; {note}".strip(),
                    now,
                ),
            )
            cursor.execute(
                """
                INSERT INTO audit_logs (
                    guild_id, admin_id, action, affected_user_id, amount,
                    system, observation, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    guild.id,
                    admin_id,
                    audit_action,
                    int(withdrawal["user_id"]),
                    pending_amount,
                    "Banco",
                    f"{code}; estado_anterior={current_status}; estado_final={WITHDRAWAL_DELEGATED}; delegado={officer_id}; lugar={place}; horario={schedule}; {note}".strip(),
                    now,
                ),
            )
        bank_cog = self.bot.get_cog("Bank")
        if bank_cog is not None:
            await bank_cog.send_delegated_withdrawal_dm(guild, code, officer, note)
        warning = await self.refresh_withdrawal_admin_message(guild, code, admin_id)
        return f"Cobro `{code}` delegado a {officer.mention}.{warning}"

    async def return_delegated_withdrawal(self, guild: discord.Guild, code: str, officer_id: int, reason: str) -> str:
        withdrawal = self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?", (guild.id, code.strip().upper()))
        if withdrawal is None:
            raise ValueError("No encontre esa solicitud.")
        if withdrawal["status"] != WITHDRAWAL_DELEGATED or int(withdrawal["assigned_officer_id"] or 0) != officer_id:
            raise ValueError("Solo el oficial asignado puede retornar este cobro.")
        now = utc_now_iso()
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, assigned_officer_id = NULL, returned_at = ?, return_reason = ?, updated_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (WITHDRAWAL_REASSIGNMENT, now, reason[:600], now, guild.id, int(withdrawal["id"])),
        )
        self.log_withdrawal_action(int(withdrawal["id"]), action_type="retorno_oficial", author_id=officer_id, amount=self.withdrawal_pending_amount(withdrawal), old_status=str(withdrawal["status"]), new_status=WITHDRAWAL_REASSIGNMENT, note=reason)
        log_action(self.db, guild.id, admin_id=officer_id, action="Oficial retorno cobro", system="Banco", affected_user_id=int(withdrawal["user_id"]), amount=self.withdrawal_pending_amount(withdrawal), observation=f"{code}; {reason}")
        await send_admin_notification(self.db, guild=guild, category="withdrawals", content=f"?? Cobro `{code}` retornado por <@{officer_id}>. Motivo: {reason}. Estado: {WITHDRAWAL_REASSIGNMENT}.")
        warning = await self.refresh_withdrawal_admin_message(guild, code, officer_id)
        return f"Cobro `{code}` retornado a administracion.{warning}"

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

    def approved_ping_channel_ids(self, guild_id: int) -> set[int]:
        return split_csv_ids(self.db.get_setting(guild_id, APPROVED_PING_CHANNELS_SETTING_KEY))

    def set_approved_ping_channel_ids(self, guild_id: int, channel_ids: set[int]) -> None:
        self.db.set_setting(guild_id, APPROVED_PING_CHANNELS_SETTING_KEY, join_csv_ids(channel_ids))

    def approved_ping_channels_summary(self, guild_id: int) -> str:
        channel_ids = sorted(self.approved_ping_channel_ids(guild_id))
        if not channel_ids:
            return "Sin canales aprobados"
        shown = ", ".join(f"<#{channel_id}>" for channel_id in channel_ids[:12])
        if len(channel_ids) > 12:
            shown += f" y {len(channel_ids) - 12} mas"
        return shown

    def approved_ping_channels_text(self, guild_id: int) -> str:
        channel_ids = sorted(self.approved_ping_channel_ids(guild_id))
        if not channel_ids:
            return "**Canales aprobados para pings**\nNo hay canales aprobados."
        guild = self.bot.get_guild(guild_id)
        lines = ["**Canales aprobados para pings**"]
        for channel_id in channel_ids:
            if guild is not None and guild.get_channel(channel_id) is None:
                lines.append(f"- ID `{channel_id}` (no encontrado)")
            else:
                lines.append(f"- <#{channel_id}>")
        return "\n".join(lines)

    def ticket_channel_status_text(self, guild_id: int) -> str:
        guild = self.bot.get_guild(guild_id)
        lines = ["🎫 **Sistema de tickets**"]
        lines.extend(
            self.ticket_channel_status_lines(
                guild,
                guild_id,
                setting_key=TICKET_CHANNEL_SETTING_KEY,
                title="Canal de notificaciones",
                conversation=False,
            )
        )
        lines.extend(
            self.ticket_channel_status_lines(
                guild,
                guild_id,
                setting_key=TICKET_CONVERSATION_CHANNEL_SETTING_KEY,
                title="Canal de conversaciones",
                conversation=True,
            )
        )
        return "\n".join(lines)

    def ticket_channel_status_lines(
        self,
        guild: discord.Guild | None,
        guild_id: int,
        *,
        setting_key: str,
        title: str,
        conversation: bool,
    ) -> list[str]:
        raw_channel_id = self.db.get_setting(guild_id, setting_key)
        if not raw_channel_id:
            return [f"{title}: No configurado"]
        if not raw_channel_id.isdigit():
            return [
                f"{title}: ID invalido `{raw_channel_id}`",
                f"Advertencia: selecciona un canal valido para {title.lower()}.",
            ]
        channel = guild.get_channel(int(raw_channel_id)) if guild is not None else None
        if channel is None:
            return [
                f"{title}: canal no disponible · ID `{raw_channel_id}`",
                "Advertencia: el canal configurado no existe o el bot no puede verlo.",
            ]
        mention = getattr(channel, "mention", f"<#{raw_channel_id}>")
        lines = [f"{title}: {mention}"]
        if conversation and not is_normal_text_ticket_channel(channel):
            lines.append("Advertencia: el canal de conversaciones debe ser un canal de texto normal.")
            return lines
        missing = ticket_channel_permission_errors(channel, guild, conversation=conversation)
        if missing:
            lines.append("Advertencia: faltan permisos del bot: " + ", ".join(missing) + ".")
        return lines

    def default_rates_text(self, guild_id: int) -> str:
        lines = [
            "**Tasas predeterminadas**",
            "Estas tasas se usan como sugerencia inicial. El caller puede cambiarlas al crear o corregir un split.",
            "",
        ]
        for key, (title, _label, fallback) in DEFAULT_RATE_SETTINGS.items():
            value = self.db.get_setting(guild_id, key, fallback)
            try:
                value = parse_admin_percent(value)
            except ValueError:
                value = fallback
            lines.append(f"{title}: `{value}%`")
        return "\n".join(lines)

    def notification_settings_text(self, guild_id: int) -> str:
        pings_channel = self.db.get_setting(guild_id, PING_PUBLICATIONS_SETTING_KEY)
        regear_channel = self.db.get_setting(guild_id, REGEAR_CHANNEL_SETTING_KEY)
        regear_notification_channel = self.db.get_setting(guild_id, REGEAR_NOTIFICATION_CHANNEL_SETTING_KEY)
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
                f"📣 **{PING_PUBLICATIONS_LABEL}:** {channel_setting_text(pings_channel)}",
                f"   Aprobados para callers: {self.approved_ping_channels_summary(guild_id)}",
                f"🛡️ **{REGEAR_CHANNEL_LABEL}:** {channel_setting_text(regear_channel)}",
                f"🛡️ **{REGEAR_NOTIFICATION_CHANNEL_LABEL}:** {channel_setting_text(regear_notification_channel)}",
                self.ticket_channel_status_text(guild_id),
                "",
                "Selecciona una categoría para establecer o cambiar su canal.",
                "Usa los botones de pings, Requips, Notificaciones de Requips y Tickets para elegir sus canales de trabajo.",
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
        lines.append("Comandos: `!aprobar_cobro CODIGO`, `!rechazar_cobro CODIGO [motivo opcional]`, `!liquidar_cobro CODIGO monto`. Botones: Aprobar, Rechazar, Pagado, Pago parcial, No pagado, Delegar.")
        return "\n".join(lines)

    def liquidation_history_text(self, guild_id: int) -> str:
        withdrawal_rows = self.db.fetch_all(
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
        quick_rows = self.db.fetch_all(
            """
            SELECT q.id, q.code, q.mode, q.admin_id, q.total_amount, q.created_at,
                   p.code AS payout_code, i.user_id, i.amount
            FROM quick_liquidations q
            JOIN payouts p ON p.id = q.payout_id
            JOIN quick_liquidation_items i ON i.liquidation_id = q.id
            WHERE q.guild_id = ?
              AND q.id IN (
                  SELECT id FROM quick_liquidations
                  WHERE guild_id = ? ORDER BY id DESC LIMIT 15
              )
            ORDER BY q.id DESC, i.id ASC
            """,
            (guild_id, guild_id),
        )
        if not withdrawal_rows and not quick_rows:
            return "No hay liquidaciones registradas."
        lines = ["🧾 **Historial de liquidaciones**"]
        grouped_quick: dict[int, dict] = {}
        for row in quick_rows:
            liquidation = grouped_quick.setdefault(
                int(row["id"]),
                {
                    "code": row["code"],
                    "mode": row["mode"],
                    "admin_id": row["admin_id"],
                    "total_amount": row["total_amount"],
                    "created_at": row["created_at"],
                    "payout_code": row["payout_code"],
                    "items": [],
                },
            )
            liquidation["items"].append((int(row["user_id"]), int(row["amount"])))
        for liquidation in grouped_quick.values():
            members = ", ".join(
                f"<@{user_id}> ({format_amount(amount)})"
                for user_id, amount in liquidation["items"]
            )
            lines.append(
                f"⚡ {liquidation['code']} · Split {liquidation['payout_code']} · "
                f"{liquidation['mode']} · {format_amount(liquidation['total_amount'])} · "
                f"Por <@{liquidation['admin_id']}> · {liquidation['created_at']}"
            )
            lines.append(f"↳ {members}")
        for row in withdrawal_rows:
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

    def pending_split_activities(self, guild_id: int, limit: int = 15):
        return self.db.fetch_all(
            """
            SELECT a.id, a.code, a.name, a.caller_id, a.horario,
                   a.voice_channel_id, a.created_at, a.started_at, a.ended_at,
                   COALESCE((
                       SELECT COUNT(*) FROM asistencia_actividades aa
                       WHERE aa.actividad_id = a.id AND aa.estado = 'Confirmado'
                   ), 0) AS confirmed,
                   COALESCE((
                       SELECT COUNT(*) FROM asistencia_actividades aa
                       WHERE aa.actividad_id = a.id AND aa.estado = 'Ausente'
                   ), 0) AS absent,
                   COALESCE((
                       SELECT COUNT(*) FROM activity_participants ap
                       WHERE ap.activity_id = a.id
                   ), 0) AS registered
            FROM activities a
            WHERE a.guild_id = ?
              AND a.status = ?
              AND COALESCE(a.activity_type, 'regular') != ?
              AND NOT EXISTS (
                  SELECT 1 FROM payouts p
                  WHERE p.guild_id = a.guild_id AND p.activity_id = a.id
              )
            ORDER BY COALESCE(a.ended_at, a.created_at) DESC, a.id DESC
            LIMIT ?
            """,
            (guild_id, ACTIVITY_FINISHED, ACTIVITY_TYPE_MANDATORY, limit),
        )

    def pending_split_activities_text(self, guild_id: int, *, rows=None) -> str:
        rows = list(rows) if rows is not None else self.pending_split_activities(guild_id)
        if not rows:
            return "No hay actividades pendientes de split."
        lines = ["🔴 **Actividades pendientes de split**"]
        for row in rows:
            voice = f"<#{row['voice_channel_id']}>" if row["voice_channel_id"] else "Sin canal"
            date = row["horario"] or row["ended_at"] or row["created_at"]
            lines.extend(
                [
                    "",
                    f"`{row['code']}` **{row['name']}**",
                    f"Caller: <@{row['caller_id']}> · Fecha/hora: `{date}` · Voz: {voice}",
                    (
                        f"Asistencia: {row['confirmed']} confirmados, "
                        f"{row['absent']} ausentes, {row['registered']} registrados"
                    ),
                ]
            )
        lines.append("\nSelecciona una actividad para revisar detalles, recordar al caller o crear split.")
        return "\n".join(lines)[:1900]

    def pending_split_activity_detail_text(self, guild: discord.Guild, activity_id: int) -> str:
        activity = self.db.fetch_one(
            """
            SELECT * FROM activities
            WHERE guild_id = ? AND id = ?
            """,
            (guild.id, activity_id),
        )
        if activity is None:
            return "No encontre esa actividad."
        rows = self.db.fetch_all(
            """
            SELECT ap.user_id, ap.display_name, ar.name AS role_name,
                   aa.estado, aa.voice_seconds, aa.participation_percent
            FROM activity_participants ap
            LEFT JOIN activity_roles ar ON ar.id = ap.role_id
            LEFT JOIN asistencia_actividades aa
              ON aa.actividad_id = ap.activity_id AND aa.usuario_id = ap.user_id
            WHERE ap.activity_id = ?
            ORDER BY ar.position ASC, ap.joined_at ASC
            """,
            (activity_id,),
        )
        voice = f"<#{activity['voice_channel_id']}>" if activity["voice_channel_id"] else "Sin canal"
        date = activity["horario"] or activity["ended_at"] or activity["created_at"]
        stats = get_persisted_activity_voice_stats(self.db, guild.id, activity_id)
        voice_summary = summarize_voice_stats(stats) if stats else None
        lines = [
            f"🔴 **Actividad pendiente de split:** `{activity['code']}`",
            f"Nombre: **{activity['name']}**",
            f"Caller: <@{activity['caller_id']}>",
            f"Fecha/hora: `{date}`",
            f"Canal de voz: {voice}",
        ]
        if voice_summary is not None:
            lines.append(
                f"Voz: {format_duration(voice_summary.monitoring_duration_seconds)} \u00b7 "
                f"promedio {voice_summary.average_attendance_percentage:.1f}% \u00b7 "
                f"sin entrar {voice_summary.never_joined}"
            )
        lines.extend([
            "",
            "**Participantes con asistencia**",
        ])
        if not rows:
            lines.append("Sin participantes registrados.")
        for row in rows:
            state = row["estado"] or "Sin registro"
            percent = float(row["participation_percent"] or 0)
            minutes = int(row["voice_seconds"] or 0) // 60
            lines.append(
                f"• <@{row['user_id']}> · {row['role_name'] or 'Sin rol'} · "
                f"{state} · {percent:.1f}% · {minutes} min"
            )
        return "\n".join(lines)[:1900]

    async def remind_pending_split_caller(
        self,
        interaction: discord.Interaction,
        activity_id: int,
    ) -> None:
        activity = self.db.fetch_one(
            """
            SELECT * FROM activities
            WHERE guild_id = ? AND id = ? AND status = ?
              AND COALESCE(activity_type, 'regular') != ?
            """,
            (interaction.guild.id, activity_id, ACTIVITY_FINISHED, ACTIVITY_TYPE_MANDATORY),
        )
        if activity is None:
            await private_response(interaction, "Esta actividad ya no esta pendiente de split.")
            return
        payout = self.db.fetch_one(
            "SELECT 1 FROM payouts WHERE guild_id = ? AND activity_id = ?",
            (interaction.guild.id, activity_id),
        )
        if payout is not None:
            await private_response(interaction, "Esta actividad ya tiene split asociado.")
            return
        caller = interaction.guild.get_member(int(activity["caller_id"]))
        if caller is None:
            await private_response(interaction, "No encontre al caller dentro del servidor.")
            return
        sent = await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=caller,
            action="recordatorio_split_pendiente",
            content=(
                f"🔴 Recordatorio: la actividad `{activity['code']}` **{activity['name']}** "
                "ya fue finalizada y sigue pendiente de split."
            ),
        )
        if sent:
            await private_response(interaction, f"Recordatorio enviado a {caller.mention}.")
        else:
            await private_response(interaction, "No pude enviar DM al caller; quedo registrado el intento.")

    def pending_payout_rows(self, guild_id: int):
        return self.db.fetch_all(
            """
            SELECT code, caller_id, distributable, guild_amount,
                   caller_amount, status, created_at, reviewed_at
            FROM payouts
            WHERE guild_id = ? AND status IN (?, ?) AND sent_to_admin_at IS NOT NULL
            ORDER BY CASE WHEN status = ? THEN 0 ELSE 1 END, id DESC LIMIT 25
            """,
            (guild_id, PAYOUT_PENDING, PAYOUT_CORRECTION, PAYOUT_PENDING),
        )

    def pending_payouts_text(self, guild_id: int) -> str:
        rows = self.pending_payout_rows(guild_id)
        if not rows:
            return "No hay Splits pendientes de aprobación."
        pending = [row for row in rows if row["status"] == PAYOUT_PENDING]
        correction = [row for row in rows if row["status"] == PAYOUT_CORRECTION]
        lines = ["⏳ **Splits pendientes de aprobación**"]
        if pending:
            for row in pending:
                lines.append(
                    f"`{row['code']}` · Caller <@{row['caller_id']}> · "
                    f"Repartible {format_amount(row['distributable'])} · "
                    f"Gremio {format_amount(row['guild_amount'])} · "
                    f"Caller {format_amount(row['caller_amount'])}"
                )
        else:
            lines.append("Sin splits listos para aprobar.")
        if correction:
            lines.extend(["", "🔁 **Requiere corrección**"])
            for row in correction:
                lines.append(
                    f"`{row['code']}` · Caller <@{row['caller_id']}> · "
                    f"Repartible {format_amount(row['distributable'])} · "
                    f"Gremio {format_amount(row['guild_amount'])} · "
                    f"Caller {format_amount(row['caller_amount'])}"
                )
        return "\n".join(lines)[:1900]

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

    def regear_table_columns(self) -> set[str]:
        return {
            row["name"]
            for row in self.db.fetch_all("PRAGMA table_info(regear_requests)")
        }

    def regear_ranking_rows(
        self,
        guild_id: int,
        filter_key: str,
        *,
        limit: int = 25,
    ) -> list:
        columns = self.regear_table_columns()
        amount_expr = (
            "COALESCE(SUM(CASE WHEN status = 'paid' "
            "THEN COALESCE(approved_amount, 0) ELSE 0 END), 0) AS approved_total"
            if "approved_amount" in columns
            else "0 AS approved_total"
        )
        where = ["guild_id = ?"]
        params: list = [guild_id]
        cutoff = regear_filter_cutoff(filter_key)
        if cutoff is not None:
            where.append("created_at >= ?")
            params.append(cutoff)
        params.append(limit)
        return self.db.fetch_all(
            f"""
            SELECT
                user_id,
                COUNT(*) AS total_requests,
                SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) AS pending_count,
                SUM(CASE WHEN status = 'paid' THEN 1 ELSE 0 END) AS paid_count,
                SUM(CASE WHEN status = 'pending_payment' THEN 1 ELSE 0 END) AS pending_payment_count,
                SUM(CASE WHEN status = 'rejected' THEN 1 ELSE 0 END) AS rejected_count,
                {amount_expr},
                MAX(created_at) AS last_created
            FROM regear_requests
            WHERE {' AND '.join(where)}
            GROUP BY user_id
            ORDER BY total_requests DESC, paid_count DESC, last_created DESC
            LIMIT ?
            """,
            params,
        )

    def regear_ranking_embed(self, guild: discord.Guild, filter_key: str) -> discord.Embed:
        rows = self.regear_ranking_rows(guild.id, filter_key, limit=25)
        label = regear_filter_label(filter_key)
        currency = self.db.get_setting(guild.id, "currency_name", "plata")
        embed = discord.Embed(
            title=f"🛡️ Ranking de Requips - {label}",
            color=discord.Color.gold(),
        )
        embed.description = (
            "Ordenado por mayor número de solicitudes, luego más pagados y luego solicitud más reciente.\n"
            "Usa el selector para consultar el historial completo de un jugador."
        )
        if not rows:
            embed.add_field(name="Sin datos", value="No hay solicitudes de requip en este filtro.", inline=False)
            return embed
        lines = []
        for index, row in enumerate(rows[:15], start=1):
            user_id = int(row["user_id"])
            amount = format_amount(int(row["approved_total"] or 0), currency)
            lines.append(
                f"**{index}.** <@{user_id}> | "
                f"Total: **{row['total_requests']}** | "
                f"✅ {row['paid_count']} | 🕒 {row['pending_payment_count']} | "
                f"❌ {row['rejected_count']} | ⏳ {row['pending_count']} | "
                f"Monto: {amount} | Última: {discord_date(row['last_created'], 'd')}"
            )
        embed.add_field(name="Jugadores", value="\n".join(lines), inline=False)
        if len(rows) > 15:
            embed.set_footer(text="El selector incluye hasta 25 jugadores del ranking.")
        return embed

    def regear_user_history_rows(self, guild_id: int, user_id: int) -> list:
        columns = self.regear_table_columns()
        optional_columns = []
        if "approved_amount" in columns:
            optional_columns.append("approved_amount")
        if "review_notes" in columns:
            optional_columns.append("review_notes")
        optional_sql = ", " + ", ".join(optional_columns) if optional_columns else ""
        return self.db.fetch_all(
            f"""
            SELECT request_code, status, created_at, reviewed_at, reviewed_by,
                   image_url, message_url{optional_sql}
            FROM regear_requests
            WHERE guild_id = ? AND user_id = ?
            ORDER BY created_at DESC, id DESC
            """,
            (guild_id, user_id),
        )

    def regear_user_history_chunks(self, guild: discord.Guild, user_id: int) -> list[str]:
        rows = self.regear_user_history_rows(guild.id, user_id)
        member = guild.get_member(user_id)
        subject = member.mention if member is not None else f"<@{user_id}>"
        header = f"🛡️ **Requips de {subject}**"
        if not rows:
            return [f"{header}\nSin solicitudes registradas."]
        currency = self.db.get_setting(guild.id, "currency_name", "plata")
        lines: list[str] = []
        for row in rows:
            reviewed_by = f"<@{row['reviewed_by']}>" if row["reviewed_by"] else "Sin revisar"
            reviewed_at = discord_date(row["reviewed_at"], "f") if row["reviewed_at"] else "Sin revisión"
            capture = f"[Ver captura]({row['image_url']})" if row["image_url"] else "Sin captura"
            message = f"[Ver mensaje]({row['message_url']})" if row["message_url"] else "Sin mensaje"
            parts = [
                f"**{row['request_code']}**",
                regear_status_display(str(row["status"] or "pending")),
                f"Creada: {discord_date(row['created_at'], 'd')}",
                f"Revisión: {reviewed_at}",
                f"Revisado por: {reviewed_by}",
                capture,
                message,
            ]
            if "approved_amount" in row.keys():
                amount = row["approved_amount"]
                parts.append(
                    f"Monto: {format_amount(int(amount), currency)}"
                    if amount is not None
                    else "Monto: sin registrar"
                )
            if "review_notes" in row.keys() and row["review_notes"]:
                parts.append(f"Obs: {str(row['review_notes'])[:180]}")
            lines.append(" | ".join(parts))

        chunks: list[str] = []
        current = header
        for line in lines:
            candidate = f"{current}\n{line}"
            if len(candidate) > 1800 and current != header:
                chunks.append(current)
                current = f"{header} (cont.)\n{line}"
            else:
                current = candidate
        chunks.append(current)
        return chunks

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

    def outside_balances_text(self, guild: discord.Guild, *, page: int = 0) -> tuple[str, int]:
        rows, total = list_outside_users_with_balance(self.db, guild, limit=8, offset=page * 8)
        lines = ["👥 **SALDOS DE USUARIOS FUERA**"]
        if not rows:
            lines.append("No hay usuarios fuera del servidor con balance disponible positivo.")
            return "\n".join(lines), total
        for row in rows:
            left_text = discord_date(row.left_at, "d") if row.left_at else "No disponible / anterior al registro"
            time_text = f"{row.days_out} días" if row.days_out is not None else "No disponible"
            lines.extend(
                [
                    "",
                    f"Usuario: `{row.user_id}`",
                    f"Nombre conocido: {row.display_name}",
                    f"Albion: {row.albion_name}",
                    f"Balance: {format_amount(row.available)}",
                    f"Fuera del servidor desde: {left_text}",
                    f"Tiempo fuera: {time_text}",
                ]
            )
        last_page = max(0, (total - 1) // 8)
        lines.append(f"\nPágina {page + 1}/{last_page + 1} · {total} usuario(s)")
        return "\n".join(lines)[:1900], total

    def balance_seizure_target_text(self, guild: discord.Guild, user_id: int) -> str:
        account = get_account(self.db, guild.id, user_id)
        name = known_user_name(self.db, guild.id, user_id, guild)
        return "\n".join(
            [
                "💰 **DECOMISAR BALANCE**",
                f"Usuario: {name}",
                f"Discord ID: `{user_id}`",
                "Albion: No registrado",
                f"Balance disponible actual: {format_amount(account['available'])}",
                "",
                "Elige si deseas decomisar todo el balance disponible o una cantidad específica.",
            ]
        )

    def balance_seizure_confirmation_text(
        self,
        guild: discord.Guild,
        user_id: int,
        *,
        amount: int,
        reason: str,
        origin: str,
        all_balance: bool = False,
    ) -> str:
        name = known_user_name(self.db, guild.id, user_id, guild)
        header = "⚠️ **CONFIRMAR DECOMISO**"
        scope = "TODO el balance disponible" if all_balance else "la cantidad indicada"
        return "\n".join(
            [
                header,
                "",
                f"Usuario: {name}",
                f"Discord ID: `{user_id}`",
                f"Balance a decomisar: {format_amount(amount)}",
                "",
                f"Esta acción moverá {scope} del usuario a Decomisado.",
                "",
                f"Razón:\n{reason or 'Sin razón'}",
                f"Origen/tipo: {origin or 'otro'}",
            ]
        )

    async def execute_balance_seizure(
        self,
        guild: discord.Guild,
        *,
        user_id: int,
        amount: int,
        admin_id: int,
        reason: str,
        origin: str,
    ) -> str:
        name = known_user_name(self.db, guild.id, user_id, guild)
        result = seize_user_balance(
            self.db,
            guild.id,
            user_id=user_id,
            amount=amount,
            admin_id=admin_id,
            reason=reason,
            origin=origin,
            known_name=name,
        )
        await send_admin_notification(
            self.db,
            guild=guild,
            category="general_admin",
            content=(
                "💰 **BALANCE DECOMISADO**\n\n"
                f"Usuario: <@{user_id}> (`{user_id}`)\n"
                f"Monto: {format_amount(result.amount)}\n"
                f"Disponible anterior: {format_amount(result.previous_available)}\n"
                f"Disponible posterior: {format_amount(result.new_available)}\n"
                f"Ejecutado por: <@{admin_id}>\n"
                f"Origen/tipo: {origin or 'otro'}\n"
                f"Razón: {reason}\n"
                f"Movimiento ID: `{result.movement_id}`"
            ),
        )
        return (
            "✅ Decomiso registrado correctamente.\n"
            f"Movimiento ID: `{result.movement_id}`\n"
            f"Disponible: {format_amount(result.previous_available)} → {format_amount(result.new_available)}\n"
            f"Decomisado acumulado: {format_amount(result.previous_seized)} → {format_amount(result.new_seized)}"
        )

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

