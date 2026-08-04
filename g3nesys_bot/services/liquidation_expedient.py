from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import tempfile
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from ..constants import ACTIVITY_TYPE_MANDATORY, PAYOUT_DEPOSITED
from ..database import Database
from ..utils import format_amount, utc_now_iso
from .activity_audit import movement_mentions_activity_code, normalize_activity_code
from .voice_monitoring import VOICE_STATUS_LATE, format_duration, get_persisted_activity_voice_stats
from .withdrawal_audit import get_withdrawal_audit_dataset


EXPEDIENT_VERSION = "1.0"
MISSING = "No registrado"
NameResolver = Callable[[int | None], str]


class LiquidationExpedientError(ValueError):
    pass


class ActivityNotFoundError(LiquidationExpedientError):
    pass


class ActivityWithoutLiquidationError(LiquidationExpedientError):
    pass


@dataclass(frozen=True)
class LiquidationExpedientFile:
    filename: str
    data: bytes
    activity_code: str
    participant_count: int
    request_count: int
    payment_count: int
    liquidation_status: str
    included_files: tuple[str, ...]
    unavailable_data: tuple[str, ...]


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _value(row, key: str, default=None):
    try:
        if key in row.keys():
            return row[key]
    except AttributeError:
        return default
    return default


def _text(value) -> str:
    if value is None:
        return MISSING
    text = str(value).strip()
    return text if text else MISSING


def _optional(value) -> str:
    return "" if value is None else str(value)


def _yes_no(value: bool) -> str:
    return "Si" if value else "No"


def _csv_safe(value) -> str:
    text = _text(value)
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([_csv_safe(item) if isinstance(item, str) or item is None else item for item in row])
    return output.getvalue().encode("utf-8-sig")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _duration(start: str | None, end: str | None) -> str:
    start_dt = _parse_datetime(start)
    end_dt = _parse_datetime(end)
    if start_dt is None or end_dt is None:
        return MISSING
    return format_duration(max(0, int((end_dt - start_dt).total_seconds())))


def activity_message_url(activity) -> str:
    guild_id = _as_int(_value(activity, "guild_id"))
    channel_id = _as_int(_value(activity, "channel_id"))
    message_id = _as_int(_value(activity, "message_id"))
    if guild_id and channel_id and message_id:
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"
    return MISSING


def activity_thread_url(activity) -> str:
    guild_id = _as_int(_value(activity, "guild_id"))
    thread_id = _as_int(_value(activity, "thread_id"))
    panel_message_id = _as_int(_value(activity, "thread_panel_message_id"))
    if not (guild_id and thread_id):
        return MISSING
    if panel_message_id:
        return f"https://discord.com/channels/{guild_id}/{thread_id}/{panel_message_id}"
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def get_activity_for_expedient(db: Database, guild_id: int, code: str):
    normalized = normalize_activity_code(code)
    if normalized is None:
        return None
    return db.fetch_one(
        "SELECT * FROM activities WHERE guild_id = ? AND UPPER(code) = ?",
        (guild_id, normalized),
    )


def activity_has_liquidation_record(db: Database, guild_id: int, activity_id: int) -> bool:
    activity = db.fetch_one("SELECT * FROM activities WHERE guild_id = ? AND id = ?", (guild_id, activity_id))
    if activity is None:
        return False
    payout = db.fetch_one("SELECT 1 FROM payouts WHERE guild_id = ? AND activity_id = ? LIMIT 1", (guild_id, activity_id))
    if payout is not None:
        return True
    if _value(activity, "mandatory_loot_amount") is not None:
        return True
    code = normalize_activity_code(_value(activity, "code")) or str(_value(activity, "code") or "").upper()
    return bool(_fetch_activity_deposit_movements(db, guild_id, code))


def _fetch_activity_deposit_movements(db: Database, guild_id: int, code: str):
    rows = db.fetch_all(
        """
        SELECT *
        FROM movements
        WHERE guild_id = ?
          AND type = 'DEPOSITO'
          AND description LIKE ?
        ORDER BY created_at ASC, id ASC
        """,
        (guild_id, f"%{code}%"),
    )
    return [row for row in rows if movement_mentions_activity_code(str(row["description"] or ""), code)]


def _fetch_payouts(db: Database, guild_id: int, activity_id: int):
    return db.fetch_all(
        """
        SELECT *
        FROM payouts
        WHERE guild_id = ? AND activity_id = ?
        ORDER BY id ASC
        """,
        (guild_id, activity_id),
    )


def _fetch_quick_liquidations(db: Database, guild_id: int, payout_ids: list[int]):
    if not payout_ids:
        return []
    placeholders = ",".join("?" for _ in payout_ids)
    return db.fetch_all(
        f"""
        SELECT ql.*, p.code AS payout_code
        FROM quick_liquidations ql
        JOIN payouts p ON p.id = ql.payout_id
        WHERE ql.guild_id = ? AND ql.payout_id IN ({placeholders})
        ORDER BY ql.created_at ASC, ql.id ASC
        """,
        (guild_id, *payout_ids),
    )


def _fetch_activity_participants(db: Database, activity_id: int):
    return db.fetch_all(
        """
        SELECT ap.*, ar.name AS role_name
        FROM activity_participants ap
        LEFT JOIN activity_roles ar ON ar.id = ap.role_id
        WHERE ap.activity_id = ?
        ORDER BY ap.joined_at ASC, ap.id ASC
        """,
        (activity_id,),
    )


def _fetch_attendance(db: Database, activity_id: int) -> dict[int, object]:
    rows = db.fetch_all("SELECT * FROM asistencia_actividades WHERE actividad_id = ?", (activity_id,))
    return {int(row["usuario_id"]): row for row in rows}


def _fetch_late_join_requests(db: Database, guild_id: int, activity_id: int) -> dict[int, object]:
    rows = db.fetch_all(
        """
        SELECT *
        FROM activity_join_requests
        WHERE guild_id = ? AND activity_id = ? AND status = 'Aceptada'
        ORDER BY requested_at ASC, id ASC
        """,
        (guild_id, activity_id),
    )
    return {int(row["user_id"]): row for row in rows}


def _fetch_payout_participants(db: Database, payout_ids: list[int]):
    if not payout_ids:
        return []
    placeholders = ",".join("?" for _ in payout_ids)
    return db.fetch_all(
        f"""
        SELECT pp.*, p.code AS payout_code, p.created_at AS payout_created_at
        FROM payout_participants pp
        JOIN payouts p ON p.id = pp.payout_id
        WHERE pp.payout_id IN ({placeholders})
        ORDER BY p.id ASC, pp.id ASC
        """,
        tuple(payout_ids),
    )


def _fetch_payout_audit_logs(db: Database, guild_id: int, payout_ids: list[int]):
    if not payout_ids:
        return []
    placeholders = ",".join("?" for _ in payout_ids)
    return db.fetch_all(
        f"""
        SELECT pal.*, p.code AS payout_code
        FROM payout_audit_logs pal
        JOIN payouts p ON p.id = pal.payout_id
        WHERE pal.guild_id = ? AND pal.payout_id IN ({placeholders})
        ORDER BY pal.created_at ASC, pal.id ASC
        """,
        (guild_id, *payout_ids),
    )


def _fetch_related_audit_logs(db: Database, guild_id: int, terms: Iterable[str]):
    rows = db.fetch_all(
        """
        SELECT *
        FROM audit_logs
        WHERE guild_id = ?
        ORDER BY created_at ASC, id ASC
        """,
        (guild_id,),
    )
    terms_upper = [term.upper() for term in terms if term]
    matched = []
    for row in rows:
        haystack = " ".join(
            str(_value(row, key, "") or "")
            for key in ("action", "system", "observation")
        ).upper()
        if any(term in haystack for term in terms_upper):
            matched.append(row)
    return matched


def _withdrawal_haystack(record, raw, movements) -> str:
    parts = [
        record.code,
        record.reason,
        record.rejection_reason,
        record.return_reason,
        _value(raw, "approval_admin_message", ""),
        _value(raw, "liquidation_admin_message", ""),
    ]
    parts.extend(item.note for item in movements)
    parts.extend(item.source for item in movements)
    return " ".join(str(part or "") for part in parts).upper()


def _related_withdrawal_records(db: Database, guild_id: int, terms: list[str]):
    dataset = get_withdrawal_audit_dataset(db, guild_id)
    raw_by_code = {
        str(row["code"]).upper(): row
        for row in db.fetch_all("SELECT * FROM withdrawals WHERE guild_id = ?", (guild_id,))
    }
    terms_upper = [term.upper() for term in terms if term]
    records = []
    for record in dataset.records:
        raw = raw_by_code.get(record.code.upper())
        movements = list(dataset.movements_for(record.code))
        haystack = _withdrawal_haystack(record, raw, movements)
        if any(term in haystack for term in terms_upper):
            records.append((record, raw, movements))
    return records


def _member_name(name_resolver: NameResolver | None, user_id: int | None) -> str:
    if user_id is None:
        return MISSING
    if name_resolver is None:
        return MISSING
    resolved = str(name_resolver(user_id) or "").strip()
    return resolved or MISSING


def _liquidation_status(activity, payouts, quick_liquidations) -> str:
    if quick_liquidations:
        return "Liquidada"
    if any(_value(payout, "quick_liquidated_at") for payout in payouts):
        return "Liquidada"
    if any(str(_value(payout, "status", "")) == PAYOUT_DEPOSITED for payout in payouts):
        return "Split depositado"
    if payouts:
        return "Split registrado"
    if _value(activity, "mandatory_loot_amount") is not None:
        return "Botin Mandatory registrado"
    return "Liquidacion registrada"


def _liquidated_at(payouts, quick_liquidations) -> str:
    dates = [str(row["created_at"]) for row in quick_liquidations if _value(row, "created_at")]
    dates.extend(str(_value(row, "quick_liquidated_at")) for row in payouts if _value(row, "quick_liquidated_at"))
    dates.extend(str(_value(row, "reviewed_at")) for row in payouts if _value(row, "reviewed_at"))
    return max(dates) if dates else MISSING


def _liquidated_by(payouts, quick_liquidations, name_resolver: NameResolver | None) -> str:
    user_id = None
    if quick_liquidations:
        user_id = _as_int(_value(quick_liquidations[-1], "admin_id"))
    if user_id is None:
        reviewed = [row for row in payouts if _value(row, "quick_liquidated_by") or _value(row, "reviewed_by")]
        if reviewed:
            user_id = _as_int(_value(reviewed[-1], "quick_liquidated_by")) or _as_int(_value(reviewed[-1], "reviewed_by"))
    return _member_name(name_resolver, user_id) if user_id is not None else MISSING


def _summary_txt(activity, payouts, quick_liquidations, participant_count: int, name_resolver: NameResolver | None) -> bytes:
    lines = [
        "EXPEDIENTE DE LIQUIDACION",
        "",
        f"Codigo de actividad: {_text(_value(activity, 'code'))}",
        f"Tipo de actividad: {_text(_value(activity, 'activity_type'))}",
        f"Nombre o titulo del ping: {_text(_value(activity, 'name'))}",
        f"Fecha de creacion: {_text(_value(activity, 'created_at'))}",
        f"Fecha de inicio: {_text(_value(activity, 'started_at'))}",
        f"Fecha de finalizacion: {_text(_value(activity, 'ended_at'))}",
        f"Duracion total: {_duration(_value(activity, 'started_at'), _value(activity, 'ended_at'))}",
        f"Estado actual: {_text(_value(activity, 'status'))}",
        f"Usuario que creo el ping: {_member_name(name_resolver, _as_int(_value(activity, 'pinged_by_id')) or _as_int(_value(activity, 'caller_id')))}",
        f"Caller asignado: {_member_name(name_resolver, _as_int(_value(activity, 'caller_id')))}",
        f"Canal del ping: {_text(_value(activity, 'channel_id'))}",
        f"Hilo relacionado: {_text(_value(activity, 'thread_id'))}",
        f"Enlace directo al mensaje del ping: {activity_message_url(activity)}",
        f"Enlace directo al hilo: {activity_thread_url(activity)}",
        f"Estado de liquidacion: {_liquidation_status(activity, payouts, quick_liquidations)}",
        f"Fecha de liquidacion: {_liquidated_at(payouts, quick_liquidations)}",
        f"Usuario que realizo o aprobo la liquidacion: {_liquidated_by(payouts, quick_liquidations, name_resolver)}",
        "",
        "INFORMACION DEL SPLIT",
    ]
    if not payouts:
        if _value(activity, "mandatory_loot_amount") is not None:
            lines.extend([
                "Split: No aplica a Ping Mandatory",
                f"Botin registrado: {format_amount(int(_value(activity, 'mandatory_loot_amount') or 0))}",
                f"Registrado por: {_member_name(name_resolver, _as_int(_value(activity, 'mandatory_loot_recorded_by')))}",
                f"Fecha del registro: {_text(_value(activity, 'mandatory_loot_recorded_at'))}",
            ])
        else:
            lines.append("No hay campos de split guardados para esta actividad.")
    for payout in payouts:
        rows = [
            "",
            f"Split: {_text(_value(payout, 'code'))}",
            f"Botin bruto: {format_amount(int(_value(payout, 'gross_loot') or 0))}",
            f"Reparaciones: {format_amount(int(_value(payout, 'repairs') or 0))}",
            f"Tasa o descuento de mercado: {float(_value(payout, 'market_rate_percent') or 0):.2f}%",
            f"Otros descuentos: {format_amount(int(_value(payout, 'other_expenses') or 0))}",
            f"Porcentaje del gremio: {float(_value(payout, 'guild_percent') or 0):.2f}%",
            f"Total neto a repartir: {format_amount(int(_value(payout, 'distributable') or 0))}",
            f"Cantidad de participantes incluidos: {participant_count}",
            f"Monto individual: {MISSING}",
            "Metodo de reparto: Porcentaje de participacion guardado",
            f"Fecha del calculo: {_text(_value(payout, 'created_at'))}",
            f"Usuario que genero el split: {_member_name(name_resolver, _as_int(_value(payout, 'caller_id')))}",
            f"Recalculaciones realizadas: {MISSING}",
            f"Observaciones registradas: {_text(_value(payout, 'notes'))}",
        ]
        lines.extend(rows)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _participants_csv(
    db: Database,
    guild_id: int,
    activity,
    payouts,
    name_resolver: NameResolver | None,
) -> tuple[bytes, int]:
    activity_id = int(activity["id"])
    participants = _fetch_activity_participants(db, activity_id)
    attendance = _fetch_attendance(db, activity_id)
    late_requests = _fetch_late_join_requests(db, guild_id, activity_id)
    stats = {item.user_id: item for item in get_persisted_activity_voice_stats(db, guild_id, activity_id)}
    payout_rows = _fetch_payout_participants(db, [int(row["id"]) for row in payouts])
    payout_by_user: dict[int, list] = {}
    for row in payout_rows:
        payout_by_user.setdefault(int(row["user_id"]), []).append(row)
    rows = []
    for participant in participants:
        user_id = int(participant["user_id"])
        stat = stats.get(user_id)
        att = attendance.get(user_id)
        user_payouts = payout_by_user.get(user_id, [])
        first_join = stat.first_join_at if stat else None
        last_leave = stat.last_leave_at if stat else None
        total_seconds = stat.total_present_seconds if stat else _as_int(_value(att, "voice_seconds")) or 0
        percent = stat.attendance_percentage if stat else float(_value(att, "participation_percent", 0) or 0)
        joined_late = user_id in late_requests or (stat is not None and stat.final_voice_status == VOICE_STATUS_LATE)
        if not joined_late and first_join and _value(activity, "started_at"):
            start_dt = _parse_datetime(str(_value(activity, "started_at")))
            join_dt = _parse_datetime(first_join)
            joined_late = start_dt is not None and join_dt is not None and join_dt > start_dt
        rows.append([
            user_id,
            _member_name(name_resolver, user_id),
            _text(_value(participant, "display_name")),
            MISSING,
            _text(_value(att, "estado")),
            _text(first_join or _value(participant, "joined_at")),
            _text(last_leave),
            format_duration(total_seconds),
            f"{percent:.2f}",
            (stat.rejoin_count + 1 if stat and stat.first_join_at else MISSING),
            stat.leave_count if stat else MISSING,
            _yes_no(joined_late),
            _yes_no(bool(user_payouts)),
            "; ".join(f"{row['payout_code']}:{int(row['amount'] or 0)}" for row in user_payouts) or MISSING,
            MISSING,
            MISSING,
        ])
    headers = [
        "Discord ID",
        "Nombre de Discord",
        "Nombre visible",
        "Nombre de Albion",
        "Estado de participacion",
        "Hora de entrada",
        "Hora de salida",
        "Tiempo total dentro del canal de voz",
        "Porcentaje de participacion",
        "Cantidad de entradas al canal",
        "Cantidad de salidas del canal",
        "Se unio despues del inicio",
        "Fue incluido en el split",
        "Monto asignado",
        "Penalizacion o descuento manual",
        "Motivo de la modificacion",
    ]
    return _csv_bytes(headers, rows), len(rows)


def _payments_csv(related_withdrawals, name_resolver: NameResolver | None) -> tuple[bytes, int, int]:
    headers = [
        "Numero de solicitud",
        "Codigo de actividad",
        "Usuario beneficiario",
        "Discord ID",
        "Monto solicitado",
        "Monto pagado",
        "Saldo pendiente",
        "Estado",
        "Movimiento",
        "Fecha de solicitud",
        "Fecha de pago",
        "Usuario que aprobo",
        "Usuario que pago",
        "Metodo de pago",
        "Motivo de rechazo o devolucion",
        "Observaciones",
    ]
    rows = []
    payment_count = 0
    for record, raw, movements in related_withdrawals:
        rows.append([
            record.code,
            MISSING,
            _member_name(name_resolver, record.user_id),
            record.user_id,
            record.amount_requested,
            record.amount_paid,
            record.pending_amount,
            record.audit_status,
            "Solicitud",
            _text(record.created_at),
            _text(record.last_payment_at),
            _member_name(name_resolver, record.approved_by) if record.approved_by else MISSING,
            ", ".join(_member_name(name_resolver, user_id) for user_id in record.paid_by_ids) or MISSING,
            record.payment_place or record.payment_schedule or MISSING,
            record.rejection_reason or record.return_reason or MISSING,
            record.reason or _value(raw, "liquidation_admin_message", MISSING),
        ])
        for movement in movements:
            is_payment = "pago" in movement.action.lower() or movement.movement_id is not None
            if is_payment:
                payment_count += 1
            rows.append([
                record.code,
                MISSING,
                _member_name(name_resolver, record.user_id),
                record.user_id,
                record.amount_requested,
                movement.amount if movement.amount is not None else "",
                record.pending_amount,
                movement.new_status or record.audit_status,
                movement.action,
                _text(record.created_at),
                _text(movement.date),
                _member_name(name_resolver, record.approved_by) if record.approved_by else MISSING,
                _member_name(name_resolver, movement.actor_id),
                movement.source or record.payment_place or MISSING,
                record.rejection_reason or record.return_reason or MISSING,
                movement.note or record.reason or MISSING,
            ])
    return _csv_bytes(headers, rows), len(related_withdrawals), payment_count


def _audit_csv(
    activity,
    payouts,
    quick_liquidations,
    payout_logs,
    related_audit_logs,
    related_withdrawals,
    deposit_movements,
    name_resolver: NameResolver | None,
) -> bytes:
    rows = []

    def add(date, actor_id, action, old="", new="", reason="", reference=""):
        rows.append([
            _text(date),
            _text(str(date)[11:19] if date and len(str(date)) >= 19 else None),
            _member_name(name_resolver, _as_int(actor_id)) if actor_id is not None else MISSING,
            action,
            old or MISSING,
            new or MISSING,
            reason or MISSING,
            reference or MISSING,
        ])

    code = str(activity["code"])
    add(_value(activity, "created_at"), _as_int(_value(activity, "pinged_by_id")) or _as_int(_value(activity, "caller_id")), "Actividad creada", "", _value(activity, "status"), "", code)
    if _value(activity, "message_id"):
        add(_value(activity, "created_at"), _as_int(_value(activity, "pinged_by_id")), "Ping publicado", "", activity_message_url(activity), "", code)
    if _value(activity, "caller_id"):
        add(_value(activity, "created_at"), _as_int(_value(activity, "pinged_by_id")), "Caller asignado", "", _value(activity, "caller_id"), "", code)
    if _value(activity, "started_at"):
        add(_value(activity, "started_at"), _as_int(_value(activity, "caller_id")), "Actividad iniciada", "", "", "", code)
    if _value(activity, "ended_at"):
        add(_value(activity, "ended_at"), _as_int(_value(activity, "caller_id")), "Actividad finalizada", "", "", "", code)
    if _value(activity, "cancelled_by"):
        add(_value(activity, "ended_at") or _value(activity, "created_at"), _as_int(_value(activity, "cancelled_by")), "Actividad cancelada", "", _value(activity, "status"), _value(activity, "cancellation_reason", ""), code)
    for movement in deposit_movements:
        add(_value(movement, "created_at"), _as_int(_value(movement, "created_by")), "Movimiento de deposito relacionado", "", _value(movement, "amount"), _value(movement, "description"), _value(movement, "code"))
    for payout in payouts:
        add(_value(payout, "created_at"), _as_int(_value(payout, "caller_id")), "Split generado", "", _value(payout, "code"), _value(payout, "notes", ""), _value(payout, "code"))
        if _value(payout, "reviewed_at"):
            add(_value(payout, "reviewed_at"), _as_int(_value(payout, "reviewed_by")), "Liquidacion aprobada", "", _value(payout, "status"), "", _value(payout, "code"))
    for log in payout_logs:
        action = str(_value(log, "action", ""))
        add(_value(log, "created_at"), _as_int(_value(log, "actor_id")), action or "Evento de Split", "", "", _value(log, "details", ""), _value(log, "payout_code"))
    for liquidation in quick_liquidations:
        add(_value(liquidation, "created_at"), _as_int(_value(liquidation, "admin_id")), "Liquidacion rapida registrada", "", _value(liquidation, "total_amount"), _value(liquidation, "mode"), _value(liquidation, "code"))
    for record, _raw, movements in related_withdrawals:
        add(record.created_at, record.user_id, "Solicitud de cobro creada", "", record.audit_status, record.reason, record.code)
        for movement in movements:
            add(movement.date, movement.actor_id, movement.action, movement.old_status or "", movement.new_status or "", movement.note, record.code)
    for log in related_audit_logs:
        add(_value(log, "created_at"), _as_int(_value(log, "admin_id")), _text(_value(log, "action")), "", _value(log, "amount", ""), _value(log, "observation", ""), _value(log, "system", ""))
    rows.sort(key=lambda row: (row[0], row[3], row[7]))
    return _csv_bytes(
        ["Fecha", "Hora", "Usuario responsable", "Accion", "Valor anterior", "Valor nuevo", "Motivo", "Referencia relacionada"],
        rows,
    )


def _technical_txt(
    activity,
    payouts,
    quick_liquidations,
    generated_at: str,
    included_count: int,
    participant_count: int,
    request_count: int,
    payment_count: int,
    liquidation_status: str,
) -> bytes:
    liquidation_id = _value(quick_liquidations[-1], "id") if quick_liquidations else MISSING
    lines = [
        "INFORMACION TECNICA",
        "",
        f"Fecha y hora de generacion: {generated_at}",
        f"Codigo de actividad: {_text(_value(activity, 'code'))}",
        f"ID interno de la actividad: {_text(_value(activity, 'id'))}",
        f"ID del servidor: {_text(_value(activity, 'guild_id'))}",
        f"ID del canal: {_text(_value(activity, 'channel_id'))}",
        f"ID del mensaje: {_text(_value(activity, 'message_id'))}",
        f"ID del hilo: {_text(_value(activity, 'thread_id'))}",
        f"ID de la liquidacion: {_text(liquidation_id)}",
        f"IDs de split: {', '.join(str(row['id']) for row in payouts) or MISSING}",
        f"Version del formato del expediente: {EXPEDIENT_VERSION}",
        f"Cantidad de archivos incluidos: {included_count}",
        f"Cantidad total de participantes: {participant_count}",
        f"Cantidad total de solicitudes: {request_count}",
        f"Cantidad total de pagos: {payment_count}",
        f"Estado final de la liquidacion: {liquidation_status}",
    ]
    return ("\n".join(lines) + "\n").encode("utf-8")


def _manifest(activity_code: str, generated_at: str, files: list[tuple[str, bytes]]) -> bytes:
    payload = {
        "codigo_actividad": activity_code,
        "fecha_generacion": generated_at,
        "version_expediente": EXPEDIENT_VERSION,
        "archivos": [
            {
                "nombre": filename,
                "sha256": _sha256(data),
                "bytes": len(data),
            }
            for filename, data in files
        ],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8")


def build_liquidation_expedient_file(
    db: Database,
    guild_id: int,
    code: str,
    *,
    generated_at: str | None = None,
    name_resolver: NameResolver | None = None,
) -> LiquidationExpedientFile:
    activity = get_activity_for_expedient(db, guild_id, code)
    if activity is None:
        raise ActivityNotFoundError("No encontre esa actividad.")
    activity_id = int(activity["id"])
    activity_code = normalize_activity_code(activity["code"]) or str(activity["code"]).upper()
    payouts = _fetch_payouts(db, guild_id, activity_id)
    payout_ids = [int(row["id"]) for row in payouts]
    quick_liquidations = _fetch_quick_liquidations(db, guild_id, payout_ids)
    deposit_movements = _fetch_activity_deposit_movements(db, guild_id, activity_code)
    if not payouts and not quick_liquidations and not deposit_movements and _value(activity, "mandatory_loot_amount") is None:
        raise ActivityWithoutLiquidationError("Esta actividad todavia no tiene una liquidacion registrada.")

    generated = generated_at or utc_now_iso()
    payout_codes = [str(row["code"]) for row in payouts]
    liquidation_codes = [str(row["code"]) for row in quick_liquidations]
    terms = [activity_code, *payout_codes, *liquidation_codes]
    related_withdrawals = _related_withdrawal_records(db, guild_id, terms)
    related_audit_logs = _fetch_related_audit_logs(db, guild_id, terms)
    payout_logs = _fetch_payout_audit_logs(db, guild_id, payout_ids)
    participants_data, participant_count = _participants_csv(db, guild_id, activity, payouts, name_resolver)
    payments_data, request_count, payment_count = _payments_csv(related_withdrawals, name_resolver)
    liquidation_status = _liquidation_status(activity, payouts, quick_liquidations)
    summary_data = _summary_txt(activity, payouts, quick_liquidations, participant_count, name_resolver)
    audit_data = _audit_csv(
        activity,
        payouts,
        quick_liquidations,
        payout_logs,
        related_audit_logs,
        related_withdrawals,
        deposit_movements,
        name_resolver,
    )
    base_files = [
        ("resumen_liquidacion.txt", summary_data),
        ("participantes.csv", participants_data),
        ("pagos_cobros.csv", payments_data),
        ("historial_auditoria.csv", audit_data),
    ]
    technical_data = _technical_txt(
        activity,
        payouts,
        quick_liquidations,
        generated,
        included_count=len(base_files) + 2,
        participant_count=participant_count,
        request_count=request_count,
        payment_count=payment_count,
        liquidation_status=liquidation_status,
    )
    base_files.append(("informacion_tecnica.txt", technical_data))
    manifest_data = _manifest(activity_code, generated, base_files)
    all_files = [*base_files, ("manifest.json", manifest_data)]
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for filename, data in all_files:
            archive.writestr(filename, data)

    unavailable = [
        "Nombre de Albion: no existe un campo dedicado en la base actual.",
        "Penalizacion o descuento manual por participante: no existe un campo dedicado; solo se conservan eventos de auditoria cuando fueron registrados.",
        "Vinculo directo entre cobros y splits: se incluye cuando el codigo de actividad, split o liquidacion aparece en campos o logs existentes.",
    ]
    return LiquidationExpedientFile(
        filename=f"EXPEDIENTE_{activity_code}.zip",
        data=buffer.getvalue(),
        activity_code=activity_code,
        participant_count=participant_count,
        request_count=request_count,
        payment_count=payment_count,
        liquidation_status=liquidation_status,
        included_files=tuple(filename for filename, _data in all_files),
        unavailable_data=tuple(unavailable),
    )


@contextmanager
def liquidation_expedient_tempfile(expedient: LiquidationExpedientFile):
    suffix = "_" + re.sub(r"[^A-Z0-9-]+", "_", expedient.activity_code.upper()) + ".zip"
    handle = tempfile.NamedTemporaryFile(prefix="g3n_expediente_", suffix=suffix, delete=False)
    path = Path(handle.name)
    try:
        with handle:
            handle.write(expedient.data)
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
