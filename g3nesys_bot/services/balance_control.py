from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import discord

from ..constants import WITHDRAWAL_CANCELLED, WITHDRAWAL_PENDING
from ..database import Database
from ..utils import format_amount, utc_now_iso


WITHDRAWAL_CANCEL_COOLDOWN = timedelta(hours=24)
BALANCE_SEIZURE_TYPE = "BALANCE_DECOMISADO"
BALANCE_SEIZURE_CATEGORY = "Decomiso administrativo"


class WithdrawalCancellationCooldown(ValueError):
    def __init__(self, retry_at: datetime):
        super().__init__("No puedes cancelar otra solicitud todavía.")
        self.retry_at = retry_at


@dataclass(frozen=True)
class BalanceSeizureResult:
    movement_id: int
    previous_available: int
    new_available: int
    previous_seized: int
    new_seized: int
    amount: int


@dataclass(frozen=True)
class OutsideBalanceRow:
    user_id: int
    display_name: str
    albion_name: str
    available: int
    left_at: str | None
    days_out: int | None


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def human_cooldown(remaining: timedelta) -> str:
    seconds = max(0, int(remaining.total_seconds()))
    hours, remainder = divmod(seconds, 3600)
    minutes = max(1, (remainder + 59) // 60) if hours == 0 else (remainder + 59) // 60
    if hours and minutes:
        return f"{hours} horas / {minutes} minutos"
    if hours:
        return f"{hours} horas"
    return f"{minutes} minutos"


def known_user_name(db: Database, guild_id: int, user_id: int, guild: discord.Guild | None = None) -> str:
    member = guild.get_member(user_id) if guild is not None else None
    if member is not None:
        return str(member.display_name)
    row = db.fetch_one(
        """
        SELECT display_name FROM member_departures
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    )
    if row is not None and str(row["display_name"] or "").strip():
        return str(row["display_name"]).strip()
    queries = [
        """
        SELECT ap.display_name
        FROM activity_participants ap
        JOIN activities a ON a.id = ap.activity_id
        WHERE a.guild_id = ? AND ap.user_id = ?
        ORDER BY ap.id DESC
        LIMIT 1
        """,
        """
        SELECT display_name
        FROM activity_voice_stats
        WHERE guild_id = ? AND user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        """
        SELECT display_name
        FROM activity_join_requests
        WHERE guild_id = ? AND user_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
    ]
    for query in queries:
        historical = db.fetch_one(query, (guild_id, user_id))
        if historical is not None and str(historical["display_name"] or "").strip():
            return str(historical["display_name"]).strip()
    return f"Usuario {user_id}"


def cancel_pending_withdrawal_by_user(
    db: Database,
    guild_id: int,
    *,
    user_id: int,
    code: str | None = None,
) -> str:
    now = utc_now_iso()
    now_dt = parse_iso_datetime(now) or datetime.now(timezone.utc)
    with db.transaction() as cursor:
        cooldown = cursor.execute(
            """
            SELECT last_cancelled_at
            FROM withdrawal_user_cancellations
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        if cooldown is not None:
            last_cancelled_at = parse_iso_datetime(cooldown["last_cancelled_at"])
            if last_cancelled_at is not None:
                retry_at = last_cancelled_at + WITHDRAWAL_CANCEL_COOLDOWN
                if retry_at > now_dt:
                    raise WithdrawalCancellationCooldown(retry_at)

        if code:
            withdrawal = cursor.execute(
                """
                SELECT * FROM withdrawals
                WHERE guild_id = ? AND user_id = ? AND UPPER(code) = UPPER(?)
                """,
                (guild_id, user_id, code.strip()),
            ).fetchone()
        else:
            withdrawal = cursor.execute(
                """
                SELECT * FROM withdrawals
                WHERE guild_id = ? AND user_id = ? AND status = ?
                ORDER BY created_at ASC, id ASC
                LIMIT 1
                """,
                (guild_id, user_id, WITHDRAWAL_PENDING),
            ).fetchone()
        if withdrawal is None:
            raise ValueError("No encontré una solicitud de cobro pendiente tuya para cancelar.")
        if withdrawal["status"] != WITHDRAWAL_PENDING:
            raise ValueError("Solo puedes cancelar solicitudes que siguen pendientes.")

        cursor.execute(
            """
            UPDATE withdrawals
            SET status = ?, closed_at = ?, updated_at = ?, return_reason = ?
            WHERE guild_id = ? AND id = ? AND user_id = ? AND status = ?
            """,
            (
                WITHDRAWAL_CANCELLED,
                now,
                now,
                "Cancelada por el usuario",
                guild_id,
                int(withdrawal["id"]),
                user_id,
                WITHDRAWAL_PENDING,
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError("La solicitud ya no está pendiente.")
        cursor.execute(
            """
            INSERT INTO withdrawal_user_cancellations (guild_id, user_id, last_cancelled_at)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET last_cancelled_at = excluded.last_cancelled_at
            """,
            (guild_id, user_id, now),
        )
        cursor.execute(
            """
            INSERT INTO withdrawal_action_logs (
                withdrawal_id, action_type, author_id, amount,
                old_status, new_status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(withdrawal["id"]),
                "cancelada_usuario",
                user_id,
                int(withdrawal["amount_requested"]),
                str(withdrawal["status"]),
                WITHDRAWAL_CANCELLED,
                "Cancelada por el usuario",
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO audit_logs (
                guild_id, admin_id, action, affected_user_id, amount,
                system, observation, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                "Solicitud de cobro cancelada por usuario",
                user_id,
                int(withdrawal["amount_requested"]),
                "Banco",
                str(withdrawal["code"]),
                now,
            ),
        )
    return str(withdrawal["code"])


def record_member_departure(
    db: Database,
    guild_id: int,
    *,
    user_id: int,
    display_name: str,
    left_at: str | None = None,
) -> bool:
    left_at = left_at or utc_now_iso()
    existing = db.fetch_one(
        "SELECT in_server, last_alerted_at FROM member_departures WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )
    should_alert = existing is None or int(existing["in_server"] or 0) == 1 or existing["last_alerted_at"] is None
    db.execute(
        """
        INSERT INTO member_departures (
            guild_id, user_id, display_name, left_at, last_alerted_at, in_server, updated_at
        ) VALUES (?, ?, ?, ?, NULL, 0, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET display_name = excluded.display_name,
                      left_at = CASE
                          WHEN member_departures.in_server = 1 THEN excluded.left_at
                          WHEN member_departures.left_at IS NULL THEN excluded.left_at
                          ELSE member_departures.left_at
                      END,
                      in_server = 0,
                      updated_at = excluded.updated_at
        """,
        (guild_id, user_id, display_name[:120], left_at, utc_now_iso()),
    )
    return should_alert


def mark_member_alerted(db: Database, guild_id: int, user_id: int) -> None:
    db.execute(
        """
        UPDATE member_departures
        SET last_alerted_at = ?, updated_at = ?
        WHERE guild_id = ? AND user_id = ?
        """,
        (utc_now_iso(), utc_now_iso(), guild_id, user_id),
    )


def record_member_join(db: Database, guild_id: int, *, user_id: int, display_name: str) -> None:
    db.execute(
        """
        INSERT INTO member_departures (
            guild_id, user_id, display_name, left_at, last_alerted_at, in_server, updated_at
        ) VALUES (?, ?, ?, NULL, NULL, 1, ?)
        ON CONFLICT(guild_id, user_id)
        DO UPDATE SET display_name = excluded.display_name,
                      in_server = 1,
                      updated_at = excluded.updated_at
        """,
        (guild_id, user_id, display_name[:120], utc_now_iso()),
    )


def list_outside_users_with_balance(
    db: Database,
    guild: discord.Guild,
    *,
    limit: int = 8,
    offset: int = 0,
) -> tuple[list[OutsideBalanceRow], int]:
    departure_columns = {
        str(row["name"])
        for row in db.fetch_all("PRAGMA table_info(member_departures)")
    }
    has_departures = {"guild_id", "user_id"}.issubset(departure_columns)
    has_display_name = "display_name" in departure_columns
    has_left_at = "left_at" in departure_columns
    if has_departures:
        display_expr = "md.display_name" if has_display_name else "NULL AS display_name"
        left_at_select = "md.left_at" if has_left_at else "NULL AS left_at"
        left_at_order = "md.left_at" if has_left_at else "NULL"
        rows = db.fetch_all(
            f"""
            SELECT a.user_id, a.available, {display_expr}, {left_at_select}
            FROM accounts a
            LEFT JOIN member_departures md
              ON md.guild_id = a.guild_id AND md.user_id = a.user_id
            WHERE a.guild_id = ? AND a.available > 0
            ORDER BY COALESCE({left_at_order}, '9999') ASC, a.available DESC, a.user_id ASC
            """,
            (guild.id,),
        )
    else:
        rows = db.fetch_all(
            """
            SELECT user_id, available, NULL AS display_name, NULL AS left_at
            FROM accounts
            WHERE guild_id = ? AND available > 0
            ORDER BY available DESC, user_id ASC
            """,
            (guild.id,),
        )
    outside: list[OutsideBalanceRow] = []
    now_dt = datetime.now(timezone.utc)
    guild_members = getattr(guild, "members", [])
    if isinstance(guild_members, dict):
        guild_members = guild_members.values()
    current_member_ids = {int(member.id) for member in guild_members}
    for row in rows:
        user_id = int(row["user_id"])
        if user_id in current_member_ids or guild.get_member(user_id) is not None:
            continue
        left_at = str(row["left_at"]) if row["left_at"] else None
        left_dt = parse_iso_datetime(left_at)
        display_name = str(row["display_name"] or "").strip()
        if not display_name:
            display_name = known_user_name(db, guild.id, user_id) if has_departures else f"Usuario {user_id}"
        outside.append(
            OutsideBalanceRow(
                user_id=user_id,
                display_name=display_name,
                albion_name="No registrado",
                available=int(row["available"]),
                left_at=left_at,
                days_out=(now_dt - left_dt).days if left_dt is not None else None,
            )
        )
    return outside[offset : offset + limit], len(outside)


def seize_user_balance(
    db: Database,
    guild_id: int,
    *,
    user_id: int,
    amount: int,
    admin_id: int,
    reason: str,
    origin: str,
    known_name: str,
    albion_name: str = "No registrado",
) -> BalanceSeizureResult:
    reason = str(reason or "").strip()
    origin = str(origin or "otro").strip()[:80] or "otro"
    if amount <= 0:
        raise ValueError("La cantidad debe ser mayor que cero.")
    if not reason:
        raise ValueError("La razón del decomiso es obligatoria.")
    now = utc_now_iso()
    with db.transaction() as cursor:
        cursor.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (?, ?, 0, 0, 0, ?)
            ON CONFLICT(guild_id, user_id) DO NOTHING
            """,
            (guild_id, user_id, now),
        )
        account = cursor.execute(
            """
            SELECT available, seized
            FROM accounts
            WHERE guild_id = ? AND user_id = ?
            """,
            (guild_id, user_id),
        ).fetchone()
        previous_available = int(account["available"])
        previous_seized = int(account["seized"])
        if amount > previous_available:
            raise ValueError(
                f"No puedes decomisar más que el saldo disponible. Disponible: {format_amount(previous_available)}."
            )
        new_available = previous_available - amount
        new_seized = previous_seized + amount
        cursor.execute(
            """
            UPDATE accounts
            SET available = ?, seized = ?, updated_at = ?
            WHERE guild_id = ? AND user_id = ? AND available = ? AND seized = ?
            """,
            (new_available, new_seized, now, guild_id, user_id, previous_available, previous_seized),
        )
        if cursor.rowcount != 1:
            raise ValueError("El balance cambió mientras se procesaba el decomiso. Intenta de nuevo.")

        counter = cursor.execute(
            "SELECT last_value FROM id_counters WHERE guild_id = ? AND prefix = ?",
            (guild_id, "DEC"),
        ).fetchone()
        next_value = int(counter["last_value"]) + 1 if counter else 1
        cursor.execute(
            """
            INSERT INTO id_counters (guild_id, prefix, last_value)
            VALUES (?, ?, ?)
            ON CONFLICT(guild_id, prefix)
            DO UPDATE SET last_value = excluded.last_value
            """,
            (guild_id, "DEC", next_value),
        )
        movement_code = f"DEC-{next_value:06d}"
        movement_id = cursor.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, counterparty_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            )
            VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, 0, NULL)
            """,
            (
                movement_code,
                guild_id,
                BALANCE_SEIZURE_TYPE,
                BALANCE_SEIZURE_CATEGORY,
                user_id,
                amount,
                (
                    f"{reason} | origen={origin} | "
                    f"disponible {previous_available}->{new_available}"
                )[:1800],
                admin_id,
                now,
            ),
        ).lastrowid
        cursor.execute(
            """
            INSERT INTO balance_seizure_logs (
                guild_id, user_id, known_name, albion_name, amount,
                balance_before, balance_after, reason, origin,
                admin_id, movement_id, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                user_id,
                known_name[:120],
                albion_name[:120],
                amount,
                previous_available,
                new_available,
                reason[:900],
                origin,
                admin_id,
                movement_id,
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO audit_logs (
                guild_id, admin_id, action, affected_user_id, amount,
                system, observation, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                admin_id,
                "Decomiso administrativo de balance",
                user_id,
                amount,
                "Banco",
                (
                    f"movimiento={movement_code}; anterior={previous_available}; "
                    f"posterior={new_available}; origen={origin}; razon={reason}"
                )[:1800],
                now,
            ),
        )
    return BalanceSeizureResult(
        movement_id=int(movement_id),
        previous_available=previous_available,
        new_available=new_available,
        previous_seized=previous_seized,
        new_seized=new_seized,
        amount=amount,
    )
