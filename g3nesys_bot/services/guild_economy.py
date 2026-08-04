from __future__ import annotations

import csv
import io
import os
import re
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from ..database import Database
from ..utils import format_amount


MISSING = "No registrado"
NameResolver = Callable[[int | None], str]


@dataclass(frozen=True)
class GuildEconomySummary:
    guild_id: int
    total_deposited: int
    total_paid: int
    total_pending: int
    users_with_pending_balance: int
    generated_at: str


@dataclass(frozen=True)
class GuildEconomyUserRow:
    user_id: int
    discord_name: str
    display_name: str
    albion_name: str
    total_deposited: int
    total_paid: int
    available: int
    retained: int
    seized: int
    last_deposit_at: str
    last_payment_at: str
    status: str


@dataclass(frozen=True)
class GuildEconomyReport:
    summary: GuildEconomySummary
    rows: tuple[GuildEconomyUserRow, ...]
    csv_data: bytes
    filename: str


def _utc_stamp(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _utc_date(now: datetime | None = None) -> str:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc).date().isoformat()


def _csv_safe(value) -> str:
    text = str(value if value is not None else MISSING).strip() or MISSING
    if text[:1] in {"=", "+", "-", "@"}:
        return "'" + text
    return text


def _fetch_amounts_by_user(db: Database, guild_id: int, movement_type: str):
    return {
        int(row["user_id"]): row
        for row in db.fetch_all(
            """
            SELECT user_id,
                   COALESCE(SUM(amount), 0) AS total,
                   MAX(created_at) AS last_at
            FROM movements
            WHERE guild_id = ?
              AND type = ?
              AND user_id IS NOT NULL
            GROUP BY user_id
            """,
            (guild_id, movement_type),
        )
    }


def _fetch_accounts_by_user(db: Database, guild_id: int):
    return {
        int(row["user_id"]): row
        for row in db.fetch_all(
            """
            SELECT user_id, available, retained, seized
            FROM accounts
            WHERE guild_id = ?
            """,
            (guild_id,),
        )
    }


def _fetch_historical_names(db: Database, guild_id: int) -> dict[int, str]:
    names: dict[int, str] = {}
    queries = [
        """
        SELECT ap.user_id, ap.display_name
        FROM activity_participants ap
        JOIN activities a ON a.id = ap.activity_id
        WHERE a.guild_id = ?
        ORDER BY ap.id ASC
        """,
        """
        SELECT user_id, display_name
        FROM activity_voice_stats
        WHERE guild_id = ?
        ORDER BY id ASC
        """,
        """
        SELECT user_id, display_name
        FROM activity_join_requests
        WHERE guild_id = ?
        ORDER BY id ASC
        """,
    ]
    for query in queries:
        for row in db.fetch_all(query, (guild_id,)):
            text = str(row["display_name"] or "").strip()
            if text:
                names[int(row["user_id"])] = text
    return names


def _user_ids(*sources) -> list[int]:
    ids: set[int] = set()
    for source in sources:
        ids.update(int(item) for item in source)
    return sorted(ids)


def _status(available: int) -> str:
    if available < 0:
        return "Saldo negativo"
    return "Pendiente" if available > 0 else "Sin saldo"


def get_guild_economy_summary(
    db: Database,
    guild_id: int,
    *,
    generated_at: str | None = None,
) -> GuildEconomySummary:
    deposits = _fetch_amounts_by_user(db, guild_id, "DEPOSITO")
    payments = _fetch_amounts_by_user(db, guild_id, "LIQUIDACION")
    accounts = _fetch_accounts_by_user(db, guild_id)
    pending_values = [max(0, int(row["available"] or 0)) for row in accounts.values()]
    return GuildEconomySummary(
        guild_id=guild_id,
        total_deposited=sum(int(row["total"] or 0) for row in deposits.values()),
        total_paid=sum(int(row["total"] or 0) for row in payments.values()),
        total_pending=sum(pending_values),
        users_with_pending_balance=sum(1 for value in pending_values if value > 0),
        generated_at=generated_at or _utc_stamp(),
    )


def get_guild_user_balance_report(
    db: Database,
    guild_id: int,
    *,
    generated_at: str | None = None,
    name_resolver: NameResolver | None = None,
) -> tuple[GuildEconomySummary, tuple[GuildEconomyUserRow, ...]]:
    deposits = _fetch_amounts_by_user(db, guild_id, "DEPOSITO")
    payments = _fetch_amounts_by_user(db, guild_id, "LIQUIDACION")
    accounts = _fetch_accounts_by_user(db, guild_id)
    historical_names = _fetch_historical_names(db, guild_id)
    summary = get_guild_economy_summary(db, guild_id, generated_at=generated_at)
    rows: list[GuildEconomyUserRow] = []
    for user_id in _user_ids(deposits, payments, accounts):
        account = accounts.get(user_id)
        deposited = deposits.get(user_id)
        paid = payments.get(user_id)
        available = int(account["available"] or 0) if account is not None else 0
        retained = int(account["retained"] or 0) if account is not None else 0
        seized = int(account["seized"] or 0) if account is not None else 0
        resolved_name = str(name_resolver(user_id) if name_resolver else "").strip()
        historical_name = historical_names.get(user_id, "")
        display_name = resolved_name or historical_name or "Usuario no disponible"
        rows.append(
            GuildEconomyUserRow(
                user_id=user_id,
                discord_name=resolved_name or historical_name or "Usuario no disponible",
                display_name=display_name,
                albion_name=MISSING,
                total_deposited=int(deposited["total"] or 0) if deposited is not None else 0,
                total_paid=int(paid["total"] or 0) if paid is not None else 0,
                available=available,
                retained=retained,
                seized=seized,
                last_deposit_at=str(deposited["last_at"] or MISSING) if deposited is not None else MISSING,
                last_payment_at=str(paid["last_at"] or MISSING) if paid is not None else MISSING,
                status=_status(available),
            )
        )
    rows.sort(key=lambda row: (-max(0, row.available), row.display_name.casefold(), row.user_id))
    return summary, tuple(rows)


def safe_guild_filename_part(name: str | None) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or "G3NESYS").strip())
    cleaned = re.sub(r"_+", "_", cleaned).strip("_")
    return cleaned[:60] or "G3NESYS"


def build_guild_economy_csv_report(
    db: Database,
    guild_id: int,
    *,
    guild_name: str | None = None,
    generated_at: str | None = None,
    name_resolver: NameResolver | None = None,
    today: str | None = None,
) -> GuildEconomyReport:
    summary, rows = get_guild_user_balance_report(
        db,
        guild_id,
        generated_at=generated_at,
        name_resolver=name_resolver,
    )
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["Resumen", "Valor"])
    writer.writerow(["Total depositado en balances", summary.total_deposited])
    writer.writerow(["Total depositado en balances formateado", format_amount(summary.total_deposited)])
    writer.writerow(["Total ya pagado", summary.total_paid])
    writer.writerow(["Total ya pagado formateado", format_amount(summary.total_paid)])
    writer.writerow(["Total pendiente por pagar", summary.total_pending])
    writer.writerow(["Total pendiente por pagar formateado", format_amount(summary.total_pending)])
    writer.writerow(["Usuarios con saldo pendiente", summary.users_with_pending_balance])
    writer.writerow(["Fecha UTC de generacion", summary.generated_at])
    writer.writerow(["Guild ID", guild_id])
    writer.writerow(["Nombre del servidor", guild_name or "G3NESYS"])
    writer.writerow([])
    writer.writerow(
        [
            "Discord ID",
            "Nombre de Discord",
            "Nombre visible",
            "Nombre de Albion",
            "total_depositado_historico_valor",
            "total_depositado_historico_formateado",
            "total_pagado_historico_valor",
            "total_pagado_historico_formateado",
            "saldo_disponible_valor",
            "saldo_disponible_formateado",
            "saldo_retenido_valor",
            "saldo_retenido_formateado",
            "saldo_decomisado_valor",
            "saldo_decomisado_formateado",
            "Fecha del ultimo deposito",
            "Fecha del ultimo pago",
            "Estado",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.user_id,
                _csv_safe(row.discord_name),
                _csv_safe(row.display_name),
                row.albion_name,
                row.total_deposited,
                format_amount(row.total_deposited),
                row.total_paid,
                format_amount(row.total_paid),
                row.available,
                format_amount(row.available),
                row.retained,
                format_amount(row.retained),
                row.seized,
                format_amount(row.seized),
                row.last_deposit_at,
                row.last_payment_at,
                row.status,
            ]
        )
    filename = f"economia_gremial_{safe_guild_filename_part(guild_name)}_{today or _utc_date()}.csv"
    return GuildEconomyReport(
        summary=summary,
        rows=rows,
        csv_data=output.getvalue().encode("utf-8-sig"),
        filename=filename,
    )


@contextmanager
def guild_economy_report_tempfile(report: GuildEconomyReport):
    handle = tempfile.NamedTemporaryFile(prefix="g3n_economia_gremial_", suffix=".csv", delete=False)
    path = Path(handle.name)
    try:
        with handle:
            handle.write(report.csv_data)
        yield path
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
