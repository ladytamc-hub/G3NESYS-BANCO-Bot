from __future__ import annotations

import discord

from ..constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_PAYOUT_CREATED,
    ACTIVITY_TYPE_MANDATORY,
    ATTENDANCE_ABSENT,
    ATTENDANCE_CONFIRMED,
    CALLERS_WELCOME_IMAGE,
    PAYOUT_APPROVED,
    PAYOUT_DEPOSITED,
)
from ..database import Database
from ..utils import utc_now_iso
from .audit import log_action
from .notifications import send_dm_safe


CALLER_PENALTY_THRESHOLD = -14


def authorize_caller(
    db: Database,
    guild_id: int,
    user_id: int,
    added_by: int,
) -> bool:
    existing = db.fetch_one(
        "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    if existing is not None:
        return False
    db.execute(
        """
        INSERT INTO callers (guild_id, user_id, added_by, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (guild_id, user_id, added_by, utc_now_iso()),
    )
    return True


def revoke_caller(db: Database, guild_id: int, user_id: int) -> bool:
    existing = db.fetch_one(
        "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    if existing is None:
        return False
    db.execute(
        "DELETE FROM callers WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    return True


def is_caller_penalized(db: Database, guild_id: int, user_id: int) -> bool:
    return db.fetch_one(
        """
        SELECT 1
        FROM caller_penalties
        WHERE guild_id = ? AND user_id = ? AND active = 1
        """,
        (guild_id, user_id),
    ) is not None


def remove_caller_penalty(
    db: Database,
    guild_id: int,
    user_id: int,
    removed_by: int,
) -> bool:
    penalty = db.fetch_one(
        """
        SELECT 1
        FROM caller_penalties
        WHERE guild_id = ? AND user_id = ? AND active = 1
        """,
        (guild_id, user_id),
    )
    if penalty is None:
        return False
    db.execute(
        """
        UPDATE caller_penalties
        SET active = 0, removed_by = ?, removed_at = ?
        WHERE guild_id = ? AND user_id = ? AND active = 1
        """,
        (removed_by, utc_now_iso(), guild_id, user_id),
    )
    return True


def cancellation_capacity(
    db: Database,
    guild_id: int,
    activity_id: int,
) -> tuple[int, int, bool]:
    row = db.fetch_one(
        """
        SELECT
            COALESCE((
                SELECT SUM(slots)
                FROM activity_roles
                WHERE activity_id = activities.id
            ), 0) AS required_slots,
            COALESCE((
                SELECT COUNT(*)
                FROM activity_participants
                WHERE activity_id = activities.id
            ), 0) AS registered_slots
        FROM activities
        WHERE guild_id = ? AND id = ?
        """,
        (guild_id, activity_id),
    )
    if row is None:
        return 0, 0, False
    required = int(row["required_slots"])
    registered = int(row["registered_slots"])
    return required, registered, required > registered


def caller_welcome_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="📣 Bienvenido a la familia de Callers de G3NESYS",
        description=(
            "Has sido elegido como **caller de G3NESYS**. Tu liderazgo ayudara a que "
            "las actividades sean organizadas, claras y justas para todos."
        ),
        color=discord.Color.magenta(),
    )
    embed.add_field(
        name="⚔️ Tus funciones",
        value=(
            "• Usar el Panel de Callers.\n"
            "• Crear plantillas y crear pings.\n"
            "• Administrar cupos, avisos y asistencia.\n"
            "• Generar Splits y enviarlos a revision administrativa."
        ),
        inline=False,
    )
    embed.add_field(
        name="📜 Responsabilidades del caller",
        value=(
            "• Asistir a las actividades que elijas organizar o en las que te registres.\n"
            "• Dirigir con respeto, orden e imparcialidad.\n"
            "• Verificar asistencia y canal de voz antes de cerrar la actividad.\n"
            "• Revisar participantes y porcentajes antes de enviar un Split.\n"
            "• Avisar con claridad si una actividad debe cambiarse o cancelarse."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ Tu acceso",
        value=(
            "Ya puedes usar el **Panel de Callers** de G3NESYS. "
            "Si necesitas publicarlo, utiliza `!panel_pings` en el canal correspondiente."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"{guild_name} • Tu liderazgo representa a G3NESYS. Gracias por asumir esta responsabilidad."
    )
    embed.set_image(url=CALLERS_WELCOME_IMAGE)
    return embed


def caller_removal_embed(guild_name: str) -> discord.Embed:
    embed = discord.Embed(
        title="👋 Cambio en tu acceso de Caller",
        description=(
            "Se te ha dado de baja de la lista de **callers autorizados de G3NESYS**. "
            "Agradecemos el tiempo, liderazgo y apoyo que brindaste a las actividades."
        ),
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="💛 Sigues siendo parte de la comunidad",
        value=(
            "Este cambio solo retira el acceso para crear y dirigir actividades como caller. "
            "Puedes seguir participando normalmente en las actividades del gremio."
        ),
        inline=False,
    )
    embed.add_field(
        name="📩 Si tienes dudas",
        value="Puedes comunicarte con el equipo administrativo para conocer más detalles.",
        inline=False,
    )
    embed.set_footer(text=f"{guild_name} • Gracias por tu apoyo a G3NESYS.")
    return embed


class CallerRemovalNoticeView(discord.ui.View):
    def __init__(
        self,
        db: Database,
        *,
        guild_id: int,
        guild_name: str,
        admin_id: int,
        member: discord.Member,
    ):
        super().__init__(timeout=180)
        self.db = db
        self.guild_id = guild_id
        self.guild_name = guild_name
        self.admin_id = admin_id
        self.member = member

    async def require_admin(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.admin_id:
            return True
        await interaction.response.send_message(
            "Solo el admin que elimino al caller puede elegir esta opcion.",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="Enviar aviso", emoji="📨", style=discord.ButtonStyle.primary)
    async def send_notice(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        delivered = await send_dm_safe(
            self.db,
            guild_id=self.guild_id,
            user=self.member,
            action="aviso_baja_caller",
            embed=caller_removal_embed(self.guild_name),
        )
        result = (
            f"📨 {self.member.mention} ya no es caller autorizado. Aviso enviado por DM."
            if delivered
            else f"⚠️ {self.member.mention} fue eliminado, pero no pude enviarle el DM."
        )
        log_action(
            self.db,
            self.guild_id,
            admin_id=self.admin_id,
            action="Aviso de baja de caller",
            affected_user_id=self.member.id,
            system="Callers",
            observation="DM enviado." if delivered else "Discord rechazo el DM.",
        )
        await interaction.response.edit_message(content=result, view=None)

    @discord.ui.button(label="No enviar aviso", emoji="🔕", style=discord.ButtonStyle.secondary)
    async def skip_notice(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not await self.require_admin(interaction):
            return
        log_action(
            self.db,
            self.guild_id,
            admin_id=self.admin_id,
            action="Omitir aviso de baja de caller",
            affected_user_id=self.member.id,
            system="Callers",
            observation="El admin eligio no enviar DM.",
        )
        await interaction.response.edit_message(
            content=f"🔕 {self.member.mention} ya no es caller autorizado. No se envio ningun aviso.",
            view=None,
        )


def caller_penalty_embed(guild_name: str, score: int) -> discord.Embed:
    embed = discord.Embed(
        title="⚠️ Advertencia de reputacion de Caller",
        description=(
            f"Tu reputacion de caller llego a **{score} puntos**. Por este motivo, tu acceso "
            "para crear y controlar actividades ha sido suspendido temporalmente."
        ),
        color=discord.Color.red(),
    )
    embed.add_field(
        name="📋 ¿Que significa?",
        value=(
            "Ya no podras usar las funciones de caller del Panel de Callers hasta que "
            "un administrador retire la penalizacion."
        ),
        inline=False,
    )
    embed.add_field(
        name="💡 Importante",
        value=(
            "Las actividades canceladas por no completar los cupos de la composicion no "
            "afectan tu reputacion. Puedes hablar con la administracion si necesitas una revision."
        ),
        inline=False,
    )
    embed.set_footer(text=f"{guild_name} • Queremos ayudarte a recuperar tu buen historial.")
    return embed


def caller_ranking(db: Database, guild_id: int) -> list[dict]:
    rows = db.fetch_all(
        """
        WITH activity_stats AS (
            SELECT
                ac.*,
                COALESCE(SUM(ar.slots), 0) AS required_slots,
                COALESCE((
                    SELECT COUNT(*)
                    FROM activity_participants ap
                    WHERE ap.activity_id = ac.id
                ), 0) AS registered_slots
            FROM activities ac
            LEFT JOIN activity_roles ar ON ar.activity_id = ac.id
            WHERE ac.guild_id = ?
              AND COALESCE(ac.activity_type, 'regular') != ?
            GROUP BY ac.id
        )
        SELECT
            c.user_id,
            c.created_at,
            COUNT(DISTINCT ac.id) AS activities_created,
            COUNT(DISTINCT CASE
                WHEN ac.status IN (?, ?) THEN ac.id
            END) AS activities_completed,
            COUNT(DISTINCT CASE
                WHEN ac.status = ? AND COALESCE(
                    ac.cancellation_reputation_exempt,
                    CASE WHEN ac.registered_slots < ac.required_slots THEN 1 ELSE 0 END
                ) = 0 THEN ac.id
            END) AS activities_cancelled,
            COUNT(DISTINCT CASE
                WHEN ac.status = ? AND COALESCE(
                    ac.cancellation_reputation_exempt,
                    CASE WHEN ac.registered_slots < ac.required_slots THEN 1 ELSE 0 END
                ) = 1 THEN ac.id
            END) AS cancellations_exempt,
            COALESCE((
                SELECT COUNT(*)
                FROM asistencia_actividades aa
                JOIN activities attended ON attended.id = aa.actividad_id
                WHERE attended.guild_id = c.guild_id
                  AND aa.usuario_id = c.user_id
                  AND aa.estado = ?
                  AND COALESCE(attended.activity_type, 'regular') != ?
            ), 0) AS attendances,
            COALESCE((
                SELECT COUNT(*)
                FROM asistencia_actividades aa
                JOIN activities missed ON missed.id = aa.actividad_id
                WHERE missed.guild_id = c.guild_id
                  AND aa.usuario_id = c.user_id
                  AND aa.estado = ?
                  AND COALESCE(missed.activity_type, 'regular') != ?
            ), 0) AS absences,
            COALESCE((
                SELECT SUM(p.distributable)
                FROM payouts p
                WHERE p.guild_id = c.guild_id
                  AND p.caller_id = c.user_id
                  AND p.status IN (?, ?)
            ), 0) AS distributed,
            EXISTS(
                SELECT 1
                FROM caller_penalties cp
                WHERE cp.guild_id = c.guild_id
                  AND cp.user_id = c.user_id
                  AND cp.active = 1
            ) AS penalized
        FROM callers c
        LEFT JOIN activity_stats ac
            ON ac.guild_id = c.guild_id AND ac.caller_id = c.user_id
        WHERE c.guild_id = ?
        GROUP BY c.guild_id, c.user_id, c.created_at
        """,
        (
            guild_id,
            ACTIVITY_TYPE_MANDATORY,
            ACTIVITY_FINISHED,
            ACTIVITY_PAYOUT_CREATED,
            ACTIVITY_CANCELLED,
            ACTIVITY_CANCELLED,
            ATTENDANCE_CONFIRMED,
            ACTIVITY_TYPE_MANDATORY,
            ATTENDANCE_ABSENT,
            ACTIVITY_TYPE_MANDATORY,
            PAYOUT_APPROVED,
            PAYOUT_DEPOSITED,
            guild_id,
        ),
    )
    ranking: list[dict] = []
    for row in rows:
        item = dict(row)
        item["score"] = (
            int(item["activities_completed"]) * 10
            + int(item["attendances"]) * 2
            - int(item["activities_cancelled"]) * 4
            - int(item["absences"]) * 6
        )
        ranking.append(item)
    ranking.sort(
        key=lambda item: (
            -int(item["score"]),
            -int(item["distributed"]),
            -int(item["activities_completed"]),
            int(item["activities_cancelled"]),
            int(item["absences"]),
            int(item["user_id"]),
        )
    )
    return ranking


async def evaluate_caller_penalties(db: Database, guild: discord.Guild) -> list[int]:
    penalized_users: list[int] = []
    for caller in caller_ranking(db, guild.id):
        user_id = int(caller["user_id"])
        score = int(caller["score"])
        active_penalty = db.fetch_one(
            """
            SELECT id, notified_at
            FROM caller_penalties
            WHERE guild_id = ? AND user_id = ? AND active = 1
            ORDER BY id DESC LIMIT 1
            """,
            (guild.id, user_id),
        )
        if score > CALLER_PENALTY_THRESHOLD:
            db.execute(
                """
                UPDATE caller_penalties
                SET rearmed = 1
                WHERE guild_id = ? AND user_id = ? AND active = 0 AND rearmed = 0
                """,
                (guild.id, user_id),
            )
            continue
        if active_penalty is not None:
            if not active_penalty["notified_at"]:
                member = guild.get_member(user_id)
                if member is not None:
                    delivered = await send_dm_safe(
                        db,
                        guild_id=guild.id,
                        user=member,
                        action="penalizacion_caller",
                        embed=caller_penalty_embed(guild.name, score),
                    )
                    if delivered:
                        db.execute(
                            """
                            UPDATE caller_penalties
                            SET notified_at = ?
                            WHERE id = ?
                            """,
                            (utc_now_iso(), int(active_penalty["id"])),
                        )
            continue

        latest_inactive = db.fetch_one(
            """
            SELECT rearmed
            FROM caller_penalties
            WHERE guild_id = ? AND user_id = ? AND active = 0
            ORDER BY id DESC LIMIT 1
            """,
            (guild.id, user_id),
        )
        if latest_inactive is not None and not int(latest_inactive["rearmed"]):
            continue

        now = utc_now_iso()
        db.execute(
            """
            INSERT INTO caller_penalties (
                guild_id, user_id, score_at_penalty, reason,
                active, penalized_at, notified_at, rearmed
            )
            VALUES (?, ?, ?, ?, 1, ?, NULL, 0)
            """,
            (
                guild.id,
                user_id,
                score,
                f"Reputacion de caller igual o menor a {CALLER_PENALTY_THRESHOLD} puntos.",
                now,
            ),
        )
        member = guild.get_member(user_id)
        delivered = False
        if member is not None:
            delivered = await send_dm_safe(
                db,
                guild_id=guild.id,
                user=member,
                action="penalizacion_caller",
                embed=caller_penalty_embed(guild.name, score),
            )
        if delivered:
            db.execute(
                """
                UPDATE caller_penalties
                SET notified_at = ?
                WHERE guild_id = ? AND user_id = ? AND active = 1
                """,
                (utc_now_iso(), guild.id, user_id),
            )
        log_action(
            db,
            guild.id,
            admin_id=None,
            action="Penalizacion automatica de caller",
            affected_user_id=user_id,
            system="Callers",
            observation=f"Puntaje: {score}. Acceso de caller suspendido.",
        )
        penalized_users.append(user_id)
    return penalized_users
