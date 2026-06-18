from __future__ import annotations

import discord

from ..constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_PAYOUT_CREATED,
    ATTENDANCE_ABSENT,
    ATTENDANCE_CONFIRMED,
    PAYOUT_APPROVED,
    PAYOUT_DEPOSITED,
)
from ..database import Database
from ..utils import utc_now_iso


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
            "• Usar el panel de actividades.\n"
            "• Crear plantillas y publicar actividades.\n"
            "• Administrar cupos, avisos y asistencia.\n"
            "• Generar repartos y enviarlos a revision administrativa."
        ),
        inline=False,
    )
    embed.add_field(
        name="📜 Responsabilidades del caller",
        value=(
            "• Asistir a las actividades que elijas organizar o en las que te registres.\n"
            "• Dirigir con respeto, orden e imparcialidad.\n"
            "• Verificar asistencia y canal de voz antes de cerrar la actividad.\n"
            "• Revisar participantes y porcentajes antes de enviar un reparto.\n"
            "• Avisar con claridad si una actividad debe cambiarse o cancelarse."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎛️ Tu acceso",
        value=(
            "Ya puedes usar el **Panel de Actividades** de G3NESYS. "
            "Si necesitas publicarlo, utiliza `!panel_pings` en el canal correspondiente."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"{guild_name} • Tu liderazgo representa a G3NESYS. Gracias por asumir esta responsabilidad."
    )
    return embed


def caller_ranking(db: Database, guild_id: int) -> list[dict]:
    rows = db.fetch_all(
        """
        SELECT
            c.user_id,
            c.created_at,
            COUNT(DISTINCT ac.id) AS activities_created,
            COUNT(DISTINCT CASE
                WHEN ac.status IN (?, ?) THEN ac.id
            END) AS activities_completed,
            COUNT(DISTINCT CASE
                WHEN ac.status = ? THEN ac.id
            END) AS activities_cancelled,
            COALESCE((
                SELECT COUNT(*)
                FROM asistencia_actividades aa
                JOIN activities attended ON attended.id = aa.actividad_id
                WHERE attended.guild_id = c.guild_id
                  AND aa.usuario_id = c.user_id
                  AND aa.estado = ?
            ), 0) AS attendances,
            COALESCE((
                SELECT COUNT(*)
                FROM asistencia_actividades aa
                JOIN activities missed ON missed.id = aa.actividad_id
                WHERE missed.guild_id = c.guild_id
                  AND aa.usuario_id = c.user_id
                  AND aa.estado = ?
            ), 0) AS absences,
            COALESCE((
                SELECT SUM(p.distributable)
                FROM payouts p
                WHERE p.guild_id = c.guild_id
                  AND p.caller_id = c.user_id
                  AND p.status IN (?, ?)
            ), 0) AS distributed
        FROM callers c
        LEFT JOIN activities ac
            ON ac.guild_id = c.guild_id AND ac.caller_id = c.user_id
        WHERE c.guild_id = ?
        GROUP BY c.guild_id, c.user_id, c.created_at
        """,
        (
            ACTIVITY_FINISHED,
            ACTIVITY_PAYOUT_CREATED,
            ACTIVITY_CANCELLED,
            ATTENDANCE_CONFIRMED,
            ATTENDANCE_ABSENT,
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
