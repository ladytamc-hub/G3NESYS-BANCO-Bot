from __future__ import annotations

import io

import discord

from ..permissions import is_admin_subject
from ..services.withdrawal_audit import (
    AUDIT_CANCELLED,
    AUDIT_PAID,
    AUDIT_PARTIAL,
    AUDIT_REJECTED,
    AUDIT_RETURNED,
    AUDIT_UNPAID,
    WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE,
    WITHDRAWAL_AUDIT_PAGE_SIZE,
    WithdrawalAuditRecord,
    build_withdrawal_audit_report_files,
    get_withdrawal_audit_dataset,
    normalize_user_search,
    normalize_withdrawal_code,
    search_withdrawal_records,
)
from ..utils import format_amount




async def defer_ephemeral(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)

async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=True, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=True, **kwargs)


def short_date(value: str | None) -> str:
    return str(value or "Sin fecha")[:10]


def clip(value: str, limit: int) -> str:
    text = str(value or "")
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "\u2026"


def user_label(guild: discord.Guild | None, user_id: int | None) -> str:
    if user_id is None:
        return "Sin registro"
    member = guild.get_member(int(user_id)) if guild is not None else None
    if member is None:
        return f"Usuario fuera del servidor \u00b7 ID {int(user_id)}"
    return f"{member.mention} \u00b7 {member.display_name}"


def plain_user_name(guild: discord.Guild | None, user_id: int | None) -> str:
    if user_id is None:
        return ""
    member = guild.get_member(int(user_id)) if guild is not None else None
    if member is None:
        return f"Usuario fuera del servidor ID {int(user_id)}"
    names = [member.display_name]
    name = getattr(member, "name", None)
    if name and name not in names:
        names.append(name)
    return " ".join(names)


def add_message_button(view: discord.ui.View, record: WithdrawalAuditRecord | None, *, row: int) -> None:
    if record is None or record.message_url is None:
        return
    view.add_item(
        discord.ui.Button(
            label="Mensaje original",
            emoji="\U0001F517",
            style=discord.ButtonStyle.link,
            url=record.message_url,
            row=row,
        )
    )


async def edit_audit_message(
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


def build_withdrawal_audit_home_embed(cog, guild: discord.Guild) -> discord.Embed:
    dataset = get_withdrawal_audit_dataset(cog.db, guild.id)
    summary = dataset.summary
    embed = discord.Embed(
        title="\U0001F4B3 Auditor\u00eda de pagos y cobros",
        description="Consulta solo lectura de todas las solicitudes de cobro, abiertas y cerradas.",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Solicitado", value=format_amount(summary.total_requested), inline=True)
    embed.add_field(name="Pagado", value=format_amount(summary.total_paid), inline=True)
    embed.add_field(name="Pendiente", value=format_amount(summary.total_pending), inline=True)
    embed.add_field(name="Rechazado", value=format_amount(summary.total_rejected), inline=True)
    embed.add_field(name="Cobros abiertos", value=str(summary.open_count), inline=True)
    embed.add_field(name="Pagos parciales", value=str(summary.partial_count), inline=True)
    embed.add_field(name="Total solicitudes", value=str(summary.total_count), inline=True)
    embed.add_field(name="M\u00e1s reciente", value=short_date(summary.newest_date), inline=True)
    return embed


def title_for_mode(mode: str) -> str:
    return {
        "pending": "\U0001F7E1 Pendientes",
        "partial": "\U0001F7E3 Pagadas parcialmente",
        "paid": "\U0001F7E2 Pagadas",
        "rejected": "\U0001F534 Rechazadas",
        "returned": "\u21a9\ufe0f Regresadas",
        "cancelled": "\u26ab Canceladas",
        "unpaid": "\U0001F7E0 No pagadas",
        "all": "\U0001F4CB Todas",
    }.get(mode, "\U0001F4CB Todas")


def build_withdrawal_audit_list_embed(
    cog,
    guild: discord.Guild,
    mode: str,
    page: int,
    order: str = "desc",
) -> tuple[discord.Embed, list[WithdrawalAuditRecord], int]:
    dataset = get_withdrawal_audit_dataset(cog.db, guild.id)
    rows = dataset.filter_records(mode, order)
    total_pages = max(1, (len(rows) + WITHDRAWAL_AUDIT_PAGE_SIZE - 1) // WITHDRAWAL_AUDIT_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    start = page * WITHDRAWAL_AUDIT_PAGE_SIZE
    page_rows = rows[start : start + WITHDRAWAL_AUDIT_PAGE_SIZE]
    order_text = "M\u00e1s recientes primero" if order != "asc" else "M\u00e1s antiguas primero"
    embed = discord.Embed(title=title_for_mode(mode), color=discord.Color.teal())
    if not page_rows:
        embed.description = "No hay solicitudes en esta categor\u00eda."
    else:
        lines: list[str] = []
        for index, record in enumerate(page_rows, start=start + 1):
            responsible = (
                user_label(guild, record.rejected_by)
                if record.audit_status == AUDIT_REJECTED
                else user_label(guild, record.approved_by or record.delegated_by or record.liquidated_by)
            )
            lines.extend(
                [
                    f"**{index}. `{record.code}` \u00b7 {user_label(guild, record.user_id)}**",
                    f"Solicitado: **{format_amount(record.amount_requested)}** \u00b7 Pagado: **{format_amount(record.amount_paid)}** \u00b7 Pendiente: **{format_amount(record.pending_amount)}**",
                    f"Estado: **{record.audit_label}** \u00b7 Responsable: {responsible}",
                    f"Creado: `{short_date(record.created_at)}` \u00b7 \u00daltimo pago: `{short_date(record.last_payment_at)}`",
                ]
            )
            if record.is_partial or record.is_paid:
                paid_by = ", ".join(user_label(guild, user_id) for user_id in record.paid_by_ids) or "Sin pago registrado"
                lines.append(f"Pagado por: {paid_by}")
            if record.audit_status in {AUDIT_REJECTED, AUDIT_RETURNED, AUDIT_CANCELLED, AUDIT_UNPAID}:
                reason = record.rejection_reason or record.return_reason or "Sin motivo registrado"
                lines.append(f"Motivo: {clip(reason, 160)}")
            lines.append("")
        embed.description = "\n".join(lines)[:3900]
    embed.set_footer(text=f"P\u00e1gina {page + 1} de {total_pages} \u00b7 {len(rows)} solicitudes \u00b7 {order_text}")
    return embed, page_rows, total_pages


def build_withdrawal_audit_record_embed(cog, guild: discord.Guild, record: WithdrawalAuditRecord) -> discord.Embed:
    color = discord.Color.green() if record.is_paid else discord.Color.purple() if record.is_partial else discord.Color.gold()
    if record.audit_status in {AUDIT_REJECTED, AUDIT_CANCELLED}:
        color = discord.Color.red()
    embed = discord.Embed(
        title=f"\U0001F4B3 {record.code}",
        description="Ficha completa de auditor\u00eda de cobro.",
        color=color,
    )
    embed.add_field(name="Solicitante", value=user_label(guild, record.user_id), inline=False)
    embed.add_field(name="Monto solicitado", value=format_amount(record.amount_requested), inline=True)
    embed.add_field(name="Total pagado", value=format_amount(record.amount_paid), inline=True)
    embed.add_field(name="Saldo pendiente", value=format_amount(record.pending_amount), inline=True)
    embed.add_field(name="Estado actual", value=record.audit_label, inline=True)
    embed.add_field(name="Concepto", value=record.reason or "Sin concepto", inline=False)
    embed.add_field(name="Fecha de solicitud", value=record.created_at or "Sin fecha", inline=True)
    embed.add_field(name="Qui\u00e9n aprob\u00f3", value=user_label(guild, record.approved_by), inline=True)
    embed.add_field(name="\u00daltimo pago", value=record.last_payment_at or "Sin pago", inline=True)
    embed.add_field(name="Pagado por", value=", ".join(user_label(guild, user_id) for user_id in record.paid_by_ids) or "Sin pago", inline=False)
    if record.payment_place or record.payment_schedule:
        embed.add_field(name="Fuente o lugar de pago", value=" \u00b7 ".join(part for part in [record.payment_place, record.payment_schedule] if part), inline=False)
    if record.rejection_reason or record.return_reason:
        embed.add_field(name="Motivo", value=(record.rejection_reason or record.return_reason)[:1024], inline=False)
    embed.add_field(
        name="Mensaje original",
        value="Mensaje administrativo original disponible." if record.message_url else "Mensaje administrativo original no disponible.",
        inline=False,
    )
    embed.set_footer(text=f"Solicitado: {record.amount_requested} \u00b7 Pagado: {record.amount_paid} \u00b7 Pendiente: {record.pending_amount}")
    return embed


def build_withdrawal_audit_details_embed(
    cog,
    guild: discord.Guild,
    code: str,
    page: int,
) -> tuple[discord.Embed, int]:
    dataset = get_withdrawal_audit_dataset(cog.db, guild.id)
    record = dataset.get_record(code)
    movements = list(dataset.movements_for(code))
    total_pages = max(1, (len(movements) + WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE - 1) // WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE)
    page = min(max(page, 0), total_pages - 1)
    embed = discord.Embed(title=f"\U0001F4C4 Detalle de {normalize_withdrawal_code(code) or code}", color=discord.Color.green())
    if record is None:
        embed.description = "No encontr\u00e9 esa solicitud."
        return embed, total_pages
    start = page * WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE
    page_movements = movements[start : start + WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE]
    if not page_movements:
        embed.description = "No hay historial registrado para esta solicitud."
    else:
        lines: list[str] = []
        for index, movement in enumerate(page_movements, start=start + 1):
            lines.append(f"**{index}. {movement.action} por {user_label(guild, movement.actor_id)}**")
            if movement.amount is not None:
                lines.append(f"Monto: {format_amount(movement.amount)}")
            if movement.old_status or movement.new_status:
                lines.append(f"Estado: `{movement.old_status or 'N/D'}` \u2192 `{movement.new_status or 'N/D'}`")
            if movement.source:
                lines.append(f"Fuente: {movement.source}")
            if movement.note:
                lines.append(f"Motivo/nota: {clip(movement.note, 220)}")
            if movement.movement_code:
                lines.append(f"Movimiento: `{movement.movement_code}` #{movement.movement_id}")
            lines.append(f"Fecha: `{movement.date or 'Sin fecha'}`")
            lines.append("")
        embed.description = "\n".join(lines)[:3900]
    embed.add_field(name="Solicitante", value=user_label(guild, record.user_id), inline=False)
    embed.add_field(name="Solicitado", value=format_amount(record.amount_requested), inline=True)
    embed.add_field(name="Pagado", value=format_amount(record.amount_paid), inline=True)
    embed.add_field(name="Pendiente", value=format_amount(record.pending_amount), inline=True)
    embed.add_field(name="Estado", value=record.audit_label, inline=True)
    embed.set_footer(text=f"P\u00e1gina {page + 1} de {total_pages}")
    return embed, total_pages


class WithdrawalAuditBaseView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
        return False


class WithdrawalAuditHomeView(WithdrawalAuditBaseView):
    def __init__(self, cog, order: str = "desc", admin_panel_view_cls=None):
        super().__init__(cog)
        self.order = order
        if admin_panel_view_cls is not None:
            self.cog._withdrawal_audit_admin_panel_view_cls = admin_panel_view_cls
        self._add_buttons()

    def _add_buttons(self) -> None:
        self.add_item(WithdrawalAuditModeButton(self.cog, "Pendientes", "\U0001F7E1", "pending", self.order, discord.ButtonStyle.secondary, 0))
        self.add_item(WithdrawalAuditModeButton(self.cog, "Pagos parciales", "\U0001F7E3", "partial", self.order, discord.ButtonStyle.secondary, 0))
        self.add_item(WithdrawalAuditModeButton(self.cog, "Pagadas", "\U0001F7E2", "paid", self.order, discord.ButtonStyle.success, 0))
        self.add_item(WithdrawalAuditModeButton(self.cog, "Rechazadas", "\U0001F534", "rejected", self.order, discord.ButtonStyle.danger, 1))
        self.add_item(WithdrawalAuditModeButton(self.cog, "Regresadas", "\u21a9\ufe0f", "returned", self.order, discord.ButtonStyle.secondary, 1))
        self.add_item(WithdrawalAuditModeButton(self.cog, "Todas", "\U0001F4CB", "all", self.order, discord.ButtonStyle.primary, 1))
        self.add_item(WithdrawalAuditSearchButton(self.cog))
        next_order = "asc" if self.order != "asc" else "desc"
        label = "M\u00e1s recientes" if self.order != "asc" else "M\u00e1s antiguas"
        emoji = "\u2b07\ufe0f" if self.order != "asc" else "\u2b06\ufe0f"
        self.add_item(WithdrawalAuditOrderButton(self.cog, next_order, label, emoji))
        self.add_item(WithdrawalAuditReportMenuButton(self.cog))
        self.add_item(WithdrawalAuditBackAdminButton(self.cog))


class WithdrawalAuditModeButton(discord.ui.Button):
    def __init__(self, cog, label: str, emoji: str, mode: str, order: str, style: discord.ButtonStyle, row: int):
        super().__init__(label=label, emoji=emoji, style=style, custom_id=f"g3n:admin:withdrawal_audit:{mode}:{order}", row=row)
        self.cog = cog
        self.mode = mode
        self.order = order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        embed, page_rows, _ = build_withdrawal_audit_list_embed(self.cog, interaction.guild, self.mode, 0, self.order)
        await edit_audit_message(
            interaction,
            content="Auditor\u00eda de pagos y cobros:",
            embed=embed,
            view=WithdrawalAuditListView.from_records(self.cog, self.mode, 0, self.order, page_rows),
        )


class WithdrawalAuditSearchButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Buscar solicitud", emoji="\U0001F50E", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:search", row=2)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        await interaction.response.send_modal(WithdrawalAuditSearchModal(self.cog))


class WithdrawalAuditOrderButton(discord.ui.Button):
    def __init__(self, cog, next_order: str, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:order:{next_order}", row=2)
        self.cog = cog
        self.next_order = next_order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        view = WithdrawalAuditHomeView(self.cog, self.next_order)
        if not await view.require_admin(interaction):
            return
        await edit_audit_message(
            interaction,
            content="Auditor\u00eda de pagos y cobros:",
            embed=build_withdrawal_audit_home_embed(self.cog, interaction.guild),
            view=view,
        )


class WithdrawalAuditReportMenuButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Descargar reporte", emoji="\U0001F4E5", style=discord.ButtonStyle.primary, custom_id="g3n:admin:withdrawal_audit:report_menu", row=2)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        await edit_audit_message(
            interaction,
            content="Elige el reporte de auditor\u00eda de pagos y cobros:",
            view=WithdrawalAuditReportOptionsView(self.cog),
        )


class WithdrawalAuditBackAdminButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:back_admin", row=3)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        admin_panel_view_cls = getattr(self.cog, "_withdrawal_audit_admin_panel_view_cls", None)
        await edit_audit_message(
            interaction,
            content="Panel Administrativo G3NESYS",
            view=admin_panel_view_cls(self.cog) if admin_panel_view_cls is not None else None,
        )


class WithdrawalAuditDetailButton(discord.ui.Button):
    def __init__(self, cog, code: str, mode: str, page: int, order: str, index: int):
        super().__init__(label=f"Detalle {index}", emoji="\U0001F4C4", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:detail:{code}:{mode}:{page}:{order}", row=3)
        self.cog = cog
        self.code = code
        self.mode = mode
        self.page = page
        self.order = order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        embed, _ = build_withdrawal_audit_details_embed(self.cog, interaction.guild, self.code, 0)
        await edit_audit_message(
            interaction,
            content=f"Detalle de solicitud `{self.code}`:",
            embed=embed,
            view=WithdrawalAuditDetailsView(self.cog, self.code, 0, back_mode=self.mode, back_page=self.page, order=self.order, record=record),
        )


class WithdrawalAuditListView(WithdrawalAuditBaseView):
    def __init__(self, cog, mode: str, page: int, order: str):
        super().__init__(cog)
        self.mode = mode
        self.page = page
        self.order = order

    @classmethod
    def from_records(cls, cog, mode: str, page: int, order: str, records: list[WithdrawalAuditRecord]) -> "WithdrawalAuditListView":
        view = cls(cog, mode, page, order)
        for offset, record in enumerate(records, start=1 + page * WITHDRAWAL_AUDIT_PAGE_SIZE):
            view.add_item(WithdrawalAuditDetailButton(cog, record.code, mode, page, order, offset))
        view.add_item(WithdrawalAuditPreviousButton(cog, mode, page, order))
        view.add_item(WithdrawalAuditNextButton(cog, mode, page, order))
        next_order = "asc" if order != "asc" else "desc"
        label = "M\u00e1s recientes" if order != "asc" else "M\u00e1s antiguas"
        emoji = "\u2b07\ufe0f" if order != "asc" else "\u2b06\ufe0f"
        view.add_item(WithdrawalAuditListOrderButton(cog, mode, next_order, label, emoji))
        view.add_item(WithdrawalAuditBackHomeButton(cog, order))
        return view


class WithdrawalAuditPreviousButton(discord.ui.Button):
    def __init__(self, cog, mode: str, page: int, order: str):
        super().__init__(label="Anterior", emoji="\u2b05\ufe0f", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:prev:{mode}:{page}:{order}", row=4, disabled=page <= 0)
        self.cog = cog
        self.mode = mode
        self.page = page
        self.order = order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        new_page = max(0, self.page - 1)
        embed, page_rows, _ = build_withdrawal_audit_list_embed(self.cog, interaction.guild, self.mode, new_page, self.order)
        await edit_audit_message(interaction, content="Auditor\u00eda de pagos y cobros:", embed=embed, view=WithdrawalAuditListView.from_records(self.cog, self.mode, new_page, self.order, page_rows))


class WithdrawalAuditNextButton(discord.ui.Button):
    def __init__(self, cog, mode: str, page: int, order: str):
        super().__init__(label="Siguiente", emoji="\u27a1\ufe0f", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:next:{mode}:{page}:{order}", row=4)
        self.cog = cog
        self.mode = mode
        self.page = page
        self.order = order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        rows = dataset.filter_records(self.mode, self.order)
        total_pages = max(1, (len(rows) + WITHDRAWAL_AUDIT_PAGE_SIZE - 1) // WITHDRAWAL_AUDIT_PAGE_SIZE)
        new_page = min(total_pages - 1, self.page + 1)
        embed, page_rows, _ = build_withdrawal_audit_list_embed(self.cog, interaction.guild, self.mode, new_page, self.order)
        await edit_audit_message(interaction, content="Auditor\u00eda de pagos y cobros:", embed=embed, view=WithdrawalAuditListView.from_records(self.cog, self.mode, new_page, self.order, page_rows))


class WithdrawalAuditListOrderButton(discord.ui.Button):
    def __init__(self, cog, mode: str, next_order: str, label: str, emoji: str):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:list_order:{mode}:{next_order}", row=4)
        self.cog = cog
        self.mode = mode
        self.next_order = next_order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        embed, page_rows, _ = build_withdrawal_audit_list_embed(self.cog, interaction.guild, self.mode, 0, self.next_order)
        await edit_audit_message(interaction, content="Auditor\u00eda de pagos y cobros:", embed=embed, view=WithdrawalAuditListView.from_records(self.cog, self.mode, 0, self.next_order, page_rows))


class WithdrawalAuditBackHomeButton(discord.ui.Button):
    def __init__(self, cog, order: str = "desc"):
        super().__init__(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:back_home:{order}", row=4)
        self.cog = cog
        self.order = order

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        view = WithdrawalAuditHomeView(self.cog, self.order)
        if not await view.require_admin(interaction):
            return
        await edit_audit_message(
            interaction,
            content="Auditor\u00eda de pagos y cobros:",
            embed=build_withdrawal_audit_home_embed(self.cog, interaction.guild),
            view=view,
        )


class WithdrawalAuditRecordView(WithdrawalAuditBaseView):
    def __init__(self, cog, code: str, *, record: WithdrawalAuditRecord | None = None):
        super().__init__(cog)
        self.code = normalize_withdrawal_code(code) or code
        self.add_item(WithdrawalAuditRecordDetailsButton(cog, self.code))
        add_message_button(self, record, row=1)
        self.add_item(WithdrawalAuditBackHomeButton(cog))


class WithdrawalAuditRecordDetailsButton(discord.ui.Button):
    def __init__(self, cog, code: str):
        super().__init__(label="Historial", emoji="\U0001F4C4", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:record_history:{code}", row=0)
        self.cog = cog
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        embed, _ = build_withdrawal_audit_details_embed(self.cog, interaction.guild, self.code, 0)
        await edit_audit_message(
            interaction,
            content=f"Historial de solicitud `{self.code}`:",
            embed=embed,
            view=WithdrawalAuditDetailsView(self.cog, self.code, 0, back_mode="record", back_page=0, order="desc", record=record),
        )


class WithdrawalAuditDetailsView(WithdrawalAuditBaseView):
    def __init__(
        self,
        cog,
        code: str,
        page: int,
        *,
        back_mode: str,
        back_page: int,
        order: str,
        record: WithdrawalAuditRecord | None = None,
    ):
        super().__init__(cog)
        self.code = normalize_withdrawal_code(code) or code
        self.page = page
        self.back_mode = back_mode
        self.back_page = back_page
        self.order = order
        add_message_button(self, record, row=3)

    async def _show_page(self, interaction: discord.Interaction, page: int) -> None:
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        embed, _ = build_withdrawal_audit_details_embed(self.cog, interaction.guild, self.code, page)
        await edit_audit_message(
            interaction,
            content=f"Historial de solicitud `{self.code}`:",
            embed=embed,
            view=WithdrawalAuditDetailsView(self.cog, self.code, page, back_mode=self.back_mode, back_page=self.back_page, order=self.order, record=record),
        )

    @discord.ui.button(label="Anterior", emoji="\u2b05\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:detail_prev", row=4)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await defer_ephemeral(interaction)
        if not await self.require_admin(interaction):
            return
        await self._show_page(interaction, max(0, self.page - 1))

    @discord.ui.button(label="Siguiente", emoji="\u27a1\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:detail_next", row=4)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await defer_ephemeral(interaction)
        if not await self.require_admin(interaction):
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        movements = dataset.movements_for(self.code)
        total_pages = max(1, (len(movements) + WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE - 1) // WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE)
        await self._show_page(interaction, min(total_pages - 1, self.page + 1))

    @discord.ui.button(label="Volver", emoji="\u21a9\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:detail_back", row=4)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await defer_ephemeral(interaction)
        if not await self.require_admin(interaction):
            return
        if self.back_mode == "record":
            dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
            record = dataset.get_record(self.code)
            if record is None:
                await edit_audit_message(interaction, content="Auditor\u00eda de pagos y cobros:", embed=build_withdrawal_audit_home_embed(self.cog, interaction.guild), view=WithdrawalAuditHomeView(self.cog, self.order))
                return
            await edit_audit_message(interaction, content=f"Resultado de b\u00fasqueda `{self.code}`:", embed=build_withdrawal_audit_record_embed(self.cog, interaction.guild, record), view=WithdrawalAuditRecordView(self.cog, self.code, record=record))
            return
        embed, page_rows, _ = build_withdrawal_audit_list_embed(self.cog, interaction.guild, self.back_mode, self.back_page, self.order)
        await edit_audit_message(interaction, content="Auditor\u00eda de pagos y cobros:", embed=embed, view=WithdrawalAuditListView.from_records(self.cog, self.back_mode, self.back_page, self.order, page_rows))


class WithdrawalAuditSearchModal(discord.ui.Modal, title="Buscar solicitud"):
    query = discord.ui.TextInput(label="N\u00famero de solicitud o nombre del usuario", placeholder="COBRO-000006, 000006, Cometeelpan", max_length=80)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        matches = search_withdrawal_records(
            dataset,
            str(self.query.value),
            name_resolver=lambda user_id: plain_user_name(interaction.guild, user_id),
        )
        if not matches:
            await private_response(interaction, "No encontr\u00e9 solicitudes con esa b\u00fasqueda.")
            return
        if len(matches) == 1:
            record = matches[0]
            await private_response(
                interaction,
                f"Resultado de b\u00fasqueda `{record.code}`:",
                embed=build_withdrawal_audit_record_embed(self.cog, interaction.guild, record),
                view=WithdrawalAuditRecordView(self.cog, record.code, record=record),
            )
            return
        embed = discord.Embed(title="\U0001F50E Coincidencias de cobros", color=discord.Color.blurple())
        lines = [
            f"**{index}. `{record.code}`** \u00b7 {user_label(interaction.guild, record.user_id)} \u00b7 {format_amount(record.amount_requested)} \u00b7 {record.audit_label}"
            for index, record in enumerate(matches[:WITHDRAWAL_AUDIT_PAGE_SIZE], start=1)
        ]
        embed.description = "\n".join(lines)
        await private_response(
            interaction,
            "Selecciona una solicitud:",
            embed=embed,
            view=WithdrawalAuditSearchResultsView(self.cog, matches[:WITHDRAWAL_AUDIT_PAGE_SIZE]),
        )


class WithdrawalAuditSearchResultsView(WithdrawalAuditBaseView):
    def __init__(self, cog, records: list[WithdrawalAuditRecord]):
        super().__init__(cog)
        for index, record in enumerate(records, start=1):
            self.add_item(WithdrawalAuditSearchResultButton(cog, record.code, index))
        self.add_item(WithdrawalAuditBackHomeButton(cog))


class WithdrawalAuditSearchResultButton(discord.ui.Button):
    def __init__(self, cog, code: str, index: int):
        super().__init__(label=f"Solicitud {index}", emoji="\U0001F4C4", style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:search_result:{code}", row=0)
        self.cog = cog
        self.code = code

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        dataset = get_withdrawal_audit_dataset(self.cog.db, interaction.guild.id)
        record = dataset.get_record(self.code)
        if record is None:
            await private_response(interaction, "No encontr\u00e9 esa solicitud.")
            return
        await edit_audit_message(interaction, content=f"Resultado de b\u00fasqueda `{self.code}`:", embed=build_withdrawal_audit_record_embed(self.cog, interaction.guild, record), view=WithdrawalAuditRecordView(self.cog, self.code, record=record))




def _matches_user_query(guild: discord.Guild, user_id: int | None, query: str) -> bool:
    needle = normalize_user_search(query)
    if not needle:
        return True
    if user_id is None:
        return False
    names = [str(user_id), plain_user_name(guild, user_id)]
    return any(needle in normalize_user_search(name) or normalize_user_search(name) in needle for name in names if normalize_user_search(name))


def _parse_optional_amount(raw: str) -> int | None:
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return int(digits) if digits else None


def _parse_pair(raw: str) -> tuple[str, str]:
    text = str(raw or "").strip()
    if not text:
        return "", ""
    for separator in (" - ", " a ", "|", ","):
        if separator in text:
            left, right = text.split(separator, 1)
            return left.strip(), right.strip()
    return text, ""


def _advanced_filtered_records(cog, guild: discord.Guild, filters: dict[str, str]) -> list[WithdrawalAuditRecord]:
    dataset = get_withdrawal_audit_dataset(cog.db, guild.id)
    rows = dataset.sorted_records("desc")
    query = filters.get("query", "")
    code = normalize_withdrawal_code(query)
    if code is not None:
        rows = [record for record in rows if record.code == code]
    elif query:
        rows = [record for record in rows if _matches_user_query(guild, record.user_id, query)]
    approved_by = filters.get("approved_by", "")
    if approved_by:
        rows = [record for record in rows if _matches_user_query(guild, record.approved_by, approved_by)]
    paid_by = filters.get("paid_by", "")
    if paid_by:
        rows = [record for record in rows if any(_matches_user_query(guild, user_id, paid_by) for user_id in record.paid_by_ids)]
    date_start, date_end = _parse_pair(filters.get("dates", ""))
    if date_start:
        rows = [record for record in rows if short_date(record.created_at) >= date_start]
    if date_end:
        rows = [record for record in rows if short_date(record.created_at) <= date_end]
    amount_min_raw, amount_max_raw = _parse_pair(filters.get("amounts", ""))
    amount_min = _parse_optional_amount(amount_min_raw)
    amount_max = _parse_optional_amount(amount_max_raw)
    if amount_min is not None:
        rows = [record for record in rows if record.amount_requested >= amount_min]
    if amount_max is not None:
        rows = [record for record in rows if record.amount_requested <= amount_max]
    return rows


class WithdrawalAuditFilterButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Filtros", emoji="\u2699\ufe0f", style=discord.ButtonStyle.secondary, custom_id="g3n:admin:withdrawal_audit:filters", row=3)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        await interaction.response.send_modal(WithdrawalAuditFilterModal(self.cog))


class WithdrawalAuditFilterModal(discord.ui.Modal, title="Filtrar auditoría"):
    query = discord.ui.TextInput(label="Usuario o código", required=False, placeholder="COBRO-000006 o Cometeelpan", max_length=80)
    approved_by = discord.ui.TextInput(label="Responsable que aprobó", required=False, placeholder="LadySweett o ID", max_length=80)
    paid_by = discord.ui.TextInput(label="Responsable que pagó", required=False, placeholder="Tesorero o ID", max_length=80)
    dates = discord.ui.TextInput(label="Rango de fechas", required=False, placeholder="2026-08-01 - 2026-08-03", max_length=32)
    amounts = discord.ui.TextInput(label="Rango de monto", required=False, placeholder="100000 - 500000", max_length=32)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
            return
        filters = {
            "query": str(self.query.value).strip(),
            "approved_by": str(self.approved_by.value).strip(),
            "paid_by": str(self.paid_by.value).strip(),
            "dates": str(self.dates.value).strip(),
            "amounts": str(self.amounts.value).strip(),
        }
        matches = _advanced_filtered_records(self.cog, interaction.guild, filters)
        if not matches:
            await private_response(interaction, "No encontré solicitudes con esos filtros.")
            return
        embed = discord.Embed(title="⚙️ Resultados filtrados", color=discord.Color.blurple())
        embed.description = "\n".join(
            f"**{index}. `{record.code}`** · {user_label(interaction.guild, record.user_id)} · {format_amount(record.amount_requested)} · {record.audit_label}"
            for index, record in enumerate(matches[:WITHDRAWAL_AUDIT_PAGE_SIZE], start=1)
        )
        embed.set_footer(text=f"{len(matches)} coincidencias · mostrando primeras {min(len(matches), WITHDRAWAL_AUDIT_PAGE_SIZE)}")
        await private_response(
            interaction,
            "Resultados filtrados:",
            embed=embed,
            view=WithdrawalAuditSearchResultsView(self.cog, matches[:WITHDRAWAL_AUDIT_PAGE_SIZE]),
        )

class WithdrawalAuditReportOptionsView(WithdrawalAuditBaseView):
    def __init__(self, cog):
        super().__init__(cog)
        options = [
            ("Periodo completo", "all", "\U0001F4CB", 0),
            ("D\u00eda actual", "today", "\U0001F4C5", 0),
            ("\u00daltimos 7 d\u00edas", "last_7_days", "\U0001F5D3\ufe0f", 0),
            ("Mes actual", "month", "\U0001F4C6", 1),
            ("Solo pendientes", "pending", "\U0001F7E1", 1),
            ("Solo pagadas", "paid", "\U0001F7E2", 1),
            ("Pagos parciales", "partial", "\U0001F7E3", 2),
            ("Rech. o regresadas", "rejected_returned", "\U0001F534", 2),
        ]
        for label, mode, emoji, row in options:
            self.add_item(WithdrawalAuditReportButton(cog, label, mode, emoji, row))
        self.add_item(WithdrawalAuditReportRangeButton(cog))
        self.add_item(WithdrawalAuditBackHomeButton(cog))


class WithdrawalAuditReportButton(discord.ui.Button):
    def __init__(self, cog, label: str, mode: str, emoji: str, row: int):
        super().__init__(label=label, emoji=emoji, style=discord.ButtonStyle.secondary, custom_id=f"g3n:admin:withdrawal_audit:report:{mode}", row=row)
        self.cog = cog
        self.mode = mode

    async def callback(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        await send_withdrawal_audit_report(self.cog, interaction, self.mode)


class WithdrawalAuditReportRangeButton(discord.ui.Button):
    def __init__(self, cog):
        super().__init__(label="Rango personalizado", emoji="\U0001F4C6", style=discord.ButtonStyle.primary, custom_id="g3n:admin:withdrawal_audit:report_range", row=3)
        self.cog = cog

    async def callback(self, interaction: discord.Interaction) -> None:
        gate = WithdrawalAuditBaseView(self.cog)
        if not await gate.require_admin(interaction):
            return
        await interaction.response.send_modal(WithdrawalAuditReportRangeModal(self.cog))


class WithdrawalAuditReportRangeModal(discord.ui.Modal, title="Rango personalizado"):
    start_date = discord.ui.TextInput(label="Desde", placeholder="2026-08-01", max_length=10)
    end_date = discord.ui.TextInput(label="Hasta", placeholder="2026-08-03", max_length=10)

    def __init__(self, cog):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await defer_ephemeral(interaction)
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden usar este panel.")
            return
        await send_withdrawal_audit_report(
            self.cog,
            interaction,
            "range",
            date_from=str(self.start_date.value).strip(),
            date_to=str(self.end_date.value).strip(),
        )


async def send_withdrawal_audit_report(
    cog,
    interaction: discord.Interaction,
    mode: str,
    *,
    date_from: str | None = None,
    date_to: str | None = None,
) -> None:
    await defer_ephemeral(interaction)
    try:
        report_files = build_withdrawal_audit_report_files(
            cog.db,
            interaction.guild.id,
            mode=mode,
            date_from=date_from,
            date_to=date_to,
            name_resolver=lambda user_id: plain_user_name(interaction.guild, user_id),
        )
    except ValueError as exc:
        await interaction.followup.send(str(exc), ephemeral=True)
        return
    for index in range(0, len(report_files), 10):
        batch = report_files[index : index + 10]
        files = [discord.File(io.BytesIO(item.data), filename=item.filename) for item in batch]
        await interaction.followup.send(
            "Reporte de auditor\u00eda de pagos y cobros generado." if index == 0 else "Reporte de auditor\u00eda de pagos y cobros, continuaci\u00f3n.",
            files=files,
            ephemeral=True,
        )

