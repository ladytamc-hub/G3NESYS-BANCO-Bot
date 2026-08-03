from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from collections.abc import Callable, Iterable

from ..constants import (
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_CANCELLED,
    WITHDRAWAL_DELEGATED,
    WITHDRAWAL_LIQUIDATED,
    WITHDRAWAL_PAID,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REASSIGNMENT,
    WITHDRAWAL_REJECTED,
    WITHDRAWAL_UNPAID,
)
from ..database import Database


WITHDRAWAL_AUDIT_PAGE_SIZE = 4
WITHDRAWAL_AUDIT_DETAIL_PAGE_SIZE = 8
REPORT_MAX_ATTACHMENT_BYTES = 24 * 1024 * 1024
NameResolver = Callable[[int | None], str]
_WITHDRAWAL_CODE_RE = re.compile(r"^COBRO-(\d+)$", re.IGNORECASE)
_DIGITS_RE = re.compile(r"^\d+$")
_TAG_RE = re.compile(r"\[[^\]]+\]")
_NON_SEARCH_RE = re.compile(r"[^a-z0-9]+")

AUDIT_PENDING = "Pendiente"
AUDIT_APPROVED = "Aprobado pendiente de pago"
AUDIT_PARTIAL = "Pago parcial"
AUDIT_PAID = "Pagado total"
AUDIT_REJECTED = "Rechazado"
AUDIT_UNPAID = "No pagado"
AUDIT_RETURNED = "Regresado"
AUDIT_CANCELLED = "Cancelado"
AUDIT_STATUS_LABELS = {
    AUDIT_PENDING: "ðŸŸ¡ Pendiente",
    AUDIT_APPROVED: "ðŸ”µ Aprobado pendiente de pago",
    AUDIT_PARTIAL: "ðŸŸ£ Pago parcial",
    AUDIT_PAID: "ðŸŸ¢ Pagado total",
    AUDIT_REJECTED: "ðŸ”´ Rechazado",
    AUDIT_UNPAID: "ðŸŸ  No pagado",
    AUDIT_RETURNED: "â†©ï¸ Regresado",
    AUDIT_CANCELLED: "âš« Cancelado",
}


@dataclass(frozen=True)
class WithdrawalAuditMovement:
    withdrawal_code: str
    date: str | None
    action: str
    actor_id: int | None
    amount: int | None = None
    old_status: str | None = None
    new_status: str | None = None
    note: str = ""
    movement_id: int | None = None
    movement_code: str | None = None
    source: str = ""


@dataclass(frozen=True)
class WithdrawalAuditRecord:
    internal_id: int
    code: str
    code_number: int
    guild_id: int
    user_id: int
    amount_requested: int
    amount_paid: int
    status: str
    audit_status: str
    reason: str
    created_at: str | None
    approved_by: int | None = None
    approved_at: str | None = None
    liquidated_by: int | None = None
    liquidated_at: str | None = None
    rejected_by: int | None = None
    rejected_at: str | None = None
    rejection_reason: str = ""
    assigned_officer_id: int | None = None
    delegated_by: int | None = None
    payment_place: str = ""
    payment_schedule: str = ""
    delegated_at: str | None = None
    returned_at: str | None = None
    return_reason: str = ""
    closed_at: str | None = None
    updated_at: str | None = None
    notification_channel_id: int | None = None
    notification_message_id: int | None = None
    paid_by_ids: tuple[int, ...] = field(default_factory=tuple)
    last_payment_at: str | None = None

    @property
    def audit_label(self) -> str:
        return AUDIT_STATUS_LABELS.get(self.audit_status, self.audit_status)

    @property
    def pending_amount(self) -> int:
        if self.audit_status in {AUDIT_REJECTED, AUDIT_CANCELLED, AUDIT_UNPAID}:
            return 0
        return max(0, self.amount_requested - self.amount_paid)

    @property
    def has_pending_balance(self) -> bool:
        return self.pending_amount > 0 and self.audit_status not in {AUDIT_REJECTED, AUDIT_CANCELLED, AUDIT_UNPAID}

    @property
    def is_partial(self) -> bool:
        return self.audit_status == AUDIT_PARTIAL

    @property
    def is_paid(self) -> bool:
        return self.audit_status == AUDIT_PAID

    @property
    def message_url(self) -> str | None:
        if self.guild_id and self.notification_channel_id and self.notification_message_id:
            return f"https://discord.com/channels/{self.guild_id}/{self.notification_channel_id}/{self.notification_message_id}"
        return None


@dataclass(frozen=True)
class WithdrawalAuditSummary:
    total_requested: int
    total_paid: int
    total_pending: int
    total_rejected: int
    open_count: int
    partial_count: int
    total_count: int
    oldest_date: str | None
    newest_date: str | None


@dataclass(frozen=True)
class WithdrawalAuditDataset:
    records: tuple[WithdrawalAuditRecord, ...]
    movements_by_withdrawal: dict[str, tuple[WithdrawalAuditMovement, ...]]
    summary: WithdrawalAuditSummary

    def sorted_records(self, order: str = "desc") -> list[WithdrawalAuditRecord]:
        reverse = order != "asc"
        return sorted(
            self.records,
            key=lambda record: ((record.created_at or ""), record.code_number, record.internal_id),
            reverse=reverse,
        )

    def filter_records(self, mode: str, order: str = "desc") -> list[WithdrawalAuditRecord]:
        rows = self.sorted_records(order)
        if mode == "pending":
            return [record for record in rows if record.has_pending_balance]
        if mode == "partial":
            return [record for record in rows if record.audit_status == AUDIT_PARTIAL]
        if mode == "paid":
            return [record for record in rows if record.audit_status == AUDIT_PAID]
        if mode == "rejected":
            return [record for record in rows if record.audit_status == AUDIT_REJECTED]
        if mode == "returned":
            return [record for record in rows if record.audit_status == AUDIT_RETURNED]
        if mode == "cancelled":
            return [record for record in rows if record.audit_status == AUDIT_CANCELLED]
        if mode == "unpaid":
            return [record for record in rows if record.audit_status == AUDIT_UNPAID]
        return rows

    def get_record(self, code: str) -> WithdrawalAuditRecord | None:
        normalized = normalize_withdrawal_code(code)
        if normalized is None:
            return None
        for record in self.records:
            if record.code.upper() == normalized:
                return record
        return None

    def movements_for(self, code: str) -> tuple[WithdrawalAuditMovement, ...]:
        normalized = normalize_withdrawal_code(code) or code.upper()
        return self.movements_by_withdrawal.get(normalized, ())


@dataclass(frozen=True)
class WithdrawalAuditReportFile:
    filename: str
    data: bytes


def normalize_withdrawal_code(raw: str | int | None) -> str | None:
    value = str(raw or "").strip().upper()
    if not value:
        return None
    if _DIGITS_RE.fullmatch(value):
        return f"COBRO-{int(value):06d}"
    match = _WITHDRAWAL_CODE_RE.fullmatch(value)
    if match:
        return f"COBRO-{int(match.group(1)):06d}"
    return None


def withdrawal_code_number(code: str | None) -> int | None:
    normalized = normalize_withdrawal_code(code)
    if normalized is None:
        return None
    match = _WITHDRAWAL_CODE_RE.fullmatch(normalized)
    return int(match.group(1)) if match else None


def normalize_user_search(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    value = value.removeprefix("@")
    value = _TAG_RE.sub("", value)
    return _NON_SEARCH_RE.sub("", value)


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


def _parse_day(value: str | None) -> date | None:
    text = _iso_date(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def _placeholders(count: int) -> str:
    return ",".join("?" for _ in range(count))


def _audit_status(row, paid: int) -> str:
    status = str(row["status"] or "")
    requested = int(row["amount_requested"] or 0)
    if status == WITHDRAWAL_REJECTED:
        return AUDIT_REJECTED
    if status == WITHDRAWAL_CANCELLED:
        return AUDIT_CANCELLED
    if status == WITHDRAWAL_UNPAID:
        return AUDIT_UNPAID
    if status == WITHDRAWAL_REASSIGNMENT:
        return AUDIT_RETURNED
    if status == WITHDRAWAL_PARTIAL:
        return AUDIT_PARTIAL
    if status in {WITHDRAWAL_PAID, WITHDRAWAL_LIQUIDATED} or (requested > 0 and paid >= requested):
        return AUDIT_PAID
    if status in {WITHDRAWAL_APPROVED, WITHDRAWAL_DELEGATED}:
        return AUDIT_APPROVED
    return AUDIT_PENDING


def _payment_source(row) -> str:
    parts = []
    if row["payment_place"]:
        parts.append(str(row["payment_place"]))
    if row["payment_schedule"]:
        parts.append(str(row["payment_schedule"]))
    return " | ".join(parts)


def _log_action_label(action_type: str) -> str:
    labels = {
        "no_aprobada": "Solicitud rechazada",
        "pago_completo": "Pago total registrado",
        "pago_parcial": "Pago parcial registrado",
        "no_pagado": "Solicitud marcada no pagada",
        "delegacion": "Pago delegado",
        "aprobacion_delegacion": "Solicitud aprobada y delegada",
        "retorno_oficial": "Solicitud regresada",
    }
    return labels.get(action_type, action_type.replace("_", " ").title())


def _fetch_logs(db: Database, withdrawal_ids: list[int]):
    if not withdrawal_ids:
        return []
    placeholders = _placeholders(len(withdrawal_ids))
    return db.fetch_all(
        f"""
        SELECT *
        FROM withdrawal_action_logs
        WHERE withdrawal_id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        tuple(withdrawal_ids),
    )


def _fetch_movements(db: Database, guild_id: int, withdrawal_ids: list[int]):
    if not withdrawal_ids:
        return []
    placeholders = _placeholders(len(withdrawal_ids))
    return db.fetch_all(
        f"""
        SELECT *
        FROM movements
        WHERE guild_id = ?
          AND source_table = 'withdrawals'
          AND source_id IN ({placeholders})
        ORDER BY created_at ASC, id ASC
        """,
        (guild_id, *withdrawal_ids),
    )


def get_withdrawal_audit_dataset(db: Database, guild_id: int) -> WithdrawalAuditDataset:
    withdrawal_rows = db.fetch_all(
        """
        SELECT *
        FROM withdrawals
        WHERE guild_id = ?
        ORDER BY id ASC
        """,
        (guild_id,),
    )
    withdrawal_ids = [int(row["id"]) for row in withdrawal_rows]
    logs_by_withdrawal: dict[int, list] = {withdrawal_id: [] for withdrawal_id in withdrawal_ids}
    for log in _fetch_logs(db, withdrawal_ids):
        logs_by_withdrawal.setdefault(int(log["withdrawal_id"]), []).append(log)

    movements_by_withdrawal_id: dict[int, list] = {withdrawal_id: [] for withdrawal_id in withdrawal_ids}
    for movement in _fetch_movements(db, guild_id, withdrawal_ids):
        movements_by_withdrawal_id.setdefault(int(movement["source_id"]), []).append(movement)

    records: list[WithdrawalAuditRecord] = []
    movements_by_code: dict[str, tuple[WithdrawalAuditMovement, ...]] = {}
    for row in withdrawal_rows:
        withdrawal_id = int(row["id"])
        code = normalize_withdrawal_code(row["code"]) or str(row["code"]).upper()
        movement_rows = movements_by_withdrawal_id.get(withdrawal_id, [])
        payment_logs = [
            log for log in logs_by_withdrawal.get(withdrawal_id, [])
            if str(log["action_type"]) in {"pago_completo", "pago_parcial"}
        ]
        paid_from_logs = sum(int(log["amount"] or 0) for log in payment_logs)
        paid_from_movements = sum(int(movement["amount"] or 0) for movement in movement_rows)
        stored_paid = int(row["amount_liquidated"] or 0)
        amount_paid = max(stored_paid, paid_from_logs, paid_from_movements)
        audit_status = _audit_status(row, amount_paid)
        paid_by_ids = tuple(
            dict.fromkeys(
                [
                    _as_int(log["author_id"])
                    for log in payment_logs
                    if _as_int(log["author_id"]) is not None
                ]
                + [
                    _as_int(movement["created_by"])
                    for movement in movement_rows
                    if _as_int(movement["created_by"]) is not None
                ]
                + ([_as_int(row["liquidated_by"])] if _as_int(row["liquidated_by"]) is not None else [])
            )
        )
        payment_dates = [str(log["created_at"]) for log in payment_logs if log["created_at"]]
        payment_dates.extend(str(movement["created_at"]) for movement in movement_rows if movement["created_at"])
        if row["liquidated_at"]:
            payment_dates.append(str(row["liquidated_at"]))

        history: list[WithdrawalAuditMovement] = [
            WithdrawalAuditMovement(
                withdrawal_code=code,
                date=str(row["created_at"] or "") if row["created_at"] else None,
                action="Solicitud creada",
                actor_id=_as_int(row["user_id"]),
                amount=int(row["amount_requested"] or 0),
                new_status=WITHDRAWAL_PENDING,
                note=str(row["reason"] or ""),
            )
        ]
        if row["approved_at"] and row["approved_by"]:
            history.append(
                WithdrawalAuditMovement(
                    withdrawal_code=code,
                    date=str(row["approved_at"]),
                    action="Solicitud aprobada",
                    actor_id=_as_int(row["approved_by"]),
                    amount=int(row["amount_requested"] or 0),
                    old_status=WITHDRAWAL_PENDING,
                    new_status=WITHDRAWAL_APPROVED,
                    note=str(row["approval_admin_message"] or ""),
                )
            )
        for log in logs_by_withdrawal.get(withdrawal_id, []):
            history.append(
                WithdrawalAuditMovement(
                    withdrawal_code=code,
                    date=str(log["created_at"] or "") if log["created_at"] else None,
                    action=_log_action_label(str(log["action_type"])),
                    actor_id=_as_int(log["author_id"]),
                    amount=_as_int(log["amount"]),
                    old_status=str(log["old_status"] or "") or None,
                    new_status=str(log["new_status"] or "") or None,
                    note=str(log["note"] or ""),
                    source=_payment_source(row),
                )
            )
        logged_rejection = any(str(log["action_type"]) == "no_aprobada" for log in logs_by_withdrawal.get(withdrawal_id, []))
        if row["rejected_at"] and row["rejected_by"] and not logged_rejection:
            history.append(
                WithdrawalAuditMovement(
                    withdrawal_code=code,
                    date=str(row["rejected_at"]),
                    action="Solicitud rechazada",
                    actor_id=_as_int(row["rejected_by"]),
                    amount=int(row["amount_requested"] or 0),
                    old_status=WITHDRAWAL_PENDING,
                    new_status=WITHDRAWAL_REJECTED,
                    note=str(row["rejection_reason"] or ""),
                )
            )
        for movement in movement_rows:
            history.append(
                WithdrawalAuditMovement(
                    withdrawal_code=code,
                    date=str(movement["created_at"] or "") if movement["created_at"] else None,
                    action="Movimiento financiero",
                    actor_id=_as_int(movement["created_by"]),
                    amount=int(movement["amount"] or 0),
                    note=str(movement["description"] or ""),
                    movement_id=int(movement["id"]),
                    movement_code=str(movement["code"] or ""),
                    source=str(movement["category"] or ""),
                )
            )
        history.sort(key=lambda item: ((item.date or ""), item.movement_id or 0, item.action))

        records.append(
            WithdrawalAuditRecord(
                internal_id=withdrawal_id,
                code=code,
                code_number=withdrawal_code_number(code) or 0,
                guild_id=int(row["guild_id"]),
                user_id=int(row["user_id"]),
                amount_requested=int(row["amount_requested"] or 0),
                amount_paid=amount_paid,
                status=str(row["status"] or ""),
                audit_status=audit_status,
                reason=str(row["reason"] or ""),
                created_at=str(row["created_at"] or "") if row["created_at"] else None,
                approved_by=_as_int(row["approved_by"]),
                approved_at=str(row["approved_at"] or "") if row["approved_at"] else None,
                liquidated_by=_as_int(row["liquidated_by"]),
                liquidated_at=str(row["liquidated_at"] or "") if row["liquidated_at"] else None,
                rejected_by=_as_int(row["rejected_by"]),
                rejected_at=str(row["rejected_at"] or "") if row["rejected_at"] else None,
                rejection_reason=str(row["rejection_reason"] or ""),
                assigned_officer_id=_as_int(row["assigned_officer_id"]),
                delegated_by=_as_int(row["delegated_by"]),
                payment_place=str(row["payment_place"] or ""),
                payment_schedule=str(row["payment_schedule"] or ""),
                delegated_at=str(row["delegated_at"] or "") if row["delegated_at"] else None,
                returned_at=str(row["returned_at"] or "") if row["returned_at"] else None,
                return_reason=str(row["return_reason"] or ""),
                closed_at=str(row["closed_at"] or "") if row["closed_at"] else None,
                updated_at=str(row["updated_at"] or "") if row["updated_at"] else None,
                notification_channel_id=_as_int(row["notification_channel_id"]),
                notification_message_id=_as_int(row["notification_message_id"]),
                paid_by_ids=paid_by_ids,
                last_payment_at=max(payment_dates) if payment_dates else None,
            )
        )
        movements_by_code[code] = tuple(history)

    return WithdrawalAuditDataset(tuple(records), movements_by_code, summarize_withdrawal_audit(records))


def summarize_withdrawal_audit(records: Iterable[WithdrawalAuditRecord]) -> WithdrawalAuditSummary:
    rows = list(records)
    created_dates = [row.created_at for row in rows if row.created_at]
    return WithdrawalAuditSummary(
        total_requested=sum(row.amount_requested for row in rows),
        total_paid=sum(row.amount_paid for row in rows),
        total_pending=sum(row.pending_amount for row in rows),
        total_rejected=sum(row.amount_requested for row in rows if row.audit_status == AUDIT_REJECTED),
        open_count=sum(1 for row in rows if row.has_pending_balance),
        partial_count=sum(1 for row in rows if row.audit_status == AUDIT_PARTIAL),
        total_count=len(rows),
        oldest_date=min(created_dates) if created_dates else None,
        newest_date=max(created_dates) if created_dates else None,
    )


def search_withdrawal_records(
    dataset: WithdrawalAuditDataset,
    query: str,
    *,
    name_resolver: NameResolver | None = None,
) -> list[WithdrawalAuditRecord]:
    code = normalize_withdrawal_code(query)
    if code is not None:
        record = dataset.get_record(code)
        return [record] if record is not None else []
    needle = normalize_user_search(query)
    if not needle:
        return []
    matches = []
    for record in dataset.sorted_records("desc"):
        names = [str(record.user_id)]
        if name_resolver is not None:
            names.append(name_resolver(record.user_id))
        normalized_names = [normalize_user_search(name) for name in names if name]
        if any(needle in name or name in needle for name in normalized_names if name):
            matches.append(record)
    return matches


def filter_records_for_report(
    dataset: WithdrawalAuditDataset,
    *,
    mode: str = "all",
    today: date | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
) -> list[WithdrawalAuditRecord]:
    current = today or datetime.now().date()
    if mode == "range":
        try:
            start = date.fromisoformat(str(date_from or ""))
            end = date.fromisoformat(str(date_to or ""))
        except ValueError as exc:
            raise ValueError("Usa fechas con formato YYYY-MM-DD.") from exc
        if start > end:
            raise ValueError("La fecha inicial no puede ser mayor que la fecha final.")
        return [
            row for row in dataset.sorted_records("desc")
            if (day := _parse_day(row.created_at)) is not None and start <= day <= end
        ]
    if mode == "today":
        return [row for row in dataset.sorted_records("desc") if _parse_day(row.created_at) == current]
    if mode == "last_7_days":
        start = current - timedelta(days=6)
        return [row for row in dataset.sorted_records("desc") if (day := _parse_day(row.created_at)) is not None and start <= day <= current]
    if mode == "month":
        return [row for row in dataset.sorted_records("desc") if (day := _parse_day(row.created_at)) is not None and day.year == current.year and day.month == current.month]
    if mode == "rejected_returned":
        return [row for row in dataset.sorted_records("desc") if row.audit_status in {AUDIT_REJECTED, AUDIT_RETURNED}]
    return dataset.filter_records(mode, "desc")


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


def withdrawal_report_rows(
    records: Iterable[WithdrawalAuditRecord],
    *,
    name_resolver: NameResolver | None = None,
) -> list[list]:
    rows = []
    for record in records:
        rows.append(
            [
                record.code,
                _resolved_name(name_resolver, record.user_id),
                record.user_id,
                record.reason,
                record.amount_requested,
                record.amount_paid,
                record.pending_amount,
                record.audit_status,
                _iso_date(record.created_at) or "",
                _resolved_name(name_resolver, record.approved_by),
                ", ".join(_resolved_name(name_resolver, user_id) or str(user_id) for user_id in record.paid_by_ids),
                _iso_date(record.last_payment_at) or "",
                _resolved_name(name_resolver, record.rejected_by) if record.rejected_by else _resolved_name(name_resolver, record.assigned_officer_id),
                record.rejection_reason or record.return_reason,
                record.payment_place,
                record.message_url or "",
            ]
        )
    return rows


def movement_report_rows(
    dataset: WithdrawalAuditDataset,
    records: Iterable[WithdrawalAuditRecord],
    *,
    name_resolver: NameResolver | None = None,
) -> list[list]:
    rows = []
    for record in records:
        for movement in dataset.movements_for(record.code):
            rows.append(
                [
                    record.code,
                    movement.date or "",
                    movement.action,
                    _resolved_name(name_resolver, movement.actor_id),
                    movement.actor_id or "",
                    movement.amount if movement.amount is not None else "",
                    movement.old_status or "",
                    movement.new_status or "",
                    movement.source,
                    movement.note,
                    movement.movement_code or "",
                    movement.movement_id or "",
                ]
            )
    return rows


def withdrawals_csv(records: Iterable[WithdrawalAuditRecord], *, name_resolver: NameResolver | None = None) -> bytes:
    headers = [
        "codigo_solicitud",
        "solicitante",
        "discord_id",
        "concepto",
        "monto_solicitado",
        "total_pagado",
        "saldo_pendiente",
        "estado",
        "fecha_creacion",
        "aprobado_por",
        "pagado_por",
        "fecha_ultimo_pago",
        "rechazado_o_regresado_por",
        "motivo",
        "fuente_o_lugar_pago",
        "enlace_mensaje",
    ]
    return _csv_bytes(headers, withdrawal_report_rows(records, name_resolver=name_resolver))


def movements_csv(
    dataset: WithdrawalAuditDataset,
    records: Iterable[WithdrawalAuditRecord],
    *,
    name_resolver: NameResolver | None = None,
) -> bytes:
    headers = [
        "codigo_solicitud",
        "fecha_movimiento",
        "accion",
        "responsable",
        "responsable_discord_id",
        "monto",
        "estado_anterior",
        "estado_nuevo",
        "fuente",
        "motivo_o_nota",
        "codigo_movimiento",
        "movimiento_id",
    ]
    return _csv_bytes(headers, movement_report_rows(dataset, records, name_resolver=name_resolver))


def _zip_report(files: list[WithdrawalAuditReportFile], filename: str) -> WithdrawalAuditReportFile:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for report_file in files:
            archive.writestr(report_file.filename, report_file.data)
    return WithdrawalAuditReportFile(filename, buffer.getvalue())


def _split_csv_file(filename: str, data: bytes, max_bytes: int) -> list[WithdrawalAuditReportFile]:
    if len(data) <= max_bytes:
        return [WithdrawalAuditReportFile(filename, data)]
    text = data.decode("utf-8-sig")
    lines = text.splitlines(keepends=True)
    if not lines:
        return [WithdrawalAuditReportFile(filename, data)]
    header = lines[0]
    parts: list[WithdrawalAuditReportFile] = []
    current = header
    part = 1
    stem = filename.rsplit(".", 1)[0]
    suffix = filename.rsplit(".", 1)[1] if "." in filename else "csv"
    for line in lines[1:]:
        encoded = (current + line).encode("utf-8-sig")
        if len(encoded) > max_bytes and current != header:
            parts.append(WithdrawalAuditReportFile(f"{stem}_parte_{part}.{suffix}", current.encode("utf-8-sig")))
            part += 1
            current = header + line
        else:
            current += line
    if current:
        parts.append(WithdrawalAuditReportFile(f"{stem}_parte_{part}.{suffix}", current.encode("utf-8-sig")))
    return parts


def build_withdrawal_audit_report_files(
    db: Database,
    guild_id: int,
    *,
    mode: str = "all",
    today: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    max_bytes: int = REPORT_MAX_ATTACHMENT_BYTES,
    name_resolver: NameResolver | None = None,
) -> list[WithdrawalAuditReportFile]:
    dataset = get_withdrawal_audit_dataset(db, guild_id)
    current = date.fromisoformat(today) if today else datetime.now().date()
    records = filter_records_for_report(dataset, mode=mode, today=current, date_from=date_from, date_to=date_to)
    stamp = current.isoformat()
    base_files = [
        WithdrawalAuditReportFile("auditoria_cobros.csv", withdrawals_csv(records, name_resolver=name_resolver)),
        WithdrawalAuditReportFile("historial_cobros.csv", movements_csv(dataset, records, name_resolver=name_resolver)),
    ]
    zip_file = _zip_report(base_files, f"auditoria_pagos_cobros_G3NESYS_{mode}_{stamp}.zip")
    if len(zip_file.data) <= max_bytes:
        return [zip_file]
    result: list[WithdrawalAuditReportFile] = []
    for report_file in base_files:
        result.extend(_split_csv_file(report_file.filename, report_file.data, max_bytes))
    return result




