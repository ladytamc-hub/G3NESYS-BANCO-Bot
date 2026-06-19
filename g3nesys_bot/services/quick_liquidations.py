from __future__ import annotations

from dataclasses import dataclass

from ..constants import PAYOUT_DEPOSITED
from ..database import Database
from ..utils import utc_now_iso


@dataclass(frozen=True)
class QuickLiquidationItem:
    user_id: int
    amount: int
    balance_type: str
    movement_id: int


@dataclass(frozen=True)
class QuickLiquidationResult:
    code: str
    payout_code: str
    activity_name: str
    mode: str
    total_amount: int
    items: tuple[QuickLiquidationItem, ...]
    split_completed: bool


def recent_liquidatable_payouts(db: Database, guild_id: int, limit: int = 25):
    return db.fetch_all(
        """
        SELECT p.id, p.code, p.created_at, p.reviewed_at,
               COALESCE(a.name, a.code, 'Actividad sin nombre') AS activity_name,
               COUNT(pp.id) AS pending_members,
               COALESCE(SUM(pp.amount), 0) AS pending_total
        FROM payouts p
        LEFT JOIN activities a ON a.id = p.activity_id
        JOIN payout_participants pp ON pp.payout_id = p.id
        WHERE p.guild_id = ? AND p.status = ?
          AND pp.deposited_at IS NOT NULL AND pp.liquidated_at IS NULL
        GROUP BY p.id
        ORDER BY COALESCE(p.reviewed_at, p.created_at) DESC, p.id DESC
        LIMIT ?
        """,
        (guild_id, PAYOUT_DEPOSITED, limit),
    )


def get_liquidatable_payout(db: Database, guild_id: int, payout_id: int):
    return db.fetch_one(
        """
        SELECT p.*, COALESCE(a.name, a.code, 'Actividad sin nombre') AS activity_name,
               a.code AS activity_code
        FROM payouts p
        LEFT JOIN activities a ON a.id = p.activity_id
        WHERE p.guild_id = ? AND p.id = ?
        """,
        (guild_id, payout_id),
    )


def get_liquidatable_participants(db: Database, payout_id: int):
    return db.fetch_all(
        """
        SELECT id, user_id, amount, balance_type, deposited_at, liquidated_at
        FROM payout_participants
        WHERE payout_id = ? AND deposited_at IS NOT NULL AND liquidated_at IS NULL
        ORDER BY id ASC
        """,
        (payout_id,),
    )


def _next_code(cursor, guild_id: int, prefix: str) -> str:
    row = cursor.execute(
        "SELECT last_value FROM id_counters WHERE guild_id = ? AND prefix = ?",
        (guild_id, prefix),
    ).fetchone()
    next_value = int(row["last_value"]) + 1 if row else 1
    cursor.execute(
        """
        INSERT INTO id_counters (guild_id, prefix, last_value)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, prefix)
        DO UPDATE SET last_value = excluded.last_value
        """,
        (guild_id, prefix, next_value),
    )
    return f"{prefix}-{next_value:06d}"


def liquidate_payout(
    db: Database,
    guild_id: int,
    *,
    payout_id: int,
    admin_id: int,
    user_id: int | None = None,
) -> QuickLiquidationResult:
    now = utc_now_iso()
    with db.transaction() as cursor:
        payout = cursor.execute(
            """
            SELECT p.*, COALESCE(a.name, a.code, 'Actividad sin nombre') AS activity_name
            FROM payouts p
            LEFT JOIN activities a ON a.id = p.activity_id
            WHERE p.guild_id = ? AND p.id = ?
            """,
            (guild_id, payout_id),
        ).fetchone()
        if payout is None:
            raise ValueError("No encontre ese Split.")
        if payout["status"] != PAYOUT_DEPOSITED:
            raise ValueError("El Split debe estar aprobado y depositado antes de liquidarlo.")

        if user_id is not None:
            participant = cursor.execute(
                "SELECT * FROM payout_participants WHERE payout_id = ? AND user_id = ?",
                (payout_id, user_id),
            ).fetchone()
            if participant is None:
                raise ValueError("El ID ingresado no corresponde a ningun miembro del Split.")
            if participant["liquidated_at"] is not None:
                raise ValueError("Ese miembro ya fue liquidado en este Split.")
            if participant["deposited_at"] is None:
                raise ValueError("Ese miembro aun no tiene acreditado el saldo del Split.")
            participants = [participant]
            mode = "Individual"
        else:
            participants = list(
                cursor.execute(
                    """
                    SELECT * FROM payout_participants
                    WHERE payout_id = ? AND deposited_at IS NOT NULL
                      AND liquidated_at IS NULL
                    ORDER BY id ASC
                    """,
                    (payout_id,),
                ).fetchall()
            )
            mode = "Completa"

        if not participants:
            raise ValueError("Este Split ya fue liquidado por completo.")

        prepared: list[tuple[object, str, int]] = []
        for participant in participants:
            participant_user_id = int(participant["user_id"])
            amount = int(participant["amount"])
            balance_type = str(participant["balance_type"] or "available")
            if balance_type not in {"available", "retained"}:
                balance_type = "available"
            cursor.execute(
                """
                INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
                VALUES (?, ?, 0, 0, 0, ?)
                ON CONFLICT(guild_id, user_id) DO NOTHING
                """,
                (guild_id, participant_user_id, now),
            )
            account = cursor.execute(
                "SELECT available, retained FROM accounts WHERE guild_id = ? AND user_id = ?",
                (guild_id, participant_user_id),
            ).fetchone()
            current = int(account[balance_type])
            if current < amount:
                readable = "retenido" if balance_type == "retained" else "disponible"
                raise ValueError(
                    f"<@{participant_user_id}> no tiene saldo {readable} suficiente "
                    f"para liquidar {amount}. No se realizo ningun movimiento."
                )
            prepared.append((participant, balance_type, amount))

        total_amount = sum(item[2] for item in prepared)
        liquidation_code = _next_code(cursor, guild_id, "LIQR")
        cursor.execute(
            """
            INSERT INTO quick_liquidations (
                code, guild_id, payout_id, mode, admin_id, total_amount, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                liquidation_code,
                guild_id,
                payout_id,
                mode,
                admin_id,
                total_amount,
                now,
            ),
        )
        liquidation_id = int(cursor.lastrowid)
        result_items: list[QuickLiquidationItem] = []

        for participant, balance_type, amount in prepared:
            participant_user_id = int(participant["user_id"])
            cursor.execute(
                f"""
                UPDATE accounts
                SET {balance_type} = {balance_type} - ?, updated_at = ?
                WHERE guild_id = ? AND user_id = ?
                """,
                (amount, now, guild_id, participant_user_id),
            )
            movement_code = _next_code(cursor, guild_id, "LIQ")
            cursor.execute(
                """
                INSERT INTO movements (
                    code, guild_id, type, category, user_id, amount,
                    source_table, source_id, description, created_by, created_at,
                    fee_amount, net_amount
                ) VALUES (?, ?, 'LIQUIDACION', 'Liquidacion rapida', ?, ?,
                          'payouts', ?, ?, ?, ?, 0, ?)
                """,
                (
                    movement_code,
                    guild_id,
                    participant_user_id,
                    amount,
                    payout_id,
                    f"Liquidacion rapida del Split {payout['code']} ({mode})",
                    admin_id,
                    now,
                    amount,
                ),
            )
            movement_id = int(cursor.lastrowid)
            cursor.execute(
                """
                INSERT INTO quick_liquidation_items (
                    liquidation_id, payout_participant_id, user_id, amount,
                    balance_type, movement_id
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    liquidation_id,
                    int(participant["id"]),
                    participant_user_id,
                    amount,
                    balance_type,
                    movement_id,
                ),
            )
            cursor.execute(
                """
                UPDATE payout_participants
                SET liquidated_at = ?, liquidated_by = ?, liquidation_id = ?,
                    liquidation_movement_id = ?
                WHERE id = ? AND liquidated_at IS NULL
                """,
                (
                    now,
                    admin_id,
                    liquidation_id,
                    movement_id,
                    int(participant["id"]),
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("La liquidacion cambio mientras la confirmabas. Intenta de nuevo.")
            result_items.append(
                QuickLiquidationItem(
                    user_id=participant_user_id,
                    amount=amount,
                    balance_type=balance_type,
                    movement_id=movement_id,
                )
            )

        remaining = cursor.execute(
            """
            SELECT COUNT(*) AS total FROM payout_participants
            WHERE payout_id = ? AND deposited_at IS NOT NULL AND liquidated_at IS NULL
            """,
            (payout_id,),
        ).fetchone()
        split_completed = int(remaining["total"]) == 0
        if split_completed:
            cursor.execute(
                """
                UPDATE payouts SET quick_liquidated_at = ?, quick_liquidated_by = ?
                WHERE guild_id = ? AND id = ?
                """,
                (now, admin_id, guild_id, payout_id),
            )

        members_detail = ", ".join(
            f"{item.user_id}:{item.amount}" for item in result_items
        )
        cursor.execute(
            """
            INSERT INTO payout_audit_logs (
                guild_id, payout_id, actor_id, action, details, created_at
            ) VALUES (?, ?, ?, 'Liquidacion rapida', ?, ?)
            """,
            (
                guild_id,
                payout_id,
                admin_id,
                f"{mode}; {liquidation_code}; miembros {members_detail}; total {total_amount}",
                now,
            ),
        )
        cursor.execute(
            """
            INSERT INTO audit_logs (
                guild_id, admin_id, action, affected_user_id, amount,
                system, observation, created_at
            ) VALUES (?, ?, 'Liquidacion rapida', ?, ?, 'Banco', ?, ?)
            """,
            (
                guild_id,
                admin_id,
                result_items[0].user_id if len(result_items) == 1 else None,
                total_amount,
                f"{mode}; Split {payout['code']}; {liquidation_code}; {members_detail}",
                now,
            ),
        )

    return QuickLiquidationResult(
        code=liquidation_code,
        payout_code=str(payout["code"]),
        activity_name=str(payout["activity_name"]),
        mode=mode,
        total_amount=total_amount,
        items=tuple(result_items),
        split_completed=split_completed,
    )
