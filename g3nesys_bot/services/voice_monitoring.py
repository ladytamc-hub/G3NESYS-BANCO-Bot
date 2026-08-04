from __future__ import annotations

import csv
import io
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone

from ..database import Database
from ..utils import utc_now_iso


VOICE_STATUS_STAYED = "Permanecio hasta el final"
VOICE_STATUS_LEFT_EARLY = "Se salio antes de finalizar"
VOICE_STATUS_LATE = "Entro tarde"
VOICE_STATUS_MULTIPLE = "Entro y salio varias veces"
VOICE_STATUS_NEVER = "No ingreso al canal de voz"
VOICE_PAGE_SIZE = 8


@dataclass(frozen=True)
class VoiceParticipantStat:
    guild_id: int
    activity_id: int
    user_id: int
    display_name: str
    monitor_started_at: str
    monitor_ended_at: str
    first_join_at: str | None
    last_join_at: str | None
    last_leave_at: str | None
    total_present_seconds: int
    total_absent_seconds: int
    leave_count: int
    rejoin_count: int
    attendance_percentage: float
    final_voice_status: str
    monitoring_duration_seconds: int


@dataclass(frozen=True)
class VoiceStatsSummary:
    activity_id: int
    guild_id: int
    monitor_started_at: str | None
    monitor_ended_at: str | None
    monitoring_duration_seconds: int
    total_participants: int
    stayed_until_end: int
    left_before_end: int
    never_joined: int
    average_attendance_percentage: float


@dataclass(frozen=True)
class VoiceStatsReportFile:
    filename: str
    data: bytes


def parse_voice_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def seconds_between(start: str | None, end: str | None) -> int:
    start_at = parse_voice_datetime(start)
    end_at = parse_voice_datetime(end)
    if start_at is None or end_at is None:
        return 0
    return max(0, int((end_at - start_at).total_seconds()))


def format_duration(seconds: int | None) -> str:
    total = max(0, int(seconds or 0))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _activity_monitor_bounds(db: Database, guild_id: int, activity_id: int, ended_at: str | None = None) -> tuple[str, str]:
    activity = db.fetch_one(
        "SELECT started_at, ended_at, created_at FROM activities WHERE guild_id = ? AND id = ?",
        (guild_id, activity_id),
    )
    session_bounds = db.fetch_one(
        """
        SELECT MIN(joined_at) AS first_join, MAX(COALESCE(left_at, joined_at)) AS last_seen
        FROM activity_voice_sessions
        WHERE guild_id = ? AND activity_id = ?
        """,
        (guild_id, activity_id),
    )
    now = utc_now_iso()
    start = (
        (activity["started_at"] if activity is not None else None)
        or (session_bounds["first_join"] if session_bounds is not None else None)
        or (activity["created_at"] if activity is not None else None)
        or ended_at
        or now
    )
    end = (
        ended_at
        or (activity["ended_at"] if activity is not None else None)
        or (session_bounds["last_seen"] if session_bounds is not None else None)
        or now
    )
    return str(start), str(end)


def _tracked_users(db: Database, guild_id: int, activity_id: int, name_resolver=None) -> dict[int, str]:
    users: dict[int, str] = {}
    for row in db.fetch_all(
        """
        SELECT user_id, display_name
        FROM activity_participants
        WHERE activity_id = ?
        ORDER BY id ASC
        """,
        (activity_id,),
    ):
        users[int(row["user_id"])] = str(row["display_name"] or f"Usuario {row['user_id']}")
    activity = db.fetch_one(
        "SELECT caller_id FROM activities WHERE guild_id = ? AND id = ?",
        (guild_id, activity_id),
    )
    if activity is not None and activity["caller_id"] is not None:
        caller_id = int(activity["caller_id"])
        users.setdefault(caller_id, "Caller")
    for row in db.fetch_all(
        """
        SELECT DISTINCT user_id
        FROM activity_voice_sessions
        WHERE guild_id = ? AND activity_id = ?
        """,
        (guild_id, activity_id),
    ):
        user_id = int(row["user_id"])
        users.setdefault(user_id, f"Usuario {user_id}")
    for row in db.fetch_all(
        """
        SELECT user_id, display_name
        FROM activity_voice_stats
        WHERE guild_id = ? AND activity_id = ?
        """,
        (guild_id, activity_id),
    ):
        user_id = int(row["user_id"])
        users.setdefault(user_id, str(row["display_name"] or f"Usuario {user_id}"))
    if name_resolver is not None:
        for user_id, current in list(users.items()):
            if current and not current.startswith("Usuario ") and current != "Caller":
                continue
            resolved = name_resolver(user_id)
            if resolved:
                users[user_id] = resolved
            elif not current:
                users[user_id] = f"Usuario {user_id}"
    return users


def calculate_voice_participant_stat(
    db: Database,
    guild_id: int,
    activity_id: int,
    user_id: int,
    display_name: str,
    *,
    monitor_started_at: str,
    monitor_ended_at: str,
) -> VoiceParticipantStat:
    rows = db.fetch_all(
        """
        SELECT joined_at, left_at, seconds
        FROM activity_voice_sessions
        WHERE guild_id = ? AND activity_id = ? AND user_id = ?
        ORDER BY joined_at ASC, id ASC
        """,
        (guild_id, activity_id, user_id),
    )
    duration = max(0, seconds_between(monitor_started_at, monitor_ended_at))
    total_present = 0
    first_join_at = None
    last_join_at = None
    last_leave_at = None
    leave_count = 0
    end_dt = parse_voice_datetime(monitor_ended_at)
    for row in rows:
        joined_at = str(row["joined_at"])
        left_at = str(row["left_at"]) if row["left_at"] is not None else monitor_ended_at
        first_join_at = first_join_at or joined_at
        last_join_at = joined_at
        if row["left_at"] is not None:
            last_leave_at = str(row["left_at"])
            left_dt = parse_voice_datetime(str(row["left_at"]))
            if end_dt is None or left_dt is None or left_dt < end_dt:
                leave_count += 1
        total_present += seconds_between(joined_at, left_at)
    total_present = min(total_present, duration)
    total_absent = max(0, duration - total_present)
    percentage = 0.0 if duration <= 0 else max(0.0, min(100.0, (total_present / duration) * 100))
    rejoin_count = max(0, len(rows) - 1)

    if not rows or total_present <= 0:
        status = VOICE_STATUS_NEVER
    elif rejoin_count > 0 or leave_count > 1:
        status = VOICE_STATUS_MULTIPLE
    elif last_leave_at is not None and parse_voice_datetime(last_leave_at) is not None and end_dt is not None and parse_voice_datetime(last_leave_at) < end_dt:
        status = VOICE_STATUS_LEFT_EARLY
    elif first_join_at and parse_voice_datetime(first_join_at) and parse_voice_datetime(monitor_started_at) and parse_voice_datetime(first_join_at) > parse_voice_datetime(monitor_started_at):
        status = VOICE_STATUS_LATE
    else:
        status = VOICE_STATUS_STAYED

    return VoiceParticipantStat(
        guild_id=guild_id,
        activity_id=activity_id,
        user_id=user_id,
        display_name=display_name,
        monitor_started_at=monitor_started_at,
        monitor_ended_at=monitor_ended_at,
        first_join_at=first_join_at,
        last_join_at=last_join_at,
        last_leave_at=last_leave_at,
        total_present_seconds=total_present,
        total_absent_seconds=total_absent,
        leave_count=leave_count,
        rejoin_count=rejoin_count,
        attendance_percentage=round(percentage, 2),
        final_voice_status=status,
        monitoring_duration_seconds=duration,
    )


def persist_activity_voice_stats(
    db: Database,
    guild_id: int,
    activity_id: int,
    *,
    ended_at: str | None = None,
    name_resolver=None,
) -> list[VoiceParticipantStat]:
    started_at, monitor_ended_at = _activity_monitor_bounds(db, guild_id, activity_id, ended_at)
    users = _tracked_users(db, guild_id, activity_id, name_resolver=name_resolver)
    now = utc_now_iso()
    stats = [
        calculate_voice_participant_stat(
            db,
            guild_id,
            activity_id,
            user_id,
            display_name,
            monitor_started_at=started_at,
            monitor_ended_at=monitor_ended_at,
        )
        for user_id, display_name in sorted(users.items(), key=lambda item: item[1].lower())
    ]
    for item in stats:
        db.execute(
            """
            INSERT INTO activity_voice_stats (
                guild_id, activity_id, user_id, display_name,
                monitor_started_at, monitor_ended_at, first_join_at, last_join_at, last_leave_at,
                total_present_seconds, total_absent_seconds, leave_count, rejoin_count,
                attendance_percentage, final_voice_status, monitoring_duration_seconds,
                created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(guild_id, activity_id, user_id)
            DO UPDATE SET display_name = excluded.display_name,
                          monitor_started_at = excluded.monitor_started_at,
                          monitor_ended_at = excluded.monitor_ended_at,
                          first_join_at = excluded.first_join_at,
                          last_join_at = excluded.last_join_at,
                          last_leave_at = excluded.last_leave_at,
                          total_present_seconds = excluded.total_present_seconds,
                          total_absent_seconds = excluded.total_absent_seconds,
                          leave_count = excluded.leave_count,
                          rejoin_count = excluded.rejoin_count,
                          attendance_percentage = excluded.attendance_percentage,
                          final_voice_status = excluded.final_voice_status,
                          monitoring_duration_seconds = excluded.monitoring_duration_seconds,
                          updated_at = excluded.updated_at
            """,
            (
                item.guild_id,
                item.activity_id,
                item.user_id,
                item.display_name,
                item.monitor_started_at,
                item.monitor_ended_at,
                item.first_join_at,
                item.last_join_at,
                item.last_leave_at,
                item.total_present_seconds,
                item.total_absent_seconds,
                item.leave_count,
                item.rejoin_count,
                item.attendance_percentage,
                item.final_voice_status,
                item.monitoring_duration_seconds,
                now,
                now,
            ),
        )
    return stats


def get_persisted_activity_voice_stats(db: Database, guild_id: int, activity_id: int) -> list[VoiceParticipantStat]:
    rows = db.fetch_all(
        """
        SELECT *
        FROM activity_voice_stats
        WHERE guild_id = ? AND activity_id = ?
        ORDER BY attendance_percentage DESC, total_present_seconds DESC, display_name ASC
        """,
        (guild_id, activity_id),
    )
    return [
        VoiceParticipantStat(
            guild_id=int(row["guild_id"]),
            activity_id=int(row["activity_id"]),
            user_id=int(row["user_id"]),
            display_name=str(row["display_name"] or f"Usuario {row['user_id']}"),
            monitor_started_at=str(row["monitor_started_at"] or ""),
            monitor_ended_at=str(row["monitor_ended_at"] or ""),
            first_join_at=str(row["first_join_at"]) if row["first_join_at"] else None,
            last_join_at=str(row["last_join_at"]) if row["last_join_at"] else None,
            last_leave_at=str(row["last_leave_at"]) if row["last_leave_at"] else None,
            total_present_seconds=int(row["total_present_seconds"] or 0),
            total_absent_seconds=int(row["total_absent_seconds"] or 0),
            leave_count=int(row["leave_count"] or 0),
            rejoin_count=int(row["rejoin_count"] or 0),
            attendance_percentage=float(row["attendance_percentage"] or 0),
            final_voice_status=str(row["final_voice_status"] or ""),
            monitoring_duration_seconds=int(row["monitoring_duration_seconds"] or 0),
        )
        for row in rows
    ]


def get_activity_voice_stats(
    db: Database,
    guild_id: int,
    activity_id: int,
    *,
    name_resolver=None,
) -> list[VoiceParticipantStat]:
    stats = get_persisted_activity_voice_stats(db, guild_id, activity_id)
    activity = db.fetch_one("SELECT status, ended_at FROM activities WHERE guild_id = ? AND id = ?", (guild_id, activity_id))
    if stats and activity is not None and activity["ended_at"]:
        return stats
    return persist_activity_voice_stats(db, guild_id, activity_id, name_resolver=name_resolver)


def summarize_voice_stats(stats: list[VoiceParticipantStat]) -> VoiceStatsSummary:
    if not stats:
        return VoiceStatsSummary(0, 0, None, None, 0, 0, 0, 0, 0, 0.0)
    return VoiceStatsSummary(
        activity_id=stats[0].activity_id,
        guild_id=stats[0].guild_id,
        monitor_started_at=stats[0].monitor_started_at,
        monitor_ended_at=stats[0].monitor_ended_at,
        monitoring_duration_seconds=stats[0].monitoring_duration_seconds,
        total_participants=len(stats),
        stayed_until_end=sum(1 for item in stats if item.final_voice_status == VOICE_STATUS_STAYED),
        left_before_end=sum(1 for item in stats if item.final_voice_status == VOICE_STATUS_LEFT_EARLY),
        never_joined=sum(1 for item in stats if item.final_voice_status == VOICE_STATUS_NEVER),
        average_attendance_percentage=round(sum(item.attendance_percentage for item in stats) / len(stats), 2),
    )


_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_DANGEROUS_CSV_PREFIXES = ("=", "+", "-", "@")


def _csv_safe(value) -> str:
    text = str(value or "")
    if text.startswith(_DANGEROUS_CSV_PREFIXES):
        return "'" + text
    return text


def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def safe_stats_filename_part(value: str | None, *, fallback: str = "actividad") -> str:
    normalized = unicodedata.normalize("NFKD", str(value or fallback))
    ascii_text = normalized.encode("ascii", "ignore").decode("ascii")
    cleaned = _SAFE_FILENAME_RE.sub("_", ascii_text).strip("._-")
    cleaned = re.sub(r"_+", "_", cleaned)
    return (cleaned or fallback)[:60]


def _date_part(activity) -> str:
    raw = None
    if activity is not None:
        raw = activity["started_at"] or activity["ended_at"] or activity["created_at"]
    text = str(raw or datetime.now(timezone.utc).date().isoformat())[:10]
    return safe_stats_filename_part(text, fallback="sin_fecha")


def _activity_display_date(activity) -> str:
    if activity is None:
        return "Sin fecha"
    return str(activity["started_at"] or activity["ended_at"] or activity["created_at"] or "Sin fecha")


def _activity_voice_channel(activity) -> str:
    if activity is None or activity["voice_channel_id"] is None:
        return "Sin canal"
    return str(activity["voice_channel_id"])


def build_short_voice_stats_report_files(db: Database, guild_id: int, activity_id: int) -> list[VoiceStatsReportFile]:
    activity = db.fetch_one("SELECT * FROM activities WHERE guild_id = ? AND id = ?", (guild_id, activity_id))
    stats = get_persisted_activity_voice_stats(db, guild_id, activity_id)
    summary = summarize_voice_stats(stats)
    safe_name = safe_stats_filename_part(activity["name"] if activity is not None else None)
    date_name = _date_part(activity)
    txt_lines = [
        "ESTADISTICAS DE PARTICIPACION",
        "",
        f"Actividad: {activity['name'] if activity is not None else activity_id}",
        f"Fecha: {_activity_display_date(activity)}",
        f"Duracion del monitoreo: {format_duration(summary.monitoring_duration_seconds)}",
        "",
    ]
    for item in stats:
        txt_lines.append(f"{item.display_name} \u2014 {item.attendance_percentage:.2f} %")
    txt_data = ("\n".join(txt_lines) + "\n").encode("utf-8")
    csv_data = _csv_bytes(
        ["nombre", "porcentaje_participacion"],
        [[_csv_safe(item.display_name), f"{item.attendance_percentage:.2f}"] for item in stats],
    )
    return [
        VoiceStatsReportFile(f"stats_cortas_{safe_name}_{date_name}_{activity_id}.txt", txt_data),
        VoiceStatsReportFile(f"stats_cortas_{safe_name}_{date_name}_{activity_id}.csv", csv_data),
    ]


def build_full_voice_stats_report_files(db: Database, guild_id: int, activity_id: int) -> list[VoiceStatsReportFile]:
    activity = db.fetch_one("SELECT * FROM activities WHERE guild_id = ? AND id = ?", (guild_id, activity_id))
    stats = get_persisted_activity_voice_stats(db, guild_id, activity_id)
    summary = summarize_voice_stats(stats)
    safe_name = safe_stats_filename_part(activity["name"] if activity is not None else None)
    date_name = _date_part(activity)
    late_count = sum(1 for item in stats if item.final_voice_status == VOICE_STATUS_LATE)
    txt_lines = [
        "ESTADISTICAS COMPLETAS DE VOZ",
        "",
        f"ID de actividad: {activity_id}",
        f"Nombre: {activity['name'] if activity is not None else activity_id}",
        f"Servidor: {guild_id}",
        f"Caller: {activity['caller_id'] if activity is not None else 'Sin registro'}",
        f"Canal de voz monitoreado: {_activity_voice_channel(activity)}",
        f"Fecha de actividad: {_activity_display_date(activity)}",
        f"Inicio del monitoreo: {summary.monitor_started_at or 'Sin registro'}",
        f"Finalizacion del monitoreo: {summary.monitor_ended_at or 'Sin registro'}",
        f"Duracion total: {format_duration(summary.monitoring_duration_seconds)}",
        f"Estado de actividad: {activity['status'] if activity is not None else 'Sin registro'}",
        f"Total de participantes: {summary.total_participants}",
        f"Permanencia promedio: {summary.average_attendance_percentage:.2f} %",
        f"Permanecieron hasta el final: {summary.stayed_until_end}",
        f"Abandonaron antes: {summary.left_before_end}",
        f"Entraron tarde: {late_count}",
        f"Nunca ingresaron: {summary.never_joined}",
        "",
        "PARTICIPANTES",
    ]
    for item in stats:
        txt_lines.extend([
            "",
            f"Nombre guardado: {item.display_name}",
            f"Discord ID: {item.user_id}",
            f"Primera entrada: {item.first_join_at or 'Sin entrada'}",
            f"Ultima salida: {item.last_leave_at or 'Sin salida'}",
            f"Tiempo presente: {format_duration(item.total_present_seconds)}",
            f"Tiempo ausente: {format_duration(item.total_absent_seconds)}",
            f"Salidas: {item.leave_count}",
            f"Reingresos: {item.rejoin_count}",
            f"Porcentaje: {item.attendance_percentage:.2f} %",
            f"Estado final: {item.final_voice_status}",
        ])
    txt_data = ("\n".join(txt_lines) + "\n").encode("utf-8")
    rows = []
    for item in stats:
        entered_late = item.final_voice_status == VOICE_STATUS_LATE
        stayed = item.final_voice_status == VOICE_STATUS_STAYED
        never = item.final_voice_status == VOICE_STATUS_NEVER
        rows.append([
            item.activity_id,
            item.guild_id,
            item.user_id,
            _csv_safe(item.display_name),
            item.monitor_started_at,
            item.monitor_ended_at,
            item.first_join_at or "",
            item.last_leave_at or "",
            format_duration(item.total_present_seconds),
            format_duration(item.total_absent_seconds),
            item.leave_count,
            item.rejoin_count,
            f"{item.attendance_percentage:.2f}",
            "si" if entered_late else "no",
            "si" if stayed else "no",
            "si" if never else "no",
            item.final_voice_status,
        ])
    csv_data = _csv_bytes(
        [
            "activity_id", "guild_id", "user_id", "nombre", "monitor_inicio", "monitor_final",
            "primera_entrada", "ultima_salida", "tiempo_presente", "tiempo_ausente", "salidas",
            "reingresos", "porcentaje_permanencia", "entro_tarde", "permanecio_hasta_final",
            "nunca_ingreso", "estado_final",
        ],
        rows,
    )
    return [
        VoiceStatsReportFile(f"stats_completas_{safe_name}_{date_name}_{activity_id}.csv", csv_data),
        VoiceStatsReportFile(f"stats_completas_{safe_name}_{date_name}_{activity_id}.txt", txt_data),
    ]
