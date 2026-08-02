from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from collections.abc import Callable
from typing import Iterable

from ..constants import ACTIVITY_CANCELLED, ACTIVITY_DELETED, ACTIVITY_TYPE_MANDATORY
from ..database import Database


ACTIVITY_AUDIT_START_NUMBER = 50
AUDIT_SPLIT = "Spliteada"
AUDIT_PENDING = "Pendiente"
AUDIT_NO_SPLIT = "Sin split por diseño"
AUDIT_CANCELLED = "Cancelada"
AUDIT_STATUS_LABELS = {
    AUDIT_SPLIT: "✅ Spliteada",
    AUDIT_PENDING: "⏳ Pendiente",
    AUDIT_NO_SPLIT: "🚫 Sin split por diseño",
    AUDIT_CANCELLED: "❌ Cancelada",
}
REPORT_MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024
NameResolver = Callable[[int | None], str]
_ACTIVITY_CODE_RE = re.compile(r"^ACT-(\d+)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")


@dataclass(frozen=True)
class ActivityAuditMovement:
    activity_code: str
    date: str | None
    concept: str
    user_id: int | None
    amount: int
    movement_id: int | None
    movement_code: str | None
    status: str = "registrado"


@dataclass(frozen=True)
class ActivityAuditRecord:
    internal_id: int
    code: str
    code_number: int
    guild_id: int
    name: str
    created_at: str | None
    pinged_by_id: int | None
    caller_id: int | None
    activity_type: str
    real_status: str
    audit_status: str
    total_deposited: int
    beneficiaries: int
    movement_count: int
    first_deposit_at: str | None
    last_deposit_at: str | None
    payout_ids: tuple[int, ...] = field(default_factory=tuple)
    observations: str = ""

    @property
    def audit_label(self) -> str:
        return AUDIT_STATUS_LABELS.get(self.audit_status, self.audit_status)

    @property
    def has_split_details(self) -> bool:
        return self.movement_count > 0

    @property
    def is_pending(self) -> bool:
        return self.audit_status == AUDIT_PENDING

    @property
    def is_split(self) -> bool:
        return self.audit_status == AUDIT_SPLIT


@dataclass(frozen=True)
class ActivityAuditSummary:
    total: int
    split: int
    pending: int
    no_split: int
    cancelled: int
    total_deposited: int
    oldest_activity_date: str | None
    newest_activity_date: str | None


@dataclass(frozen=True)
class ActivityAuditDataset:
    records: tuple[ActivityAuditRecord, ...]
    movements_by_activity: dict[str, tuple[ActivityAuditMovement, ...]]
    summary: ActivityAuditSummary

    def filter_records(self, mode: str) -> list[ActivityAuditRecord]:
        if mode == "split":
            return [record for record in self.records if record.is_split]
        if mode == "pending":
            return [record for record in self.records if record.is_pending]
        return list(self.records)

    def get_record(self, code: str) -> ActivityAuditRecord | None:
        normalized = normalize_activity_code(code)
        if normalized is None:
            return None
        for record in self.records:
            if record.code.upper() == normalized:
                return record
        return None

    def movements_for(self, code: str) -> tuple[ActivityAuditMovement, ...]:
        normalized = normalize_activity_code(code) or code.upper()
        return self.movements_by_activity.get(normalized, ())


@dataclass(frozen=True)
class ActivityAuditReportFile:
    filename: str
    data: bytes


def normalize_activity_code(raw: str | int | None) -> str | None:
    value = str(raw or "").strip().upper()
    if not value:
        return None
    if _DIGITS_RE.fullmatch(value):
        return f"ACT-{int(value):06d}"
    match = _ACTIVITY_CODE_RE.fullmatch(value)
    if match:
        return f"ACT-{int(match.group(1)):06d}"
    return None


def activity_code_number(code: str | None) -> int | None:
    normalized = normalize_activity_code(code)
    if normalized is None:
        return None
    match = _ACTIVITY_CODE_RE.fullmatch(normalized)
    return int(match.group(1)) if match else None


def activity_in_audit_scope(code: str | None, *, start_number: int = ACTIVITY_AUDIT_START_NUMBER) -> bool:
    number = activity_code_number(code)
    return number is not None and number >= start_number


def movement_mentions_activity_code(text: str | None, code: str) -> bool:
    if not text:
        return False
    normalized = normalize_activity_code(code)
    if normalized is None:
        return False
    pattern = re.compile(rf"(?<![A-Z0-9]){re.escape(normalized)}(?![A-Z0-9])", re.IGNORECASE)
    return bool(pattern.search(text))


def _as_int(value) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iso_date(value: str | None) -> str | None:
    if not value:
        return None
    return str(value)[:10]


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


def pending_days(created_at: str | None, *, now: datetime | None = None) -> int | None:
    created = _parse_datetime(created_at)
    if created is None:
        return None
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return max(0, (current.astimezone(timezone.utc) - created).days)


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _fetch_payout_rows(db: Database, guild_id: int, activity_ids: list[int]):
    if not activity_ids:
        return []
    placeholders = _placeholders(len(activity_ids))
    return db.fetch_all(
        f"""
        SELECT *
        FROM payouts
        WHERE guild_id = ? AND activity_id IN ({placeholders})
        ORDER BY id ASC
        """,
        (guild_id, *activity_ids),
    )


def _fetch_payout_movements(db: Database, guild_id: int, payout_ids: list[int]):
    if not payout_ids:
        return []
    placeholders = _placeholders(len(payout_ids))
    return db.fetch_all(
        f"""
        SELECT *
        FROM movements
        WHERE guild_id = ?
          AND source_table = 'payouts'
          AND source_id IN ({placeholders})
          AND type = 'DEPOSITO'
        ORDER BY created_at ASC, id ASC
        """,
        (guild_id, *payout_ids),
    )


def _fetch_participant_deposits(db: Database, payout_ids: list[int]):
    if not payout_ids:
        return []
    placeholders = _placeholders(len(payout_ids))
    return db.fetch_all(
        f"""
        SELECT payout_id, user_id, amount, deposited_at
        FROM payout_participants
        WHERE payout_id IN ({placeholders}) AND deposited_at IS NOT NULL
        ORDER BY deposited_at ASC, id ASC
        """,
        tuple(payout_ids),
    )


def _fetch_fallback_movements(db: Database, guild_id: int):
    return db.fetch_all(
        """
        SELECT *
        FROM movements
        WHERE guild_id = ?
          AND type = 'DEPOSITO'
          AND description LIKE '%ACT-%'
        ORDER BY created_at ASC, id ASC
        """,
        (guild_id,),
    )


def _audit_status(activity, total_deposited: int, participant_deposit_count: int) -> str:
    real_status = str(activity["status"] or "")
    activity_type = str(activity["activity_type"] or "regular")
    if real_status in {ACTIVITY_CANCELLED, ACTIVITY_DELETED}:
        return AUDIT_CANCELLED
    if activity_type == ACTIVITY_TYPE_MANDATORY:
        return AUDIT_NO_SPLIT
    if total_deposited > 0 or participant_deposit_count > 0:
        return AUDIT_SPLIT
    return AUDIT_PENDING


def get_activity_audit_dataset(
    db: Database,
    guild_id: int,
    *,
    start_number: int = ACTIVITY_AUDIT_START_NUMBER,
    now: datetime | None = None,
) -> ActivityAuditDataset:
    activity_rows = db.fetch_all(
        """
        SELECT *
        FROM activities
        WHERE guild_id = ? AND UPPER(code) LIKE 'ACT-%'
        ORDER BY CAST(SUBSTR(code, 5) AS INTEGER) ASC, id ASC
        """,
        (guild_id,),
    )
    activities = [row for row in activity_rows if activity_in_audit_scope(row["code"], start_number=start_number)]
    activity_ids = [int(row["id"]) for row in activities]
    activity_by_id = {int(row["id"]): row for row in activities}
    activity_code_by_id = {int(row["id"]): normalize_activity_code(row["code"]) for row in activities}

    payout_rows = _fetch_payout_rows(db, guild_id, activity_ids)
    payouts_by_id = {int(row["id"]): row for row in payout_rows}
    payout_ids_by_activity: dict[int, list[int]] = {activity_id: [] for activity_id in activity_ids}
    for payout in payout_rows:
        activity_id = _as_int(payout["activity_id"])
        if activity_id in payout_ids_by_activity:
            payout_ids_by_activity[activity_id].append(int(payout["id"]))

    payout_ids = list(payouts_by_id)
    movements_by_activity_id: dict[int, list[ActivityAuditMovement]] = {activity_id: [] for activity_id in activity_ids}
    movement_ids_seen: set[int] = set()
    for movement in _fetch_payout_movements(db, guild_id, payout_ids):
        payout = payouts_by_id.get(int(movement["source_id"]))
        if payout is None:
            continue
        activity_id = _as_int(payout["activity_id"])
        code = activity_code_by_id.get(activity_id or -1)
        if activity_id not in movements_by_activity_id or code is None:
            continue
        movement_ids_seen.add(int(movement["id"]))
        movements_by_activity_id[activity_id].append(
            ActivityAuditMovement(
                activity_code=code,
                date=str(movement["created_at"] or ""),
                concept=str(movement["description"] or movement["category"] or "Deposito"),
                user_id=_as_int(movement["user_id"]),
                amount=int(movement["amount"] or 0),
                movement_id=int(movement["id"]),
                movement_code=str(movement["code"] or ""),
            )
        )

    code_to_activity_id = {code: activity_id for activity_id, code in activity_code_by_id.items() if code}
    for movement in _fetch_fallback_movements(db, guild_id):
        movement_id = int(movement["id"])
        if movement_id in movement_ids_seen:
            continue
        description = str(movement["description"] or "")
        for code, activity_id in code_to_activity_id.items():
            if movement_mentions_activity_code(description, code):
                movements_by_activity_id[activity_id].append(
                    ActivityAuditMovement(
                        activity_code=code,
                        date=str(movement["created_at"] or ""),
                        concept=description or str(movement["category"] or "Deposito"),
                        user_id=_as_int(movement["user_id"]),
                        amount=int(movement["amount"] or 0),
                        movement_id=movement_id,
                        movement_code=str(movement["code"] or ""),
                    )
                )
                movement_ids_seen.add(movement_id)
                break

    participant_deposits_by_activity_id: dict[int, list] = {activity_id: [] for activity_id in activity_ids}
    payout_activity_by_id = {
        int(payout["id"]): _as_int(payout["activity_id"])
        for payout in payout_rows
    }
    for participant in _fetch_participant_deposits(db, payout_ids):
        activity_id = payout_activity_by_id.get(int(participant["payout_id"]))
        if activity_id in participant_deposits_by_activity_id:
            participant_deposits_by_activity_id[activity_id].append(participant)

    records: list[ActivityAuditRecord] = []
    movements_by_code: dict[str, tuple[ActivityAuditMovement, ...]] = {}
    for activity in activities:
        activity_id = int(activity["id"])
        code = activity_code_by_id[activity_id] or str(activity["code"]).upper()
        movements = sorted(
            movements_by_activity_id.get(activity_id, []),
            key=lambda item: ((item.date or ""), item.movement_id or 0),
        )
        participant_deposits = participant_deposits_by_activity_id.get(activity_id, [])
        movement_user_ids = {item.user_id for item in movements if item.user_id is not None}
        participant_user_ids = {_as_int(item["user_id"]) for item in participant_deposits if _as_int(item["user_id"]) is not None}
        total_from_movements = sum(item.amount for item in movements)
        total_from_participants = sum(int(item["amount"] or 0) for item in participant_deposits)
        total_deposited = total_from_movements if movements else total_from_participants
        beneficiary_count = len(movement_user_ids) if movements else len(participant_user_ids)
        dates = [item.date for item in movements if item.date]
        if not dates:
            dates = [str(item["deposited_at"]) for item in participant_deposits if item["deposited_at"]]
        audit_status = _audit_status(activity, total_deposited, len(participant_deposits))
        observations = ""
        if audit_status == AUDIT_PENDING:
            observations = "Sin depositos asociados"
            if str(activity["status"] or "") not in {"Finalizada", "Split creado"}:
                observations = f"Sin depositos asociados; estado actual: {activity['status']}"
        elif audit_status == AUDIT_NO_SPLIT:
            observations = "Tipo de actividad configurado sin split"
        elif audit_status == AUDIT_CANCELLED:
            observations = "Actividad cancelada o eliminada"
        else:
            observations = "Depositos asociados encontrados"

        record = ActivityAuditRecord(
            internal_id=activity_id,
            code=code,
            code_number=activity_code_number(code) or 0,
            guild_id=int(activity["guild_id"]),
            name=str(activity["name"] or ""),
            created_at=str(activity["created_at"] or "") if activity["created_at"] else None,
            pinged_by_id=_as_int(activity["pinged_by_id"]) or _as_int(activity["caller_id"]),
            caller_id=_as_int(activity["caller_id"]),
            activity_type=str(activity["activity_type"] or "regular"),
            real_status=str(activity["status"] or ""),
            audit_status=audit_status,
            total_deposited=total_deposited,
            beneficiaries=beneficiary_count,
            movement_count=len(movements),
            first_deposit_at=min(dates) if dates else None,
            last_deposit_at=max(dates) if dates else None,
            payout_ids=tuple(payout_ids_by_activity.get(activity_id, [])),
            observations=observations,
        )
        records.append(record)
        movements_by_code[code] = tuple(movements)

    summary = summarize_activity_audit(records)
    return ActivityAuditDataset(tuple(records), movements_by_code, summary)


def summarize_activity_audit(records: Iterable[ActivityAuditRecord]) -> ActivityAuditSummary:
    rows = list(records)
    created_dates = [row.created_at for row in rows if row.created_at]
    return ActivityAuditSummary(
        total=len(rows),
        split=sum(1 for row in rows if row.audit_status == AUDIT_SPLIT),
        pending=sum(1 for row in rows if row.audit_status == AUDIT_PENDING),
        no_split=sum(1 for row in rows if row.audit_status == AUDIT_NO_SPLIT),
        cancelled=sum(1 for row in rows if row.audit_status == AUDIT_CANCELLED),
        total_deposited=sum(row.total_deposited for row in rows),
        oldest_activity_date=min(created_dates) if created_dates else None,
        newest_activity_date=max(created_dates) if created_dates else None,
    )


def _csv_bytes(headers: list[str], rows: list[list]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8-sig")


def _resolved_name(name_resolver: NameResolver | None, user_id: int | None) -> str:
    if user_id is None:
        return ""
    return name_resolver(user_id) if name_resolver is not None else ""


def activity_report_rows(
    dataset: ActivityAuditDataset,
    *,
    now: datetime | None = None,
    name_resolver: NameResolver | None = None,
) -> list[list]:
    rows: list[list] = []
    for record in dataset.records:
        rows.append(
            [
                record.code,
                _iso_date(record.created_at) or "",
                _resolved_name(name_resolver, record.pinged_by_id),
                record.pinged_by_id or "",
                _resolved_name(name_resolver, record.caller_id),
                record.caller_id or "",
                record.activity_type,
                record.real_status,
                record.audit_status,
                "Si" if record.is_split else "No",
                record.total_deposited,
                record.beneficiaries,
                record.movement_count,
                _iso_date(record.first_deposit_at) or "",
                _iso_date(record.last_deposit_at) or "",
                pending_days(record.created_at, now=now) if record.is_pending else "",
                record.observations,
            ]
        )
    return rows


def movement_report_rows(dataset: ActivityAuditDataset, *, name_resolver: NameResolver | None = None) -> list[list]:
    rows: list[list] = []
    for record in dataset.records:
        for movement in dataset.movements_for(record.code):
            rows.append(
                [
                    record.code,
                    _iso_date(movement.date) or "",
                    movement.concept,
                    _resolved_name(name_resolver, movement.user_id),
                    movement.user_id or "",
                    movement.amount,
                    movement.movement_id or "",
                    movement.status,
                ]
            )
    return rows


def activity_csv(
    dataset: ActivityAuditDataset,
    *,
    now: datetime | None = None,
    name_resolver: NameResolver | None = None,
) -> bytes:
    headers = [
        "actividad_id",
        "fecha_actividad",
        "pingueada_por",
        "pingueada_por_discord_id",
        "caller_asignado",
        "caller_discord_id",
        "tipo_actividad",
        "estado_real",
        "estado_auditoria",
        "spliteada",
        "total_depositado",
        "beneficiarios_unicos",
        "cantidad_movimientos",
        "fecha_primer_deposito",
        "fecha_ultimo_deposito",
        "dias_pendiente",
        "observaciones",
    ]
    return _csv_bytes(headers, activity_report_rows(dataset, now=now, name_resolver=name_resolver))


def movement_csv(dataset: ActivityAuditDataset, *, name_resolver: NameResolver | None = None) -> bytes:
    headers = [
        "actividad_id",
        "fecha_deposito",
        "concepto",
        "usuario",
        "usuario_discord_id",
        "cantidad",
        "movimiento_id",
        "estado_movimiento",
    ]
    return _csv_bytes(headers, movement_report_rows(dataset, name_resolver=name_resolver))


def _zip_report(files: list[ActivityAuditReportFile], filename: str) -> ActivityAuditReportFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for report_file in files:
            archive.writestr(report_file.filename, report_file.data)
    return ActivityAuditReportFile(filename, buffer.getvalue())


def _split_csv_file(filename: str, data: bytes, max_bytes: int) -> list[ActivityAuditReportFile]:
    if len(data) <= max_bytes:
        return [ActivityAuditReportFile(filename, data)]
    text = data.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines:
        return [ActivityAuditReportFile(filename, data)]
    header = lines[0]
    parts: list[ActivityAuditReportFile] = []
    current = header
    part = 1
    stem = filename.rsplit(".", 1)[0]
    suffix = filename.rsplit(".", 1)[1] if "." in filename else "csv"
    for line in lines[1:]:
        encoded = (current + line).encode("utf-8-sig")
        if len(encoded) > max_bytes and current != header:
            parts.append(ActivityAuditReportFile(f"{stem}_parte_{part}.{suffix}", current.encode("utf-8-sig")))
            part += 1
            current = header + line
        else:
            current += line
    if current:
        parts.append(ActivityAuditReportFile(f"{stem}_parte_{part}.{suffix}", current.encode("utf-8-sig")))
    return parts


def build_activity_audit_report_files(
    db: Database,
    guild_id: int,
    *,
    today: str | None = None,
    max_bytes: int = REPORT_MAX_ATTACHMENT_BYTES,
    name_resolver: NameResolver | None = None,
) -> list[ActivityAuditReportFile]:
    dataset = get_activity_audit_dataset(db, guild_id)
    stamp = today or datetime.now().date().isoformat()
    activity_name = "actividades_desde_ACT-000050.csv"
    detail_name = "detalle_splits_desde_ACT-000050.csv"
    base_files = [
        ActivityAuditReportFile(activity_name, activity_csv(dataset, name_resolver=name_resolver)),
        ActivityAuditReportFile(detail_name, movement_csv(dataset, name_resolver=name_resolver)),
    ]
    zip_file = _zip_report(base_files, f"auditoria_actividades_G3NESYS_{stamp}.zip")
    if len(zip_file.data) <= max_bytes:
        return [zip_file]
    result: list[ActivityAuditReportFile] = []
    for report_file in base_files:
        result.extend(_split_csv_file(report_file.filename, report_file.data, max_bytes))
    return result