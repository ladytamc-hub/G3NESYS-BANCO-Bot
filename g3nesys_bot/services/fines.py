from __future__ import annotations

import discord

from ..constants import FINE_CANCELLED, FINE_PENDING
from ..database import Database
from ..utils import format_amount, utc_now_iso
from .audit import log_action
from .notifications import send_dm_safe


async def create_fine(
    db: Database,
    *,
    guild_id: int,
    user: discord.Member | discord.User,
    amount: int,
    reason: str,
    origin: str,
    created_by: int,
) -> str:
    code = db.next_code(guild_id, "MULTA")
    db.execute(
        """
        INSERT INTO fines (
            code, guild_id, user_id, amount, reason, status, origin,
            created_by, created_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            code,
            guild_id,
            user.id,
            amount,
            reason,
            FINE_PENDING,
            origin,
            created_by,
            utc_now_iso(),
        ),
    )
    log_action(
        db,
        guild_id,
        admin_id=created_by,
        action="Creacion de multa",
        system="Multas",
        affected_user_id=user.id,
        amount=amount,
        observation=f"{code}: {reason}",
    )
    await send_dm_safe(
        db,
        guild_id=guild_id,
        user=user,
        action="crear_multa",
        content=(
            "🚨 Has recibido una multa.\n\n"
            f"ID: {code}\n"
            f"Monto: {format_amount(amount)}\n"
            f"Motivo: {reason}\n"
            f"Estado: {FINE_PENDING}\n"
            f"Origen: {origin}\n\n"
            "Puedes consultar tus multas con `!mis_multas`."
        ),
    )
    return code


async def cancel_fine(
    db: Database,
    *,
    guild: discord.Guild,
    fine_code: str,
    admin_id: int,
    reason: str,
) -> None:
    fine = db.fetch_one(
        "SELECT * FROM fines WHERE guild_id = ? AND code = ?",
        (guild.id, fine_code),
    )
    if fine is None:
        raise ValueError("No encontre esa multa.")
    if fine["status"] != FINE_PENDING:
        raise ValueError("Solo se pueden cancelar multas pendientes.")

    db.execute(
        """
        UPDATE fines
        SET status = ?, cancelled_by = ?, cancelled_at = ?, cancel_reason = ?
        WHERE id = ?
        """,
        (FINE_CANCELLED, admin_id, utc_now_iso(), reason, int(fine["id"])),
    )
    log_action(
        db,
        guild.id,
        admin_id=admin_id,
        action="Cancelacion de multa",
        system="Multas",
        affected_user_id=int(fine["user_id"]),
        amount=int(fine["amount"]),
        observation=f"{fine_code}: {reason}",
    )

    user = guild.get_member(int(fine["user_id"]))
    if user is not None:
        await send_dm_safe(
            db,
            guild_id=guild.id,
            user=user,
            action="cancelar_multa",
            content=(
                "🟢 Tu multa fue cancelada.\n\n"
                f"ID: {fine_code}\n"
                f"Motivo: {reason}\n"
                f"Estado: {FINE_CANCELLED}"
            ),
        )
