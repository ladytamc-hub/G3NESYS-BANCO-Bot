from __future__ import annotations

import asyncio
import logging
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlparse

import discord
from discord.ext import commands

from ..constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_DELETED,
    ACTIVITY_DRAFT,
    ACTIVITY_TYPE_MANDATORY,
    ACTIVITY_TYPE_REGULAR,
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
    is_caller_panel_subject,
    is_official_caller_subject,
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
    split_csv_ids,
    utc_now_iso,
)
from ..weapon_aliases import resolve_weapon_alias


LOGGER = logging.getLogger("g3nesys.activities")
MAX_ACTIVITY_ROLES = 15
ACTIVITY_MANAGEMENT_DENIED_MESSAGE = (
    "No puedes administrar esta actividad porque no fuiste quien la creó."
)
VOICE_CHANNEL_ERROR = "❌ Debes ingresar un ID válido de canal de voz."
ACTIVITY_EMBED_COLOR = discord.Color(0xE83E8C)
ACTIVITY_SEPARATOR = "────────────────────────────────"
ACTIVITY_COMPOSITION_SEPARATOR = "━━━━━━━━━━━━"
ACTIVITY_EMBED_SPACER = "\u200b"
ACTIVITY_COMPOSITION_FIELD_LIMIT = 1024
ACTIVITY_COMPOSITION_FIELDS_PER_EMBED = 25
ACTIVITY_GENERAL_INFO_FIELD_COUNT = 6
ACTIVITY_GENERAL_TO_COMPOSITION_SPACERS = 1
ACTIVITY_PRIMARY_COMPOSITION_FIELDS = (
    ACTIVITY_COMPOSITION_FIELDS_PER_EMBED
    - ACTIVITY_GENERAL_INFO_FIELD_COUNT
    - ACTIVITY_GENERAL_TO_COMPOSITION_SPACERS
)
ACTIVITY_FOOTER_TEXT = (
    "✅ Haz check cuando el caller lo indique • "
    "🎤 Permanece en el canal de voz • "
    "⚔️ Sigue las indicaciones del caller"
)
MANDATORY_FOOTER_TEXT = "Convocatoria oficial - Asistencia calculada por presencia en voz"
MANDATORY_PARTICIPANT_ROLE_KEY = "__mandatory_participant__"
MANDATORY_PARTICIPANT_ROLE_NAME = "Participante"
MANDATORY_ROLE_SLOTS = 100000
IMAGE_FILE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".gif", ".webp")
ACTIVITY_STATUS_LABELS = {
    ACTIVITY_DRAFT: "⚪ BORRADOR",
    ACTIVITY_OPEN: "🟢 ABIERTA",
    ACTIVITY_NOTICE: "🟡 EN AVISO",
    ACTIVITY_IN_PROGRESS: "🔵 EN CURSO",
    ACTIVITY_CANCELLED: "🔴 CANCELADA",
    ACTIVITY_DELETED: "⚫ ELIMINADA",
    ACTIVITY_FINISHED: "⚫ FINALIZADA",
    ACTIVITY_PAYOUT_CREATED: "🟣 EN SPLIT",
}
APPROVED_PING_CHANNELS_SETTING_KEY = "approved_ping_channel_ids"
PING_THREAD_MESSAGE = "\U0001F4CC **Cualquier asunto relacionado con la composici\u00f3n o la actividad, favor de comentarlo en este hilo.**"


def is_mandatory_activity(activity) -> bool:
    if activity is None:
        return False
    return str(activity["activity_type"] or ACTIVITY_TYPE_REGULAR) == ACTIVITY_TYPE_MANDATORY

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
        return "🟢"
    if current > 0:
        return "🟡"
    return "⚪"


def activity_note_description(notes: str) -> str | None:
    if not notes:
        return None
    return f"{ACTIVITY_EMBED_SPACER}\n📝 {notes}\n{ACTIVITY_EMBED_SPACER}"


def activity_composition_field_value(names: list[str]) -> str:
    player_lines = [f"▸ {name}" for name in names] or ["▸ Disponible"]
    value = "\n".join(player_lines)
    if len(value) <= ACTIVITY_COMPOSITION_FIELD_LIMIT:
        return value

    clipped_lines: list[str] = []
    notice = "▸ Lista recortada por límite de Discord."
    for line in player_lines:
        candidate = "\n".join([*clipped_lines, line, notice])
        if len(candidate) > ACTIVITY_COMPOSITION_FIELD_LIMIT:
            break
        clipped_lines.append(line)
    clipped_lines.append(notice)
    return "\n".join(clipped_lines)


def parse_template_image_url(value: str) -> str:
    image_url = str(value or "").strip().strip("<>")
    if not image_url:
        raise ValueError("Pega una URL de imagen valida.")
    parsed = urlparse(image_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("La imagen debe ser una URL http(s) valida.")
    if len(image_url) > 2000:
        raise ValueError("La URL de imagen es demasiado larga.")
    return image_url


def image_url_from_attachments(attachments) -> str | None:
    for attachment in attachments:
        content_type = str(getattr(attachment, "content_type", "") or "").lower()
        filename = str(getattr(attachment, "filename", "") or "").lower()
        if content_type.startswith("image/") or filename.endswith(IMAGE_FILE_EXTENSIONS):
            return str(attachment.url)
    return None

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
        weapon = resolve_weapon_alias(name)
        if weapon is None:
            LOGGER.warning("Arma no reconocida en composicion de actividad: %s", name)
            role_key = normalize_key(name)
            display_name = name[:80]
        else:
            role_key = weapon.key
            display_name = weapon.display_name[:80]
            emoji = weapon.emoji
        roles.append(
            {
                "key": role_key,
                "name": display_name,
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


def parse_mandatory_loot_amount(raw: str) -> int:
    value = str(raw or "").strip().lower().replace(" ", "")
    if not value:
        raise ValueError("El botin obtenido es obligatorio.")
    if "-" in value:
        raise ValueError("El botin no puede ser negativo.")
    suffix_match = re.fullmatch(r"(\d+(?:[\.,]\d+)?)(mm|m|millon(?:es)?)", value)
    if suffix_match:
        number = float(suffix_match.group(1).replace(",", "."))
        amount = int(round(number * 1_000_000))
        if amount <= 0:
            raise ValueError("El botin debe ser mayor que cero.")
        return amount
    return parse_split_amount(raw, "Botin obtenido")

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
            "Tu acceso al Panel de Callers esta suspendido por reputacion. "
            "Un administrador debe retirar la penalizacion desde el Panel Administrativo.",
        )
        return
    await private_response(interaction, f"Solo admins, callers autorizados o usuarios con rol PCALL pueden {action}.")


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

    def __init__(
        self,
        cog: "Activities",
        *,
        voice_channel_id: int,
        publica: bool = False,
        image_url: str | None = None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.voice_channel_id = voice_channel_id
        self.publica = publica
        self.image_url = image_url

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.create_template_from_modal(interaction, self)


class EditTemplateModal(discord.ui.Modal):
    def __init__(self, cog: "Activities", template, roles):
        super().__init__(title="Editar plantilla", timeout=300)
        self.cog = cog
        self.template_id = int(template["id"])
        roles_text = "\n".join(
            f"{row['emoji'] or ''} | {row['name']} | {row['slots']}".strip()
            for row in roles
        )
        self.template_name = discord.ui.TextInput(
            label="Nombre de plantilla",
            max_length=80,
            default=str(template["name"] or "")[:80],
        )
        self.activity_name = discord.ui.TextInput(
            label="Nombre base de actividad",
            max_length=100,
            default=str(template["activity_name"] or "")[:100],
        )
        self.default_time = discord.ui.TextInput(
            label="Horario base",
            max_length=40,
            default=str(template["default_time"] or "")[:40],
        )
        self.description = discord.ui.TextInput(
            label="Nota / observaciones",
            style=discord.TextStyle.paragraph,
            max_length=600,
            default=str(template["description"] or "")[:600],
        )
        self.roles = discord.ui.TextInput(
            label="Composición / armas / cantidades",
            style=discord.TextStyle.paragraph,
            placeholder="Falce 2\n🔮 Prisma 2\n🛡️ | Tanque | 1",
            max_length=4000,
            default=roles_text[:4000],
        )
        self.add_item(self.template_name)
        self.add_item(self.activity_name)
        self.add_item(self.default_time)
        self.add_item(self.description)
        self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.update_template_from_modal(interaction, self)


class TemplateImageModal(discord.ui.Modal):
    def __init__(
        self,
        cog: "Activities",
        *,
        template_id: int | None = None,
        draft_view: "TemplateVisibilityView" | None = None,
        current_url: str = "",
    ):
        super().__init__(title="Imagen de composicion", timeout=180)
        self.cog = cog
        self.template_id = template_id
        self.draft_view = draft_view
        self.image_url = discord.ui.TextInput(
            label="URL de imagen",
            placeholder="https://...",
            max_length=2000,
            default=current_url[:2000],
        )
        self.add_item(self.image_url)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            image_url = parse_template_image_url(str(self.image_url.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        if self.draft_view is not None:
            if not await self.draft_view.require_author(interaction):
                return
            self.draft_view.image_url = image_url
            await private_response(interaction, "Imagen de composicion guardada para esta plantilla.")
            return
        if self.template_id is None:
            await private_response(interaction, "No encontre la plantilla a editar.")
            return
        await self.cog.set_template_image_url(interaction, self.template_id, image_url)


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


class TemplateEditVoiceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, cog: "Activities", template_id: int):
        super().__init__(
            placeholder="Cambiar canal de voz de la plantilla",
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.cog = cog
        self.template_id = template_id

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self.values:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        channel = resolve_selected_voice_channel(interaction.guild, self.values[0])
        if channel is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        await self.cog.update_template_voice_channel(
            interaction,
            self.template_id,
            channel.id,
        )


class TemplateVisibilityView(discord.ui.View):
    def __init__(self, cog: "Activities", *, author_id: int, publica: bool = False):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.publica = publica
        self.voice_channel_id: int | None = None
        self.image_url: str | None = None
        self.add_item(TemplateVoiceChannelSelect(self))
        self.update_toggle_button()

    def visibility_text(self) -> str:
        voice_text = f"<#{self.voice_channel_id}>" if self.voice_channel_id else "Pendiente"
        image_text = "configurada" if self.image_url else "sin imagen"
        return (
            "Elige la visibilidad de la plantilla antes de completar el formulario.\n"
            "Privada: solo tu puedes verla y usarla.\n"
            "Publica: cualquier Caller puede verla y usarla; solo tu o un admin podran administrarla.\n"
            f"Canal de voz: {voice_text}\n"
            f"Imagen de composicion: {image_text}"
        )

    def update_toggle_button(self) -> None:
        self.public_toggle.label = "Plantilla publica: Si" if self.publica else "Plantilla publica: No"
        self.public_toggle.style = discord.ButtonStyle.success if self.publica else discord.ButtonStyle.secondary
        self.public_toggle.emoji = "🌐" if self.publica else "🔒"

    async def require_author(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id and is_caller_panel_subject(self.cog.db, interaction):
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

    @discord.ui.button(label="Imagen", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
    async def image_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_author(interaction):
            return
        await interaction.response.send_modal(
            TemplateImageModal(
                self.cog,
                draft_view=self,
                current_url=self.image_url or "",
            )
        )

    @discord.ui.button(label="Adjunto", emoji="📎", style=discord.ButtonStyle.secondary, row=2)
    async def attachment_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.capture_template_image_message(interaction, draft_view=self)

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
                image_url=self.image_url,
            )
        )

    @discord.ui.button(label="Cancelar", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_author(interaction):
            return
        await interaction.response.edit_message(content="Creacion de plantilla cancelada.", view=None)


class PingPublicationChannelSelect(discord.ui.Select):
    def __init__(self, parent_view, guild: discord.Guild, *, row: int):
        self.parent_view = parent_view
        super().__init__(
            placeholder="Canal aprobado donde se publicara el ping",
            min_values=1,
            max_values=1,
            options=parent_view.cog.approved_ping_channel_options(
                guild,
                selected_channel_id=parent_view.publish_channel_id,
            ),
            row=row,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self.parent_view.can_use(interaction):
            await private_response(interaction, "Solo quien abrio este menu puede elegir canal.")
            return
        channel_id = int(self.values[0])
        if channel_id not in self.parent_view.cog.approved_ping_channel_ids(interaction.guild.id):
            await private_response(interaction, "Ese canal ya no esta aprobado para pings.")
            return
        self.parent_view.publish_channel_id = channel_id
        await interaction.response.edit_message(
            content=self.parent_view.text(interaction.guild),
            view=self.parent_view,
        )


class ActivityCreationChannelView(discord.ui.View):
    def __init__(
        self,
        cog: "Activities",
        *,
        author_id: int,
        guild: discord.Guild,
        template_id: int | None,
        default_name: str = "",
        default_time: str = "",
        default_notes: str = "",
        default_voice_channel_id: int | None = None,
        publish_channel_id: int | None = None,
    ):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.template_id = template_id
        self.default_name = default_name
        self.default_time = default_time
        self.default_notes = default_notes
        self.default_voice_channel_id = default_voice_channel_id
        self.publish_channel_id: int | None = publish_channel_id
        self.add_item(PingPublicationChannelSelect(self, guild, row=0))

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id and is_caller_panel_subject(self.cog.db, interaction)

    def text(self, guild: discord.Guild) -> str:
        return (
            "**Canal de publicacion del ping**\n"
            f"Destino: {self.cog.ping_publication_channel_text(guild, self.publish_channel_id)}\n"
            "Si no eliges canal, se usara el canal predeterminado actual."
        )

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.success, row=1)
    async def continue_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.can_use(interaction):
            await private_response(interaction, "Solo quien abrio este menu puede continuar.")
            return
        await interaction.response.send_modal(
            ActivityModal(
                self.cog,
                template_id=self.template_id,
                default_name=self.default_name,
                default_time=self.default_time,
                default_notes=self.default_notes,
                default_voice_channel_id=self.default_voice_channel_id,
                publish_channel_id=self.publish_channel_id,
            )
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.can_use(interaction):
            await private_response(interaction, "Solo quien abrio este menu puede cancelarlo.")
            return
        await interaction.response.edit_message(content="Creacion de ping cancelada.", view=None)

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
        publish_channel_id: int | None = None,
        draft_id: int | None = None,
        default_roles: str = "",
    ):
        title = "Crear Ping"
        super().__init__(title=title, timeout=300)
        self.cog = cog
        self.template_id = template_id
        self.publish_channel_id = publish_channel_id
        self.draft_id = draft_id
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
                default=default_roles[:1800],
            )
            self.add_item(self.roles)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_activity_draft_from_modal(interaction, self)


class MandatoryActivityModal(discord.ui.Modal, title="Ping Mandatory"):
    horario = discord.ui.TextInput(label="Horario de la actividad", max_length=40)
    voice_channel = discord.ui.TextInput(
        label="ID o mencion del canal de voz",
        placeholder="Selecciona voz o escribe <#123456789012345678>",
        required=True,
        max_length=80,
    )
    description = discord.ui.TextInput(
        label="Descripcion de la actividad",
        style=discord.TextStyle.paragraph,
        required=True,
        max_length=900,
    )
    image_url = discord.ui.TextInput(
        label="Imagen opcional URL",
        required=False,
        max_length=500,
        placeholder="https://...",
    )

    def __init__(
        self,
        cog: "Activities",
        *,
        default_voice_channel_id: int | None = None,
        default_horario: str = "",
        default_description: str = "",
        default_image_url: str = "",
        publish_channel_id: int | None = None,
        draft_id: int | None = None,
    ):
        super().__init__(timeout=300)
        self.cog = cog
        self.publish_channel_id = publish_channel_id
        self.draft_id = draft_id
        self.horario.default = default_horario[:40]
        self.description.default = default_description[:900]
        self.image_url.default = default_image_url[:500]
        if default_voice_channel_id:
            self.voice_channel.default = str(default_voice_channel_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_mandatory_draft_from_modal(interaction, self)


class MandatoryVoiceChannelSelect(discord.ui.ChannelSelect):
    def __init__(self, parent_view: "MandatoryPingDraftView"):
        super().__init__(
            placeholder="Selecciona el canal de voz del Mandatory",
            channel_types=[discord.ChannelType.voice, discord.ChannelType.stage_voice],
            min_values=1,
            max_values=1,
            row=0,
        )
        self.parent_view = parent_view

    async def callback(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not self.values:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        if not self.parent_view.can_use(interaction):
            await private_response(interaction, "Solo quien abrio este menu puede continuar.")
            return
        channel = resolve_selected_voice_channel(interaction.guild, self.values[0])
        if channel is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        self.parent_view.voice_channel_id = channel.id
        await interaction.response.edit_message(content=self.parent_view.text(interaction.guild), view=self.parent_view)


class MandatoryPingDraftView(discord.ui.View):
    def __init__(self, cog: "Activities", *, author_id: int, guild: discord.Guild):
        super().__init__(timeout=180)
        self.cog = cog
        self.author_id = author_id
        self.voice_channel_id: int | None = None
        self.publish_channel_id: int | None = None
        self.add_item(MandatoryVoiceChannelSelect(self))
        if cog.can_author_choose_ping_channel(guild.id, author_id) and cog.approved_ping_channel_options(guild):
            self.add_item(PingPublicationChannelSelect(self, guild, row=1))

    def can_use(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id and self.cog.can_create_mandatory_ping(interaction)

    def text(self, guild: discord.Guild | None = None) -> str:
        voice_text = f"<#{self.voice_channel_id}>" if self.voice_channel_id else "Puedes seleccionarlo aqui o escribir el ID en el formulario."
        return (
            "**Ping Mandatory**\n"
            "Convocatoria oficial sin composicion, roles, check ni split.\n"
            f"Canal de voz: {voice_text}\n"
            f"Publicacion: {self.cog.ping_publication_channel_text(guild, self.publish_channel_id) if guild else ''}"
        )

    @discord.ui.button(label="Continuar", style=discord.ButtonStyle.danger, row=2)
    async def continue_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.can_use(interaction):
            await private_response(interaction, "Solo el caller oficial o admin que abrio este menu puede continuar.")
            return
        await interaction.response.send_modal(
            MandatoryActivityModal(self.cog, default_voice_channel_id=self.voice_channel_id, publish_channel_id=self.publish_channel_id)
        )

    @discord.ui.button(label="Cancelar", style=discord.ButtonStyle.secondary, row=2)
    async def cancel_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.can_use(interaction):
            await private_response(interaction, "Solo quien abrio este menu puede cancelarlo.")
            return
        await interaction.response.edit_message(content="Creacion de Ping Mandatory cancelada.", view=None)


class MandatoryLootModal(discord.ui.Modal, title="Registrar Botin"):
    loot = discord.ui.TextInput(label="Botin obtenido", placeholder="35,000,000 o 35m", max_length=40)

    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=180)
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        if activity is not None and activity["mandatory_loot_amount"] is not None:
            self.loot.default = str(int(activity["mandatory_loot_amount"]))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.save_mandatory_loot_from_modal(interaction, self.activity_id, self)

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
        await self.cog.prompt_activity_creation(
            interaction,
            template_id=template_id,
            default_name=str(template["activity_name"] or ""),
            default_time=str(template["default_time"] or ""),
            default_notes=str(template["description"] or ""),
            default_voice_channel_id=template["voice_channel_id"],
        )


class TemplateSelectView(discord.ui.View):
    def __init__(self, cog: "Activities", templates):
        super().__init__(timeout=180)
        self.add_item(TemplateSelect(cog, templates))


class TemplateEditSelect(discord.ui.Select):
    def __init__(self, cog: "Activities", templates):
        self.cog = cog
        options = []
        for row in templates[:25]:
            image_note = "con imagen" if row["image_url"] else "sin imagen"
            options.append(
                discord.SelectOption(
                    label=row["name"][:100],
                    description=f"{row['activity_name']} - {image_note}"[:100],
                    value=str(row["id"]),
                )
            )
        super().__init__(
            placeholder="Selecciona una plantilla para editar",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        template_id = int(self.values[0])
        if self.cog.get_editable_template(interaction, template_id) is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        await self.cog.show_template_edit_panel(
            interaction,
            template_id,
            "Administra la plantilla seleccionada:",
            edit_current=True,
        )


class TemplateEditSelectView(discord.ui.View):
    def __init__(self, cog: "Activities", templates):
        super().__init__(timeout=180)
        self.add_item(TemplateEditSelect(cog, templates))


class TemplateEditManageView(discord.ui.View):
    def __init__(self, cog: "Activities", template):
        super().__init__(timeout=300)
        self.cog = cog
        self.template_id = int(template["id"])
        self.publica = bool(int(template["publica"]))
        self.add_item(TemplateEditVoiceChannelSelect(cog, self.template_id))
        self.update_visibility_button()

    def update_visibility_button(self) -> None:
        self.visibility_toggle.label = "Plantilla publica: Si" if self.publica else "Plantilla publica: No"
        self.visibility_toggle.style = discord.ButtonStyle.success if self.publica else discord.ButtonStyle.secondary
        self.visibility_toggle.emoji = "🌐" if self.publica else "🔒"

    @discord.ui.button(label="Editar datos", emoji="✏️", style=discord.ButtonStyle.primary, row=1)
    async def edit_details(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        template = self.cog.get_editable_template(interaction, self.template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        roles = self.cog.get_template_roles(self.template_id)
        await interaction.response.send_modal(EditTemplateModal(self.cog, template, roles))

    @discord.ui.button(label="Plantilla publica: No", emoji="🔒", style=discord.ButtonStyle.secondary, row=1)
    async def visibility_toggle(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.update_template_visibility(
            interaction,
            self.template_id,
            not self.publica,
        )

    @discord.ui.button(label="Cambiar imagen de composición", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
    async def change_image(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        template = self.cog.get_editable_template(interaction, self.template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        await interaction.response.send_modal(
            TemplateImageModal(
                self.cog,
                template_id=self.template_id,
                current_url=str(template["image_url"] or ""),
            )
        )

    @discord.ui.button(label="Subir adjunto", emoji="📎", style=discord.ButtonStyle.secondary, row=2)
    async def upload_attachment(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.capture_template_image_message(interaction, template_id=self.template_id)

    @discord.ui.button(label="Quitar imagen de composición", emoji="✖️", style=discord.ButtonStyle.danger, row=2)
    async def remove_image(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.set_template_image_url(
            interaction,
            self.template_id,
            None,
            edit_current=True,
        )

    @discord.ui.button(label="Dejar imagen actual", emoji="✅", style=discord.ButtonStyle.secondary, row=3)
    async def keep_image(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.cog.show_template_edit_panel(
            interaction,
            self.template_id,
            "Imagen actual conservada.",
            edit_current=True,
        )


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

    @discord.ui.button(label="Multas ON/OFF", emoji="🟢", style=discord.ButtonStyle.secondary, row=1)
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
        layout = [
            ("g3n:pings:create_mandatory", 0),
            ("g3n:pings:create_activity", 0),
            ("g3n:pings:select_template", 0),
            ("g3n:pings:create_template", 1),
            ("g3n:pings:edit_template", 1),
            ("g3n:pings:view_templates", 1),
            ("g3n:pings:my_activities", 3),
            ("g3n:pings:my_caller_penalties", 3),
            ("g3n:pings:my_caller_ranking", 3),
            ("g3n:pings:my_caller_report", 3),
            ("g3n:pings:configuration", 4),
        ]
        items = {
            item.custom_id: item
            for item in self.children
            if isinstance(item, discord.ui.Button) and item.custom_id
        }
        self.clear_items()
        for custom_id, row in layout:
            item = items.get(custom_id)
            if item is not None:
                item.row = row
                self.add_item(item)

    @discord.ui.button(
        label="Ping Mandatory",
        emoji="⚔️",
        style=discord.ButtonStyle.danger,
        custom_id="g3n:pings:create_mandatory",
        row=2,
    )
    async def create_mandatory(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not self.cog.can_create_mandatory_ping(interaction):
            await private_response(interaction, "Solo callers oficiales o admins pueden crear Ping Mandatory.")
            return
        view = MandatoryPingDraftView(self.cog, author_id=interaction.user.id, guild=interaction.guild)
        await private_response(interaction, view.text(interaction.guild), view=view)

    @discord.ui.button(
        label="Crear Ping Rápido",
        emoji="📍",
        style=discord.ButtonStyle.success,
        custom_id="g3n:pings:create_activity",
        row=0,
    )
    async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_panel_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "crear pings")
            return
        await self.cog.prompt_activity_creation(interaction, template_id=None)

    @discord.ui.button(
        label="Crear Ping desde Plantilla",
        emoji="📍",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:select_template",
        row=0,
    )
    async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_panel_subject(self.cog.db, interaction):
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
        label="Crear Plantilla de Ping",
        emoji="📝",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:create_template",
        row=0,
    )
    async def create_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_panel_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "crear plantillas")
            return
        view = TemplateVisibilityView(self.cog, author_id=interaction.user.id)
        await private_response(interaction, view.visibility_text(), view=view)

    @discord.ui.button(
        label="Ver mis Plantillas",
        emoji="📚",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:view_templates",
        row=0,
    )
    async def view_templates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_panel_subject(self.cog.db, interaction):
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
        label="Editar plantilla",
        emoji="✏️",
        style=discord.ButtonStyle.secondary,
        custom_id="g3n:pings:edit_template",
        row=0,
    )
    async def edit_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not is_caller_panel_subject(self.cog.db, interaction):
            await reject_caller_access(self.cog.db, interaction, "editar plantillas")
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
                WHERE guild_id = ? AND created_by = ?
                ORDER BY created_at DESC
                LIMIT 25
                """,
                (interaction.guild.id, interaction.user.id),
            )
        if not templates:
            await private_response(interaction, "No tienes plantillas editables.")
            return
        await private_response(
            interaction,
            "Elige la plantilla que quieres editar:",
            view=TemplateEditSelectView(self.cog, templates),
        )

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
        row=1,
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
        if activity is not None and is_mandatory_activity(activity):
            if status in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
                self.add_control_button("Participar", "mandatory_join", discord.ButtonStyle.success, 0, False, "✅")
                self.add_control_button("Salir", "mandatory_leave", discord.ButtonStyle.danger, 0, False, "🚪")
                self.add_control_button("Ver participantes", "mandatory_participants", discord.ButtonStyle.secondary, 0, False, "👥")
                self.add_control_button("Mandar aviso", "notice", discord.ButtonStyle.primary, 0, False, "📢")
                self.add_control_button("Iniciar actividad", "start", discord.ButtonStyle.success, 1, False, "▶️")
                self.add_control_button("Cancelar actividad", "cancel", discord.ButtonStyle.danger, 1, False, "✖️")
                self.add_control_button("Eliminar ping", "delete", discord.ButtonStyle.danger, 1, False)
            elif status == ACTIVITY_IN_PROGRESS:
                self.add_control_button("Participar", "mandatory_join", discord.ButtonStyle.success, 0, False, "✅")
                self.add_control_button("Salir", "mandatory_leave", discord.ButtonStyle.danger, 0, False, "🚪")
                self.add_control_button("Ver participantes", "mandatory_participants", discord.ButtonStyle.secondary, 0, False, "👥")
                self.add_control_button("Mandar aviso", "notice", discord.ButtonStyle.primary, 0, False, "📢")
                self.add_control_button("Finalizar actividad", "finish", discord.ButtonStyle.success, 1, False, "⏹️")
                self.add_control_button("Cancelar actividad", "cancel", discord.ButtonStyle.danger, 1, False, "✖️")
                self.add_control_button("Eliminar ping", "delete", discord.ButtonStyle.danger, 1, False)
            elif status == ACTIVITY_FINISHED:
                self.add_control_button("Ver participantes", "mandatory_participants", discord.ButtonStyle.secondary, 0, False, "👥")
                loot_label = "Editar Botin" if activity["mandatory_loot_amount"] is not None else "Botin"
                self.add_control_button(loot_label, "mandatory_loot", discord.ButtonStyle.primary, 0, False, "💰")
                self.add_control_button("Eliminar ping", "delete", discord.ButtonStyle.danger, 1, False)
            elif status == ACTIVITY_CANCELLED:
                self.add_control_button("Ver participantes", "mandatory_participants", discord.ButtonStyle.secondary, 0, False, "👥")
                self.add_control_button("Eliminar ping", "delete", discord.ButtonStyle.danger, 1, False)
            return
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
            self.add_control_button("Eliminar", "delete", discord.ButtonStyle.danger, 4, False)
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
            self.add_control_button("Eliminar", "delete", discord.ButtonStyle.danger, 1, False)
        elif status == ACTIVITY_FINISHED:
            self.add_control_button("Ver asistencia", "verify", discord.ButtonStyle.secondary, 0, False, "🔍")
            self.add_control_button("Splitear", "payout", discord.ButtonStyle.primary, 0, False, "💰")
            self.add_control_button("Liquidación rápida", "quick_liquidation", discord.ButtonStyle.danger, 0, False, "⚡")
            self.add_control_button("Eliminar", "delete", discord.ButtonStyle.danger, 1, False)
        elif status == ACTIVITY_PAYOUT_CREATED:
            self.add_control_button("Ver asistencia", "verify", discord.ButtonStyle.secondary, 0, False, "🔍")
            self.add_control_button("Liquidación rápida", "quick_liquidation", discord.ButtonStyle.danger, 0, False, "⚡")
            self.add_control_button("Eliminar", "delete", discord.ButtonStyle.danger, 1, False)

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


class ActivityThreadRoleSelect(discord.ui.Select):
    def __init__(self, cog: "Activities", activity_id: int):
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        guild = cog.bot.get_guild(int(activity["guild_id"])) if activity else None
        options: list[discord.SelectOption] = []
        for row in cog.get_activity_roles(activity_id)[:25]:
            current = int(row["participant_count"])
            slots = int(row["slots"])
            label = str(row["name"])[:80] or "Rol"
            option = discord.SelectOption(
                label=label,
                value=str(row["id"]),
                description=f"{current}/{slots} cupos ocupados"[:100],
            )
            emoji = str(resolve_custom_emojis(row["emoji"], guild) or row["emoji"] or "").strip()
            if emoji and not is_custom_emoji_placeholder(emoji):
                try:
                    option.emoji = discord.PartialEmoji.from_str(emoji)
                except ValueError:
                    pass
            options.append(option)
        super().__init__(
            placeholder="Elige rol o arma",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.cog.join_role(interaction, self.activity_id, int(self.values[0]))


class ActivityThreadRoleSelectView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int):
        super().__init__(timeout=180)
        self.add_item(ActivityThreadRoleSelect(cog, activity_id))


class ActivityThreadPanelView(discord.ui.View):
    def __init__(
        self,
        cog: "Activities",
        activity_id: int,
        *,
        force_disabled: bool = False,
    ):
        super().__init__(timeout=None)
        self.cog = cog
        self.activity_id = activity_id
        activity = cog.get_activity(activity_id)
        status = str(activity["status"]) if activity else ACTIVITY_DELETED
        is_mandatory = is_mandatory_activity(activity)
        if is_mandatory:
            join_available = status in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}
            leave_available = join_available
        else:
            join_available = status in {ACTIVITY_OPEN, ACTIVITY_NOTICE}
            leave_available = join_available
        participants_available = activity is not None and status != ACTIVITY_DELETED
        self.add_thread_button(
            "Participar",
            "participate",
            discord.ButtonStyle.success,
            disabled=force_disabled or not join_available,
        )
        self.add_thread_button(
            "Salir",
            "leave",
            discord.ButtonStyle.danger,
            disabled=force_disabled or not leave_available,
        )
        self.add_thread_button(
            "Ver participantes",
            "participants",
            discord.ButtonStyle.secondary,
            disabled=force_disabled or not participants_available,
        )

    def add_thread_button(
        self,
        label: str,
        action: str,
        style: discord.ButtonStyle,
        *,
        disabled: bool,
    ) -> None:
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"g3n:activity_thread:{action}:{self.activity_id}",
            disabled=disabled,
        )
        button.callback = self.handle
        self.add_item(button)

    async def handle(self, interaction: discord.Interaction) -> None:
        custom_id = str(interaction.data["custom_id"])
        _, _, action, activity_id = custom_id.split(":")
        await self.cog.handle_activity_thread_action(interaction, action, int(activity_id))


class PingPreviewView(discord.ui.View):
    def __init__(self, cog: "Activities", activity_id: int, author_id: int):
        super().__init__(timeout=900)
        self.cog = cog
        self.activity_id = activity_id
        self.author_id = author_id
        activity = cog.get_activity(activity_id)
        guild = cog.bot.get_guild(int(activity["guild_id"])) if activity is not None else None
        if activity is not None and is_mandatory_activity(activity):
            for label in ("Participar", "Salir", "Ver participantes"):
                self.add_item(discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, row=0, disabled=True))
        else:
            roles = cog.get_activity_roles(activity_id)
            for index, row in enumerate(roles[:15]):
                slots = int(row["slots"])
                role_name = str(row["name"])
                counter = f" [0/{slots}]"
                button = discord.ui.Button(
                    label=f"{role_name[:80 - len(counter)]}{counter}",
                    style=discord.ButtonStyle.secondary,
                    row=index // 5,
                    disabled=True,
                )
                emoji = str(resolve_custom_emojis(row["emoji"], guild) or row["emoji"] or "").strip()
                if emoji and not is_custom_emoji_placeholder(emoji):
                    try:
                        button.emoji = discord.PartialEmoji.from_str(emoji)
                    except ValueError:
                        pass
                self.add_item(button)
            if not roles:
                self.add_item(discord.ui.Button(label="Sin composicion", style=discord.ButtonStyle.secondary, row=0, disabled=True))
        self.add_action_button("Publicar ping", "publish", discord.ButtonStyle.success)
        self.add_action_button("Editar ping", "edit", discord.ButtonStyle.primary)
        self.add_action_button("Cancelar creacion", "cancel", discord.ButtonStyle.danger)

    def add_action_button(self, label: str, action: str, style: discord.ButtonStyle) -> None:
        button = discord.ui.Button(label=label, style=style, row=4)
        button.callback = self._action_callback(action)
        self.add_item(button)

    def _action_callback(self, action: str):
        async def callback(interaction: discord.Interaction) -> None:
            if action == "publish":
                await self.cog.publish_activity_draft(interaction, self.activity_id, self.author_id)
            elif action == "edit":
                await self.cog.prompt_edit_activity_draft(interaction, self.activity_id, self.author_id)
            else:
                await self.cog.cancel_activity_draft(interaction, self.activity_id, self.author_id)

        return callback

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
        thread_panel_rows = self.db.fetch_all(
            """
            SELECT id
            FROM activities
            WHERE thread_panel_message_id IS NOT NULL AND status != ?
            """,
            (ACTIVITY_DELETED,),
        )
        for row in thread_panel_rows:
            self.bot.add_view(ActivityThreadPanelView(self, int(row["id"])))
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
            await self.refresh_pings_panel_message(guild)

    async def refresh_pings_panel_message(self, guild: discord.Guild) -> None:
        row = self.db.fetch_one(
            """
            SELECT channel_id, message_id
            FROM panel_messages
            WHERE guild_id = ? AND panel_type = 'pings'
            """,
            (guild.id,),
        )
        if row is None:
            return
        channel = guild.get_channel(int(row["channel_id"]))
        if channel is None or not hasattr(channel, "fetch_message"):
            return
        try:
            message = await channel.fetch_message(int(row["message_id"]))
            await message.edit(embed=self.build_pings_panel_embed(), view=PingsPanelView(self))
        except discord.HTTPException:
            return

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
                SELECT user_id AS usuario_id FROM activity_participants
                WHERE activity_id = ?
                UNION
                SELECT ? AS usuario_id
                """,
                (int(activity["id"]), int(activity["caller_id"])),
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

    def build_pings_panel_embed(self) -> discord.Embed:
        embed = discord.Embed(
            title="Panel de Callers",
            description=(
                "Crea pings, reutiliza plantillas y organiza composiciones "
                "sin saturar el canal."
            ),
            color=discord.Color.dark_gold(),
        )
        embed.set_image(url=PINGS_PANEL_IMAGE)
        return embed

    def can_author_choose_ping_channel(self, guild_id: int, user_id: int) -> bool:
        row = self.db.fetch_one(
            "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
            (guild_id, user_id),
        )
        return row is not None and not is_caller_penalized(self.db, guild_id, user_id)

    def can_choose_ping_publication_channel(self, interaction: discord.Interaction) -> bool:
        return interaction.guild is not None and is_official_caller_subject(self.db, interaction)

    def approved_ping_channel_ids(self, guild_id: int) -> set[int]:
        return split_csv_ids(self.db.get_setting(guild_id, APPROVED_PING_CHANNELS_SETTING_KEY))

    def approved_ping_channel_options(
        self,
        guild: discord.Guild,
        *,
        selected_channel_id: int | None = None,
    ) -> list[discord.SelectOption]:
        options: list[discord.SelectOption] = []
        for channel_id in sorted(self.approved_ping_channel_ids(guild.id)):
            channel = guild.get_channel(channel_id)
            if channel is None or not callable(getattr(channel, "send", None)):
                continue
            options.append(
                discord.SelectOption(
                    label=f"#{channel.name}"[:100],
                    value=str(channel_id),
                    description=f"Canal aprobado ID {channel_id}"[:100],
                    default=selected_channel_id == channel_id,
                )
            )
            if len(options) >= 25:
                break
        return options

    def ping_publication_channel_text(
        self,
        guild: discord.Guild,
        selected_channel_id: int | None = None,
    ) -> str:
        if selected_channel_id is not None:
            return f"<#{selected_channel_id}>"
        default_id = self.db.get_setting(guild.id, "channel_pings_id")
        if default_id and str(default_id).isdigit():
            return f"<#{default_id}> (predeterminado)"
        return "canal predeterminado sin configurar"

    def resolve_ping_publication_channel(
        self,
        interaction: discord.Interaction,
        selected_channel_id: int | None,
    ):
        if interaction.guild is None:
            raise ValueError("Esta accion solo esta disponible en un servidor.")
        channel_id: int | None = None
        if selected_channel_id is not None and self.can_choose_ping_publication_channel(interaction):
            if selected_channel_id not in self.approved_ping_channel_ids(interaction.guild.id):
                raise ValueError("Ese canal ya no esta aprobado para publicar pings.")
            channel_id = selected_channel_id
        else:
            channel_id_raw = self.db.get_setting(interaction.guild.id, "channel_pings_id")
            if not channel_id_raw:
                raise ValueError("No hay canal configurado para publicaciones de pings.")
            channel_id = int(channel_id_raw)
        channel = interaction.guild.get_channel(channel_id)
        if channel is None or not callable(getattr(channel, "send", None)):
            raise ValueError("El canal de pings configurado ya no existe o no permite publicar.")
        return channel

    async def prompt_activity_creation(
        self,
        interaction: discord.Interaction,
        *,
        template_id: int | None,
        default_name: str = "",
        default_time: str = "",
        default_notes: str = "",
        default_voice_channel_id: int | None = None,
    ) -> None:
        if (
            interaction.guild is not None
            and self.can_choose_ping_publication_channel(interaction)
            and self.approved_ping_channel_options(interaction.guild)
        ):
            view = ActivityCreationChannelView(
                self,
                author_id=interaction.user.id,
                guild=interaction.guild,
                template_id=template_id,
                default_name=default_name,
                default_time=default_time,
                default_notes=default_notes,
                default_voice_channel_id=default_voice_channel_id,
            )
            await private_response(interaction, view.text(interaction.guild), view=view)
            return
        await interaction.response.send_modal(
            ActivityModal(
                self,
                template_id=template_id,
                default_name=default_name,
                default_time=default_time,
                default_notes=default_notes,
                default_voice_channel_id=default_voice_channel_id,
            )
        )

    def can_create_mandatory_ping(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            return False
        return is_admin_subject(self.db, interaction) or is_official_caller_subject(self.db, interaction)

    def ensure_mandatory_participant_role(self, activity_id: int) -> int:
        role = self.db.fetch_one(
            "SELECT id FROM activity_roles WHERE activity_id = ? AND key = ?",
            (activity_id, MANDATORY_PARTICIPANT_ROLE_KEY),
        )
        if role is not None:
            return int(role["id"])
        return self.db.execute(
            """
            INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
            VALUES (?, ?, ?, ?, ?, 0)
            """,
            (
                activity_id,
                MANDATORY_PARTICIPANT_ROLE_KEY,
                MANDATORY_PARTICIPANT_ROLE_NAME,
                MANDATORY_ROLE_SLOTS,
                "",
            ),
        )

    @commands.command(name="panel_pings")
    async def panel_pings(self, ctx: commands.Context) -> None:
        if not await require_caller_context(ctx, self.db):
            return
        message = await ctx.send(embed=self.build_pings_panel_embed(), view=PingsPanelView(self))
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

    async def set_template_image_from_context(
        self,
        ctx: commands.Context,
        template_id: int,
        image_url: str | None,
    ) -> None:
        if ctx.guild is None:
            await ctx.reply("Este comando solo funciona dentro del servidor.", mention_author=False)
            return
        if is_admin_subject(self.db, ctx):
            template = self.db.fetch_one(
                "SELECT * FROM templates WHERE id = ? AND guild_id = ?",
                (template_id, ctx.guild.id),
            )
        else:
            template = self.db.fetch_one(
                """
                SELECT *
                FROM templates
                WHERE id = ? AND guild_id = ? AND created_by = ?
                """,
                (template_id, ctx.guild.id, ctx.author.id),
            )
        if template is None:
            await ctx.reply("No encontre una plantilla editable para ti.", mention_author=False)
            return
        self.db.execute(
            "UPDATE templates SET image_url = ? WHERE id = ? AND guild_id = ?",
            (image_url, template_id, ctx.guild.id),
        )
        self.log_template_edit(
            ctx.guild.id,
            ctx.author.id,
            template_id,
            str(template["name"]),
            "campo=image_url; estado=" + ("configurada" if image_url else "quitada"),
        )
        await self.refresh_template_activity_messages(template_id)
        message = "Imagen de composicion actualizada." if image_url else "Imagen de composicion quitada."
        await ctx.reply(message, mention_author=False)

    @commands.command(name="plantilla_imagen")
    async def plantilla_imagen(
        self,
        ctx: commands.Context,
        template_id: int,
        *,
        image_url: str = "",
    ) -> None:
        if not await require_caller_context(ctx, self.db):
            return
        attachment_url = image_url_from_attachments(getattr(ctx.message, "attachments", []))
        raw_image_url = attachment_url or image_url
        if not raw_image_url:
            await ctx.reply(
                "Adjunta una imagen o pega una URL despues del ID de plantilla.",
                mention_author=False,
            )
            return
        try:
            parsed_url = parse_template_image_url(raw_image_url)
        except ValueError as exc:
            await ctx.reply(str(exc), mention_author=False)
            return
        await self.set_template_image_from_context(ctx, template_id, parsed_url)

    @commands.command(name="plantilla_imagen_quitar")
    async def plantilla_imagen_quitar(
        self,
        ctx: commands.Context,
        template_id: int,
    ) -> None:
        if not await require_caller_context(ctx, self.db):
            return
        await self.set_template_image_from_context(ctx, template_id, None)

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
        if not interaction.guild or not is_caller_panel_subject(self.db, interaction):
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
                voice_channel_id, description, image_url, publica, created_by, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                template_name,
                activity_name,
                default_time,
                voice_channel.id,
                description,
                modal.image_url,
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

    def activity_roles_input_text(self, activity_id: int) -> str:
        return "\n".join(
            f"{row['emoji'] or ''} | {row['name']} | {row['slots']}".strip()
            for row in self.get_activity_roles(activity_id)
        )

    async def send_ping_preview(
        self,
        interaction: discord.Interaction,
        activity_id: int,
    ) -> None:
        activity = self.get_activity(activity_id)
        if activity is None:
            await private_response(interaction, "No pude generar la vista previa del ping.")
            return
        guild = interaction.guild or self.bot.get_guild(int(activity["guild_id"]))
        channel_text = f"<#{activity['channel_id']}>" if activity["channel_id"] else "sin canal"
        content = (
            "**Vista previa privada del ping**\n"
            f"Se publicara en: {channel_text}\n"
            "Revisa el mensaje. Si todo esta bien, pulsa **Publicar ping**."
        )
        await private_response(
            interaction,
            content,
            embeds=self.build_activity_embeds(activity_id, preview_status=ACTIVITY_OPEN),
            view=PingPreviewView(self, activity_id, int(activity["caller_id"])),
        )

    def draft_activity_for_user(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        author_id: int,
    ):
        if interaction.guild is None:
            return None, "Esta accion solo esta disponible en un servidor."
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            return None, "No encontre este borrador en el servidor."
        if int(activity["caller_id"]) != author_id or interaction.user.id != author_id:
            return None, "Solo quien creo este borrador puede usar esta vista previa."
        if activity["status"] != ACTIVITY_DRAFT:
            return None, "Este borrador ya fue publicado, eliminado o cancelado."
        if is_mandatory_activity(activity):
            if not self.can_create_mandatory_ping(interaction):
                return None, "Ya no tienes permiso para publicar este Ping Mandatory."
        elif not is_caller_panel_subject(self.db, interaction):
            return None, "Ya no tienes permiso para publicar pings."
        return activity, None

    async def save_mandatory_draft_from_modal(
        self,
        interaction: discord.Interaction,
        modal: MandatoryActivityModal,
    ) -> None:
        if not interaction.guild or not self.can_create_mandatory_ping(interaction):
            await private_response(interaction, "Solo callers oficiales o admins pueden crear Ping Mandatory.")
            return
        try:
            channel = self.resolve_ping_publication_channel(interaction, modal.publish_channel_id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        voice_channel = resolve_voice_channel(interaction.guild, str(modal.voice_channel.value))
        if voice_channel is None:
            await private_response(interaction, VOICE_CHANNEL_ERROR)
            return
        image_url = str(modal.image_url.value or "").strip()
        if image_url:
            try:
                image_url = parse_template_image_url(image_url)
            except ValueError as exc:
                await private_response(interaction, str(exc))
                return
        description = resolve_template_text(str(modal.description.value), interaction.guild)
        horario = resolve_template_text(str(modal.horario.value), interaction.guild)
        draft_id = modal.draft_id
        if draft_id is not None:
            current, error = self.draft_activity_for_user(interaction, int(draft_id), interaction.user.id)
            if current is None:
                await private_response(interaction, error or "No encontre este borrador.")
                return
            self.db.execute(
                """
                UPDATE activities
                SET horario = ?, voice_channel_id = ?, notes = ?, channel_id = ?, image_url = ?
                WHERE guild_id = ? AND id = ?
                """,
                (
                    horario,
                    voice_channel.id,
                    description,
                    channel.id,
                    image_url or None,
                    interaction.guild.id,
                    int(draft_id),
                ),
            )
            activity_id = int(draft_id)
        else:
            code = self.db.next_code(interaction.guild.id, "MAND")
            activity_id = self.db.execute(
                """
                INSERT INTO activities (
                    code, guild_id, template_id, name, caller_id, horario,
                    voice_channel_id, notes, status, channel_id, created_at,
                    activity_type, image_url
                )
                VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    code,
                    interaction.guild.id,
                    "Ping Mandatory",
                    interaction.user.id,
                    horario,
                    voice_channel.id,
                    description,
                    ACTIVITY_DRAFT,
                    channel.id,
                    utc_now_iso(),
                    ACTIVITY_TYPE_MANDATORY,
                    image_url or None,
                ),
            )
        self.ensure_mandatory_participant_role(activity_id)
        await self.send_ping_preview(interaction, activity_id)

    async def save_activity_draft_from_modal(
        self,
        interaction: discord.Interaction,
        modal: ActivityModal,
    ) -> None:
        if not interaction.guild or not is_caller_panel_subject(self.db, interaction):
            await private_response(interaction, "No tienes permiso para crear pings.")
            return
        try:
            channel = self.resolve_ping_publication_channel(interaction, modal.publish_channel_id)
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        try:
            if modal.template_id is None:
                roles = parse_role_lines(str(modal.roles.value))
            elif modal.draft_id is not None:
                roles = [
                    {
                        "key": row["key"],
                        "name": row["name"],
                        "slots": int(row["slots"]),
                        "emoji": row["emoji"] or "",
                        "position": int(row["position"]),
                    }
                    for row in self.db.fetch_all(
                        "SELECT * FROM activity_roles WHERE activity_id = ? ORDER BY position ASC",
                        (int(modal.draft_id),),
                    )
                ]
            else:
                roles = [
                    {
                        "key": row["key"],
                        "name": row["name"],
                        "slots": int(row["slots"]),
                        "emoji": row["emoji"] or "",
                        "position": int(row["position"]),
                    }
                    for row in self.db.fetch_all(
                        "SELECT * FROM template_roles WHERE template_id = ? ORDER BY position ASC",
                        (modal.template_id,),
                    )
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
        if modal.draft_id is not None:
            current, error = self.draft_activity_for_user(interaction, int(modal.draft_id), interaction.user.id)
            if current is None:
                await private_response(interaction, error or "No encontre este borrador.")
                return
            activity_id = int(modal.draft_id)
            with self.db.transaction() as cursor:
                cursor.execute(
                    """
                    UPDATE activities
                    SET template_id = ?, name = ?, horario = ?, voice_channel_id = ?,
                        notes = ?, channel_id = ?
                    WHERE guild_id = ? AND id = ?
                    """,
                    (
                        modal.template_id,
                        activity_name,
                        horario,
                        voice_channel.id,
                        notes,
                        channel.id,
                        interaction.guild.id,
                        activity_id,
                    ),
                )
                cursor.execute("DELETE FROM activity_roles WHERE activity_id = ?", (activity_id,))
                for role in roles:
                    cursor.execute(
                        """
                        INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (activity_id, role["key"], role["name"], role["slots"], role["emoji"], role["position"]),
                    )
        else:
            code = self.db.next_code(interaction.guild.id, "ACT")
            with self.db.transaction() as cursor:
                cursor.execute(
                    """
                    INSERT INTO activities (
                        code, guild_id, template_id, name, caller_id, horario,
                        voice_channel_id, notes, status, channel_id, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        code,
                        interaction.guild.id,
                        modal.template_id,
                        activity_name,
                        interaction.user.id,
                        horario,
                        voice_channel.id,
                        notes,
                        ACTIVITY_DRAFT,
                        channel.id,
                        utc_now_iso(),
                    ),
                )
                activity_id = int(cursor.lastrowid)
                for role in roles:
                    cursor.execute(
                        """
                        INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (activity_id, role["key"], role["name"], role["slots"], role["emoji"], role["position"]),
                    )
        await self.send_ping_preview(interaction, activity_id)

    async def prompt_edit_activity_draft(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        author_id: int,
    ) -> None:
        activity, error = self.draft_activity_for_user(interaction, activity_id, author_id)
        if activity is None:
            await private_response(interaction, error or "No encontre este borrador.")
            return
        stored_channel_id = int(activity["channel_id"]) if activity["channel_id"] else None
        selected_channel_id = (
            stored_channel_id
            if stored_channel_id is not None
            and self.can_author_choose_ping_channel(interaction.guild.id, interaction.user.id)
            and stored_channel_id in self.approved_ping_channel_ids(interaction.guild.id)
            else None
        )
        if is_mandatory_activity(activity):
            await interaction.response.send_modal(
                MandatoryActivityModal(
                    self,
                    default_voice_channel_id=activity["voice_channel_id"],
                    default_horario=str(activity["horario"] or ""),
                    default_description=str(activity["notes"] or ""),
                    default_image_url=str(activity["image_url"] or ""),
                publish_channel_id=selected_channel_id,
                    draft_id=activity_id,
                )
            )
            return
        await interaction.response.send_modal(
            ActivityModal(
                self,
                template_id=activity["template_id"],
                default_name=str(activity["name"] or ""),
                default_time=str(activity["horario"] or ""),
                default_notes=str(activity["notes"] or ""),
                default_voice_channel_id=activity["voice_channel_id"],
                publish_channel_id=selected_channel_id,
                draft_id=activity_id,
                default_roles=self.activity_roles_input_text(activity_id),
            )
        )

    async def cancel_activity_draft(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        author_id: int,
    ) -> None:
        activity, error = self.draft_activity_for_user(interaction, activity_id, author_id)
        if activity is None:
            await private_response(interaction, error or "No encontre este borrador.")
            return
        self.db.execute(
            """
            UPDATE activities
            SET status = ?, deleted_by = ?, deleted_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (ACTIVITY_DELETED, interaction.user.id, utc_now_iso(), interaction.guild.id, activity_id),
        )
        await interaction.response.edit_message(content="Creacion de ping cancelada. No se publico nada.", embeds=[], view=None)

    async def publish_activity_draft(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        author_id: int,
    ) -> None:
        activity, error = self.draft_activity_for_user(interaction, activity_id, author_id)
        if activity is None:
            await private_response(interaction, error or "No encontre este borrador.")
            return
        channel = interaction.guild.get_channel(int(activity["channel_id"])) if activity["channel_id"] else None
        if channel is None or not callable(getattr(channel, "send", None)):
            await private_response(interaction, "El canal elegido ya no existe o no permite publicar.")
            return
        await interaction.response.defer(ephemeral=True)
        self.db.execute(
            "UPDATE activities SET status = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_OPEN, interaction.guild.id, activity_id),
        )
        try:
            message = await channel.send(
                embeds=self.build_activity_embeds(activity_id),
                view=ActivityView(self, activity_id),
            )
        except discord.HTTPException as exc:
            self.db.execute(
                "UPDATE activities SET status = ? WHERE guild_id = ? AND id = ?",
                (ACTIVITY_DRAFT, interaction.guild.id, activity_id),
            )
            await interaction.followup.send(f"No pude publicar el ping: {exc}", ephemeral=True)
            return
        self.db.execute(
            "UPDATE activities SET message_id = ?, channel_id = ? WHERE id = ?",
            (message.id, channel.id, activity_id),
        )
        thread_panel_created = await self.create_ping_thread(message, activity)
        self.bot.add_view(ActivityView(self, activity_id))
        if thread_panel_created:
            self.bot.add_view(ActivityThreadPanelView(self, activity_id))
        action = "Crear Ping Mandatory" if is_mandatory_activity(activity) else "Crear Ping"
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action=action,
            system="Actividades",
            observation=str(activity["code"]),
        )
        if is_mandatory_activity(activity):
            await send_admin_notification(
                self.db,
                guild=interaction.guild,
                category="activities",
                content=(
                    f"Ping Mandatory `{activity['code']}` creado por <@{interaction.user.id}> "
                    f"para <#{activity['voice_channel_id']}> en <#{channel.id}>."
                ),
            )
        else:
            await send_admin_notification(
                self.db,
                guild=interaction.guild,
                category="activities",
                content=(
                    f"📍 Ping `{activity['code']}` creado por <@{interaction.user.id}>: "
                    f"**{activity['name']}** en <#{channel.id}>."
                ),
            )
        await interaction.edit_original_response(
            content=f"Ping publicado en <#{channel.id}>: `{activity['code']}`.",
            embeds=[],
            view=None,
        )

    def activity_thread_panel_text(self, activity_id: int) -> str:
        activity = self.get_activity(activity_id)
        if activity is None:
            return "Panel del ping."
        return (
            f"Panel del ping `{activity['code']}`. "
            "Usa estos botones para participar, salir o consultar participantes."
        )

    async def send_activity_thread_panel(self, thread, activity_id: int):
        message = await thread.send(
            content=self.activity_thread_panel_text(activity_id),
            view=ActivityThreadPanelView(self, activity_id),
        )
        self.db.execute(
            """
            UPDATE activities
            SET thread_id = ?, thread_panel_message_id = ?
            WHERE id = ?
            """,
            (int(thread.id), int(message.id), activity_id),
        )
        return message

    async def create_ping_thread(self, message: discord.Message, activity) -> bool:
        activity_id = int(activity["id"])
        activity_code = str(activity["code"])
        try:
            thread = await message.create_thread(name=f"Ping {activity_code}")
        except discord.HTTPException:
            LOGGER.exception("No pude crear el hilo automatico para el ping %s.", activity_code)
            return False
        self.db.execute(
            "UPDATE activities SET thread_id = ? WHERE id = ?",
            (int(thread.id), activity_id),
        )
        try:
            await thread.send(PING_THREAD_MESSAGE)
            await self.send_activity_thread_panel(thread, activity_id)
            return True
        except discord.HTTPException:
            LOGGER.exception("No pude publicar el panel funcional del hilo para el ping %s.", activity_code)
            return False

    async def publish_mandatory_activity_from_modal(
        self,
        interaction: discord.Interaction,
        modal: MandatoryActivityModal,
    ) -> None:
        await self.save_mandatory_draft_from_modal(interaction, modal)

    async def publish_activity_from_modal(
        self,
        interaction: discord.Interaction,
        modal: ActivityModal,
    ) -> None:
        await self.save_activity_draft_from_modal(interaction, modal)

    def get_editable_template(self, interaction: discord.Interaction, template_id: int):
        if interaction.guild is None:
            return None
        if is_admin_subject(self.db, interaction):
            return self.db.fetch_one(
                "SELECT * FROM templates WHERE id = ? AND guild_id = ?",
                (template_id, interaction.guild.id),
            )
        return self.db.fetch_one(
            """
            SELECT *
            FROM templates
            WHERE id = ? AND guild_id = ? AND created_by = ?
            """,
            (template_id, interaction.guild.id, interaction.user.id),
        )

    def get_template_roles(self, template_id: int):
        return self.db.fetch_all(
            "SELECT * FROM template_roles WHERE template_id = ? ORDER BY position ASC",
            (template_id,),
        )

    def build_template_preview_embed(self, template, *, title: str = "Vista previa de plantilla") -> discord.Embed:
        roles = self.get_template_roles(int(template["id"]))
        total_slots = sum(max(0, int(row["slots"])) for row in roles)
        visibility = "Publica" if int(template["publica"]) else "Privada"
        voice_text = f"<#{template['voice_channel_id']}>" if template["voice_channel_id"] else "Sin canal"
        image_text = "Configurada" if template["image_url"] else "Sin imagen"
        roles_text = "\n".join(
            f"{row['emoji'] or ''} **{row['name']}** [{row['slots']}]".strip()
            for row in roles
        ) or "Sin composicion"
        embed = discord.Embed(
            title=title,
            description=str(template["description"] or "Sin observaciones"),
            color=ACTIVITY_EMBED_COLOR,
        )
        embed.add_field(name="🆔 ID", value=str(template["id"]), inline=True)
        embed.add_field(name="📌 Plantilla", value=str(template["name"]), inline=True)
        embed.add_field(name="⚔️ Actividad", value=str(template["activity_name"]), inline=True)
        embed.add_field(name="\U0001F552 Hora Albion", value=str(template["default_time"]), inline=True)
        embed.add_field(name="🔊 Voz", value=voice_text, inline=True)
        embed.add_field(name="👥 Cupo máximo", value=str(total_slots), inline=True)
        embed.add_field(name="🌐 Visibilidad", value=visibility, inline=True)
        embed.add_field(name="🖼️ Imagen", value=image_text, inline=True)
        embed.add_field(name="👤 Creador", value=f"<@{template['created_by']}>", inline=True)
        embed.add_field(name="Composición", value=roles_text[:1024], inline=False)
        if template["image_url"]:
            embed.set_image(url=str(template["image_url"]))
        return embed

    async def show_template_edit_panel(
        self,
        interaction: discord.Interaction,
        template_id: int,
        content: str,
        *,
        edit_current: bool = False,
    ) -> None:
        template = self.get_editable_template(interaction, template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        embed = self.build_template_preview_embed(template, title="Vista previa actualizada")
        embed = resolve_custom_emojis_in_embed(embed, interaction.guild) or embed
        view = TemplateEditManageView(self, template)
        if edit_current and not interaction.response.is_done():
            await interaction.response.edit_message(content=content, embed=embed, view=view)
            return
        await private_response(interaction, content, embed=embed, view=view)

    def log_template_edit(
        self,
        guild_id: int,
        user_id: int,
        template_id: int,
        template_name: str,
        details: str,
    ) -> None:
        edited_at = utc_now_iso()
        log_action(
            self.db,
            guild_id,
            admin_id=user_id,
            action="Editar plantilla",
            system="Actividades",
            observation=(
                f"template_id={template_id}; name={template_name}; "
                f"{details}; edited_at={edited_at}"
            ),
        )

    async def update_template_from_modal(
        self,
        interaction: discord.Interaction,
        modal: EditTemplateModal,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Este editor solo funciona dentro del servidor.")
            return
        template = self.get_editable_template(interaction, modal.template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
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
        if not template_name or not activity_name or not default_time:
            await private_response(interaction, "Nombre, actividad y hora son obligatorios.")
            return
        if not description:
            await private_response(interaction, "La descripcion de la plantilla es obligatoria.")
            return
        with self.db.transaction() as cursor:
            cursor.execute(
                """
                UPDATE templates
                SET name = ?, activity_name = ?, default_time = ?, description = ?
                WHERE id = ? AND guild_id = ?
                """,
                (
                    template_name,
                    activity_name,
                    default_time,
                    description,
                    modal.template_id,
                    interaction.guild.id,
                ),
            )
            cursor.execute("DELETE FROM template_roles WHERE template_id = ?", (modal.template_id,))
            for role in roles:
                cursor.execute(
                    """
                    INSERT INTO template_roles (template_id, key, name, slots, emoji, position)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        modal.template_id,
                        role["key"],
                        role["name"],
                        role["slots"],
                        role["emoji"],
                        role["position"],
                    ),
                )
        self.log_template_edit(
            interaction.guild.id,
            interaction.user.id,
            modal.template_id,
            template_name,
            f"campos=datos/composicion; roles={len(roles)}; cupos={sum(int(role['slots']) for role in roles)}",
        )
        await self.show_template_edit_panel(
            interaction,
            modal.template_id,
            "Plantilla actualizada. Vista previa:",
        )

    async def update_template_voice_channel(
        self,
        interaction: discord.Interaction,
        template_id: int,
        voice_channel_id: int,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Este editor solo funciona dentro del servidor.")
            return
        template = self.get_editable_template(interaction, template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        self.db.execute(
            "UPDATE templates SET voice_channel_id = ? WHERE id = ? AND guild_id = ?",
            (voice_channel_id, template_id, interaction.guild.id),
        )
        self.log_template_edit(
            interaction.guild.id,
            interaction.user.id,
            template_id,
            str(template["name"]),
            f"campo=voice_channel_id; voice_channel_id={voice_channel_id}",
        )
        await self.show_template_edit_panel(
            interaction,
            template_id,
            f"Canal de voz actualizado a <#{voice_channel_id}>.",
            edit_current=True,
        )

    async def update_template_visibility(
        self,
        interaction: discord.Interaction,
        template_id: int,
        publica: bool,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Este editor solo funciona dentro del servidor.")
            return
        template = self.get_editable_template(interaction, template_id)
        if template is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        publica_value = 1 if publica else 0
        self.db.execute(
            "UPDATE templates SET publica = ? WHERE id = ? AND guild_id = ?",
            (publica_value, template_id, interaction.guild.id),
        )
        visibility = "publica" if publica else "privada"
        self.log_template_edit(
            interaction.guild.id,
            interaction.user.id,
            template_id,
            str(template["name"]),
            f"campo=publica; visibilidad={visibility}",
        )
        await self.show_template_edit_panel(
            interaction,
            template_id,
            f"Plantilla marcada como **{visibility}**.",
            edit_current=True,
        )

    def get_template_image_url(self, activity) -> str:
        template_id = activity["template_id"]
        if not template_id:
            return ""
        template = self.db.fetch_one(
            "SELECT image_url FROM templates WHERE id = ? AND guild_id = ?",
            (int(template_id), int(activity["guild_id"])),
        )
        if template is None:
            return ""
        return str(template["image_url"] or "").strip()

    async def refresh_template_activity_messages(self, template_id: int) -> None:
        rows = self.db.fetch_all(
            """
            SELECT id
            FROM activities
            WHERE template_id = ? AND message_id IS NOT NULL
              AND status IN (?, ?, ?, ?)
            """,
            (
                template_id,
                ACTIVITY_OPEN,
                ACTIVITY_NOTICE,
                ACTIVITY_IN_PROGRESS,
                ACTIVITY_FINISHED,
            ),
        )
        for row in rows:
            await self.update_activity_message(int(row["id"]))

    async def set_template_image_url(
        self,
        interaction: discord.Interaction,
        template_id: int,
        image_url: str | None,
        *,
        edit_current: bool = False,
    ) -> None:
        template = self.get_editable_template(interaction, template_id)
        if template is None or interaction.guild is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return
        self.db.execute(
            "UPDATE templates SET image_url = ? WHERE id = ? AND guild_id = ?",
            (image_url, template_id, interaction.guild.id),
        )
        self.log_template_edit(
            interaction.guild.id,
            interaction.user.id,
            template_id,
            str(template["name"]),
            "campo=image_url; estado=" + ("configurada" if image_url else "quitada"),
        )
        await self.refresh_template_activity_messages(template_id)
        message = "Imagen de composicion actualizada." if image_url else "Imagen de composicion quitada."
        await self.show_template_edit_panel(
            interaction,
            template_id,
            message,
            edit_current=edit_current,
        )

    async def capture_template_image_message(
        self,
        interaction: discord.Interaction,
        *,
        draft_view: TemplateVisibilityView | None = None,
        template_id: int | None = None,
    ) -> None:
        if interaction.channel is None:
            await private_response(interaction, "No pude identificar el canal para recibir la imagen.")
            return
        if draft_view is not None:
            if not await draft_view.require_author(interaction):
                return
        elif template_id is None or self.get_editable_template(interaction, template_id) is None:
            await private_response(interaction, "No encontre una plantilla editable para ti.")
            return

        await private_response(
            interaction,
            "Envia una imagen adjunta o una URL de imagen en este canal durante los proximos 90 segundos.",
        )

        def check(message: discord.Message) -> bool:
            return (
                message.author.id == interaction.user.id
                and message.channel.id == interaction.channel.id
                and (bool(message.attachments) or bool(str(message.content).strip()))
            )

        try:
            message = await self.bot.wait_for("message", timeout=90, check=check)
        except asyncio.TimeoutError:
            await interaction.followup.send("No recibi ninguna imagen a tiempo.", ephemeral=True)
            return

        raw_image_url = image_url_from_attachments(message.attachments) or str(message.content).strip()
        try:
            image_url = parse_template_image_url(raw_image_url)
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if draft_view is not None:
            draft_view.image_url = image_url
            await interaction.followup.send(
                "Imagen de composicion guardada para esta plantilla.",
                ephemeral=True,
            )
            return
        await self.set_template_image_url(interaction, int(template_id), image_url)

    def build_mandatory_activity_embeds(self, activity_id: int, *, preview_status: str | None = None) -> list[discord.Embed]:
        activity = self.get_activity(activity_id)
        participants = self.get_activity_participants(activity_id)
        voice_text = f"<#{activity['voice_channel_id']}>" if activity["voice_channel_id"] else "Sin canal"
        status = activity_status_label(str(preview_status or activity["status"]))
        status_icon, _, status_name = status.partition(" ")
        status_name = status_name or status
        notes = str(activity["notes"] or "Sin descripcion.").strip()
        loot = activity["mandatory_loot_amount"]
        loot_text = format_amount(int(loot)) if loot is not None else "No registrado"
        participant_names = [
            " ".join(str(participant["display_name"] or f"Usuario {participant['user_id']}").split())
            for participant in participants
        ]
        participants_text = ", ".join(participant_names) if participant_names else "Sin participantes todavia"
        if len(participants_text) > 1024:
            participants_text = participants_text[:1000].rstrip(", ") + "\n... lista recortada"
        embed = discord.Embed(
            title="\u2694\ufe0f Ping Mandatory",
            description=activity_note_description(notes),
            color=discord.Color.red(),
        )
        embed.add_field(name="\U0001F464 CALLER", value=f"<@{activity['caller_id']}>", inline=True)
        embed.add_field(name="\U0001F50A CANAL", value=voice_text, inline=True)
        embed.add_field(name="\U0001F552 Hora Albion", value=str(activity["horario"]), inline=True)
        embed.add_field(name=f"{status_icon} ESTADO", value=status_name, inline=True)
        embed.add_field(name="\U0001F194 ID", value=str(activity["code"]), inline=True)
        embed.add_field(name="\U0001F4B0 BOTIN", value=loot_text, inline=True)
        embed.add_field(name=f"\U0001F465 PARTICIPANTES: {len(participants)}", value=participants_text, inline=False)
        embed.set_footer(text=MANDATORY_FOOTER_TEXT)
        guild = self.bot.get_guild(int(activity["guild_id"]))
        image_url = str(activity["image_url"] or "").strip()
        if image_url:
            embed.set_image(url=image_url)
        return [resolve_custom_emojis_in_embed(embed, guild) or embed]

    def build_activity_embeds(self, activity_id: int, *, preview_status: str | None = None) -> list[discord.Embed]:
        activity = self.get_activity(activity_id)
        if is_mandatory_activity(activity):
            return self.build_mandatory_activity_embeds(activity_id, preview_status=preview_status)
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
        status = activity_status_label(str(preview_status or activity["status"]))
        status_icon, _, status_name = status.partition(" ")
        status_name = status_name or status
        image_url = self.get_template_image_url(activity)

        composition_fields: list[tuple[str, str, bool]] = []
        if not roles:
            composition_fields.append(("Sin armas configuradas ⚪ 0/0", "▸ Disponible", False))
        for role in roles:
            role_id = int(role["id"])
            names = by_role.get(role_id, [])
            current = len(names)
            required = max(0, int(role["slots"]))
            role_emoji = str(role["emoji"] or "").strip()
            role_name = " ".join(str(role["name"]).split())
            marker = activity_composition_marker(current, required)
            role_prefix = f"{role_emoji} " if role_emoji else ""
            field_name = f"{role_prefix}{role_name} {marker} {current}/{required}"
            field_value = activity_composition_field_value(names)
            composition_fields.append((field_name, field_value, True))

        first_chunk = composition_fields[:ACTIVITY_PRIMARY_COMPOSITION_FIELDS]
        remaining_fields = composition_fields[ACTIVITY_PRIMARY_COMPOSITION_FIELDS:]
        extra_chunks = [
            remaining_fields[index : index + ACTIVITY_COMPOSITION_FIELDS_PER_EMBED]
            for index in range(0, len(remaining_fields), ACTIVITY_COMPOSITION_FIELDS_PER_EMBED)
        ]
        total_chunks = 1 + len(extra_chunks)
        embeds: list[discord.Embed] = []

        embed = discord.Embed(
            title=f"⚔️ {activity_name}",
            description=activity_note_description(notes),
            color=ACTIVITY_EMBED_COLOR,
        )
        embed.add_field(name="👤 Caller", value=f"<@{activity['caller_id']}>", inline=True)
        embed.add_field(name="\U0001F552 Hora Albion", value=str(activity["horario"]), inline=True)
        embed.add_field(name=f"{status_icon} Estado", value=status_name, inline=True)
        embed.add_field(name="🔊 Voz", value=voice_text, inline=True)
        embed.add_field(name="👥 Participantes", value=f"{registered_count}/{required_count}", inline=True)
        embed.add_field(name="🆔 ID", value=str(activity["code"]), inline=True)
        for _ in range(ACTIVITY_GENERAL_TO_COMPOSITION_SPACERS):
            embed.add_field(
                name=ACTIVITY_EMBED_SPACER,
                value=ACTIVITY_EMBED_SPACER,
                inline=False,
            )
        for name, value, inline in first_chunk:
            embed.add_field(name=name, value=value, inline=inline)
        embeds.append(embed)

        for chunk_index, chunk in enumerate(extra_chunks, start=2):
            extra_embed = discord.Embed(
                title=f"⚔️ COMPOSICIÓN ({chunk_index}/{total_chunks})",
                color=ACTIVITY_EMBED_COLOR,
            )
            for name, value, inline in chunk:
                extra_embed.add_field(name=name, value=value, inline=inline)
            embeds.append(extra_embed)

        for embed in embeds:
            embed.set_footer(text=ACTIVITY_FOOTER_TEXT)

        guild = self.bot.get_guild(int(activity["guild_id"]))
        caller = guild.get_member(int(activity["caller_id"])) if guild is not None else None
        if caller is None:
            caller = self.bot.get_user(int(activity["caller_id"]))
        if caller is not None:
            embeds[0].set_thumbnail(url=caller.display_avatar.url)
        if image_url:
            embeds[-1].set_image(url=image_url)

        return [resolve_custom_emojis_in_embed(embed, guild) or embed for embed in embeds]

    def build_activity_embed(self, activity_id: int) -> discord.Embed:
        return self.build_activity_embeds(activity_id)[0]

    async def resolve_activity_thread(self, guild: discord.Guild, thread_id: int):
        thread = None
        get_thread = getattr(guild, "get_thread", None)
        if callable(get_thread):
            thread = get_thread(thread_id)
        if thread is None:
            thread = guild.get_channel(thread_id)
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(thread_id)
            except discord.HTTPException:
                return None
        thread_guild = getattr(thread, "guild", None)
        if thread_guild is not None and thread_guild.id != guild.id:
            return None
        return thread

    async def update_activity_thread_panel(
        self,
        activity_id: int,
        activity=None,
        guild: discord.Guild | None = None,
    ) -> None:
        activity = activity or self.get_activity(activity_id)
        if not activity or not activity["thread_id"]:
            return
        guild = guild or self.bot.get_guild(int(activity["guild_id"]))
        if guild is None:
            return
        thread = await self.resolve_activity_thread(guild, int(activity["thread_id"]))
        if thread is None:
            return
        panel_message_id = activity["thread_panel_message_id"]
        if not panel_message_id:
            if activity["status"] != ACTIVITY_DELETED and hasattr(thread, "send"):
                try:
                    await self.send_activity_thread_panel(thread, activity_id)
                except discord.HTTPException:
                    LOGGER.exception(
                        "No pude recrear el panel funcional del hilo para la actividad %s.",
                        activity_id,
                    )
            return
        if not hasattr(thread, "fetch_message"):
            return
        try:
            message = await thread.fetch_message(int(panel_message_id))
            await message.edit(
                content=self.activity_thread_panel_text(activity_id),
                view=ActivityThreadPanelView(self, activity_id),
            )
        except discord.NotFound:
            if activity["status"] != ACTIVITY_DELETED and hasattr(thread, "send"):
                try:
                    await self.send_activity_thread_panel(thread, activity_id)
                except discord.HTTPException:
                    LOGGER.exception(
                        "No pude recrear el panel funcional perdido del hilo para la actividad %s.",
                        activity_id,
                    )
        except discord.HTTPException:
            return

    async def update_activity_message(self, activity_id: int) -> None:
        activity = self.get_activity(activity_id)
        if not activity:
            return
        guild = self.bot.get_guild(int(activity["guild_id"]))
        if guild is None:
            return
        if activity["channel_id"] and activity["message_id"]:
            channel = guild.get_channel(int(activity["channel_id"]))
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    message = await channel.fetch_message(int(activity["message_id"]))
                    await message.edit(
                        embeds=self.build_activity_embeds(activity_id),
                        view=ActivityView(self, activity_id),
                    )
                except discord.HTTPException:
                    pass
        await self.update_activity_thread_panel(activity_id, activity, guild)

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
            requested_role_name = str(request["requested_role"]).strip()
            requested_weapon = resolve_weapon_alias(requested_role_name)
            if requested_weapon is None:
                role_key = normalize_key(requested_role_name)
                role_name = requested_role_name[:80]
                role_emoji = ""
            else:
                role_key = requested_weapon.key
                role_name = requested_weapon.display_name[:80]
                role_emoji = requested_weapon.emoji
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
                    VALUES (?, ?, ?, 1, ?, ?)
                    """,
                    (
                        int(request["activity_id"]),
                        role_key,
                        role_name,
                        role_emoji,
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
                        "Entra al canal de voz. Si el caller pide check, puedes pulsar **Aqui estoy**; "
                        "si permaneces menos del 50% de la actividad, se aplicara la sancion configurada."
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
        if attendance is None:
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

    def mandatory_participants_text(self, activity_id: int) -> str:
        activity = self.get_activity(activity_id)
        if activity is None:
            return "No encontre esta actividad."
        participants = self.get_activity_participants(activity_id)
        lines = [f"👥 **Participantes - {activity['code']}**"]
        if not participants:
            lines.append("Sin participantes registrados.")
            return "\n".join(lines)
        for index, participant in enumerate(participants, start=1):
            user_id = int(participant["user_id"])
            if activity["status"] in {ACTIVITY_IN_PROGRESS, ACTIVITY_FINISHED}:
                seconds, percent = self.voice_stats(activity_id, user_id)
                minutes = seconds // 60
                lines.append(f"{index}. <@{user_id}> - {minutes} min - {percent:.1f}%")
            else:
                lines.append(f"{index}. <@{user_id}>")
        return "\n".join(lines)[:1900]

    def activity_participants_text(self, activity_id: int) -> str:
        activity = self.get_activity(activity_id)
        if activity is None:
            return "No encontre esta actividad."
        if is_mandatory_activity(activity):
            return self.mandatory_participants_text(activity_id)
        participants = self.get_activity_participants(activity_id)
        lines = [f"**Participantes - {activity['code']}**"]
        if not participants:
            lines.append("Sin participantes registrados.")
            return "\n".join(lines)
        for index, participant in enumerate(participants, start=1):
            user_id = int(participant["user_id"])
            role_emoji = str(participant["role_emoji"] or "").strip()
            role_name = str(participant["role_name"] or "Rol").strip()
            role_label = f"{role_emoji} {role_name}".strip()
            lines.append(f"{index}. <@{user_id}> - {role_label}")
        return "\n".join(lines)[:1900]

    async def participate_from_thread(self, interaction: discord.Interaction, activity_id: int) -> None:
        activity = self.get_guild_activity(interaction.guild.id, activity_id) if interaction.guild else None
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if is_mandatory_activity(activity):
            await self.join_mandatory_activity(interaction, activity_id)
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE}:
            await private_response(interaction, "Las inscripciones ya estan cerradas.")
            return
        roles = self.get_activity_roles(activity_id)
        if not roles:
            await private_response(interaction, "Esta actividad no tiene composicion disponible.")
            return
        await private_response(
            interaction,
            "Elige el rol o arma para participar en este ping.",
            view=ActivityThreadRoleSelectView(self, activity_id),
        )

    async def handle_activity_thread_action(
        self,
        interaction: discord.Interaction,
        action: str,
        activity_id: int,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if activity["status"] == ACTIVITY_DELETED:
            await private_response(interaction, "Este ping fue eliminado y ya no acepta acciones.")
            return
        if action == "participate":
            await self.participate_from_thread(interaction, activity_id)
        elif action == "leave":
            if is_mandatory_activity(activity):
                await self.leave_mandatory_activity(interaction, activity_id)
            else:
                await self.leave_activity(interaction, activity_id)
        elif action == "participants":
            await private_response(interaction, self.activity_participants_text(activity_id))
        else:
            await private_response(interaction, "Accion no reconocida.")

    async def join_mandatory_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        activity = self.get_activity(activity_id)
        if (
            activity is None
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
            or not is_mandatory_activity(activity)
        ):
            await interaction.followup.send("No encontre esta convocatoria Mandatory.", ephemeral=True)
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            await interaction.followup.send("Las inscripciones ya estan cerradas.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member):
            await interaction.followup.send("Esta accion solo funciona dentro del servidor.", ephemeral=True)
            return
        role_id = self.ensure_mandatory_participant_role(activity_id)
        self.db.execute(
            """
            INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(activity_id, user_id)
            DO UPDATE SET display_name = excluded.display_name
            """,
            (activity_id, role_id, interaction.user.id, interaction.user.display_name, utc_now_iso()),
        )
        self.db.execute(
            """
            INSERT INTO asistencia_actividades (
                actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz
            ) VALUES (?, ?, ?, 1, 0)
            ON CONFLICT(actividad_id, usuario_id)
            DO UPDATE SET confirmo_boton = 1
            """,
            (activity_id, interaction.user.id, ATTENDANCE_PENDING),
        )
        if activity["status"] == ACTIVITY_IN_PROGRESS:
            if (
                interaction.user.voice is not None
                and interaction.user.voice.channel is not None
                and activity["voice_channel_id"]
                and interaction.user.voice.channel.id == int(activity["voice_channel_id"])
            ):
                self.start_voice_session(activity_id, interaction.guild.id, interaction.user.id)
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Quedaste registrado en el Ping Mandatory.", ephemeral=True)

    async def leave_mandatory_activity(self, interaction: discord.Interaction, activity_id: int) -> None:
        await interaction.response.defer(ephemeral=True)
        activity = self.get_activity(activity_id)
        if (
            activity is None
            or interaction.guild is None
            or int(activity["guild_id"]) != interaction.guild.id
            or not is_mandatory_activity(activity)
        ):
            await interaction.followup.send("No encontre esta convocatoria Mandatory.", ephemeral=True)
            return
        if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
            await interaction.followup.send("Ya no puedes salir de este Ping Mandatory.", ephemeral=True)
            return
        participant = self.db.fetch_one(
            "SELECT 1 FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        if participant is None:
            await interaction.followup.send("No estabas registrado en este Ping Mandatory.", ephemeral=True)
            return
        if activity["status"] == ACTIVITY_IN_PROGRESS:
            self.close_voice_session(activity_id, interaction.guild.id, interaction.user.id)
        self.db.execute(
            "DELETE FROM activity_participants WHERE activity_id = ? AND user_id = ?",
            (activity_id, interaction.user.id),
        )
        self.db.execute(
            "DELETE FROM asistencia_actividades WHERE actividad_id = ? AND usuario_id = ?",
            (activity_id, interaction.user.id),
        )
        await self.update_activity_message(activity_id)
        await interaction.followup.send("Saliste del Ping Mandatory.", ephemeral=True)

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
                "El check es opcional si el caller lo solicita. Permanece al menos el 50% "
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

    def can_delete_activity_ping(self, interaction: discord.Interaction, activity) -> bool:
        if is_admin_subject(self.db, interaction):
            return True
        return (
            interaction.user.id == int(activity["caller_id"])
            and is_official_caller_subject(self.db, interaction)
        )

    async def delete_or_disable_activity_thread_panel(self, activity, guild: discord.Guild) -> bool:
        if not activity["thread_id"] or not activity["thread_panel_message_id"]:
            return True
        thread = await self.resolve_activity_thread(guild, int(activity["thread_id"]))
        if thread is None or not hasattr(thread, "fetch_message"):
            return True
        try:
            message = await thread.fetch_message(int(activity["thread_panel_message_id"]))
        except discord.NotFound:
            self.db.execute(
                "UPDATE activities SET thread_panel_message_id = NULL WHERE id = ?",
                (int(activity["id"]),),
            )
            return True
        except discord.HTTPException:
            LOGGER.exception(
                "No pude encontrar el panel funcional del hilo para la actividad %s.",
                int(activity["id"]),
            )
            return False
        try:
            await message.delete()
            self.db.execute(
                "UPDATE activities SET thread_panel_message_id = NULL WHERE id = ?",
                (int(activity["id"]),),
            )
            return True
        except discord.Forbidden:
            try:
                await message.edit(
                    content=self.activity_thread_panel_text(int(activity["id"])),
                    view=ActivityThreadPanelView(self, int(activity["id"]), force_disabled=True),
                )
                return True
            except discord.HTTPException:
                LOGGER.exception(
                    "No pude desactivar el panel funcional del hilo para la actividad %s.",
                    int(activity["id"]),
                )
                return False
        except discord.NotFound:
            self.db.execute(
                "UPDATE activities SET thread_panel_message_id = NULL WHERE id = ?",
                (int(activity["id"]),),
            )
            return True
        except discord.HTTPException:
            LOGGER.exception(
                "No pude eliminar el panel funcional del hilo para la actividad %s.",
                int(activity["id"]),
            )
            return False

    async def delete_activity_ping(self, interaction: discord.Interaction, activity_id: int) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if activity is None:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if activity["status"] == ACTIVITY_DELETED:
            await private_response(interaction, "Este ping ya fue eliminado.")
            return
        if not self.can_delete_activity_ping(interaction, activity):
            await private_response(interaction, "Solo el caller oficial creador o un admin puede eliminar este ping.")
            return
        await interaction.response.defer(ephemeral=True)
        deleted_at = utc_now_iso()
        if activity["status"] == ACTIVITY_IN_PROGRESS:
            tracked_users = {
                int(row["user_id"]) for row in self.get_activity_participants(activity_id)
            }
            if not is_mandatory_activity(activity):
                tracked_users.add(int(activity["caller_id"]))
            for user_id in tracked_users:
                self.close_voice_session(activity_id, interaction.guild.id, user_id, deleted_at)
        if not await self.delete_or_disable_activity_thread_panel(activity, interaction.guild):
            await interaction.followup.send(
                "No pude eliminar o desactivar el panel publicado dentro del hilo.",
                ephemeral=True,
            )
            return
        message_deleted = False
        if activity["channel_id"] and activity["message_id"]:
            channel = interaction.guild.get_channel(int(activity["channel_id"]))
            if channel is not None and hasattr(channel, "fetch_message"):
                try:
                    message = await channel.fetch_message(int(activity["message_id"]))
                    await message.delete()
                    message_deleted = True
                except discord.NotFound:
                    message_deleted = True
                except discord.Forbidden:
                    await interaction.followup.send(
                        "No pude eliminar el mensaje: me falta permiso en ese canal.",
                        ephemeral=True,
                    )
                    return
                except discord.HTTPException as exc:
                    await interaction.followup.send(
                        f"No pude eliminar el mensaje: {exc}",
                        ephemeral=True,
                    )
                    return
            else:
                message_deleted = True
        else:
            message_deleted = True
        if not message_deleted:
            await interaction.followup.send("No pude confirmar la eliminacion del mensaje.", ephemeral=True)
            return
        self.db.execute(
            """
            UPDATE activities
            SET status = ?, deleted_by = ?, deleted_at = ?, message_id = NULL,
                ended_at = COALESCE(ended_at, ?)
            WHERE guild_id = ? AND id = ?
            """,
            (ACTIVITY_DELETED, interaction.user.id, deleted_at, deleted_at, interaction.guild.id, activity_id),
        )
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Eliminar ping publicado",
            system="Actividades",
            affected_user_id=int(activity["caller_id"]),
            observation=str(activity["code"]),
        )
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=f"Ping `{activity['code']}` eliminado por <@{interaction.user.id}>.",
        )
        await interaction.followup.send("Ping eliminado del canal y marcado como eliminado.", ephemeral=True)
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
        if action == "delete":
            await self.delete_activity_ping(interaction, activity_id)
            return
        if activity["status"] == ACTIVITY_DELETED:
            await private_response(interaction, "Este ping fue eliminado y ya no acepta acciones.")
            return
        if action == "mandatory_join":
            await self.join_mandatory_activity(interaction, activity_id)
            return
        if action == "mandatory_leave":
            await self.leave_mandatory_activity(interaction, activity_id)
            return
        if action == "mandatory_participants":
            await private_response(interaction, self.mandatory_participants_text(activity_id))
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
            if is_mandatory_activity(activity):
                await private_response(interaction, "El Ping Mandatory no usa Split. Registra el Botin.")
                return
            await interaction.response.send_modal(PayoutModal(self, activity_id))
            return
        if action == "mandatory_loot":
            if not is_mandatory_activity(activity):
                await private_response(interaction, "Esta accion solo aplica a Ping Mandatory.")
                return
            await interaction.response.send_modal(MandatoryLootModal(self, activity_id))
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
        if is_mandatory_activity(activity):
            if activity["status"] not in {ACTIVITY_OPEN, ACTIVITY_NOTICE, ACTIVITY_IN_PROGRESS}:
                await interaction.followup.send("El aviso ya no esta disponible para este Ping Mandatory.", ephemeral=True)
                return
            if activity["status"] == ACTIVITY_OPEN:
                self.db.execute(
                    "UPDATE activities SET status = ? WHERE id = ?",
                    (ACTIVITY_NOTICE, activity_id),
                )
            participants = self.get_activity_participants(activity_id)
            voice_text = (
                f"<#{activity['voice_channel_id']}>"
                if activity["voice_channel_id"]
                else "el canal de voz indicado"
            )
            for participant in participants:
                member = interaction.guild.get_member(int(participant["user_id"]))
                if member:
                    await send_dm_safe(
                        self.db,
                        guild_id=interaction.guild.id,
                        user=member,
                        action="aviso_mandatory",
                        content=(
                            f"La convocatoria **Ping Mandatory** `{activity['code']}` esta activa. "
                            f"Entra a {voice_text} para que tu asistencia se mida por presencia en voz."
                        ),
                    )
            await self.update_activity_message(activity_id)
            await interaction.followup.send("Aviso Mandatory enviado por DM a los participantes.", ephemeral=True)
            return
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

    async def start_mandatory_activity(self, interaction: discord.Interaction, activity_id: int, activity) -> None:
        if not activity["voice_channel_id"]:
            await interaction.followup.send("Configura un canal de voz antes de iniciar.", ephemeral=True)
            return
        self.audit_admin_activity_action(interaction, activity, "iniciar mandatory")
        started_at = utc_now_iso()
        self.db.execute(
            "UPDATE activities SET status = ?, started_at = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_IN_PROGRESS, started_at, interaction.guild.id, activity_id),
        )
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            user_id = int(participant["user_id"])
            member = interaction.guild.get_member(user_id)
            in_voice = bool(
                member is not None
                and member.voice is not None
                and member.voice.channel is not None
                and member.voice.channel.id == int(activity["voice_channel_id"])
            )
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz, fecha_check
                ) VALUES (?, ?, ?, 1, ?, ?)
                ON CONFLICT(actividad_id, usuario_id)
                DO UPDATE SET confirmo_boton = 1,
                              confirmo_voz = excluded.confirmo_voz,
                              fecha_check = excluded.fecha_check
                """,
                (activity_id, user_id, ATTENDANCE_PENDING, 1 if in_voice else 0, started_at),
            )
            if in_voice:
                self.start_voice_session(activity_id, interaction.guild.id, user_id)
        await self.update_activity_message(activity_id)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"Ping Mandatory `{activity['code']}` iniciado por <@{interaction.user.id}>. "
                f"Participantes registrados: {len(participants)}."
            ),
        )
        await interaction.followup.send("Ping Mandatory iniciado. La asistencia se medira por voz.", ephemeral=True)

    async def finish_mandatory_activity(self, interaction: discord.Interaction, activity_id: int, activity) -> None:
        self.audit_admin_activity_action(interaction, activity, "finalizar mandatory")
        ended_at = utc_now_iso()
        participants = self.get_activity_participants(activity_id)
        for participant in participants:
            user_id = int(participant["user_id"])
            self.close_voice_session(activity_id, interaction.guild.id, user_id, ended_at)
        self.db.execute(
            "UPDATE activities SET status = ?, ended_at = ? WHERE guild_id = ? AND id = ?",
            (ACTIVITY_FINISHED, ended_at, interaction.guild.id, activity_id),
        )
        confirmed = 0
        absent = 0
        for participant in participants:
            user_id = int(participant["user_id"])
            voice_seconds, participation_percent = self.voice_stats(activity_id, user_id, ended_at)
            attendance_state = ATTENDANCE_CONFIRMED if voice_seconds > 0 else ATTENDANCE_ABSENT
            if attendance_state == ATTENDANCE_CONFIRMED:
                confirmed += 1
            else:
                absent += 1
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz,
                    fecha_check, voice_seconds, participation_percent
                ) VALUES (?, ?, ?, 1, ?, ?, ?, ?)
                ON CONFLICT(actividad_id, usuario_id)
                DO UPDATE SET estado = excluded.estado,
                              confirmo_boton = 1,
                              confirmo_voz = excluded.confirmo_voz,
                              voice_seconds = excluded.voice_seconds,
                              participation_percent = excluded.participation_percent
                """,
                (
                    activity_id,
                    user_id,
                    attendance_state,
                    1 if voice_seconds > 0 else 0,
                    ended_at,
                    voice_seconds,
                    participation_percent,
                ),
            )
        await self.update_activity_message(activity_id)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"Ping Mandatory `{activity['code']}` finalizado por <@{interaction.user.id}>. "
                f"Confirmados por voz: {confirmed}. Sin voz: {absent}."
            ),
        )
        await interaction.followup.send(
            f"Ping Mandatory finalizado. Confirmados por voz: {confirmed}. Sin voz: {absent}. Ahora puedes registrar Botin.",
            ephemeral=True,
        )

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
        if is_mandatory_activity(activity):
            await self.start_mandatory_activity(interaction, activity_id, activity)
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
        if is_mandatory_activity(activity):
            await interaction.followup.send(
                "El Ping Mandatory no usa check manual; la asistencia se mide por voz.",
                ephemeral=True,
            )
            return
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
            "Check opcional enviado por DM. Puedes iniciar o continuar la actividad sin bloquear procesos.",
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
            if row and row["estado"] == ATTENDANCE_CONFIRMED:
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
            *block("Confirmados por voz", confirmed),
            "",
            *block("Dieron check pero no cumplen voz", checked_absent),
            "",
            *block("Sin check registrado", pending),
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
        if is_mandatory_activity(activity):
            await private_response(
                interaction,
                "El Ping Mandatory no usa check manual; participa y permanece en el canal de voz.",
            )
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
        if is_mandatory_activity(activity):
            await self.finish_mandatory_activity(interaction, activity_id, activity)
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
                if participation_percent >= minimum_percent
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
                if caller_percent >= minimum_percent
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
        is_mandatory = is_mandatory_activity(activity)
        cancelled_by_admin = (
            interaction.user.id != int(activity["caller_id"])
            and is_admin_subject(self.db, interaction)
        )
        if is_mandatory:
            reputation_exempt = True
            cancellation_reason = (
                "Ping Mandatory cancelado por un administrador."
                if cancelled_by_admin
                else "Ping Mandatory cancelado."
            )
        else:
            required_slots, registered_slots, reputation_exempt = cancellation_capacity(
                self.db,
                interaction.guild.id,
                activity_id,
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
            }
            if not is_mandatory:
                tracked_users.add(int(activity["caller_id"]))
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
        if not is_mandatory:
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

    async def save_mandatory_loot_from_modal(
        self,
        interaction: discord.Interaction,
        activity_id: int,
        modal: MandatoryLootModal,
    ) -> None:
        if interaction.guild is None:
            await private_response(interaction, "Esta accion solo esta disponible en un servidor.")
            return
        activity = self.get_guild_activity(interaction.guild.id, activity_id)
        if not activity:
            await private_response(interaction, "No encontre esta actividad en este servidor.")
            return
        if not is_mandatory_activity(activity):
            await private_response(interaction, "Esta accion solo aplica a Ping Mandatory.")
            return
        if not await self.require_activity_manager(interaction, activity, "registrar botin"):
            return
        if activity["status"] != ACTIVITY_FINISHED:
            await private_response(interaction, "Solo puedes registrar Botin cuando el Ping Mandatory este finalizado.")
            return
        try:
            amount = parse_mandatory_loot_amount(str(modal.loot.value))
        except ValueError as exc:
            await private_response(interaction, str(exc))
            return
        recorded_at = utc_now_iso()
        self.db.execute(
            """
            UPDATE activities
            SET mandatory_loot_amount = ?,
                mandatory_loot_recorded_by = ?,
                mandatory_loot_recorded_at = ?
            WHERE guild_id = ? AND id = ?
            """,
            (amount, interaction.user.id, recorded_at, interaction.guild.id, activity_id),
        )
        log_action(
            self.db,
            interaction.guild.id,
            admin_id=interaction.user.id,
            action="Registrar Botin Mandatory",
            system="Actividades",
            observation=f"{activity['code']} {amount}",
        )
        await self.update_activity_message(activity_id)
        await send_admin_notification(
            self.db,
            guild=interaction.guild,
            category="activities",
            content=(
                f"Botin de Ping Mandatory `{activity['code']}` registrado por <@{interaction.user.id}>: "
                f"{format_amount(amount)}."
            ),
        )
        await private_response(interaction, f"Botin registrado: {format_amount(amount)}.")

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
        if is_mandatory_activity(activity):
            await private_response(interaction, "El Ping Mandatory no usa Split. Registra el Botin.")
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
