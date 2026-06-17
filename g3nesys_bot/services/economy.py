from __future__ import annotations

from ..constants import FINE_PAID, FINE_PENDING, WITHDRAWAL_PENDING
from ..database import Database
from ..utils import format_amount, utc_now_iso
from .audit import log_action


def ensure_account(db: Database, guild_id: int, user_id: int) -> None:
    db.execute(
        """
        INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
        VALUES (?, ?, 0, 0, 0, ?)
        ON CONFLICT(guild_id, user_id) DO NOTHING
        """,
        (guild_id, user_id, utc_now_iso()),
    )


def ensure_treasury(db: Database, guild_id: int) -> None:
    db.execute(
        """
        INSERT INTO treasury (guild_id, balance, updated_at)
        VALUES (?, 0, ?)
        ON CONFLICT(guild_id) DO NOTHING
        """,
        (guild_id, utc_now_iso()),
    )


def get_account(db: Database, guild_id: int, user_id: int):
    ensure_account(db, guild_id, user_id)
    return db.fetch_one(
        "SELECT * FROM accounts WHERE guild_id = ? AND user_id = ?",
        (guild_id, user_id),
    )


def pending_fines_total(db: Database, guild_id: int, user_id: int) -> tuple[int, int]:
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS count, COALESCE(SUM(amount), 0) AS total
        FROM fines
        WHERE guild_id = ? AND user_id = ? AND status = ?
        """,
        (guild_id, user_id, FINE_PENDING),
    )
    return int(row["count"]), int(row["total"])


def create_movement(
    db: Database,
    guild_id: int,
    *,
    movement_type: str,
    category: str,
    amount: int,
    description: str,
    created_by: int,
    user_id: int | None = None,
    counterparty_id: int | None = None,
    source_table: str | None = None,
    source_id: int | None = None,
    code_prefix: str = "MOV",
) -> int:
    code = db.next_code(guild_id, code_prefix)
    return db.execute(
        """
        INSERT INTO movements (
            code, guild_id, type, category, user_id, counterparty_id, amount,
            source_table, source_id, description, created_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            guild_id,
            movement_type,
            category,
            user_id,
            counterparty_id,
            amount,
            source_table,
            source_id,
            description,
            created_by,
            utc_now_iso(),
        ),
    )


def adjust_user_balance(
    db: Database,
    guild_id: int,
    user_id: int,
    *,
    available_delta: int = 0,
    retained_delta: int = 0,
    seized_delta: int = 0,
) -> None:
    ensure_account(db, guild_id, user_id)
    db.execute(
        """
        UPDATE accounts
        SET available = available + ?,
            retained = retained + ?,
            seized = seized + ?,
            updated_at = ?
        WHERE guild_id = ? AND user_id = ?
        """,
        (
            available_delta,
            retained_delta,
            seized_delta,
            utc_now_iso(),
            guild_id,
            user_id,
        ),
    )


def adjust_treasury(db: Database, guild_id: int, delta: int) -> None:
    ensure_treasury(db, guild_id)
    db.execute(
        """
        UPDATE treasury
        SET balance = balance + ?, updated_at = ?
        WHERE guild_id = ?
        """,
        (delta, utc_now_iso(), guild_id),
    )


def register_guild_income(
    db: Database,
    guild_id: int,
    *,
    amount: int,
    category: str,
    description: str,
    admin_id: int,
) -> int:
    ensure_treasury(db, guild_id)
    adjust_treasury(db, guild_id, amount)
    movement_id = create_movement(
        db,
        guild_id,
        movement_type="INGRESO",
        category=category,
        amount=amount,
        description=description,
        created_by=admin_id,
        code_prefix="ING",
    )
    log_action(
        db,
        guild_id,
        admin_id=admin_id,
        action="Registro ingreso gremial",
        system="Tesoreria",
        amount=amount,
        observation=description,
    )
    return movement_id


def register_guild_expense(
    db: Database,
    guild_id: int,
    *,
    amount: int,
    category: str,
    description: str,
    admin_id: int,
) -> int:
    ensure_treasury(db, guild_id)
    treasury = db.fetch_one("SELECT balance FROM treasury WHERE guild_id = ?", (guild_id,))
    if int(treasury["balance"]) < amount:
        raise ValueError("La tesoreria no tiene saldo suficiente.")
    adjust_treasury(db, guild_id, -amount)
    movement_id = create_movement(
        db,
        guild_id,
        movement_type="EGRESO",
        category=category,
        amount=amount,
        description=description,
        created_by=admin_id,
        code_prefix="EGR",
    )
    log_action(
        db,
        guild_id,
        admin_id=admin_id,
        action="Registro egreso gremial",
        system="Tesoreria",
        amount=amount,
        observation=description,
    )
    return movement_id


def deposit_to_user_from_treasury(
    db: Database,
    guild_id: int,
    *,
    user_id: int,
    amount: int,
    balance_type: str,
    reason: str,
    admin_id: int,
) -> int:
    ensure_treasury(db, guild_id)
    treasury = db.fetch_one("SELECT balance FROM treasury WHERE guild_id = ?", (guild_id,))
    if int(treasury["balance"]) < amount:
        raise ValueError("La tesoreria no tiene saldo suficiente.")
    if balance_type not in {"available", "retained"}:
        raise ValueError("Tipo de saldo invalido. Usa available o retained.")
    adjust_treasury(db, guild_id, -amount)
    if balance_type == "available":
        adjust_user_balance(db, guild_id, user_id, available_delta=amount)
    else:
        adjust_user_balance(db, guild_id, user_id, retained_delta=amount)
    movement_id = create_movement(
        db,
        guild_id,
        movement_type="DEPOSITO",
        category="Deposito administrativo",
        amount=amount,
        description=reason,
        created_by=admin_id,
        user_id=user_id,
        code_prefix="DEP",
    )
    log_action(
        db,
        guild_id,
        admin_id=admin_id,
        action="Deposito administrativo a usuario",
        system="Banco",
        affected_user_id=user_id,
        amount=amount,
        observation=reason,
    )
    return movement_id


def transfer_between_members(
    db: Database,
    guild_id: int,
    *,
    sender_id: int,
    receiver_id: int,
    amount: int,
    fee_percent: int,
) -> int:
    if sender_id == receiver_id:
        raise ValueError("No puedes transferirte plata a ti mismo.")
    fine_count, _ = pending_fines_total(db, guild_id, sender_id)
    if fine_count > 0:
        raise ValueError("No puedes transferir mientras tengas multas pendientes.")

    sender = get_account(db, guild_id, sender_id)
    if int(sender["available"]) < amount:
        raise ValueError("No tienes saldo disponible suficiente.")

    fee = int(round(amount * (fee_percent / 100)))
    receiver_amount = amount - fee
    if receiver_amount <= 0:
        raise ValueError("La comision consume todo el monto.")

    adjust_user_balance(db, guild_id, sender_id, available_delta=-amount)
    adjust_user_balance(db, guild_id, receiver_id, available_delta=receiver_amount)
    adjust_treasury(db, guild_id, fee)

    movement_id = create_movement(
        db,
        guild_id,
        movement_type="TRANSFERENCIA",
        category="Transferencia entre miembros",
        amount=amount,
        description=f"Transferencia con comision {fee_percent}% ({format_amount(fee)})",
        created_by=sender_id,
        user_id=sender_id,
        counterparty_id=receiver_id,
    )
    log_action(
        db,
        guild_id,
        admin_id=sender_id,
        action="Transferencia entre miembros",
        system="Banco",
        affected_user_id=receiver_id,
        amount=amount,
        observation=f"Comision a tesoreria: {fee}",
    )
    return movement_id


def create_withdrawal_request(
    db: Database,
    guild_id: int,
    *,
    user_id: int,
    amount: int,
    reason: str | None,
) -> str:
    fine_count, _ = pending_fines_total(db, guild_id, user_id)
    if fine_count > 0:
        raise ValueError("No puedes solicitar cobro con multas pendientes.")
    account = get_account(db, guild_id, user_id)
    if int(account["available"]) < amount:
        raise ValueError("No tienes saldo disponible suficiente.")

    code = db.next_code(guild_id, "COBRO")
    db.execute(
        """
        INSERT INTO withdrawals (
            code, guild_id, user_id, amount_requested, status, reason, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (code, guild_id, user_id, amount, WITHDRAWAL_PENDING, reason, utc_now_iso()),
    )
    log_action(
        db,
        guild_id,
        admin_id=user_id,
        action="Solicitud de cobro creada",
        system="Banco",
        affected_user_id=user_id,
        amount=amount,
        observation=reason,
    )
    return code


def pay_fine_from_balance(
    db: Database,
    guild_id: int,
    *,
    fine_code: str,
    payer_id: int,
) -> int:
    fine = db.fetch_one(
        "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
        (guild_id, fine_code),
    )
    if fine is None:
        raise ValueError("No encontre esa multa.")
    if fine["status"] != FINE_PENDING:
        raise ValueError("Esa multa no esta pendiente.")

    amount = int(fine["amount"])
    account = get_account(db, guild_id, payer_id)
    available = int(account["available"])
    retained = int(account["retained"])
    if available + retained < amount:
        raise ValueError("No hay saldo suficiente para pagar la multa.")

    available_used = min(available, amount)
    retained_used = amount - available_used
    adjust_user_balance(
        db,
        guild_id,
        payer_id,
        available_delta=-available_used,
        retained_delta=-retained_used,
    )
    adjust_treasury(db, guild_id, amount)
    movement_id = create_movement(
        db,
        guild_id,
        movement_type="INGRESO",
        category="Multa pagada",
        amount=amount,
        description=f"Pago de multa {fine_code}",
        created_by=payer_id,
        user_id=int(fine["user_id"]),
        counterparty_id=payer_id,
        source_table="fines",
        source_id=int(fine["id"]),
    )
    db.execute(
        """
        UPDATE fines
        SET status = ?, paid_by = ?, payment_method = ?, movement_id = ?, paid_at = ?
        WHERE id = ?
        """,
        (
            FINE_PAID,
            payer_id,
            "saldo disponible/retenido",
            movement_id,
            utc_now_iso(),
            int(fine["id"]),
        ),
    )
    log_action(
        db,
        guild_id,
        admin_id=payer_id,
        action="Pago de multa",
        system="Multas",
        affected_user_id=int(fine["user_id"]),
        amount=amount,
        observation=fine_code,
    )
    return movement_id
