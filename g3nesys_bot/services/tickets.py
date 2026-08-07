from __future__ import annotations

import sqlite3

from ..database import Database
from ..utils import utc_now_iso

TICKET_PENDING = "Pendiente"
TICKET_IN_PROGRESS = "En seguimiento"
TICKET_WAITING_USER = "Esperando respuesta del usuario"
TICKET_RESOLVED = "Resuelto"
TICKET_CLOSED = "Cerrado"
TICKET_STATUSES = {
    TICKET_PENDING,
    TICKET_IN_PROGRESS,
    TICKET_WAITING_USER,
    TICKET_RESOLVED,
    TICKET_CLOSED,
}
OPEN_TICKET_STATUSES = (TICKET_PENDING, TICKET_IN_PROGRESS, TICKET_WAITING_USER)
TICKET_USER_REPLY = "respuesta_usuario"
TICKET_ADMIN_REPLY = "respuesta_usuario_admin"
TICKET_INTERNAL_NOTE = "nota_interna"


def validate_ticket_status(status: str) -> str:
    if status not in TICKET_STATUSES:
        raise ValueError("Estado de ticket invalido.")
    return status


def create_ticket(
    db: Database,
    guild_id: int,
    user_id: int,
    subject: str,
    description: str,
) -> sqlite3.Row:
    subject = (subject or "").strip()[:100]
    description = (description or "").strip()[:1800]
    if not subject:
        raise ValueError("El asunto es obligatorio.")
    if not description:
        raise ValueError("La descripcion es obligatoria.")
    code = db.next_code(guild_id, "TKT")
    now = utc_now_iso()
    ticket_id = db.execute(
        """
        INSERT INTO tickets (
            code, guild_id, user_id, subject, description, status,
            created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (code, guild_id, user_id, subject, description, TICKET_PENDING, now, now),
    )
    db.execute(
        """
        INSERT INTO ticket_messages (
            ticket_id, author_id, message_type, content, created_at,
            dm_sent, dm_error, old_status, new_status
        ) VALUES (?, ?, ?, ?, ?, NULL, NULL, NULL, ?)
        """,
        (ticket_id, user_id, TICKET_USER_REPLY, description, now, TICKET_PENDING),
    )
    return get_ticket_by_id(db, ticket_id)


def get_ticket_by_id(db: Database, ticket_id: int) -> sqlite3.Row | None:
    return db.fetch_one("SELECT * FROM tickets WHERE id = ?", (ticket_id,))


def get_ticket(db: Database, guild_id: int, code: str) -> sqlite3.Row | None:
    return db.fetch_one(
        "SELECT * FROM tickets WHERE guild_id = ? AND UPPER(code) = UPPER(?)",
        (guild_id, (code or "").strip()),
    )


def set_ticket_thread(db: Database, ticket_id: int, thread_id: int | None) -> None:
    db.execute("UPDATE tickets SET thread_id = ? WHERE id = ?", (thread_id, ticket_id))


def set_ticket_notification(db: Database, ticket_id: int, message_id: int | None) -> None:
    db.execute("UPDATE tickets SET notification_message_id = ? WHERE id = ?", (message_id, ticket_id))


def list_tickets(
    db: Database,
    guild_id: int,
    statuses: tuple[str, ...] | list[str],
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[sqlite3.Row]:
    if not statuses:
        return []
    for status in statuses:
        validate_ticket_status(status)
    placeholders = ",".join("?" for _ in statuses)
    return db.fetch_all(
        f"""
        SELECT t.*,
               (SELECT COUNT(*) FROM ticket_messages tm WHERE tm.ticket_id = t.id) AS message_count,
               (SELECT COUNT(*) FROM ticket_attachments ta WHERE ta.ticket_id = t.id) AS attachment_count
        FROM tickets t
        WHERE t.guild_id = ? AND t.status IN ({placeholders})
        ORDER BY t.created_at ASC, t.id ASC
        LIMIT ? OFFSET ?
        """,
        (guild_id, *statuses, limit, offset),
    )


def count_tickets_by_user(db: Database, guild_id: int, user_id: int) -> int:
    row = db.fetch_one(
        """
        SELECT COUNT(*) AS total
        FROM tickets
        WHERE guild_id = ? AND user_id = ?
        """,
        (guild_id, user_id),
    )
    return int(row["total"] if row is not None else 0)


def search_tickets_by_user(
    db: Database,
    guild_id: int,
    user_id: int,
    *,
    limit: int = 10,
    offset: int = 0,
) -> list[sqlite3.Row]:
    return db.fetch_all(
        """
        SELECT t.*,
               (SELECT COUNT(*) FROM ticket_messages tm WHERE tm.ticket_id = t.id) AS message_count,
               (SELECT COUNT(*) FROM ticket_attachments ta WHERE ta.ticket_id = t.id) AS attachment_count
        FROM tickets t
        WHERE t.guild_id = ? AND t.user_id = ?
        ORDER BY t.created_at DESC, t.id DESC
        LIMIT ? OFFSET ?
        """,
        (guild_id, user_id, limit, offset),
    )


def ticket_messages(db: Database, ticket_id: int, *, limit: int = 12) -> list[sqlite3.Row]:
    return db.fetch_all(
        """
        SELECT * FROM ticket_messages
        WHERE ticket_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (ticket_id, limit),
    )


def ticket_attachments(db: Database, ticket_id: int, *, limit: int = 10) -> list[sqlite3.Row]:
    return db.fetch_all(
        """
        SELECT * FROM ticket_attachments
        WHERE ticket_id = ?
        ORDER BY created_at DESC, id DESC
        LIMIT ?
        """,
        (ticket_id, limit),
    )


def add_ticket_message(
    db: Database,
    ticket_id: int,
    *,
    author_id: int,
    message_type: str,
    content: str,
    dm_sent: bool | None = None,
    dm_error: str | None = None,
    old_status: str | None = None,
    new_status: str | None = None,
) -> None:
    now = utc_now_iso()
    if new_status is not None:
        validate_ticket_status(new_status)
    db.execute(
        """
        INSERT INTO ticket_messages (
            ticket_id, author_id, message_type, content, created_at,
            dm_sent, dm_error, old_status, new_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            author_id,
            message_type,
            (content or "").strip()[:1800],
            now,
            None if dm_sent is None else int(dm_sent),
            dm_error,
            old_status,
            new_status,
        ),
    )
    if new_status is not None:
        closed_at = now if new_status == TICKET_CLOSED else None
        db.execute(
            """
            UPDATE tickets
            SET status = ?, updated_at = ?, closed_at = ?
            WHERE id = ?
            """,
            (new_status, now, closed_at, ticket_id),
        )
    else:
        db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))


def change_ticket_status(
    db: Database,
    ticket_id: int,
    *,
    admin_id: int,
    new_status: str,
    note: str = "",
) -> None:
    ticket = get_ticket_by_id(db, ticket_id)
    if ticket is None:
        raise ValueError("No encontre ese ticket.")
    old_status = str(ticket["status"])
    validate_ticket_status(new_status)
    add_ticket_message(
        db,
        ticket_id,
        author_id=admin_id,
        message_type=TICKET_INTERNAL_NOTE,
        content=(note or f"Estado cambiado a {new_status}"),
        old_status=old_status,
        new_status=new_status,
    )


def assign_ticket(db: Database, ticket_id: int, admin_id: int) -> None:
    db.execute(
        "UPDATE tickets SET assigned_admin_id = ?, updated_at = ? WHERE id = ?",
        (admin_id, utc_now_iso(), ticket_id),
    )


def add_attachment(
    db: Database,
    ticket_id: int,
    *,
    author_id: int,
    url: str,
    filename: str,
    content_type: str | None,
    message_id: int,
    channel_id: int,
) -> None:
    db.execute(
        """
        INSERT INTO ticket_attachments (
            ticket_id, author_id, url, filename, content_type,
            message_id, channel_id, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            ticket_id,
            author_id,
            url,
            filename[:255],
            content_type,
            message_id,
            channel_id,
            utc_now_iso(),
        ),
    )
    db.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (utc_now_iso(), ticket_id))


def find_ticket_for_attachment(db: Database, guild_id: int, user_id: int, channel_id: int, content: str) -> sqlite3.Row | None:
    import re

    match = re.search(r"\bTKT-\d{6}\b", content or "", flags=re.IGNORECASE)
    if match:
        ticket = get_ticket(db, guild_id, match.group(0))
        if ticket is not None and int(ticket["user_id"]) == user_id and ticket["status"] != TICKET_CLOSED:
            return ticket
    return db.fetch_one(
        """
        SELECT * FROM tickets
        WHERE guild_id = ? AND user_id = ? AND thread_id = ? AND status != ?
        ORDER BY updated_at DESC, id DESC
        LIMIT 1
        """,
        (guild_id, user_id, channel_id, TICKET_CLOSED),
    )
