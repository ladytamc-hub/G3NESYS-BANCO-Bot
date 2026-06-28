from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, timezone

import discord
from discord.ext import commands

from ..constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_IN_PROGRESS,
    ACTIVITY_NOTICE,
    ACTIVITY_OPEN,
    ACTIVITY_PAYOUT_CREATED,
    ADMIN_PANEL_IMAGE,
    ATTENDANCE_ABSENT,
    ATTENDANCE_CONFIRMED,
    ATTENDANCE_PENDING,
    PAYOUT_CORRECTION,
    PAYOUT_PENDING,
    PINGS_PANEL_IMAGE,
)
from ..permissions import (
    can_manage_activity,
    has_bank_access,
    is_admin_subject,
    is_caller_subject,
    require_admin_context,
    require_caller_context,
)
from ..services.audit import log_action
from ..services.callers import (
    caller_ranking,
    cancellation_capacity,
    evaluate_caller_penalties,
    is_caller_penalized,
)
from ..services.fines import create_fine
from ..services.notifications import send_admin_notification, send_dm_safe
from ..services.payout_audit import log_payout_action
from ..services.reports import create_caller_report
from ..utils import (
    format_amount,
    is_custom_emoji_placeholder,
    normalize_key,
    parse_channel_id,
    parse_int_amount,
    resolve_custom_emojis,
    resolve_custom_emojis_in_embed,
    resolve_custom_emojis_in_send_kwargs,
    utc_now_iso,
)


MAX_ACTIVITY_ROLES = 15
ACTIVITY_MANAGEMENT_DENIED_MESSAGE = (
    "No puedes administrar esta actividad porque no fuiste quien la creó."
)
VOICE_CHANNEL_ERROR = "❌ Debes ingresar un ID válido de canal de voz."
ACTIVITY_EMBED_COLOR = discord.Color(0xE83E8C)
ACTIVITY_SEPARATOR = "────────────────────────────────"
ACTIVITY_COMPOSITION_SEPARATOR = "━━━━━━━━━━━━"
ACTIVITY_COMPOSITION_FIELD_LIMIT = 1024
ACTIVITY_COMPOSITION_FIELDS_PER_EMBED = 24
ACTIVITY_STATUS_LABELS = {
    ACTIVITY_OPEN: "🟢 ABIERTA",
    ACTIVITY_NOTICE: "🟡 EN AVISO",
    ACTIVITY_IN_PROGRESS: "🔵 EN CURSO",
    ACTIVITY_CANCELLED: "🔴 CANCELADA",
    ACTIVITY_FINISHED: "⚫ FINALIZADA",
    ACTIVITY_PAYOUT_CREATED: "🟣 EN SPLIT",
}


def looks_like_role_emoji_token(value: str) -> bool:
    token = (value or "").strip()
    return bool(token) and (
        token.startswith(("<:", "<a:"))
        or is_custom_emoji_placeholder(token)
        or not any(character.isalnum() for character in token)
    )


def resolve_template_text(value: str, guild: discord.Guild | None) -> str:
    return str(resolve_custom_emojis(str(value).strip(), guild) or "").strip()


def resolve_role_custom_emojis(
    roles: list[dict],
    guild: discord.Guild | None,
) -> list[dict]:
    resolved_roles: list[dict] = []
    for role in roles:
        resolved = dict(role)
        emoji = str(resolved.get("emoji") or "").strip()
        name = str(resolved.get("name") or "").strip()
        resolved["emoji"] = str(resolve_custom_emojis(emoji, guild) or emoji).strip()
        resolved["name"] = str(resolve_custom_emojis(name, guild) or name).strip()[:80]
        resolved_roles.append(resolved)
    return resolved_roles


def activity_status_label(status: str) -> str:
    return ACTIVITY_STATUS_LABELS.get(str(status), f"⚪ {str(status).upper()}")


def activity_visual_length(value: str) -> int:
    cleaned = re.sub(r"<@!?\d+>", "@Usuario", value)
    cleaned = re.sub(r"<#\d+>", "#voz", cleaned)
    cleaned = re.sub(r"\*\*", "", cleaned)
    return len(cleaned)


def activity_meta_row(left: str, right: str) -> str:
    gap = max(6, 38 - activity_visual_length(left))
    return f"{left}{' ' * gap}{right}"


def activity_capacity_bar(current: int, required: int) -> str:
    filled = min(max(current, 0), max(required, 0))
    empty = max(required - filled, 0)
    slots = (["🟩"] * filled) + (["▫️"] * empty)
    return " ".join(slots)


def activity_composition_marker(current: int, required: int) -> str:
    if required > 0 and current >= required:
        return "🟩"
    if current > 0:
        return "🟨"
    return "⬜"


def activity_composition_field_value(names: list[str]) -> str:
    player_lines = [f"• {name}" for name in names] or ["• Vacío"]
    value = "\n".join([ACTIVITY_COMPOSITION_SEPARATOR, *player_lines])
    if len(value) <= ACTIVITY_COMPOSITION_FIELD_LIMIT:
        return value

    clipped_lines = [ACTIVITY_COMPOSITION_SEPARATOR]
    notice = "• Lista recortada por límite de Discord."
    for line in player_lines:
        candidate = "\n".join([*clipped_lines, line, notice])
        if len(candidate) > ACTIVITY_COMPOSITION_FIELD_LIMIT:
            break
        clipped_lines.append(line)
    clipped_lines.append(notice)
    return "\n".join(clipped_lines)


def parse_role_lines(raw: str) -> list[dict]:
    roles: list[dict] = []
    for position, raw_line in enumerate(raw.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        emoji = ""
        name = ""
        slots = 0
        if "|" in line:
            parts = [part.strip() for part in line.split("|")]
            if len(parts) == 3 and parts[2].isdigit():
                emoji, name, slots_raw = parts
            elif len(parts) in {2, 3} and parts[1].isdigit():
                # Compatibilidad con plantillas escritas como nombre|cantidad|emoji.
                name, slots_raw = parts[:2]
                emoji = parts[2] if len(parts) == 3 else ""
            else:
                raise ValueError(
                    f"Linea {position}: usa Emoji | Rol/arma | Cantidad. "
                    "Ejemplo: 🌾 | Falce | 2"
                )
            slots = int(slots_raw)
        else:
            quantity_match = (
                re.fullmatch(r"(.+?)\s*=\s*(\d+)", line)
                or re.fullmatch(r"(.+?)(?::|-)\s*(\d+)", line)
                or re.fullmatch(r"(.+?)\s+(\d+)", line)
            )
            if quantity_match:
                name_part = quantity_match.group(1).strip()
                slots = int(quantity_match.group(2))
                first_part, separator, remaining = name_part.partition(" ")
                if separator and looks_like_role_emoji_token(first_part):
                    emoji = first_part
                    name = remaining.strip()
                else:
                    name = name_part
            else:
                raise ValueError(
                    f"Linea {position}: falta la cantidad requerida. "
                    "Ejemplo: Falce 2"
                )
        name = name.strip()
        emoji = emoji.strip()
        if not name:
            raise ValueError(f"Linea {position}: escribe el nombre del rol o arma.")
        if slots <= 0:
            raise ValueError(f"Linea {position}: la cantidad debe ser mayor que cero.")
        roles.append(
            {
                "key": normalize_key(name),
                "name": name[:80],
                "slots": slots,
                "emoji": emoji,
                "position": position,
            }
        )
    if not roles:
        raise ValueError("Debes agregar al menos un rol o arma.")
    if len(roles) > MAX_ACTIVITY_ROLES:
        raise ValueError(f"Puedes configurar hasta {MAX_ACTIVITY_ROLES} roles o armas por actividad.")
    keys = [role["key"] for role in roles]
    if len(keys) != len(set(keys)):
        raise ValueError("No puedes repetir el mismo nombre de rol o arma.")
    return roles


def parse_percent(raw: str) -> float:
    cleaned = (raw or "0").replace("%", "").replace(",", ".").strip()
    value = float(cleaned or 0)
    if value < 0 or value > 100:
        raise ValueError("El porcentaje debe estar entre 0 y 100.")
    return value


def parse_split_amount(raw: str, label: str, *, allow_zero: bool = False) -> int:
    if "-" in (raw or ""):
        raise ValueError(f"{label} no puede ser negativo.")
    cleaned = re.sub(r"[^0-9]", "", raw or "")
    if allow_zero and (not cleaned or int(cleaned) == 0):
        return 0
    try:
        return parse_int_amount(raw)
    except ValueError as exc:
        raise ValueError(f"{label} invalido.") from exc


def parse_cost_pair(raw: str) -> tuple[int, int]:
    parts = [part.strip() for part in re.split(r"[|;]", raw or "")]
    if len(parts) != 2:
        raise ValueError("Escribe los gastos como `reparaciones | otros`, por ejemplo `6000000 | 0`.")
    repairs = parse_split_amount(parts[0], "Reparaciones", allow_zero=True)
    expenses = parse_split_amount(parts[1], "Otros gastos", allow_zero=True)
    return repairs, expenses

def calculate_payout_totals(
    *,
    gross: int,
    market_rate: float,
    repairs: int,
    expenses: int,
    guild_percent: float,
    caller_percent: float,
) -> dict[str, int]:
    after_market = gross - int(round(gross * (market_rate / 100)))
    after_expenses = after_market - repairs - expenses
    if after_expenses < 0:
        raise ValueError("Los gastos superan el loot disponible.")
    guild_amount = int(round(after_expenses * (guild_percent / 100)))
    after_guild = after_expenses - guild_amount
    caller_amount = int(round(after_guild * (caller_percent / 100)))
    distributable = after_guild - caller_amount
    if distributable < 0:
        raise ValueError("El neto repartible queda negativo.")
    return {
        "guild_amount": guild_amount,
        "caller_amount": caller_amount,
        "distributable": distributable,
    }


def payout_values_snapshot(payout) -> str:
    return (
        f"loot={int(payout['gross_loot'])}; "
        f"mercado={float(payout['market_rate_percent'] or 0):.2f}%; "
        f"reparaciones={int(payout['repairs'] or 0)}; "
        f"otros={int(payout['other_expenses'] or 0)}; "
        f"gremio={float(payout['guild_percent'] or 0):.2f}%/{int(payout['guild_amount'] or 0)}; "
        f"caller={float(payout['caller_percent'] or 0):.2f}%/{int(payout['caller_amount'] or 0)}; "
        f"repartible={int(payout['distributable'] or 0)}"
    )

def resolve_voice_channel(guild: discord.Guild, raw: str | None):
    channel_id = parse_channel_id(raw)
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


def resolve_selected_voice_channel(guild: discord.Guild, value):
    if isinstance(value, (discord.VoiceChannel, discord.StageChannel)):
        return value
    channel_id = None
    if hasattr(value, "id"):
        try:
            channel_id = int(value.id)
        except (TypeError, ValueError):
            channel_id = None
    if channel_id is None and isinstance(value, str):
        cleaned = value.strip()
        if cleaned.isdigit():
            channel_id = int(cleaned)
        else:
            channel_id = parse_channel_id(cleaned)
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        return channel
    return None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


async def private_response(interaction: discord.Interaction, content: str, **kwargs) -> None:
    ephemeral = interaction.guild is not None
    content = resolve_custom_emojis(content, interaction.guild)
    kwargs = resolve_custom_emojis_in_send_kwargs(kwargs, interaction.guild)
    if interaction.response.is_done():
        await interaction.followup.send(content, ephemeral=ephemeral, **kwargs)
    else:
        await interaction.response.send_message(content, ephemeral=ephemeral, **kwargs)


async def reject_caller_access(db, interaction: discord.Interaction, action: str) -> None:
    if interaction.guild is not None and is_caller_penalized(
        db,
        interaction.guild.id,
        interaction.user.id,
    ):
        await private_response(
            interaction,
            "Tu acceso de caller esta suspendido por reputacion. "
            "Un administrador debe retirar la penalizacion desde el Panel Administrativo.",
        )
        return
    await private_response(interaction, f"Solo callers autorizados o admins pueden {action}.")


async def dm_or_private(cog: "Activities", interaction: discord.Interaction, content: str, action: str) -> None:
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


class TemplateModal(discord.ui.Modal, title="Crear Plantilla"):
    template_name = discord.ui.TextInput(label="Nombre de plantilla", max_length=80)
    activity_name = discord.ui.TextInput(label="Nombre base de actividad", max_length=100)
    default_time = discord.ui.TextInput(label="Horario base", max_length=40)
    description = discord.ui.TextInput(
        label="Descripcion obligatoria",
        style=discord.TextStyle.paragraph,
        placeholder="Explica el objetivo, requisitos o indicaciones de la actividad.",
        max_length=600,
    )
    roles = discord.ui.TextInput(
        label="Rol/arma y cantidad (emoji opcional)",
        style=discord.TextStyle.paragraph,
        placeholder="Falce 2\n🔮 Prisma 2\n🛡️ | Tanque | 1",
        max_length=1800,
    )

    def __init__(self, cog: "Activities", *, voice_channel_id: int, publica: bool = False):
        super().__init__(timeout=300)
        self.cog = cog
        self.voice_channel_id = voice_channel_id
        self.publica = publica

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_template_from_modal(interaction, self)


class TemplateVoiceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent_view: "TemplateVisibilityView"):
        super().__init__(
            placeholder="Selecciona el canal de voz obligatorio",
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            min_values=1,
            max_values=1,
            row=1,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self.values:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        channel = resolve_selected_voice_channel(interaction.guild, self.values[0])
        if channel is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        self.parent_view.voice_channel_id = channel.id
        await interaction.response.edit_message(content=self.parent_view.visibility_text(), view=self.parent_view)


class TemplateVisibilityView(discord.ui.View):
    def __init__(self, cog: "Activities", *, author_id: int, publica: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.publica = publica
        self.voice_channel_id: int | None = None
        self.add_item(TemplateVoiceChannelSelect(self))
        self.update_toggle_button()

    def visibility_text(self) -> str:
        voice_text = f"<#{self.voice_channel_id}>" if self.voice_channel_id else "Pendiente"
        return (
            "Elige la visibilidad de la plantilla antes de completar el formulario.\n"
            "Privada: solo tu puedes verla y usarla.\n"
            "Publica: cualquier Caller puede verla y usarla; solo tu o un admin podran administrarla.\n"
            f"Canal de voz: {voice_text}"
        )

    def update_toggle_button(self) -> None:
        self.public_toggle.label = "Plantilla publica: Si" if self.publica else "Plantilla publica: No"
        self.public_toggle.style = discord.ButtonStyle.success if self.publica else discord.ButtonStyle.secondary
        self.public_toggle.emoji = "🌐" if self.publica else "🔒"

    async def require_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id and is_caller_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo quien abrio esta creacion puede continuar.")
        return False

    @discord.ui.button(label="Plantilla publica: No", emoji="🔒", style=discord.ButtonStyle.secondary, row=0)
    async def public_toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_author(interaction):
            return
        self.publica = not self.publica
        self.update_toggle_button()
        await interaction.response.edit_message(content=self.visibility_text(), view=self)

    @discord.ui.button(label="Continuar", emoji="➡️", style=discord.ButtonStyle.primary, row=2)
    async def continue_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_author(interaction):
            return
        if self.voice_channel_id is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        channel = interaction.guild.get_channel(self.voice_channel_id) if interaction.guild else None
        if not isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        await interaction.response.send_modal(
            TemplateModal(
                self.cog,
                voice_channel_id=self.voice_channel_id,
                publica=self.publica,
            )
        )

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_author(interaction):
            return
        await interaction.response.edit_message(content="Creacion de plantilla cancelada.", view=None)


class ActivityModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "Activities",
        *,
        template_id: int | None,
        default_name: str = "",
        default_time: str = "",
        default_notes: str = "",
        default_voice_channel_id: int | None = None,
    ):
        title = "Crear Ping"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.template_id = template_id
        self.activity_name = discord.ui.TextInput(
            label="Nombre de actividad",
            max_length=100,
            default=default_name,
        )
        self.horario = discord.ui.TextInput(
            label="Horario",
            max_length=40,
            default=default_time,
        )
        self.voice_channel = discord.ui.TextInput(
            label="ID o mencion del canal de voz",
            placeholder="123456789012345678 o <#123456789012345678>",
            required=True,
            max_length=80,
            default=str(default_voice_channel_id or ""),
        )
        self.notes = discord.ui.TextInput(
            label="Observaciones",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
            default=default_notes,
        )
        self.add_item(self.activity_name)
        self.add_item(self.horario)
        self.add_item(self.voice_channel)
        self.add_item(self.notes)
        self.roles = None
        if template_id is None:
            self.roles = discord.ui.TextInput(
                label="Rol/arma y cantidad (emoji opcional)",
                style=discord.TextStyle.paragraph,
                placeholder="Falce 2\n🔮 Prisma 2\n🛡️ | Tanque | 1",
                max_length=1800,
            )
            self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.publish_activity_from_modal(interaction, self)


class PayoutModal(discord.ui.Modal, title="Splitear actividad"):
    gross_loot = discord.ui.TextInput(label="Loot bruto", placeholder="45000000")
    market_rate = discord.ui.TextInput(label="Tasa mercado %", placeholder="4", default="0")
    costs = discord.ui.TextInput(
        label="Reparaciones | otros gastos",
        placeholder="6000000 | 0",
        default="0 | 0",
    )
    guild_percent = discord.ui.TextInput(label="Porcentaje gremial %", placeholder="10", default="10")
    caller_percent = discord.ui.TextInput(label="Porcentaje para el caller %", placeholder="5", default="0")

    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=300)
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        if activity is not None:
            guild_id = int(activity["guild_id"])
            self.market_rate.default = cog.db.get_setting(guild_id, "market_rate_default", "0")
            self.guild_percent.default = cog.db.get_setting(
                guild_id, "guild_percentage_default", "10"
            )
            self.caller_percent.default = cog.db.get_setting(
                guild_id, "caller_percentage_default", "0"
            )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_payout_from_modal(interaction, self.activity_id, self)


class PayoutCorrectionModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "Activities",
        guild_id: int,
        payout_code: str,
        payout,
        source_message=None,
    ):
        super().__init__(title="Corregir Split", timeout=300)
        self.cog = cog
        self.guild_id = guild_id
        self.payout_code = payout_code
        self.source_message = source_message
        self.gross_loot = discord.ui.TextInput(
            label="Loot bruto",
            placeholder="45000000",
            default=str(int(payout["gross_loot"] or 0)),
        )
        self.market_rate = discord.ui.TextInput(
            label="Tasa mercado %",
            placeholder="4",
            default=f"{float(payout['market_rate_percent'] or 0):g}",
        )
        self.costs = discord.ui.TextInput(
            label="Reparaciones | otros gastos",
            placeholder="6000000 | 0",
            default=f"{int(payout['repairs'] or 0)} | {int(payout['other_expenses'] or 0)}",
        )
        self.guild_percent = discord.ui.TextInput(
            label="Porcentaje gremial %",
            placeholder="10",
            default=f"{float(payout['guild_percent'] or 0):g}",
        )
        self.caller_percent = discord.ui.TextInput(
            label="Porcentaje para el caller %",
            placeholder="5",
            default=f"{float(payout['caller_percent'] or 0):g}",
        )
        self.add_item(self.gross_loot)
        self.add_item(self.market_rate)
        self.add_item(self.costs)
        self.add_item(self.guild_percent)
        self.add_item(self.caller_percent)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.correct_payout_values_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            self,
            source_message=self.source_message,
        )

class EditCompositionModal(discord.ui.Modal, title="Modificar composicion"):
    roles = discord.ui.TextInput(
        label="Emoji | Rol/arma | Cantidad",
        style=discord.TextStyle.paragraph,
        max_length=1800,
    )

    def __init__(self, cog: "Activities", activity_id: int, current_roles: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.activity_id = activity_id
        self.roles.default = current_roles

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.edit_composition_from_modal(
            interaction,
            self.activity_id,
            str(self.roles.value),
        )


class EditActivityNotesModal(discord.ui.Modal, title="Editar observaciones"):
    def __init__(self, cog: "Activities", activity_id: int, current_notes: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.activity_id = activity_id
        self.notes = discord.ui.TextInput(
            label="Observaciones",
            required=False,
            style=discord.TextStyle.paragraph,
            max_length=600,
            default=current_notes[:600],
        )
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.edit_activity_notes_from_modal(
            interaction,
            self.activity_id,
            str(self.notes.value),
        )


class JoinActivityRequestModal(discord.ui.Modal, title="Solicitar unirme"):
    requested_role = discord.ui.TextInput(
        label="Rol o arma solicitado",
        placeholder="Falce",
        max_length=80,
    )

    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.activity_id = activity_id

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_join_request(
            interaction,
            self.activity_id,
            str(self.requested_role.value),
        )


class TemplateSelect(discord.ui.Select):
    def __init__(self, cog: "Activities", templates):
        self.cog = cog
        options = []
        for row in templates[:25]:
            visibility = "Publica" if int(row["publica"]) else "Privada"
            options.append(
                discord.SelectOption(
                    label=row["name"][:100],
                    description=f"{visibility} - {row['activity_name']} - {row['default_time']}"[:100],
                    value=str(row["id"]),
                )
            )
        super().__init__(
            placeholder="Selecciona una plantilla",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="g3n:pings:template_select",
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Este selector solo funciona dentro del servidor.")
            return
        template_id = int(self.values[0])
        if is_admin_subject(self.cog.db, interaction):
            template = self.cog.db.fetch_one(
                "SELECT * FROM templates WHERE id = ? AND guild_id = ?",
                (template_id, interaction.guild.id),
            )
        else:
            template = self.cog.db.fetch_one(
                """
                SELECT *
                FROM templates
                WHERE id = ? AND guild_id = ? AND (created_by = ? OR publica = 1)
                """,
                (template_id, interaction.guild.id, interaction.user.id),
            )
        if template is None:
            await private_response(interaction, "No encontre esa plantilla disponible para ti.")
            return
        await interaction.response.send_modal(
            ActivityModal(
                self.cog,
                template_id=template_id,
                default_name=template["activity_name"],
                default_time=template["default_time"],
                default_notes=template["description"],
                default_voice_channel_id=template["voice_channel_id"],
            )
        )


class TemplateSelectView(discord.ui.View):
    def __init__(self, cog: "Activities", templates):
        super().__init__(timeout=180)
        self.add_item(TemplateSelect(cog, templates))


class CallerConfigValueModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "Activities",
        *,
        key: str,
        title: str,
        label: str,
        placeholder: str,
        current_value: str,
    ):
        super().__init__(title=title, timeout=180)
        self.cog = cog
        self.key = key
        self.value_input = discord.ui.TextInput(
            label=label,
            placeholder=placeholder,
            default=current_value,
            max_length=40,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden cambiar Config.")
            return
        value = str(self.value_input.value).strip()
        try:
            if self.key in {"caller_percentage_default", "voice_minimum_percent"}:
                parsed = parse_percent(value)
                value = f"{parsed:.2f}".rstrip("0").rstrip(".")
            elif self.key == "absence_fine_amount":
                value = str(parse_int_amount(value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        self.cog.db.set_setting(interaction.guild.id, self.key, value)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar Panel de Callers",
            system="Configuracion",
            observation=f"{self.key}={value}",
        )
        await private_response(
            interaction,
            f"Config actualizada: `{self.key}` = `{value}`.\n\n"
            f"{self.cog.caller_config_text(interaction.guild.id)}",
        )


class CallerConfigPanelView(discord.ui.View):
    def __init__(self, cog: "Activities"):
        super().__init__(timeout=300)
        self.cog = cog

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is not None and is_admin_subject(self.cog.db, interaction):
            return True
        await private_response(interaction, "Solo admins autorizados pueden usar Config.")
        return False

    async def set_current_channel(
        self,
        interaction: discord.Interaction,
        *,
        key: str,
        label: str,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        channel = interaction.channel
        if channel is None or not hasattr(channel, "id"):
            await private_response(interaction, "No pude identificar este canal.")
            return
        self.cog.db.set_setting(interaction.guild.id, key, str(channel.id))
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar Panel de Callers",
            system="Configuracion",
            observation=f"{key}={channel.id}",
        )
        await private_response(
            interaction,
            f"{label} actualizado a <#{channel.id}>.\n\n{self.cog.caller_config_text(interaction.guild.id)}",
        )

    async def open_value_modal(
        self,
        interaction: discord.Interaction,
        *,
        key: str,
        title: str,
        label: str,
        placeholder: str,
    ) -> None:
        if not await self.require_admin(interaction):
            return
        await interaction.response.send_modal(
            CallerConfigValueModal(
                self.cog,
                key=key,
                title=title,
                label=label,
                placeholder=placeholder,
                current_value=self.cog.db.get_setting(interaction.guild.id, key),
            )
        )

    @discord.ui.button(label="Ver config", emoji="📋", style=discord.ButtonStyle.primary, row=0)
    async def show_config(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if await self.require_admin(interaction):
            await private_response(interaction, self.cog.caller_config_text(interaction.guild.id))

    @discord.ui.button(label="Canal pings", emoji="📍", style=discord.ButtonStyle.success, row=0)
    async def set_pings_channel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.set_current_channel(
            interaction,
            key="channel_pings_id",
            label="Canal de pings",
        )

    @discord.ui.button(label="Avisos actividad", emoji="📣", style=discord.ButtonStyle.secondary, row=0)
    async def set_activity_notifications(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.set_current_channel(
            interaction,
            key="channel_notify_activities_id",
            label="Canal de avisos de actividad",
        )

    @discord.ui.button(label="Multa ausencia", emoji="⚠️", style=discord.ButtonStyle.secondary, row=1)
    async def set_absence_fine(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_value_modal(
            interaction,
            key="absence_fine_amount",
            title="Multa por ausencia",
            label="Monto de multa",
            placeholder="200000",
        )

    @discord.ui.button(label="Permanencia %", emoji="🎙️", style=discord.ButtonStyle.secondary, row=1)
    async def set_voice_minimum(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_value_modal(
            interaction,
            key="voice_minimum_percent",
            title="Permanencia minima",
            label="Porcentaje minimo en voz",
            placeholder="50",
        )

    @discord.ui.button(label="Pago caller %", emoji="💰", style=discord.ButtonStyle.secondary, row=1)
    async def set_caller_percent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.open_value_modal(
            interaction,
            key="caller_percentage_default",
            title="Pago caller predeterminado",
            label="Porcentaje para caller",
            placeholder="5",
        )

    @discord.ui.button(label="Multas ON/OFF", emoji="🟢", style=discord.ButtonStyle.secondary, row=2)
    async def toggle_absence_fines(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        current = self.cog.db.get_setting(interaction.guild.id, "absence_fine_enabled", "1")
        enabled = str(current).strip().lower() in {"1", "true", "si", "sí", "yes", "on"}
        value = "0" if enabled else "1"
        self.cog.db.set_setting(interaction.guild.id, "absence_fine_enabled", value)
        log_action(
            self.cog.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Configurar Panel de Callers",
            system="Configuracion",
            observation=f"absence_fine_enabled={value}",
        )
        state = "desactivadas" if enabled else "activadas"
        await private_response(
            interaction,
            f"Multas por ausencia {state}.\n\n{self.cog.caller_config_text(interaction.guild.id)}",
        )


class PingsPanelView(discord.ui.View):
    def __init__(self, cog: "Activities"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Crear Ping",
        emoji="📍",
        style=discord.ButtonStyle.success,
        custom_id="g3n:pings:create_activity",
        row=0,
    )
    async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "crear pings")
            return
        await interaction.response.send_modal(ActivityModal(self.cog, template_id=None))

    @discord.ui.button(
        label="Crear Plantilla",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:create_template",
        row=0,
    )
    async def create_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "crear plantillas")
            return
        view = TemplateVisibilityView(self.cog, author_id=interaction.user.id)
        await private_response(interaction, view.visibility_text(), view=view)

    @discord.ui.button(
        label="Seleccionar Plantilla",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:select_template",
        row=0,
    )
    async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "crear pings")
            return
        if is_admin_subject(self.cog.db, interaction):
            templates = self.cog.db.fetch_all(
                "SELECT * FROM templates WHERE guild_id = ? ORDER BY created_at DESC LIMIT 25",
                (interaction.guild.id,),
            )
        else:
            templates = self.cog.db.fetch_all(
                """
                SELECT *
                FROM templates
                WHERE guild_id = ? AND (created_by = ? OR publica = 1)
                ORDER BY CASE WHEN created_by = ? THEN 0 ELSE 1 END, created_at DESC
                LIMIT 25
                """,
                (interaction.guild.id, interaction.user.id, interaction.user.id),
            )
        if not templates:
            await private_response(interaction, "Aun no hay plantillas disponibles. Crea una con `Crear Plantilla`.")
            return
        await private_response(
            interaction,
            "Elige la plantilla que quieres usar:",
            view=TemplateSelectView(self.cog, templates),
        )

    @discord.ui.button(
        label="Ver mis Plantillas",
        emoji="📚",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:view_templates",
        row=0,
    )
    async def view_templates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "ver plantillas")
            return
        is_admin = is_admin_subject(self.cog.db, interaction)
        if is_admin:
            rows = self.cog.db.fetch_all(
                """
                SELECT t.id, t.name, t.activity_name, t.default_time, t.created_by,
                       t.publica, t.voice_channel_id, COUNT(r.id) AS roles
                FROM templates t
                LEFT JOIN template_roles r ON r.template_id = t.id
                WHERE t.guild_id = ?
                GROUP BY t.id
                ORDER BY t.created_at DESC LIMIT 15
                """,
                (interaction.guild.id,),
            )
        else:
            rows = self.cog.db.fetch_all(
                """
                SELECT t.id, t.name, t.activity_name, t.default_time, t.created_by,
                       t.publica, t.voice_channel_id, COUNT(r.id) AS roles
                FROM templates t
                LEFT JOIN template_roles r ON r.template_id = t.id
                WHERE t.guild_id = ? AND (t.created_by = ? OR t.publica = 1)
                GROUP BY t.id
                ORDER BY CASE WHEN t.created_by = ? THEN 0 ELSE 1 END, t.created_at DESC
                LIMIT 15
                """,
                (interaction.guild.id, interaction.user.id, interaction.user.id),
            )
        if not rows:
            await private_response(interaction, "No hay plantillas disponibles.")
            return
        lines = ["**Todas las plantillas del servidor**" if is_admin else "**Mis plantillas y plantillas publicas**"]
        for row in rows:
            visibility = "Publica" if int(row["publica"]) else "Privada"
            voice_text = f"<#{row['voice_channel_id']}>" if row["voice_channel_id"] else "Sin canal"
            creator_note = ""
            if is_admin or int(row["created_by"]) != interaction.user.id:
                creator_note = f" — <@{row['created_by']}>"
            lines.append(
                f"`{row['id']}` {row['name']} - {row['activity_name']} "
                f"({row['roles']} roles, {row['default_time']}, {voice_text}, {visibility})"
                f"{creator_note}"
            )
        await dm_or_private(self.cog, interaction, "\n".join(lines), "plantillas_panel")

    @discord.ui.button(
        label="Ver mis Actividades",
        emoji="📅",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:my_activities",
        row=1,
    )
    async def my_activities(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        rows = self.cog.db.fetch_all(
            """
            SELECT code, name, horario, status
            FROM activities
            WHERE guild_id = ? AND caller_id = ?
            ORDER BY id DESC LIMIT 10
            """,
            (interaction.guild.id, interaction.user.id),
        )
        if not rows:
            await private_response(interaction, "No tienes actividades creadas.")
            return
        lines = ["**Tus ultimas actividades**"]
        for row in rows:
            lines.append(f"`{row['code']}` {row['name']} - {row['horario']} - {row['status']}")
        await dm_or_private(self.cog, interaction, "\n".join(lines), "mis_actividades_panel")

    @discord.ui.button(
        label="Mi Ranking",
        emoji="🏆",
        style=discord.ButtonStyle.primary,
        custom_id="g3n:pings:my_caller_ranking",
        row=1,
    )
    async def my_ranking(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await dm_or_private(
            self.cog,
            interaction,
            self.cog.my_caller_ranking_text(interaction.guild.id, interaction.user.id),
            "mi_ranking_caller",
        )

    @discord.ui.button(
        label="Mis Penalizaciones",
        emoji="⚠️",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:my_caller_penalties",
        row=1,
    )
    async def my_penalties(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await dm_or_private(
            self.cog,
            interaction,
            self.cog.my_caller_penalties_text(interaction.guild.id, interaction.user.id),
            "mis_penalizaciones_caller",
        )

    @discord.ui.button(
        label="Mi Reporte",
        emoji="📊",
        style=discord.ButtonStyle.success,
        custom_id="g3n:pings:my_caller_report",
        row=1,
    )
    async def my_report(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        caller = self.cog.db.fetch_one(
            "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
            (interaction.guild.id, interaction.user.id),
        )
        if caller is None:
            await private_response(
                interaction,
                "Solo callers autorizados pueden descargar un reporte personal.",
            )
            return
        await interaction.response.defer(ephemeral=True)
        try:
            path = create_caller_report(
                self.cog.db,
                interaction.guild.id,
                interaction.user.id,
                interaction.guild,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        await interaction.followup.send(
            "Tu reporte personal de caller esta listo.",
            file=discord.File(path),
            ephemeral=True,
        )

    @discord.ui.button(
        label="Config",
        emoji="⚙️",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:configuration",
        row=2,
    )
    async def configuration(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_admin_subject(self.cog.db, interaction):
            await private_response(interaction, "Solo admins autorizados pueden usar Config.")
            return
        await private_response(
            interaction,
            self.cog.caller_config_text(interaction.guild.id),
            view=CallerConfigPanelView(self.cog),
        )

class ActivityView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        roles = cog.get_activity_roles(activity_id)
        guild = cog.bot.get_guild(int(activity["guild_id"])) if activity else None
        status = activity["status"] if activity else ACTIVITY_CANCELLED
        if status in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            for index, row in enumerate(roles[:15]):
                current = int(row["participant_count"])
                slots = int(row["slots"])
                counter = f" [{current}/{slots}]"
                role_name = str(row["name"])
                label = f"{role_name[:80 - len(counter)]}{counter}"
                button = discord.ui.Button(
                    label=label,
                    style=discord.ButtonStyle.secondary,
                    custom_id=f"g3n:activity:role:{activity_id}:{row['id']}",
                    row=index // 5,
                    disabled=current >= slots,
                )
                emoji = str(resolve_custom_emojis(row["emoji"], guild) or row["emoji"] or "").strip()
                if emoji and not is_custom_emoji_placeholder(emoji):
                    try:
                        button.emoji = discord.PartialEmoji.from_str(emoji)
                    except ValueError:
                        pass
                button.callback = self.role_button
                self.add_item(button)

            self.add_control_button("Salirme", "leave", discord.ButtonStyle.danger, 3, False, "🚪")
            self.add_control_button("Iniciar", "start", discord.ButtonStyle.success, 3, False, "▶️")
            self.add_control_button(
                "Aviso", "notice", discord.ButtonStyle.primary, 3, status != ACTIVITY_OPEN, "📣"
            )
            self.add_control_button("Mandar check", "check", discord.ButtonStyle.primary, 3, False, "✅")
            self.add_control_button(
                "Editar", "edit", discord.ButtonStyle.secondary, 3, False, "✏️"
            )
            self.add_control_button("Cancelar", "cancel", discord.ButtonStyle.danger, 4, False, "✖️")
        elif status == ACTIVITY_IN_PROGRESS:
            self.add_control_button(
                "Solicitar unirme", "request_join", discord.ButtonStyle.primary, 0, False, "🙋"
            )
            self.add_control_button("Monitorear", "monitor", discord.ButtonStyle.secondary, 0, False, "📡")
            self.add_control_button("Mandar check", "check", discord.ButtonStyle.primary, 0, False, "✅")
            self.add_control_button("Finalizar", "finish", discord.ButtonStyle.success, 0, False, "🏁")
            self.add_control_button("Ver asistencia", "verify", discord.ButtonStyle.secondary, 0, False, "🔍")
            self.add_control_button(
                "Editar", "edit", discord.ButtonStyle.secondary, 1, False, "✏️"
            )
            self.add_control_button("Cancelar", "cancel", discord.ButtonStyle.danger, 1, False, "✖️")
        elif status == ACTIVITY_FINISHED:
            self.add_control_button("Ver asistencia", "verify", discord.ButtonStyle.secondary, 0, False, "🔍")
            self.add_control_button("Splitear", "payout", discord.ButtonStyle.primary, 0, False, "💰")
            self.add_control_button("Liquidación rápida", "quick_liquidation", discord.ButtonStyle.danger, 0, False, "⚡")
        elif status == ACTIVITY_PAYOUT_CREATED:
            self.add_control_button("Ver asistencia", "verify", discord.ButtonStyle.secondary, 0, False, "🔍")
            self.add_control_button("Liquidación rápida", "quick_liquidation", discord.ButtonStyle.danger, 0, False, "⚡")

    def add_control_button(
        self,
        label: str,
        action: str,
        style: discord.ButtonStyle,
        row: int,
        disabled: bool,
        emoji: str | None = None,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            emoji=emoji,
            style=style,
            custom_id=f"g3n:activity:{action}:{self.activity_id}",
            row=row,
            disabled=disabled,
        )
        button.callback = self.control_button
        self.add_item(button)

    async def role_button(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data["custom_id"]
        _, _, _, activity_id, role_id = custom_id.split(":")
        await self.cog.join_role(interaction, int(activity_id), int(role_id))

    async def control_button(self, interaction: discord.Interaction) -> None:
        custom_id = interaction.data["custom_id"]
        _, _, action, activity_id = custom_id.split(":")
        await self.cog.handle_activity_action(interaction, action, int(activity_id))


class ActivityEditMenuView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.activity_id = activity_id

    async def get_activity(
        self,
        interaction: discord.Interaction,
    ):
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return None
        activity = self.cog.get_guild_activity(interaction.guild.id, self.activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return None
        return activity

    @discord.ui.button(
        label="Editar composición",
        emoji="⚔️",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_composition(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        activity = await self.get_activity(interaction)
        if activity is None:
            return
        if not await self.cog.require_activity_manager(interaction, activity, "editar composicion"):
            return
        await self.cog.prompt_edit_composition_modal(interaction, int(activity["id"]))

    @discord.ui.button(
        label="Editar observaciones",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
    )
    async def edit_notes(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        activity = await self.get_activity(interaction)
        if activity is None:
            return
        if not await self.cog.require_activity_notes_editor(interaction, activity):
            return
        await self.cog.prompt_edit_notes_modal(interaction, activity)

class ConfirmAttendanceView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.activity_id = activity_id
        button = discord.ui.Button(
            label="✅ Check",
            style=discord.ButtonStyle.green,
            custom_id=f"g3n:attendance:confirm:{activity_id}",
        )
        button.callback = self.confirm
        self.add_item(button)

    async def confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.confirm_attendance(interaction, self.activity_id)


class JoinRequestReviewView(discord.ui.View):
    def __init__(self, cog: "Activities", request_id: int):
        super().__init__(timeout=None)
        self.cog = cog
        self.request_id = request_id
        for label, action, emoji, style in (
            ("Aceptar", "accept", "✅", discord.ButtonStyle.success),
            ("Rechazar", "reject", "❌", discord.ButtonStyle.danger),
        ):
            button = discord.ui.Button(
                label=label,
                emoji=emoji,
                style=style,
                custom_id=f"g3n:activity:join_request:{action}:{request_id}",
            )
            button.callback = self.handle
            self.add_item(button)

    async def handle(self, interaction: discord.Interaction) -> None:
        action = str(interaction.data["custom_id"]).split(":")[3]
        await self.cog.review_join_request(
            interaction,
            self.request_id,
            accepted=action == "accept",
        )


class PayoutPercentModal(discord.ui.Modal, title="Editar participacion"):
    user = discord.ui.TextInput(label="Usuario (ID o mencion)")
    percent = discord.ui.TextInput(label="Participacion %", placeholder="100")

    def __init__(
        self,
        cog: "Activities",
        guild_id: int,
        payout_code: str,
        source_message=None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.guild_id = guild_id
        self.payout_code = payout_code
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.edit_payout_percent_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            str(self.user.value),
            str(self.percent.value),
            source_message=self.source_message,
        )


class PayoutUserSelect(discord.ui.Select):
    def __init__(
        self,
        cog: "Activities",
        *,
        guild_id: int,
        payout_code: str,
        action: str,
        options: list[discord.SelectOption],
        source_message=None,
    ):
        placeholder = "Selecciona un usuario para añadir" if action == "add" else "Selecciona un usuario para eliminar"
        super().__init__(
            placeholder=placeholder,
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.cog = cog
        self.guild_id = guild_id
        self.payout_code = payout_code
        self.action = action
        self.source_message = source_message

    async def callback(self, interaction: discord.Interaction) -> None:
        user_id = int(self.values[0])
        if self.action == "add":
            await self.cog.add_payout_member_interaction(
                interaction,
                self.guild_id,
                self.payout_code,
                user_id,
                percent=100,
                source_message=self.source_message,
            )
            return
        await self.cog.remove_payout_user_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            user_id,
            source_message=self.source_message,
        )


class PayoutUserSelectView(discord.ui.View):
    def __init__(
        self,
        cog: "Activities",
        *,
        guild_id: int,
        payout_code: str,
        action: str,
        options: list[discord.SelectOption],
        source_message=None,
    ):
        super().__init__(timeout=180)
        self.add_item(
            PayoutUserSelect(
                cog,
                guild_id=guild_id,
                payout_code=payout_code,
                action=action,
                options=options,
                source_message=source_message,
            )
        )


class PayoutEditView(discord.ui.View):
    def __init__(self, cog: "Activities", guild_id: int, payout_code: str):
        super().__init__(timeout=900)
        self.cog = cog
        self.guild_id = guild_id
        self.payout_code = payout_code

    @discord.ui.button(
        label="Ver lista",
        emoji="📋",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:payout:view_list",
        row=0,
    )
    async def view_list(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.send_payout_list_interaction(interaction, self.guild_id, self.payout_code)

    @discord.ui.button(
        label="Editar %",
        emoji="✏️",
        style=discord.ButtonStyle.primary,
        custom_id="g3n:payout:edit_percent",
        row=0,
    )
    async def edit_percent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        payout = self.cog.get_payout_by_code(self.guild_id, self.payout_code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.cog.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.cog.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "Solo el caller del Split o un admin puede editarlo.")
            return
        await interaction.response.send_modal(
            PayoutPercentModal(
                self.cog,
                self.guild_id,
                self.payout_code,
                source_message=interaction.message,
            )
        )

    @discord.ui.button(
        label="Corregir Split",
        emoji="🛠️",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:payout:correct_split",
        row=0,
    )
    async def correct_split(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.prompt_correct_payout_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            source_message=interaction.message,
        )

    @discord.ui.button(
        label="Añadir Usuario",
        emoji="➕",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:payout:add_user",
        row=1,
    )
    async def add_user(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.prompt_add_payout_user_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            source_message=interaction.message,
        )

    @discord.ui.button(
        label="Eliminar Usuario",
        emoji="➖",
        style=discord.ButtonStyle.danger,
        custom_id="g3n:payout:remove_user",
        row=1,
    )
    async def remove_user(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.prompt_remove_payout_user_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
            source_message=interaction.message,
        )

    @discord.ui.button(
        label="Enviar a revision",
        emoji="📤",
        style=discord.ButtonStyle.success,
        custom_id="g3n:payout:send_review",
        row=2,
    )
    async def send_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.send_payout_to_review_interaction(
            interaction,
            self.guild_id,
            self.payout_code,
        )

class Activities(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.db = bot.db
        self._activity_messages_refreshed = False

    async def cog_load(self) -> None:
        self.bot.add_view(PingsPanelView(self))
        active_rows = self.db.fetch_all(
            """
            SELECT id, status
            FROM activities
            WHERE status IN (?, ?, ?, ?) AND message_id IS NOT NULL
            """,
            (ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS, ACTIVITY_FINISHED),
        )
        for row in active_rows:
            self.bot.add_view(ActivityView(self, int(row["id"])))
            if row["status"] == ACTIVITY_IN_PROGRESS:
                self.bot.add_view(ConfirmAttendanceView(self, int(row["id"])))
        pending_requests = self.db.fetch_all(
            "SELECT id FROM activity_join_requests WHERE status = 'Pendiente'"
        )
        for row in pending_requests:
            self.bot.add_view(JoinRequestReviewView(self, int(row["id"])))

    @commands.Cog.listener()
    async def on_ready(self) -> None:
        if self._activity_messages_refreshed:
            return
        self._activity_messages_refreshed = True
        active_rows = self.db.fetch_all(
            """
            SELECT id
            FROM activities
            WHERE status IN (?, ?, ?, ?) AND message_id IS NOT NULL
            """,
            (ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS, ACTIVITY_FINISHED),
        )
        for row in active_rows:
            await self.update_activity_message(int(row["id"]))
        for guild in self.bot.guilds:
            self.recover_voice_tracking(guild)
            await evaluate_caller_penalties(self.db, guild)

    def recover_voice_tracking(self, guild: discord.Guild) -> None:
        recovered_at = utc_now_iso()
        recovered_time = parse_iso_datetime(recovered_at)
        orphaned = self.db.fetch_all(
            """
            SELECT id, joined_at FROM activity_voice_sessions
            WHERE guild_id = ? AND left_at IS NULL
            """,
            (guild.id,),
        )
        for row in orphaned:
            joined_at = parse_iso_datetime(str(row["joined_at"]))
            seconds = (
                max(0, int((recovered_time - joined_at).total_seconds()))
                if recovered_time and joined_at
                else 0
            )
            self.db.execute(
                "UPDATE activity_voice_sessions SET left_at = ?, seconds = ? WHERE id = ?",
                (recovered_at, seconds, int(row["id"])),
            )
        activities = self.db.fetch_all(
            """
            SELECT id, caller_id, voice_channel_id FROM activities
            WHERE guild_id = ? AND status = ? AND voice_channel_id IS NOT NULL
            """,
            (guild.id, ACTIVITY_IN_PROGRESS),
        )
        for activity in activities:
            attendees = self.db.fetch_all(
                """
                SELECT usuario_id FROM asistencia_actividades
                WHERE actividad_id = ? AND confirmo_boton = 1
                """,
                (int(activity["id"]),),
            )
            for attendee in attendees:
                member = guild.get_member(int(attendee["usuario_id"]))
                if (
                    member is not None
                    and member.voice is not None
                    and member.voice.channel is not None
                    and member.voice.channel.id == int(activity["voice_channel_id"])
                ):
                    self.start_voice_session(int(activity["id"]), guild.id, member.id)

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        if member.bot:
            return
        before_id = before.channel.id if before.channel is not None else None
        after_id = after.channel.id if after.channel is not None else None
        if before_id == after_id:
            return
        activities = self.db.fetch_all(
            """
            SELECT id, caller_id, voice_channel_id FROM activities
            WHERE guild_id = ? AND status = ?
              AND voice_channel_id IN (?, ?)
            """,
            (member.guild.id, ACTIVITY_IN_PROGRESS, before_id or 0, after_id or 0),
        )
        for activity in activities:
            activity_id = int(activity["id"])
            registered = self.db.fetch_one(
                "SELECT 1 FROM activity_participants WHERE activity_id = ? AND user_id = ?",
                (activity_id, member.id),
            )
            if registered is None and member.id != int(activity["caller_id"]):
                continue
            channel_id = int(activity["voice_channel_id"])
            if before_id == channel_id and after_id != channel_id:
                self.close_voice_session(activity_id, member.guild.id, member.id)
            elif after_id == channel_id and before_id != channel_id:
                self.start_voice_session(activity_id, member.guild.id, member.id)

    def caller_config_text(self, guild_id: int) -> str:
        def channel_value(key: str) -> str:
            value = self.db.get_setting(guild_id, key)
            return f"<#{value}>" if value and value.isdigit() else "sin configurar"

        absence_enabled = self.db.get_setting(guild_id, "absence_fine_enabled", "1")
        absence_state = "Activas" if str(absence_enabled).strip().lower() in {"1", "true", "si", "sí", "yes", "on"} else "Inactivas"
        return "\n".join(
            [
                "**⚙️ Config Panel de Callers**",
                f"Canal de pings: {channel_value('channel_pings_id')}",
                f"Avisos de actividad: {channel_value('channel_notify_activities_id')}",
                f"Multas por ausencia: **{absence_state}**",
                f"Monto multa ausencia: `{self.db.get_setting(guild_id, 'absence_fine_amount', '0')}`",
                f"Permanencia minima en voz: `{self.db.get_setting(guild_id, 'voice_minimum_percent', '50')}%`",
                f"Pago caller predeterminado: `{self.db.get_setting(guild_id, 'caller_percentage_default', '0')}%`",
            ]
        )

    @commands.command(name="panel_pings")
    async def panel_pings(self, ctx: commands.Context) -> None:
        if not await require_caller_context(ctx, self.db):
            return
        embed = discord.Embed(
            title="Panel de Callers",
            description=(
                "Crea pings, reutiliza plantillas y organiza composiciones "
                "sin saturar el canal."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_image(url=PINGS_PANEL_IMAGE)
        message = await ctx.send(embed=embed, view=PingsPanelView(self))
        self.db.execute(
            """
            INSERT INTO panel_messages (
                guild_id, panel_type, channel_id, message_id, created_by, created_at
            )
            VALUES (?, 'pings', ?, ?, ?, ?)
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

    def my_caller_ranking_text(self, guild_id: int, user_id: int) -> str:
        ranking = caller_ranking(self.db, guild_id)
        for position, row in enumerate(ranking, start=1):
            if int(row["user_id"]) != user_id:
                continue
            status = "⛔ Penalizado" if int(row["penalized"]) else "🟢 Activo"
            return "\n".join(
                [
                    "🏆 **Mi ranking como caller**",
                    f"Posicion: **#{position} de {len(ranking)}**",
                    f"Estado: **{status}**",
                    f"Puntos: **{row['score']}**",
                    f"Plata repartida: **{format_amount(row['distributed'])}**",
                    f"Actividades creadas: **{row['activities_created']}**",
                    f"Actividades completadas: **{row['activities_completed']}**",
                    f"Cancelaciones con consecuencia: **{row['activities_cancelled']}**",
                    f"Cancelaciones justificadas por cupos: **{row['cancellations_exempt']}**",
                    f"Asistencias: **{row['attendances']}**",
                    f"Ausencias: **{row['absences']}**",
                    "",
                    "Puntuacion: +10 completada, +2 asistencia, -4 cancelacion no justificada y -6 ausencia.",
                    "Al llegar a -14 puntos, el acceso de caller queda suspendido.",
                ]
            )
        return "No tienes un perfil de caller autorizado en este servidor."

    def my_caller_penalties_text(self, guild_id: int, user_id: int) -> str:
        rows = self.db.fetch_all(
            """
            SELECT score_at_penalty, reason, active, penalized_at,
                   notified_at, removed_by, removed_at, rearmed
            FROM caller_penalties
            WHERE guild_id = ? AND user_id = ?
            ORDER BY id DESC LIMIT 8
            """,
            (guild_id, user_id),
        )
        if not rows:
            return "🟢 **Mis penalizaciones de caller**\nNo tienes penalizaciones registradas."
        lines = ["⚠️ **Mis penalizaciones de caller**"]
        for index, row in enumerate(rows, start=1):
            status = "ACTIVA" if int(row["active"]) else "RETIRADA"
            lines.extend(
                [
                    "",
                    f"**{index}. {status}**",
                    f"Puntaje al penalizar: **{row['score_at_penalty']}**",
                    f"Motivo: {row['reason']}",
                    f"Fecha: `{row['penalized_at']}`",
                    f"Aviso DM: `{'enviado' if row['notified_at'] else 'no entregado'}`",
                ]
            )
            if not int(row["active"]):
                lines.append(f"Retirada: `{row['removed_at'] or 'sin fecha'}`")
                if row["removed_by"]:
                    lines.append(f"Retirada por: <@{row['removed_by']}>")
        return "\n".join(lines)[:1900]

    @commands.command(name="penalizacion_remove")
    async def penalizacion_remove(self, ctx: commands.Context, member: discord.Member, *, reason: str = "") -> None:
        if not await require_admin_context(ctx, self.db):
            return
        self.db.execute(
            """
            UPDATE penalizacion_actividades
            SET activo = 0, removido_por = ?, fecha_remocion = ?, observaciones = ?
            WHERE guild_id = ? AND usuario_id = ? AND activo = 1
            """,
            (ctx.author.id, utc_now_iso(), reason, ctx.guild.id, member.id),
        )
        log_action(
            self.db,
            ctx.guild.id,
            admin_id=ctx.author.id,
            action="Quitar penalizacion de actividad",
            system="Actividades",
            affected_user_id=member.id,
            observation=reason,
        )
        await ctx.reply(f"{member.mention} fue retirado de penalizacion.", mention_author=False)

    @commands.command(name="penalizaciones")
    async def penalizaciones(self, ctx: commands.Context) -> None:
        if not await require_admin_context(ctx, self.db):
            return
        rows = self.db.fetch_all(
            """
            SELECT usuario_id, motivo, fecha_ingreso
            FROM penalizacion_actividades
            WHERE guild_id = ? AND activo = 1
            ORDER BY id DESC LIMIT 20
            """,
            (ctx.guild.id,),
        )
        if not rows:
            await ctx.reply("No hay usuarios penalizados.", mention_author=False)
            return
        lines = ["**Penalizaciones activas**"]
        for row in rows:
            lines.append(f"<@{row['usuario_id']}> - {row['motivo']} - {row['fecha_ingreso']}")
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="reparto_participantes", aliases=["split_participantes"])
    async def reparto_participantes(self, ctx: commands.Context, code: str) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese Split.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del Split o un admin puede verlo.", mention_author=False)
            return
        rows = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (int(payout["id"]),),
        )
        if not rows:
            await ctx.reply("Ese Split no tiene participantes.", mention_author=False)
            return
        lines = [f"**Participantes de {code}**"]
        for row in rows:
            amount = f"{int(row['amount']):,}".replace(",", ".")
            lines.append(f"<@{row['user_id']}> - {row['participation_percent']}% - {amount}")
        await ctx.reply("\n".join(lines), mention_author=False)

    @commands.command(name="reparto_participacion", aliases=["split_participacion"])
    async def reparto_participacion(
        self,
        ctx: commands.Context,
        code: str,
        member: discord.Member,
        percent_raw: str,
    ) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese Split.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del Split o un admin puede modificarlo.", mention_author=False)
            return
        if not self.is_editable_payout(payout):
            await ctx.reply("Solo se pueden modificar Splits preliminares.", mention_author=False)
            return
        try:
            percent = parse_percent(percent_raw)
            self.set_payout_participation(int(payout["id"]), member.id, percent)
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, ctx.author.id)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        log_payout_action(
            self.db,
            ctx.guild.id,
            int(payout["id"]),
            actor_id=ctx.author.id,
            action="Porcentaje actualizado",
            details=f"Usuario {member.id}: {percent}%",
        )
        await ctx.reply(f"Participacion de {member.mention} actualizada a {percent}%.", mention_author=False)

    @commands.command(name="reparto_agregar", aliases=["split_agregar"])
    async def reparto_agregar(
        self,
        ctx: commands.Context,
        code: str,
        member: discord.Member,
        percent_raw: str = "100",
    ) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese Split.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del Split o un admin puede modificarlo.", mention_author=False)
            return
        if not self.is_editable_payout(payout):
            await ctx.reply("Solo se pueden modificar Splits preliminares.", mention_author=False)
            return
        try:
            percent = parse_percent(percent_raw)
            if percent <= 0:
                raise ValueError("El porcentaje/peso debe ser mayor que cero.")
            exists = self.db.fetch_one(
                "SELECT 1 FROM payout_participants WHERE payout_id = ? AND user_id = ?",
                (int(payout["id"]), member.id),
            )
            if exists:
                raise ValueError("Ese usuario ya esta en el Split.")
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount
                )
                VALUES (?, ?, ?, 0)
                """,
                (int(payout["id"]), member.id, percent),
            )
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, ctx.author.id)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        log_payout_action(
            self.db,
            ctx.guild.id,
            int(payout["id"]),
            actor_id=ctx.author.id,
            action="Usuario añadido manualmente",
            details=f"Usuario {member.id}: {percent}%",
        )
        await ctx.reply(f"{member.mention} agregado al Split con {percent}%.", mention_author=False)

    @commands.command(name="reparto_quitar", aliases=["split_quitar"])
    async def reparto_quitar(self, ctx: commands.Context, code: str, member: discord.Member) -> None:
        payout = self.get_payout_by_code(ctx.guild.id, code)
        if payout is None:
            await ctx.reply("No encontre ese Split.", mention_author=False)
            return
        if not self.can_manage_payout(ctx, payout):
            await ctx.reply("Solo el caller del Split o un admin puede modificarlo.", mention_author=False)
            return
        if not self.is_editable_payout(payout):
            await ctx.reply("Solo se pueden modificar Splits preliminares.", mention_author=False)
            return
        self.db.execute(
            "DELETE FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (int(payout["id"]), member.id),
        )
        try:
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, ctx.author.id)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        log_payout_action(
            self.db,
            ctx.guild.id,
            int(payout["id"]),
            actor_id=ctx.author.id,
            action="Usuario eliminado",
            details=f"Usuario {member.id}",
        )
        await ctx.reply(f"{member.mention} fue retirado del Split.", mention_author=False)

    def get_activity(self, activity_id: int):
        return self.db.fetch_one("SELECT * FROM activities WHERE id = ?", (activity_id,))

    def get_guild_activity(self, guild_id: int, activity_id: int):
        return self.db.fetch_one(
            "SELECT * FROM activities WHERE guild_id = ? AND id = ?",
            (guild_id, activity_id),
        )

    async def require_activity_manager(
        self,
        interaction: discord.Interaction,
        activity,
        action: str,
    ) -> bool:
        if interaction.guild is None or int(activity["guild_id"]) != interaction.guild.id:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return False
        if can_manage_activity(self.db, interaction, int(activity["caller_id"])):
            return True

        is_foreign_activity = interaction.user.id != int(activity["caller_id"])
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Intento bloqueado de administrar actividad",
            system="Actividades",
            affected_user_id=int(activity["caller_id"]),
            observation=(
                f"{activity['code']} · Accion: {action} · "
                f"Motivo: {'caller no creador' if is_foreign_activity else 'sin permiso de caller'}"
            ),
        )
        if is_foreign_activity:
            await private_response(interaction, ACTIVITY_MANAGEMENT_DENIED_MESSAGE)
            return False
        await reject_caller_access(self.db, interaction, f"{action} actividades")
        return False

    async def require_activity_notes_editor(
        self,
        interaction: discord.Interaction,
        activity,
    ) -> bool:
        if interaction.guild is None or int(activity["guild_id"]) != interaction.guild.id:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return False
        if is_admin_subject(self.db, interaction):
            return True
        if interaction.user.id == int(activity["caller_id"]):
            return True
        await private_response(
            interaction,
            "Solo el caller creador o un admin puede editar las observaciones.",
        )
        return False

    def audit_admin_activity_action(
        self,
        interaction: discord.Interaction,
        activity,
        action: str,
    ) -> None:
        if interaction.user.id == int(activity["caller_id"]):
            return
        if not is_admin_subject(self.db, interaction):
            return
        log_action(
            self.db,
            int(activity["guild_id"]),
            admin_id=interaction.user.id,
            action=f"Administrar actividad como admin: {action}",
            system="Actividades",
            affected_user_id=int(activity["caller_id"]),
            observation=str(activity["code"]),
        )

    def get_activity_roles(self, activity_id: int):
        return self.db.fetch_all(
            """
            SELECT r.*, COUNT(p.id) AS participant_count
            FROM activity_roles r
            LEFT JOIN activity_participants p ON p.role_id = r.id
            WHERE r.activity_id = ?
            GROUP BY r.id
            ORDER BY r.position ASC
            """,
            (activity_id,),
        )

    def get_activity_participants(self, activity_id: int):
        return self.db.fetch_all(
            """
            SELECT p.*, r.name AS role_name, r.emoji AS role_emoji
            FROM activity_participants p
            JOIN activity_roles r ON r.id = p.role_id
            WHERE p.activity_id = ?
            ORDER BY r.position ASC, p.joined_at ASC
            """,
            (activity_id,),
        )

    def attendance_summary_text(self, activity_id: int) -> str:
        activity = self.get_activity(activity_id)
        participants = self.get_activity_participants(activity_id)
        attendance = {
            int(row["usuario_id"]): row
            for row in self.db.fetch_all(
                "SELECT * FROM asistencia_actividades WHERE actividad_id = ?",
                (activity_id,),
            )
        }
        checked: list[str] = []
        without_check: list[str] = []
        pending: list[str] = []
        absent: list[str] = []
        for participant in participants:
            user_id = int(participant["user_id"])
            mention = f"<@{user_id}> — {participant['role_name']}"
            row = attendance.get(user_id)
            if row is None:
                pending.append(mention)
            elif row["estado"] == ATTENDANCE_ABSENT:
                absent.append(mention)
            elif int(row["confirmo_boton"]) == 1:
                checked.append(mention)
            else:
                without_check.append(mention)

        def block(title: str, values: list[str]) -> list[str]:
            return [f"**{title} ({len(values)})**", *(values or ["Ninguno"]), ""]

        lines = [
            f"✅ **Resumen al iniciar — {activity['name']}**",
            "Tu check de caller se registró automáticamente al iniciar.",
            "",
            *block("Participantes con check", checked),
            *block("Participantes sin check", without_check),
            *block("Participantes pendientes", pending),
            *block("Participantes ausentes", absent),
        ]
        return "\n".join(lines)[:1900]

    async def create_template_from_modal(
        self,
        interaction: discord.Interaction,
        modal: TemplateModal,
    ) -> None:
        if not interaction.guild or not is_caller_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para crear plantillas.")
            return
        try:
            roles = parse_role_lines(str(modal.roles.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        roles = resolve_role_custom_emojis(roles, interaction.guild)
        template_name = resolve_template_text(str(modal.template_name.value), interaction.guild)
        activity_name = resolve_template_text(str(modal.activity_name.value), interaction.guild)
        default_time = resolve_template_text(str(modal.default_time.value), interaction.guild)
        description = resolve_template_text(str(modal.description.value), interaction.guild)
        if not description:
            await private_response(interaction, "La descripcion de la plantilla es obligatoria.")
            return
        voice_channel = interaction.guild.get_channel(int(modal.voice_channel_id))
        if not isinstance(voice_channel, (discord.VoiceChannel, discord.StageChannel)):
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        template_id = self.db.execute(
            """
            INSERT INTO templates (
                guild_id, name, activity_name, default_time,
                voice_channel_id, description, publica, created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                template_name,
                activity_name,
                default_time,
                voice_channel.id,
                description,
                1 if modal.publica else 0,
                interaction.user.id,
                utc_now_iso(),
            ),
        )
        for role in roles:
            self.db.execute(
                """
                INSERT INTO template_roles (template_id, key, name, slots, emoji, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    template_id,
                    role["key"],
                    role["name"],
                    role["slots"],
                    role["emoji"],
                    role["position"],
                ),
            )
        preview = "\n".join(
            f"{role['emoji']} **{role['name']}** [0/{role['slots']}]".strip()
            for role in roles
        )
        visibility = "Publica" if modal.publica else "Privada"
        await private_response(
            interaction,
            f"Plantilla guardada como **{visibility}** con {len(roles)} roles.\n\n"
            f"**Descripcion:** {description}\n\n{preview}",
        )

    async def publish_activity_from_modal(
        self,
        interaction: discord.Interaction,
        modal: ActivityModal,
    ) -> None:
        if not interaction.guild or not is_caller_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para crear pings.")
            return
        channel_id_raw = self.db.get_setting(interaction.guild.id, "channel_pings_id")
        if not channel_id_raw:
            await private_response(interaction, "No hay canal configurado para publicaciones de pings.")
            return
        channel = interaction.guild.get_channel(int(channel_id_raw))
        if channel is None:
            await private_response(interaction, "El canal de pings configurado ya no existe.")
            return

        try:
            if modal.template_id is None:
                roles = parse_role_lines(str(modal.roles.value))
            else:
                template_roles = self.db.fetch_all(
                    "SELECT * FROM template_roles WHERE template_id = ? ORDER BY position ASC",
                    (modal.template_id,),
                )
                roles = [
                    {
                        "key": row["key"],
                        "name": row["name"],
                        "slots": int(row["slots"]),
                        "emoji": row["emoji"] or "",
                        "position": int(row["position"]),
                    }
                    for row in template_roles
                ]
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        roles = resolve_role_custom_emojis(roles, interaction.guild)
        activity_name = resolve_template_text(str(modal.activity_name.value), interaction.guild)
        horario = resolve_template_text(str(modal.horario.value), interaction.guild)
        notes = resolve_template_text(str(modal.notes.value), interaction.guild)

        voice_channel = resolve_voice_channel(interaction.guild, str(modal.voice_channel.value))
        if voice_channel is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        voice_channel_id = voice_channel.id
        code = self.db.next_code(interaction.guild.id, "ACT")
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, template_id, name, caller_id, horario,
                voice_channel_id, notes, status, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                interaction.guild.id,
                modal.template_id,
                activity_name,
                interaction.user.id,
                horario,
                voice_channel_id,
                notes,
                ACTIVITY_OPEN,
                utc_now_iso(),
            ),
        )
        for role in roles:
            self.db.execute(
                """
                INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    activity_id,
                    role["key"],
                    role["name"],
                    role["slots"],
                    role["emoji"],
                    role["position"],
                ),
            )

        embeds = self.build_activity_embeds(activity_id)
        view = ActivityView(self, activity_id)
        message = await channel.send(embeds=embeds, view=view)
        self.db.execute(
            "UPDATE activities SET channel_id = ?, message_id = ? WHERE id = ?",
            (channel.id, message.id, activity_id),
        )
        self.bot.add_view(ActivityView(self, activity_id))
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Crear Ping",
            system="Actividades",
            observation=code,
        )
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"📍 Ping `{code}` creado por <@{interaction.user.id}>: "
                f"**{activity_name}** en <#{channel.id}>."
            ),
        )
        await private_response(interaction, f"Ping creado: `{code}`.")

    def build_activity_embeds(self, activity_id: int) -> list[discord.Embed]:
        activity = self.get_activity(activity_id)
        roles = self.get_activity_roles(activity_id)
        participants = self.get_activity_participants(activity_id)
        by_role: dict[int, list[str]] = defaultdict(list)
        for participant in participants:
            display_name = " ".join(str(participant["display_name"]).split())
            by_role[int(participant["role_id"])].append(display_name)

        voice_text = "Sin canal"
        if activity["voice_channel_id"]:
            voice_text = f"<#{activity['voice_channel_id']}>"

        registered_count = len(participants)
        required_count = sum(max(0, int(role["slots"])) for role in roles)
        activity_name = " ".join(str(activity["name"]).split()).upper()
        notes = " ".join(str(activity["notes"] or "").split())
        status = activity_status_label(str(activity["status"]))
        status_icon, _, status_name = status.partition(" ")

        lines = [
            ACTIVITY_SEPARATOR,
            "",
            f"**⚔️ {activity_name}**",
            "",
        ]
        if notes:
            lines.extend([f"📝 **Nota:** {notes}", ""])
        lines.extend(
            [
                activity_meta_row(
                    f"👤 **Caller:** <@{activity['caller_id']}>",
                    f"🆔 **ID:** {activity['code']}",
                ),
                activity_meta_row(
                    f"🕒 **Hora:** {activity['horario']}",
                    f"🔊 **Voz:** {voice_text}",
                ),
                activity_meta_row(
                    f"👥 **Participantes:** {registered_count}/{required_count}",
                    f"{status_icon} **Estado:** {status_name or status}",
                ),
                "",
                ACTIVITY_SEPARATOR,
                "",
                "**⚔️ COMPOSICIÓN**",
            ]
        )

        composition_fields: list[tuple[str, str, bool]] = []
        if not roles:
            composition_fields.append(("Sin roles configurados", "• Vacío", False))
        for role in roles:
            role_id = int(role["id"])
            names = by_role.get(role_id, [])
            current = len(names)
            required = max(0, int(role["slots"]))
            role_emoji = str(role["emoji"] or "").strip()
            role_name = " ".join(str(role["name"]).split())
            marker = activity_composition_marker(current, required)
            role_prefix = f"{marker} {role_emoji}".strip()
            field_name = f"{role_prefix} **{role_name}** [{current}/{required}]"
            field_value = activity_composition_field_value(names)
            composition_fields.append((field_name, field_value, True))

        reminders = "\n".join(
            [
                ACTIVITY_SEPARATOR,
                "✅ No olvides realizar tu check cuando el caller lo solicite.",
                "🎤 Permanece en el canal de voz durante toda la actividad.",
                "⚠️ Respeta las indicaciones del caller durante toda la actividad.",
            ]
        )
        description = "\n".join(lines)
        if len(description) > 4096:
            description = description[:4000].rstrip() + "\n\nLista recortada por límite de Discord."

        composition_chunks = [
            composition_fields[index : index + ACTIVITY_COMPOSITION_FIELDS_PER_EMBED]
            for index in range(0, len(composition_fields), ACTIVITY_COMPOSITION_FIELDS_PER_EMBED)
        ]
        embeds: list[discord.Embed] = []
        total_chunks = len(composition_chunks)
        for chunk_index, chunk in enumerate(composition_chunks):
            if chunk_index == 0:
                embed = discord.Embed(
                    title="🦅 G3NESYS • PING DE ACTIVIDAD",
                    description=description,
                    color=ACTIVITY_EMBED_COLOR,
                )
            else:
                embed = discord.Embed(
                    title=f"⚔️ COMPOSICIÓN ({chunk_index + 1}/{total_chunks})",
                    color=ACTIVITY_EMBED_COLOR,
                )
            for name, value, inline in chunk:
                embed.add_field(name=name, value=value, inline=inline)
            embeds.append(embed)

        embeds[-1].add_field(name="​", value=reminders, inline=False)
        guild = self.bot.get_guild(int(activity["guild_id"]))
        return [resolve_custom_emojis_in_embed(embed, guild) or embed for embed in embeds]

    def build_activity_embed(self, activity_id: int) -> discord.Embed:
        return self.build_activity_embeds(activity_id)[0]

    async def update_activity_message(self, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity or not activity["channel_id"] or not activity["message_id"]:
            return
        guild = self.bot.get_guild(int(activity["guild_id"]))
        if guild is None:
            return
        channel = guild.get_channel(int(activity["channel_id"]))
        if channel is None:
            return
        try:
            message = await channel.fetch_message(int(activity["message_id"]))
            await message.edit(
                embeds=self.build_activity_embeds(activity_id),
                view=ActivityView(self, activity_id),
            )
        except discord.HTTPException:
            return

    async def prompt_activity_edit_menu(
        self,
        interaction: discord.Interaction,
        activity_id: int,
    ) -> None:
        await private_response(
            interaction,
            "Selecciona qué quieres editar en este ping.",
            view=ActivityEditMenuView(self, activity_id),
        )

    async def prompt_edit_composition_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
    ) -> None:
        current_roles = "\n".join(
            f"{row['emoji'] or ''} | {row['name']} | {row['slots']}"
            for row in self.get_activity_roles(activity_id)
        )
        await interaction.response.send_modal(
            EditCompositionModal(self, activity_id, current_roles)
        )

    async def prompt_edit_notes_modal(
        self,
        interaction: discord.Interaction,
        activity,
    ) -> None:
        await interaction.response.send_modal(
            EditActivityNotesModal(
                self,
                int(activity["id"]),
                str(activity["notes"] or ""),
            ),
        )

    async def edit_activity_notes_from_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        raw_notes: str,
    ) -> None:
        activity = self.get_activity(activity_id)
        if (
            activity is None
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
        ):
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not await self.require_activity_notes_editor(interaction, activity):
            return

        notes = resolve_template_text(raw_notes, interaction.guild)
        self.db.execute(
            "UPDATE activities SET notes = ? WHERE id = ?",
            (notes, activity_id),
        )
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Modificar observaciones de actividad",
            system="Actividades",
            observation=str(activity["code"]),
        )
        await self.update_activity_message(activity_id)
        await private_response(interaction, "Observaciones actualizadas y ping refrescado.")

    async def edit_composition_from_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        raw_roles: str,
    ) -> None:
        activity = self.get_activity(activity_id)
        if (
            activity is None
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
        ):
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            await private_response(interaction, "La composicion ya no se puede modificar en este estado.")
            return
        if not can_manage_activity(self.db, interaction, int(activity["caller_id"])):
            await private_response(interaction, "Solo el caller creador o un admin puede modificar la composicion.")
            return
        try:
            requested_roles = parse_role_lines(raw_roles)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        requested_roles = resolve_role_custom_emojis(requested_roles, interaction.guild)

        existing_roles = {str(row["key"]): row for row in self.get_activity_roles(activity_id)}
        requested_by_key = {str(row["key"]): row for row in requested_roles}
        for key, current in existing_roles.items():
            participant_count = int(current["participant_count"])
            if key not in requested_by_key and participant_count:
                await private_response(
                    interaction,
                    f"No puedes eliminar **{current['name']}** porque tiene {participant_count} participante(s).",
                )
                return
            if key in requested_by_key and int(requested_by_key[key]["slots"]) < participant_count:
                await private_response(
                    interaction,
                    f"**{current['name']}** ya tiene {participant_count} participante(s); no puedes dejar menos cupos.",
                )
                return

        with self.db.transaction() as cursor:
            for role in requested_roles:
                current = existing_roles.get(str(role["key"]))
                if current is None:
                    cursor.execute(
                        """
                        INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (
                            activity_id,
                            role["key"],
                            role["name"],
                            role["slots"],
                            role["emoji"],
                            role["position"],
                        ),
                    )
                else:
                    cursor.execute(
                        """
                        UPDATE activity_roles
                        SET name = ?, slots = ?, emoji = ?, position = ?
                        WHERE id = ?
                        """,
                        (
                            role["name"],
                            role["slots"],
                            role["emoji"],
                            role["position"],
                            int(current["id"]),
                        ),
                    )
            for key, current in existing_roles.items():
                if key not in requested_by_key:
                    cursor.execute("DELETE FROM activity_roles WHERE id = ?", (int(current["id"]),))

        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Modificar composicion de actividad",
            system="Actividades",
            observation=str(activity["code"]),
        )
        await self.update_activity_message(activity_id)
        await private_response(interaction, "Composicion actualizada y ping refrescado.")

    async def create_join_request(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        requested_role: str,
    ) -> None:
        activity = self.get_activity(activity_id)
        if (
            activity is None
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
            or activity["status"] != ACTIVITY_IN_PROGRESS
        ):
            await private_response(interaction, "Esta actividad ya no acepta solicitudes.")
            return
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(
            self.db, interaction.user
        ):
            await private_response(
                interaction,
                "Necesitas el rol configurado de miembro o invitado para solicitar unirte.",
            )
            return
        penalty = self.ensure_penalty_for_user(interaction.guild.id, interaction.user.id)
        if penalty:
            await private_response(
                interaction,
                f"No puedes solicitar unirte porque estas penalizado. Motivo: {penalty}",
            )
            return
        already_joined = self.db.fetch_one(
            "SELECT 1 FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        if already_joined is not None:
            await private_response(interaction, "Ya estas registrado en esta actividad.")
            return
        pending = self.db.fetch_one(
            """
            SELECT 1 FROM activity_join_requests
            WHERE guild_id = ? AND activity_id = ? AND user_id = ? AND status = 'Pendiente'
            """,
            (interaction.guild.id, activity_id, interaction.user.id),
        )
        if pending is not None:
            await private_response(interaction, "Ya tienes una solicitud pendiente con el caller.")
            return
        role_name = requested_role.strip()
        if not role_name:
            await private_response(interaction, "Indica el rol o arma con el que quieres entrar.")
            return
        request_id = self.db.execute(
            """
            INSERT INTO activity_join_requests (
                guild_id, activity_id, user_id, display_name, requested_role,
                status, requested_at
            )
            VALUES (?, ?, ?, ?, ?, 'Pendiente', ?)
            """,
            (
                interaction.guild.id,
                activity_id,
                interaction.user.id,
                interaction.user.display_name,
                role_name[:80],
                utc_now_iso(),
            ),
        )
        view = JoinRequestReviewView(self, request_id)
        self.bot.add_view(view)
        caller = interaction.guild.get_member(int(activity["caller_id"]))
        if caller is not None:
            await send_dm_safe(
                self.db,
                guild_id=interaction.guild.id,
                user=caller,
                action="solicitud_unirse_actividad",
                content=(
                    f"🙋 **Solicitud para unirse a {activity['name']}**\n"
                    f"Usuario: {interaction.user.mention}\n"
                    f"Rol solicitado: **{role_name}**"
                ),
                view=view,
            )
        await private_response(interaction, "Solicitud enviada al caller. Recibiras la respuesta por DM.")

    async def review_join_request(
        self,
        interaction: discord.Interaction,
        request_id: int,
        *,
        accepted: bool,
    ) -> None:
        request = self.db.fetch_one(
            """
            SELECT jr.*, ac.caller_id, ac.code AS activity_code,
                   ac.status AS activity_status, ac.name AS activity_name,
                   ac.voice_channel_id
            FROM activity_join_requests jr
            JOIN activities ac ON ac.id = jr.activity_id
            WHERE jr.id = ?
            """,
            (request_id,),
        )
        if request is None or request["status"] != "Pendiente":
            await private_response(interaction, "Esta solicitud ya fue procesada.")
            return
        guild = interaction.guild or self.bot.get_guild(int(request["guild_id"]))
        if guild is None or int(request["guild_id"]) != guild.id:
            await private_response(interaction, "Esta solicitud pertenece a otro servidor.")
            return
        if (
            interaction.user.id != int(request["caller_id"])
            and not (interaction.guild is not None and is_admin_subject(self.db, interaction))
        ):
            await private_response(interaction, "Solo el caller de la actividad o un admin puede responder.")
            return
        if accepted and request["activity_status"] != ACTIVITY_IN_PROGRESS:
            await private_response(interaction, "La actividad ya no esta en curso.")
            return

        now = utc_now_iso()
        if accepted:
            role_key = normalize_key(str(request["requested_role"]))
            role = self.db.fetch_one(
                "SELECT * FROM activity_roles WHERE activity_id = ? AND key = ?",
                (int(request["activity_id"]), role_key),
            )
            if role is None:
                position_row = self.db.fetch_one(
                    "SELECT COALESCE(MAX(position), 0) AS position FROM activity_roles WHERE activity_id = ?",
                    (int(request["activity_id"]),),
                )
                role_id = self.db.execute(
                    """
                    INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                    VALUES (?, ?, ?, 1, '', ?)
                    """,
                    (
                        int(request["activity_id"]),
                        role_key,
                        str(request["requested_role"])[:80],
                        int(position_row["position"]) + 1,
                    ),
                )
            else:
                role_id = int(role["id"])
                count = self.db.fetch_one(
                    "SELECT COUNT(*) AS total FROM activity_participants WHERE role_id = ?",
                    (role_id,),
                )
                if int(count["total"]) >= int(role["slots"]):
                    self.db.execute(
                        "UPDATE activity_roles SET slots = slots + 1 WHERE id = ?",
                        (role_id,),
                    )
            self.db.execute(
                """
                INSERT OR IGNORE INTO activity_participants (
                    activity_id, role_id, user_id, display_name, joined_at
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (
                    int(request["activity_id"]),
                    role_id,
                    int(request["user_id"]),
                    str(request["display_name"]),
                    now,
                ),
            )
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
                ) VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(actividad_id, usuario_id) DO NOTHING
                """,
                (int(request["activity_id"]), int(request["user_id"]), ATTENDANCE_PENDING),
            )
        self.db.execute(
            """
            UPDATE activity_join_requests
            SET status = ?, reviewed_by = ?, reviewed_at = ?
            WHERE id = ?
            """,
            ("Aceptada" if accepted else "Rechazada", interaction.user.id, now, request_id),
        )
        log_action(
            self.db,
            guild.id,
            admin_id=interaction.user.id,
            action="Aceptar solicitud tardia" if accepted else "Rechazar solicitud tardia",
            system="Actividades",
            affected_user_id=int(request["user_id"]),
            observation=f"{request['activity_code']} — {request['requested_role']}",
        )
        member = guild.get_member(int(request["user_id"]))
        if member is not None:
            if accepted:
                await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
                    user=member,
                    action="solicitud_actividad_aceptada",
                    content=(
                        f"✅ Tu solicitud para **{request['activity_name']}** fue aceptada.\n"
                        "Entra al canal de voz y pulsa **Aqui estoy**. Si permaneces menos del 50% "
                        "de la actividad, se aplicara la sancion configurada."
                    ),
                    view=ConfirmAttendanceView(self, int(request["activity_id"])),
                )
            else:
                await send_dm_safe(
                    self.db,
                    guild_id=guild.id,
                    user=member,
                    action="solicitud_actividad_rechazada",
                    content=f"❌ Tu solicitud para **{request['activity_name']}** fue rechazada.",
                )
        await self.update_activity_message(int(request["activity_id"]))
        await private_response(
            interaction,
            "Solicitud aceptada y usuario notificado." if accepted else "Solicitud rechazada y usuario notificado.",
        )

    def start_voice_session(self, activity_id: int, guild_id: int, user_id: int) -> None:
        attendance = self.db.fetch_one(
            """
            SELECT confirmo_boton FROM asistencia_actividades
            WHERE actividad_id = ? AND usuario_id = ?
            """,
            (activity_id, user_id),
        )
        if attendance is None or int(attendance["confirmo_boton"]) != 1:
            return
        open_session = self.db.fetch_one(
            """
            SELECT 1 FROM activity_voice_sessions
            WHERE guild_id = ? AND activity_id = ? AND user_id = ? AND left_at IS NULL
            """,
            (guild_id, activity_id, user_id),
        )
        if open_session is None:
            self.db.execute(
                """
                INSERT INTO activity_voice_sessions (guild_id, activity_id, user_id, joined_at)
                VALUES (?, ?, ?, ?)
                """,
                (guild_id, activity_id, user_id, utc_now_iso()),
            )

    def close_voice_session(
        self,
        activity_id: int,
        guild_id: int,
        user_id: int,
        ended_at: str | None = None,
    ) -> None:
        end_iso = ended_at or utc_now_iso()
        end_time = parse_iso_datetime(end_iso)
        sessions = self.db.fetch_all(
            """
            SELECT id, joined_at FROM activity_voice_sessions
            WHERE guild_id = ? AND activity_id = ? AND user_id = ? AND left_at IS NULL
            """,
            (guild_id, activity_id, user_id),
        )
        for session in sessions:
            joined_at = parse_iso_datetime(str(session["joined_at"]))
            seconds = max(0, int((end_time - joined_at).total_seconds())) if end_time and joined_at else 0
            self.db.execute(
                "UPDATE activity_voice_sessions SET left_at = ?, seconds = ? WHERE id = ?",
                (end_iso, seconds, int(session["id"])),
            )

    def voice_stats(self, activity_id: int, user_id: int, at: str | None = None) -> tuple[int, float]:
        activity = self.get_activity(activity_id)
        if activity is None or not activity["started_at"]:
            return 0, 0.0
        start = parse_iso_datetime(str(activity["started_at"]))
        end = parse_iso_datetime(str(activity["ended_at"] or at or utc_now_iso()))
        duration = max(1, int((end - start).total_seconds())) if start and end else 1
        rows = self.db.fetch_all(
            """
            SELECT joined_at, left_at, seconds FROM activity_voice_sessions
            WHERE activity_id = ? AND user_id = ?
            """,
            (activity_id, user_id),
        )
        total = 0
        for row in rows:
            if row["left_at"] is not None:
                total += int(row["seconds"] or 0)
            else:
                joined = parse_iso_datetime(str(row["joined_at"]))
                total += max(0, int((end - joined).total_seconds())) if end and joined else 0
        total = min(total, duration)
        return total, round((total / duration) * 100, 2)

    def voice_monitor_text(self, activity_id: int) -> str:
        activity = self.get_activity(activity_id)
        if activity is None:
            return "No encontre esta actividad."
        users = {
            int(row["user_id"]): str(row["display_name"])
            for row in self.get_activity_participants(activity_id)
        }
        caller_id = int(activity["caller_id"])
        users.setdefault(caller_id, "Caller")
        lines = [f"📡 **Monitoreo de voz — {activity['name']}**"]
        for user_id, display_name in users.items():
            seconds, percent = self.voice_stats(activity_id, user_id)
            open_session = self.db.fetch_one(
                """
                SELECT 1 FROM activity_voice_sessions
                WHERE activity_id = ? AND user_id = ? AND left_at IS NULL
                """,
                (activity_id, user_id),
            )
            state = "🟢 En voz" if open_session is not None else "🔴 Fuera"
            minutes, remainder = divmod(seconds, 60)
            lines.append(
                f"{state} — <@{user_id}> ({display_name}): {minutes}m {remainder}s — {percent:.1f}%"
            )
        return "\n".join(lines)[:1900]

    async def join_role(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        role_id: int,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        activity = self.get_activity(activity_id)
        if not activity or activity["guild_id"] != interaction.guild.id:
            await interaction.followup.send("No encontre esta actividad.", ephemeral=True)
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await interaction.followup.send("Las inscripciones ya estan cerradas.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not has_bank_access(self.db, interaction.user):
            await interaction.followup.send(
                "Necesitas el rol MIEMBRO G3NESYS o INVITADO para anotarte.",
                ephemeral=True,
            )
            return
        penalty = self.ensure_penalty_for_user(interaction.guild.id, interaction.user.id)
        if penalty:
            await interaction.followup.send(
                "No puedes anotarte porque estas en lista de penalizacion. "
                f"Motivo: {penalty}",
                ephemeral=True,
            )
            return
        role = self.db.fetch_one(
            "SELECT * FROM activity_roles WHERE id = ? AND activity_id = ?",
            (role_id, activity_id),
        )
        if role is None:
            await interaction.followup.send("No encontre ese rol.", ephemeral=True)
            return
        current = self.db.fetch_one(
            "SELECT role_id FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        count_row = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM activity_participants
            WHERE activity_id = ? AND role_id = ? AND user_id != ?
            """,
            (activity_id, role_id, interaction.user.id),
        )
        if int(count_row["total"]) >= int(role["slots"]):
            await interaction.followup.send(
                f"**{role['name']}** ya esta completo [{role['slots']}/{role['slots']}].",
                ephemeral=True,
            )
            return
        self.db.execute(
            """
            INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(activity_id, user_id)
            DO UPDATE SET role_id = excluded.role_id,
                          display_name = excluded.display_name,
                          joined_at = excluded.joined_at
            """,
            (
                activity_id,
                role_id,
                interaction.user.id,
                interaction.user.display_name,
                utc_now_iso(),
            ),
        )
        check_view = None
        if activity["check_sent_at"]:
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
                ) VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(actividad_id, usuario_id) DO NOTHING
                """,
                (activity_id, interaction.user.id, ATTENDANCE_PENDING),
            )
            check_view = ConfirmAttendanceView(self, activity_id)
        await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=interaction.user,
            action="registro_actividad",
            content=(
                f"⚔️ Te registraste en **{activity['name']}** como **{role['name']}**.\n"
                "Debes confirmar el check y permanecer al menos el 50% de la actividad "
                "en el canal de voz; de lo contrario puede aplicarse la multa configurada."
            ),
            view=check_view,
        )
        await self.update_activity_message(activity_id)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="registration",
            content=(
                f"📝 <@{interaction.user.id}> se registro en la actividad "
                f"`{activity['code']}` como **{role['name']}**."
            ),
        )
        if current and int(current["role_id"]) != role_id:
            await interaction.followup.send(
                f"Te movi a **{role['name']}**. Recuerda confirmar el check y permanecer "
                "al menos el 50% de la actividad en voz para evitar sanciones.",
                ephemeral=True,
            )
        else:
            await interaction.followup.send(
                f"Quedaste anotado en **{role['name']}**. Debes confirmar el check y permanecer "
                "al menos el 50% de la actividad en voz; de lo contrario puede aplicarse multa.",
                ephemeral=True,
            )

    async def leave_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if (
            not activity
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
        ):
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await private_response(interaction, "No puedes salirte en este estado.")
            return
        self.db.execute(
            "DELETE FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        await self.update_activity_message(activity_id)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="registration",
            content=f"↩️ <@{interaction.user.id}> salio de la actividad `{activity['code']}`.",
        )
        await private_response(interaction, "Te quite de la actividad.")

    def build_payout_modal(self, activity_id: int) -> PayoutModal:
        return PayoutModal(self, activity_id)

    async def handle_activity_action(
        self,
        interaction: discord.Interaction,
        action: str,
        activity_id: int,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if action == "leave":
            await self.leave_activity(interaction, activity_id)
            return
        if action == "request_join":
            if activity["status"] != ACTIVITY_IN_PROGRESS:
                await private_response(interaction, "Las solicitudes solo estan disponibles durante la actividad.")
                return
            await interaction.response.send_modal(JoinActivityRequestModal(self, activity_id))
            return
        if action == "quick_liquidation":
            if not is_admin_subject(self.db, interaction):
                await private_response(interaction, "❌ Solo los administradores pueden usar liquidación rápida.")
                return
            admin_cog = self.bot.get_cog("Admin")
            if admin_cog is None:
                await private_response(interaction, "El panel administrativo no esta disponible.")
                return
            await admin_cog.prompt_quick_liquidation_for_activity(interaction, activity_id)
            return
        if action == "edit":
            if not await self.require_activity_notes_editor(interaction, activity):
                return
            await self.prompt_activity_edit_menu(interaction, activity_id)
            return
        if not await self.require_activity_manager(interaction, activity, action):
            return
        if action == "payout":
            await interaction.response.send_modal(PayoutModal(self, activity_id))
            return
        if action == "edit_composition":
            await self.prompt_edit_composition_modal(interaction, activity_id)
            return
        await interaction.response.defer(ephemeral=True)
        if action == "notice":
            await self.send_notice(interaction, activity_id)
        elif action == "start":
            await self.start_activity(interaction, activity_id)
        elif action == "check":
            await self.send_attendance_check(interaction, activity_id)
        elif action == "verify":
            await self.verify_attendance(interaction, activity_id)
        elif action == "monitor":
            await interaction.followup.send(
                self.voice_monitor_text(activity_id),
                ephemeral=True,
            )
        elif action == "finish":
            await self.finish_activity(interaction, activity_id)
        elif action == "cancel":
            await self.cancel_activity(interaction, activity_id)
        else:
            await interaction.followup.send("Accion no reconocida.", ephemeral=True)

    async def send_notice(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] != ACTIVITY_OPEN:
            await interaction.followup.send("Solo puedes mandar aviso si la actividad esta abierta.", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET status = ? WHERE id = ?",
            (ACTIVITY_NOTICE, activity_id),
        )
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            member = interaction.guild.get_member(int(participant["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="aviso_actividad",
                    content=(
                        f"La actividad **{activity['name']}** esta por iniciar. "
                        "Por favor entra al canal de voz y preparate. Debes confirmar el check "
                        "y permanecer al menos el 50% para evitar sanciones."
                    ),
                )
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Aviso enviado por DM a los participantes.", ephemeral=True)

    async def start_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not await self.require_activity_manager(interaction, activity, "iniciar"):
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await interaction.followup.send("Esta actividad no puede iniciarse en su estado actual.", ephemeral=True)
            return
        if not activity["voice_channel_id"]:
            await interaction.followup.send(
                "Configura un canal de voz antes de iniciar; se necesita para medir la participacion.",
                ephemeral=True,
            )
            return
        if not activity["check_sent_at"]:
            await interaction.followup.send(
                "Antes de iniciar debes usar **Mandar check** para avisar a los participantes.",
                ephemeral=True,
            )
            return
        self.audit_admin_activity_action(interaction, activity, "iniciar")
        started_at = utc_now_iso()
        self.db.execute(
            "UPDATE activities SET status = ?, started_at = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_IN_PROGRESS, started_at, interaction.guild.id, activity_id),
        )
        participant_ids = {
            int(participant["user_id"])
            for participant in self.get_activity_participants(activity_id)
        }
        for user_id in participant_ids:
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
                )
                VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(actividad_id, usuario_id) DO NOTHING
                """,
                (activity_id, user_id, ATTENDANCE_PENDING),
            )
            member = interaction.guild.get_member(user_id)
            if (
                member is not None
                and member.voice is not None
                and member.voice.channel is not None
                and member.voice.channel.id == int(activity["voice_channel_id"])
            ):
                self.start_voice_session(activity_id, interaction.guild.id, user_id)
        caller_id = int(activity["caller_id"])
        caller_member = interaction.guild.get_member(caller_id)
        caller_in_voice = bool(
            caller_member is not None
            and caller_member.voice is not None
            and caller_member.voice.channel is not None
            and caller_member.voice.channel.id == int(activity["voice_channel_id"])
        )
        self.db.execute(
            """
            INSERT INTO asistencia_actividades (
                actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz, fecha_check
            ) VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(actividad_id, usuario_id)
            DO UPDATE SET estado = excluded.estado,
                          confirmo_boton = 1,
                          confirmo_voz = excluded.confirmo_voz,
                          fecha_check = excluded.fecha_check
            """,
            (
                activity_id,
                caller_id,
                ATTENDANCE_CONFIRMED,
                1 if caller_in_voice else 0,
                started_at,
            ),
        )
        if caller_in_voice:
            self.start_voice_session(activity_id, interaction.guild.id, caller_id)
        self.bot.add_view(ConfirmAttendanceView(self, activity_id))
        await self.update_activity_message(activity_id)
        if caller_member is not None:
            await send_dm_safe(
                self.db,
                guild_id=interaction.guild.id,
                user=caller_member,
                action="resumen_check_inicio",
                content=self.attendance_summary_text(activity_id),
            )
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"▶️ Actividad `{activity['code']}` iniciada por <@{interaction.user.id}>. "
                f"Caller: <@{caller_id}> · Participantes: {len(participant_ids)}."
            ),
        )
        await interaction.followup.send(
            "Actividad iniciada. El conteo de permanencia en voz ya esta activo y se aceptan solicitudes tardias.",
            ephemeral=True,
        )

    async def send_attendance_check(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            await interaction.followup.send("El check ya no esta disponible para esta actividad.", ephemeral=True)
            return
        participant_ids = {
            int(participant["user_id"])
            for participant in self.get_activity_participants(activity_id)
        }
        recipient_ids = participant_ids
        for user_id in recipient_ids:
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
                ) VALUES (?, ?, ?, 0, 0)
                ON CONFLICT(actividad_id, usuario_id) DO NOTHING
                """,
                (activity_id, user_id, ATTENDANCE_PENDING),
            )
        if not activity["check_sent_at"]:
            self.db.execute(
                "UPDATE activities SET check_sent_at = ? WHERE id = ?",
                (utc_now_iso(), activity_id),
            )
        for user_id in recipient_ids:
            member = interaction.guild.get_member(user_id)
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="check_asistencia",
                    content=(
                        f"Confirma tu asistencia a **{activity['name']}**. "
                        "Si te anotaste y no participas, puedes recibir multa automatica.\n\n"
                        "El tiempo en el canal de voz define tu porcentaje del Split. "
                        "Permanecer menos del 50% cuenta como inasistencia y puede generar multa."
                    ),
                    view=ConfirmAttendanceView(self, activity_id),
                )
        await self.update_activity_message(activity_id)
        await interaction.followup.send(
            "Check enviado por DM. La actividad ya cumple el requisito previo para iniciar.",
            ephemeral=True,
        )

    async def verify_attendance(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        participants = self.get_activity_participants(activity_id)
        attendance_rows = self.db.fetch_all(
            "SELECT * FROM asistencia_actividades WHERE actividad_id = ?",
            (activity_id,),
        )
        attendance = {int(row["usuario_id"]): row for row in attendance_rows}
        confirmed: list[str] = []
        checked_absent: list[str] = []
        pending: list[str] = []
        for participant in participants:
            user_id = int(participant["user_id"])
            row = attendance.get(user_id)
            name = f"{participant['display_name']} (<@{user_id}>)"
            if row and int(row["confirmo_boton"]) == 1 and row["estado"] == ATTENDANCE_CONFIRMED:
                confirmed.append(name)
            elif row and int(row["confirmo_boton"]) == 1:
                checked_absent.append(name)
            else:
                pending.append(name)

        def block(title: str, rows: list[str]) -> list[str]:
            values = [f"**{title}**"]
            values.extend(f"• {row}" for row in rows)
            if not rows:
                values.append("• Ninguno")
            return values

        lines = [
            f"🔍 **Verificacion de asistencia**",
            f"Actividad: `{activity['code']}` {activity['name']}",
            "",
            *block("Confirmados con check y voz", confirmed),
            "",
            *block("Dieron check pero no estan en voz", checked_absent),
            "",
            *block("Sin check", pending),
        ]
        content = "\n".join(lines)
        if len(content) > 1900:
            content = content[:1850] + "\n\nLista recortada por limite de Discord."
        sent = await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=interaction.user,
            action="verificar_asistencia",
            content=content,
        )
        if sent:
            await interaction.followup.send("Te envie la lista de asistencia por DM.", ephemeral=True)
        else:
            await interaction.followup.send(content, ephemeral=True)

    async def confirm_attendance(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad.")
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            await private_response(interaction, "El check de esta actividad ya no esta disponible.")
            return
        participant = self.db.fetch_one(
            "SELECT 1 FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        if interaction.user.id == int(activity["caller_id"]):
            await private_response(
                interaction,
                "Tu check como caller se registra automaticamente al iniciar la actividad.",
            )
            return
        if participant is None and interaction.user.id != int(activity["caller_id"]):
            await private_response(interaction, "No estas registrado en esta actividad.")
            return
        guild = self.bot.get_guild(int(activity["guild_id"]))
        member = guild.get_member(interaction.user.id) if guild is not None else None
        if member is None or member.voice is None or member.voice.channel is None:
            await private_response(
                interaction,
                "Debes estar conectado a un canal de voz para confirmar. "
                "Entra a voz y vuelve a pulsar **Aqui estoy**.",
            )
            return
        if (
            activity["voice_channel_id"]
            and member.voice.channel.id != int(activity["voice_channel_id"])
        ):
            await private_response(
                interaction,
                f"Debes estar en <#{activity['voice_channel_id']}> para confirmar. "
                "Entra al canal y vuelve a pulsar **Aqui estoy**.",
            )
            return
        self.db.execute(
            """
            INSERT INTO asistencia_actividades (
                actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz, fecha_check
            )
            VALUES (?, ?, ?, 1, ?, ?)
            ON CONFLICT(actividad_id, usuario_id)
            DO UPDATE SET estado = excluded.estado,
                          confirmo_boton = 1,
                          confirmo_voz = excluded.confirmo_voz,
                          fecha_check = excluded.fecha_check
            """,
            (
                activity_id,
                interaction.user.id,
                ATTENDANCE_CONFIRMED,
                1,
                utc_now_iso(),
            ),
        )
        if activity["status"] == ACTIVITY_IN_PROGRESS:
            self.start_voice_session(activity_id, int(activity["guild_id"]), interaction.user.id)
        await private_response(
            interaction,
            "Asistencia confirmada. Recuerda permanecer al menos el 50% de la actividad en voz.",
        )

    async def finish_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not await self.require_activity_manager(interaction, activity, "finalizar"):
            return
        if activity["status"] != ACTIVITY_IN_PROGRESS:
            await interaction.followup.send("Solo puedes finalizar actividades en curso.", ephemeral=True)
            return
        self.audit_admin_activity_action(interaction, activity, "finalizar")
        ended_at = utc_now_iso()
        participants = self.get_activity_participants(activity_id)
        participant_ids = {int(participant["user_id"]) for participant in participants}
        caller_id = int(activity["caller_id"])
        tracked_users = participant_ids | {caller_id}
        for user_id in tracked_users:
            self.close_voice_session(
                activity_id,
                interaction.guild.id,
                user_id,
                ended_at,
            )
        self.db.execute(
            "UPDATE activities SET status = ?, ended_at = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_FINISHED, ended_at, interaction.guild.id, activity_id),
        )
        attendance_rows = self.db.fetch_all(
            "SELECT * FROM asistencia_actividades WHERE actividad_id = ?",
            (activity_id,),
        )
        known = {int(row["usuario_id"]): row for row in attendance_rows}
        absence_fine_enabled = self.db.get_int_setting(interaction.guild.id, "absence_fine_enabled", 0) == 1
        absence_fine_amount = self.db.get_int_setting(interaction.guild.id, "absence_fine_amount", 0)
        minimum_percent = self.db.get_int_setting(interaction.guild.id, "voice_minimum_percent", 50)
        absences = []
        for participant in participants:
            user_id = int(participant["user_id"])
            row = known.get(user_id)
            voice_seconds, participation_percent = self.voice_stats(activity_id, user_id, ended_at)
            justified = row is not None and row["estado"] == "Justificado"
            checked = row is not None and int(row["confirmo_boton"]) == 1
            attendance_state = (
                "Justificado"
                if justified
                else ATTENDANCE_CONFIRMED
                if checked and participation_percent >= minimum_percent
                else ATTENDANCE_ABSENT
            )
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz,
                    fecha_check, voice_seconds, participation_percent
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actividad_id, usuario_id)
                DO UPDATE SET estado = excluded.estado,
                              confirmo_voz = excluded.confirmo_voz,
                              voice_seconds = excluded.voice_seconds,
                              participation_percent = excluded.participation_percent
                """,
                (
                    activity_id,
                    user_id,
                    attendance_state,
                    1 if checked else 0,
                    1 if voice_seconds > 0 else 0,
                    row["fecha_check"] if row is not None else ended_at,
                    voice_seconds,
                    participation_percent,
                ),
            )
            updated = self.db.fetch_one(
                "SELECT * FROM asistencia_actividades WHERE actividad_id = ? AND usuario_id = ?",
                (activity_id, user_id),
            )
            if attendance_state == ATTENDANCE_ABSENT and int(updated["genero_multa"]) == 0:
                absences.append(user_id)
                member = interaction.guild.get_member(user_id)
                if member and absence_fine_enabled and absence_fine_amount > 0:
                    fine_code = await create_fine(
                        self.db,
                        guild_id=interaction.guild.id,
                        user=member,
                        amount=absence_fine_amount,
                        reason=(
                            f"Permanencia de {participation_percent:.1f}% en actividad "
                            f"{activity['code']} (minimo {minimum_percent}%)"
                        ),
                        origin="Sistema de Ping Actividades",
                        created_by=self.bot.user.id if self.bot.user else interaction.user.id,
                    )
                    self.db.execute(
                        """
                        UPDATE asistencia_actividades
                        SET genero_multa = 1
                        WHERE actividad_id = ? AND usuario_id = ?
                        """,
                        (activity_id, user_id),
                    )
                    self.ensure_penalty_for_user(interaction.guild.id, user_id)
                    log_action(
                        self.db,
                        interaction.guild.id,
                        admin_id=interaction.user.id,
                        action="Multa automatica por inasistencia",
                        system="Actividades",
                        affected_user_id=user_id,
                        amount=absence_fine_amount,
                        observation=fine_code,
                    )
        if caller_id not in participant_ids:
            caller_row = known.get(caller_id)
            caller_seconds, caller_percent = self.voice_stats(activity_id, caller_id, ended_at)
            caller_checked = caller_row is not None and int(caller_row["confirmo_boton"]) == 1
            caller_state = (
                ATTENDANCE_CONFIRMED
                if caller_checked and caller_percent >= minimum_percent
                else ATTENDANCE_ABSENT
            )
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz,
                    fecha_check, voice_seconds, participation_percent
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(actividad_id, usuario_id)
                DO UPDATE SET estado = excluded.estado,
                              confirmo_voz = excluded.confirmo_voz,
                              voice_seconds = excluded.voice_seconds,
                              participation_percent = excluded.participation_percent
                """,
                (
                    activity_id,
                    caller_id,
                    caller_state,
                    1 if caller_checked else 0,
                    1 if caller_seconds > 0 else 0,
                    caller_row["fecha_check"] if caller_row is not None else ended_at,
                    caller_seconds,
                    caller_percent,
                ),
            )
            if caller_state == ATTENDANCE_ABSENT:
                absences.append(caller_id)
        await self.update_activity_message(activity_id)
        await evaluate_caller_penalties(self.db, interaction.guild)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"🏁 Actividad `{activity['code']}` finalizada por <@{interaction.user.id}>. "
                f"Ausentes o bajo permanencia minima: {len(absences)}."
            ),
        )
        await interaction.followup.send(
            (
                f"Actividad finalizada. Ausentes o permanencia menor a {minimum_percent}%: "
                f"{len(absences)}. Los porcentajes quedaron listos para Splitear."
            ),
            ephemeral=True,
        )

    async def cancel_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not await self.require_activity_manager(interaction, activity, "cancelar"):
            return
        if activity["status"] in {ACTIVITY_CANCELLED, ACTIVITY_FINISHED, ACTIVITY_PAYOUT_CREATED}:
            await interaction.followup.send("Esta actividad ya no puede cancelarse.", ephemeral=True)
            return
        required_slots, registered_slots, reputation_exempt = cancellation_capacity(
            self.db,
            interaction.guild.id,
            activity_id,
        )
        cancelled_by_admin = (
            interaction.user.id != int(activity["caller_id"])
            and is_admin_subject(self.db, interaction)
        )
        reputation_exempt = reputation_exempt or cancelled_by_admin
        if cancelled_by_admin:
            cancellation_reason = "Cancelacion realizada por un administrador."
        elif registered_slots < required_slots:
            cancellation_reason = (
                f"Composicion incompleta: {registered_slots}/{required_slots} cupos ocupados."
            )
        else:
            cancellation_reason = "Cancelacion del caller con composicion completa."
        self.audit_admin_activity_action(interaction, activity, "cancelar")
        cancelled_at = utc_now_iso()
        if activity["status"] == ACTIVITY_IN_PROGRESS:
            tracked_users = {
                int(row["user_id"]) for row in self.get_activity_participants(activity_id)
            } | {int(activity["caller_id"])}
            for user_id in tracked_users:
                self.close_voice_session(
                    activity_id,
                    interaction.guild.id,
                    user_id,
                    cancelled_at,
                )
        self.db.execute(
            """
            UPDATE activities
            SET status = ?, ended_at = ?, cancelled_by = ?,
                cancellation_reputation_exempt = ?, cancellation_reason = ?
            WHERE guild_id = ? AND id = ?
            """,
            (
                ACTIVITY_CANCELLED,
                cancelled_at,
                interaction.user.id,
                1 if reputation_exempt else 0,
                cancellation_reason,
                interaction.guild.id,
                activity_id,
            ),
        )
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            member = interaction.guild.get_member(int(participant["user_id"]))
            if member:
                await send_dm_safe(
                    self.db,
                    guild_id=interaction.guild.id,
                    user=member,
                    action="cancelar_actividad",
                    content=(
                        f"La actividad **{activity['name']}** fue cancelada. "
                        f"Motivo: {cancellation_reason} "
                        "No se aplicaran multas ni penalizaciones de asistencia."
                    ),
                )
        await self.update_activity_message(activity_id)
        await evaluate_caller_penalties(self.db, interaction.guild)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"⛔ Actividad `{activity['code']}` cancelada por <@{interaction.user.id}>. "
                f"Motivo: {cancellation_reason}"
            ),
        )
        reputation_note = (
            f"No afecta la reputacion del caller: {cancellation_reason}"
            if reputation_exempt
            else "La cancelacion se registrara en la reputacion del caller."
        )
        await interaction.followup.send(
            f"Actividad cancelada y participantes notificados. {reputation_note}",
            ephemeral=True,
        )

    async def create_payout_from_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        modal: PayoutModal,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not await self.require_activity_manager(interaction, activity, "generar split"):
            return
        if activity["status"] != ACTIVITY_FINISHED:
            await private_response(interaction, "Solo se puede Splitear una actividad finalizada.")
            return
        try:
            gross = parse_split_amount(str(modal.gross_loot.value), "Loot bruto")
            market_rate = parse_percent(str(modal.market_rate.value))
            repairs, expenses = parse_cost_pair(str(modal.costs.value))
            guild_percent = parse_percent(str(modal.guild_percent.value))
            caller_percent = parse_percent(str(modal.caller_percent.value))
            totals = calculate_payout_totals(
                gross=gross,
                market_rate=market_rate,
                repairs=repairs,
                expenses=expenses,
                guild_percent=guild_percent,
                caller_percent=caller_percent,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        guild_amount = totals["guild_amount"]
        caller_amount = totals["caller_amount"]
        distributable = totals["distributable"]
        participants = self.db.fetch_all(
            """
            SELECT a.*
            FROM asistencia_actividades a
            JOIN activity_participants ap
              ON ap.activity_id = a.actividad_id AND ap.user_id = a.usuario_id
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.actividad_id = ? AND a.estado = ?
            """,
            (interaction.guild.id, activity_id, ATTENDANCE_CONFIRMED),
        )
        if not participants:
            await private_response(interaction, "No hay participantes confirmados para repartir.")
            return
        self.audit_admin_activity_action(interaction, activity, "generar split")
        code = self.db.next_code(interaction.guild.id, "SPLIT")
        payout_id = self.db.execute(
            """
            INSERT INTO payouts (
                code, guild_id, activity_id, caller_id, status, gross_loot,
                market_rate_percent, repairs, other_expenses, guild_percent,
                guild_amount, distributable, caller_percent, caller_amount, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code,
                interaction.guild.id,
                activity_id,
                int(activity["caller_id"]),
                PAYOUT_PENDING,
                gross,
                market_rate,
                repairs,
                expenses,
                guild_percent,
                guild_amount,
                distributable,
                caller_percent,
                caller_amount,
                utc_now_iso(),
            ),
        )
        log_payout_action(
            self.db,
            interaction.guild.id,
            payout_id,
            actor_id=interaction.user.id,
            action="Split preliminar creado",
            details=(
                f"Loot {gross}; mercado {market_rate}%; gremio {guild_percent}%; "
                f"caller {caller_percent}%"
            ),
        )
        for participant in participants:
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount
                )
                VALUES (?, ?, ?, 0)
                """,
                (
                    payout_id,
                    int(participant["usuario_id"]),
                    max(0.01, float(participant["participation_percent"] or 0)),
                ),
            )
        self.recalculate_payout_amounts(payout_id)
        self.db.execute(
            "UPDATE activities SET status = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_PAYOUT_CREATED, interaction.guild.id, activity_id),
        )
        await self.update_activity_message(activity_id)
        dm_content = (
            f"💰 **Split preliminar creado:** `{code}`\n\n"
            "Los porcentajes se calcularon con el tiempo permanecido en el canal de voz.\n"
            "Usa **Editar %** solo si necesitas una correccion manual.\n"
            f"Porcentaje del caller: **{caller_percent:.1f}%** ({format_amount(caller_amount)}).\n"
            "Cuando este listo, presiona **Enviar a revision**.\n\n"
            f"{self.payout_participants_text(interaction.guild.id, code)}"
        )
        sent = await send_dm_safe(
            self.db,
            guild_id=interaction.guild.id,
            user=interaction.user,
            action="reparto_preliminar_caller",
            content=dm_content[:1900],
            view=PayoutEditView(self, interaction.guild.id, code),
        )
        if sent:
            await private_response(interaction, f"Split preliminar `{code}` creado. Te envie la lista por DM.")
        else:
            await private_response(
                interaction,
                dm_content[:1900],
                view=PayoutEditView(self, interaction.guild.id, code),
            )

    def get_payout_by_code(self, guild_id: int, code: str):
        return self.db.fetch_one(
            "SELECT * FROM payouts WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )

    def can_manage_payout(self, ctx: commands.Context, payout) -> bool:
        return int(payout["caller_id"]) == ctx.author.id or is_admin_subject(self.db, ctx)

    def can_manage_payout_interaction(self, interaction: discord.Interaction, payout) -> bool:
        return int(payout["caller_id"]) == interaction.user.id or is_admin_subject(self.db, interaction)

    def is_editable_payout(self, payout) -> bool:
        return payout["status"] in {PAYOUT_PENDING, PAYOUT_CORRECTION}

    def restore_payout_pending_after_edit(self, payout, actor_id: int) -> None:
        if payout["status"] == PAYOUT_PENDING:
            return
        self.db.execute(
            "UPDATE payouts SET status = ?, reviewed_by = NULL, reviewed_at = NULL WHERE id = ?",
            (PAYOUT_PENDING, int(payout["id"])),
        )
        log_payout_action(
            self.db,
            int(payout["guild_id"]),
            int(payout["id"]),
            actor_id=actor_id,
            action="Split devuelto a pendiente",
            details=f"Estado anterior: {payout['status']}",
        )

    def payout_preliminary_text(self, guild_id: int, code: str) -> str:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            return "No encontre ese Split."
        lines = [
            f"💰 **Split preliminar `{code}`**",
            f"Loot bruto: **{format_amount(payout['gross_loot'])}**",
            f"Tasa mercado: **{float(payout['market_rate_percent'] or 0):.1f}%**",
            f"Reparaciones: **{format_amount(payout['repairs'])}**",
            f"Otros gastos: **{format_amount(payout['other_expenses'])}**",
            f"Gremio: **{float(payout['guild_percent'] or 0):.1f}%** ({format_amount(payout['guild_amount'])})",
            f"Caller: **{float(payout['caller_percent'] or 0):.1f}%** ({format_amount(payout['caller_amount'])})",
            f"Neto repartible: **{format_amount(payout['distributable'])}**",
            "",
            self.payout_participants_text(guild_id, code),
        ]
        return "\n".join(lines)[:1900]

    async def refresh_payout_source_message(self, source_message, guild_id: int, code: str) -> None:
        if source_message is None:
            return
        try:
            if getattr(source_message, "embeds", None):
                admin_cog = self.bot.get_cog("Admin")
                if admin_cog and hasattr(admin_cog, "build_payout_review_embed"):
                    await source_message.edit(
                        embed=admin_cog.build_payout_review_embed(guild_id, code),
                        view=admin_cog.build_payout_review_view(code),
                    )
                return
            await source_message.edit(
                content=self.payout_preliminary_text(guild_id, code),
                view=PayoutEditView(self, guild_id, code),
            )
        except (discord.Forbidden, discord.HTTPException, AttributeError):
            return

    async def eligible_payout_add_options(
        self,
        guild: discord.Guild,
        payout_id: int,
    ) -> list[discord.SelectOption]:
        included_rows = self.db.fetch_all(
            "SELECT user_id FROM payout_participants WHERE payout_id = ?",
            (payout_id,),
        )
        included = {int(row["user_id"]) for row in included_rows}
        if not getattr(guild, "chunked", True):
            try:
                await guild.chunk(cache=True)
            except (discord.Forbidden, discord.HTTPException, discord.ClientException):
                pass
        members = [
            member
            for member in guild.members
            if not member.bot and member.id not in included
        ]
        members.sort(key=lambda member: member.display_name.casefold())
        options: list[discord.SelectOption] = []
        for member in members[:25]:
            options.append(
                discord.SelectOption(
                    label=member.display_name[:100],
                    value=str(member.id),
                    description=str(member)[:100],
                )
            )
        return options

    async def payout_remove_options(
        self,
        guild: discord.Guild,
        payout_id: int,
    ) -> list[discord.SelectOption]:
        rows = self.db.fetch_all(
            "SELECT user_id, participation_percent, amount FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (payout_id,),
        )
        options: list[discord.SelectOption] = []
        for row in rows[:25]:
            user_id = int(row["user_id"])
            member = guild.get_member(user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(user_id)
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    member = None
            label = member.display_name if member is not None else f"Usuario {user_id}"
            options.append(
                discord.SelectOption(
                    label=label[:100],
                    value=str(user_id),
                    description=(
                        f"{float(row['participation_percent']):.1f}% · "
                        f"{format_amount(row['amount'])}"
                    )[:100],
                )
            )
        return options

    def set_payout_participation(self, payout_id: int, user_id: int, percent: float) -> None:
        row = self.db.fetch_one(
            "SELECT id FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (payout_id, user_id),
        )
        if row is None:
            raise ValueError("Ese usuario no esta en el Split. Usa `!split_agregar`.")
        self.db.execute(
            """
            UPDATE payout_participants
            SET participation_percent = ?
            WHERE payout_id = ? AND user_id = ?
            """,
            (percent, payout_id, user_id),
        )

    def recalculate_payout_amounts(self, payout_id: int) -> None:
        payout = self.db.fetch_one("SELECT * FROM payouts WHERE id = ?", (payout_id,))
        rows = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (payout_id,),
        )
        if not rows:
            raise ValueError("El Split debe tener al menos un participante.")
        total_percent = sum(float(row["participation_percent"]) for row in rows)
        if total_percent <= 0:
            raise ValueError("La participacion total debe ser mayor que cero.")
        distributable = int(payout["distributable"])
        assigned = 0
        for index, row in enumerate(rows):
            if index == len(rows) - 1:
                amount = distributable - assigned
            else:
                amount = int(round(distributable * (float(row["participation_percent"]) / total_percent)))
                assigned += amount
            self.db.execute(
                "UPDATE payout_participants SET amount = ? WHERE id = ?",
                (amount, int(row["id"])),
            )

    def payout_participants_text(self, guild_id: int, code: str) -> str:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            return "No encontre ese Split."
        rows = self.db.fetch_all(
            "SELECT * FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (int(payout["id"]),),
        )
        if not rows:
            return "Ese Split no tiene participantes."
        lines = [f"📋 **Participantes de {code}**"]
        for row in rows:
            amount = f"{int(row['amount']):,}".replace(",", ".")
            lines.append(f"• <@{row['user_id']}> - {row['participation_percent']}% - {amount}")
        return "\n".join(lines)

    async def send_payout_list_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
    ) -> None:
        await private_response(interaction, self.payout_participants_text(guild_id, code))

    async def edit_payout_percent_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        user_raw: str,
        percent_raw: str,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "Solo el caller del Split o un admin puede modificarlo.")
            return
        user_id = parse_channel_id(user_raw)
        if user_id is None:
            await private_response(interaction, "No pude leer el usuario.")
            return
        try:
            percent = parse_percent(percent_raw)
            self.set_payout_participation(int(payout["id"]), user_id, percent)
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, interaction.user.id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        log_payout_action(
            self.db,
            guild_id,
            int(payout["id"]),
            actor_id=interaction.user.id,
            action="Porcentaje actualizado",
            details=f"Usuario {user_id}: {percent}%",
        )
        await self.refresh_payout_source_message(source_message, guild_id, code)
        await private_response(
            interaction,
            f"Participacion actualizada a {percent}%.\n\n{self.payout_participants_text(guild_id, code)}",
            view=PayoutEditView(self, guild_id, code),
        )

    async def prompt_correct_payout_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "No tienes permiso para corregir este split.")
            return
        await interaction.response.send_modal(
            PayoutCorrectionModal(
                self,
                guild_id,
                code,
                payout,
                source_message=source_message,
            )
        )

    async def correct_payout_values_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        modal: PayoutCorrectionModal,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "No tienes permiso para corregir este split.")
            return
        try:
            gross = parse_split_amount(str(modal.gross_loot.value), "Loot bruto")
            market_rate = parse_percent(str(modal.market_rate.value))
            repairs, expenses = parse_cost_pair(str(modal.costs.value))
            guild_percent = parse_percent(str(modal.guild_percent.value))
            caller_percent = parse_percent(str(modal.caller_percent.value))
            totals = calculate_payout_totals(
                gross=gross,
                market_rate=market_rate,
                repairs=repairs,
                expenses=expenses,
                guild_percent=guild_percent,
                caller_percent=caller_percent,
            )
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        participants = self.db.fetch_all(
            "SELECT participation_percent FROM payout_participants WHERE payout_id = ?",
            (int(payout["id"]),),
        )
        if not participants:
            await private_response(interaction, "El Split debe tener al menos un participante.")
            return
        if sum(float(row["participation_percent"]) for row in participants) <= 0:
            await private_response(interaction, "La participacion total debe ser mayor que cero.")
            return
        old_values = payout_values_snapshot(payout)
        self.db.execute(
            """
            UPDATE payouts
            SET gross_loot = ?, market_rate_percent = ?, repairs = ?, other_expenses = ?,
                guild_percent = ?, guild_amount = ?, distributable = ?,
                caller_percent = ?, caller_amount = ?
            WHERE id = ?
            """,
            (
                gross,
                market_rate,
                repairs,
                expenses,
                guild_percent,
                totals["guild_amount"],
                totals["distributable"],
                caller_percent,
                totals["caller_amount"],
                int(payout["id"]),
            ),
        )
        self.recalculate_payout_amounts(int(payout["id"]))
        self.restore_payout_pending_after_edit(payout, interaction.user.id)
        updated = self.get_payout_by_code(guild_id, code)
        log_payout_action(
            self.db,
            guild_id,
            int(payout["id"]),
            actor_id=interaction.user.id,
            action="Split corregido",
            details=f"Antes: {old_values} | Nuevo: {payout_values_snapshot(updated)}",
        )
        await self.refresh_payout_source_message(source_message, guild_id, code)
        await private_response(
            interaction,
            f"Split `{code}` corregido y recalculado.\n\n{self.payout_preliminary_text(guild_id, code)}",
            view=PayoutEditView(self, guild_id, code),
        )

    async def prompt_add_payout_user_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "Solo el caller del Split o un admin puede modificarlo.")
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await private_response(interaction, "No pude encontrar el servidor para listar usuarios.")
            return
        options = await self.eligible_payout_add_options(guild, int(payout["id"]))
        if not options:
            await private_response(interaction, "No hay usuarios disponibles para añadir.")
            return
        await private_response(
            interaction,
            "Selecciona el usuario que deseas añadir al Split:",
            view=PayoutUserSelectView(
                self,
                guild_id=guild_id,
                payout_code=code,
                action="add",
                options=options,
                source_message=source_message,
            ),
        )

    async def add_payout_member_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        user_id: int,
        *,
        percent: float = 100,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "Solo el caller del Split o un admin puede modificarlo.")
            return
        guild = self.bot.get_guild(guild_id)
        member = guild.get_member(user_id) if guild else None
        if member is None:
            await private_response(interaction, "No pude encontrar ese usuario en el servidor.")
            return
        if member.bot:
            await private_response(interaction, "No se pueden añadir bots al Split.")
            return
        existing = self.db.fetch_one(
            "SELECT 1 FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (int(payout["id"]), user_id),
        )
        if existing is not None:
            await private_response(interaction, "Ese usuario ya esta incluido en el Split.")
            return
        try:
            if percent <= 0:
                raise ValueError("El porcentaje/peso debe ser mayor que cero.")
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount
                ) VALUES (?, ?, ?, 0)
                """,
                (int(payout["id"]), user_id, percent),
            )
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, interaction.user.id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        log_payout_action(
            self.db,
            guild_id,
            int(payout["id"]),
            actor_id=interaction.user.id,
            action="Usuario añadido",
            details=f"Usuario {user_id}: {percent}%",
        )
        await self.refresh_payout_source_message(source_message, guild_id, code)
        await private_response(
            interaction,
            f"<@{user_id}> añadido con {percent}%.\n\n{self.payout_participants_text(guild_id, code)}",
            view=PayoutEditView(self, guild_id, code),
        )

    async def prompt_remove_payout_user_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "No tienes permiso para eliminar usuarios de este split.")
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await private_response(interaction, "No pude encontrar el servidor para listar usuarios.")
            return
        options = await self.payout_remove_options(guild, int(payout["id"]))
        if not options:
            await private_response(interaction, "No hay usuarios para eliminar.")
            return
        await private_response(
            interaction,
            "Selecciona el usuario que deseas eliminar del Split:",
            view=PayoutUserSelectView(
                self,
                guild_id=guild_id,
                payout_code=code,
                action="remove",
                options=options,
                source_message=source_message,
            ),
        )

    async def remove_payout_user_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        user_id: int,
        *,
        source_message=None,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        if not self.is_editable_payout(payout):
            await private_response(interaction, "Solo se pueden modificar Splits preliminares.")
            return
        if not self.can_manage_payout_interaction(interaction, payout):
            await private_response(interaction, "No tienes permiso para eliminar usuarios de este split.")
            return
        rows = self.db.fetch_all(
            "SELECT id, user_id FROM payout_participants WHERE payout_id = ? ORDER BY id ASC",
            (int(payout["id"]),),
        )
        target = next((row for row in rows if int(row["user_id"]) == user_id), None)
        if target is None:
            await private_response(interaction, "Ese usuario no pertenece al Split.")
            return
        if len(rows) <= 1:
            await private_response(interaction, "El Split debe tener al menos un participante.")
            return
        self.db.execute(
            "DELETE FROM payout_participants WHERE payout_id = ? AND user_id = ?",
            (int(payout["id"]), user_id),
        )
        try:
            self.recalculate_payout_amounts(int(payout["id"]))
            self.restore_payout_pending_after_edit(payout, interaction.user.id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        log_payout_action(
            self.db,
            guild_id,
            int(payout["id"]),
            actor_id=interaction.user.id,
            action="Usuario eliminado",
            details=f"Usuario {user_id}; ajustes individuales asociados eliminados",
        )
        await self.refresh_payout_source_message(source_message, guild_id, code)
        await private_response(
            interaction,
            f"<@{user_id}> fue eliminado del Split.\n\n{self.payout_participants_text(guild_id, code)}",
            view=PayoutEditView(self, guild_id, code),
        )

    async def add_payout_user_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
        user_raw: str,
        percent_raw: str,
    ) -> None:
        user_id = parse_channel_id(user_raw)
        if user_id is None:
            await private_response(interaction, "No pude leer el usuario.")
            return
        try:
            percent = parse_percent(percent_raw)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        await self.add_payout_member_interaction(
            interaction,
            guild_id,
            code,
            user_id,
            percent=percent,
        )

    async def send_payout_to_review_interaction(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        code: str,
    ) -> None:
        payout = self.get_payout_by_code(guild_id, code)
        if payout is None:
            await private_response(interaction, "No encontre ese Split.")
            return
        is_admin = interaction.guild is not None and is_admin_subject(self.db, interaction)
        if int(payout["caller_id"]) != interaction.user.id and not is_admin:
            await private_response(interaction, "Solo el caller del Split o un admin puede enviarlo a revision.")
            return
        if payout["status"] != PAYOUT_PENDING:
            await private_response(interaction, "Ese Split ya no esta pendiente.")
            return
        if payout["sent_to_admin_at"]:
            await private_response(interaction, f"El Split `{code}` ya fue enviado a revision.")
            return
        guild = self.bot.get_guild(guild_id)
        if guild is None:
            await private_response(interaction, "No pude encontrar el servidor para enviar el Split.")
            return
        sent = await self.send_payout_to_admins(guild, int(payout["id"]))
        if not sent:
            await private_response(interaction, "No encontre canal de Splits/admins configurado.")
            return
        self.db.execute(
            "UPDATE payouts SET sent_to_admin_at = ? WHERE id = ?",
            (utc_now_iso(), int(payout["id"])),
        )
        log_payout_action(
            self.db,
            guild_id,
            int(payout["id"]),
            actor_id=interaction.user.id,
            action="Enviado a revision administrativa",
        )
        await private_response(interaction, f"📤 Split `{code}` enviado a revision admin.")

    async def send_payout_to_admins(self, guild: discord.Guild, payout_id: int) -> bool:
        payout = self.db.fetch_one("SELECT * FROM payouts WHERE id = ?", (payout_id,))
        if payout is None or int(payout["guild_id"]) != guild.id:
            return False
        admin_cog = self.bot.get_cog("Admin")
        if admin_cog and hasattr(admin_cog, "build_payout_review_embed"):
            embed = admin_cog.build_payout_review_embed(guild.id, payout["code"])
            view = admin_cog.build_payout_review_view(payout["code"])
        else:
            embed = discord.Embed(
                title=f"📋 Split pendiente {payout['code']}",
                description="Requiere revision y aprobacion admin antes de depositar saldos.",
                color=discord.Color.gold(),
            )
            embed.add_field(name="Loot bruto", value=f"{payout['gross_loot']:,}".replace(",", "."))
            embed.add_field(name="Aporte gremial", value=f"{payout['guild_amount']:,}".replace(",", "."))
            embed.add_field(
                name="Pago caller",
                value=(
                    f"{float(payout['caller_percent'] or 0):.1f}% — "
                    f"{int(payout['caller_amount'] or 0):,}"
                ).replace(",", "."),
            )
            embed.add_field(name="Monto repartible", value=f"{payout['distributable']:,}".replace(",", "."))
            embed.add_field(
                name="Participantes confirmados",
                value=self.payout_participants_text(guild.id, payout["code"])[:1024],
                inline=False,
            )
            embed.set_image(url=ADMIN_PANEL_IMAGE)
            view = None
        message = await send_admin_notification(
            self.db,
            guild=guild,
            category="splits",
            embed=embed,
            view=view,
        )
        return message is not None

    def ensure_penalty_for_user(self, guild_id: int, user_id: int) -> str | None:
        active = self.db.fetch_one(
            """
            SELECT motivo FROM penalizacion_actividades
            WHERE guild_id = ? AND usuario_id = ? AND activo = 1
            ORDER BY id DESC LIMIT 1
            """,
            (guild_id, user_id),
        )
        if active:
            return str(active["motivo"])

        pending_limit = self.db.get_int_setting(guild_id, "pending_fine_penalty_limit", 3)
        pending = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM fines
            WHERE guild_id = ? AND user_id = ? AND status = 'Pendiente'
            """,
            (guild_id, user_id),
        )
        if int(pending["total"]) >= pending_limit:
            return self.add_penalty(guild_id, user_id, "3 multas pendientes", "Sistema de Multas")

        total_limit = self.db.get_int_setting(guild_id, "total_absence_limit", 10)
        total_absent = self.db.fetch_one(
            """
            SELECT COUNT(*) AS total
            FROM asistencia_actividades a
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.usuario_id = ? AND a.estado = ?
            """,
            (guild_id, user_id, ATTENDANCE_ABSENT),
        )
        if int(total_absent["total"]) >= total_limit:
            return self.add_penalty(guild_id, user_id, "10 inasistencias acumuladas", "Actividades")

        consecutive_limit = self.db.get_int_setting(guild_id, "consecutive_absence_limit", 3)
        last_rows = self.db.fetch_all(
            """
            SELECT a.estado
            FROM asistencia_actividades a
            JOIN activities ac ON ac.id = a.actividad_id
            WHERE ac.guild_id = ? AND a.usuario_id = ?
            ORDER BY a.id DESC LIMIT ?
            """,
            (guild_id, user_id, consecutive_limit),
        )
        if (
            len(last_rows) >= consecutive_limit
            and all(row["estado"] == ATTENDANCE_ABSENT for row in last_rows)
        ):
            return self.add_penalty(guild_id, user_id, "3 inasistencias seguidas", "Actividades")
        return None

    def add_penalty(self, guild_id: int, user_id: int, reason: str, origin: str) -> str:
        self.db.execute(
            """
            INSERT INTO penalizacion_actividades (
                guild_id, usuario_id, motivo, origen, fecha_ingreso, activo
            )
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (guild_id, user_id, reason, origin, utc_now_iso()),
        )
        log_action(
            self.db,
            guild_id,
            admin_id=None,
            action="Penalizacion automatica",
            system="Actividades",
            affected_user_id=user_id,
            observation=reason,
        )
        return reason


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Activities(bot))
