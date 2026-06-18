from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable

from .constants import DEFAULT_SETTINGS
from .utils import utc_now_iso


class Database:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA journal_mode = WAL")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def init_schema(self) -> None:
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._apply_migrations()
            self._conn.commit()

    def _apply_migrations(self) -> None:
        payout_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(payouts)").fetchall()
        }
        if "sent_to_admin_at" not in payout_columns:
            self._conn.execute("ALTER TABLE payouts ADD COLUMN sent_to_admin_at TEXT")

    def execute(self, query: str, params: Iterable = ()) -> int:
        with self._lock:
            cursor = self._conn.execute(query, tuple(params))
            self._conn.commit()
            return int(cursor.lastrowid)

    def fetch_one(self, query: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self._lock:
            return self._conn.execute(query, tuple(params)).fetchone()

    def fetch_all(self, query: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(query, tuple(params)).fetchall())

    @contextmanager
    def transaction(self):
        with self._lock:
            cursor = self._conn.cursor()
            try:
                cursor.execute("BEGIN IMMEDIATE")
                yield cursor
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                cursor.close()

    def next_code(self, guild_id: int, prefix: str) -> str:
        with self.transaction() as cursor:
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

    def get_setting(self, guild_id: int, key: str, default: str | None = None) -> str:
        row = self.fetch_one(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        if row is not None:
            return str(row["value"])
        return DEFAULT_SETTINGS.get(key, default or "")

    def set_setting(self, guild_id: int, key: str, value: str) -> None:
        self.execute(
            """
            INSERT INTO guild_settings (guild_id, key, value, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(guild_id, key)
            DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at
            """,
            (guild_id, key, value, utc_now_iso()),
        )

    def get_int_setting(self, guild_id: int, key: str, default: int = 0) -> int:
        value = self.get_setting(guild_id, key, str(default))
        try:
            return int(value)
        except ValueError:
            return default


SCHEMA = """
CREATE TABLE IF NOT EXISTS guild_settings (
    guild_id INTEGER NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, key)
);

CREATE TABLE IF NOT EXISTS id_counters (
    guild_id INTEGER NOT NULL,
    prefix TEXT NOT NULL,
    last_value INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, prefix)
);

CREATE TABLE IF NOT EXISTS callers (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    added_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS panel_messages (
    guild_id INTEGER NOT NULL,
    panel_type TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, panel_type)
);

CREATE TABLE IF NOT EXISTS templates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    activity_name TEXT NOT NULL,
    default_time TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS template_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    template_id INTEGER NOT NULL REFERENCES templates(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    slots INTEGER NOT NULL,
    emoji TEXT,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    template_id INTEGER,
    name TEXT NOT NULL,
    caller_id INTEGER NOT NULL,
    horario TEXT NOT NULL,
    voice_channel_id INTEGER,
    notes TEXT,
    status TEXT NOT NULL,
    channel_id INTEGER,
    message_id INTEGER,
    created_at TEXT NOT NULL,
    started_at TEXT,
    ended_at TEXT
);

CREATE TABLE IF NOT EXISTS activity_roles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    key TEXT NOT NULL,
    name TEXT NOT NULL,
    slots INTEGER NOT NULL,
    emoji TEXT,
    position INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    role_id INTEGER NOT NULL REFERENCES activity_roles(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    joined_at TEXT NOT NULL,
    UNIQUE(activity_id, user_id)
);

CREATE TABLE IF NOT EXISTS asistencia_actividades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actividad_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    usuario_id INTEGER NOT NULL,
    estado TEXT NOT NULL,
    confirmo_boton INTEGER NOT NULL DEFAULT 0,
    confirmo_voz INTEGER NOT NULL DEFAULT 0,
    fecha_check TEXT,
    genero_multa INTEGER NOT NULL DEFAULT 0,
    justificado_por INTEGER,
    observaciones TEXT,
    UNIQUE(actividad_id, usuario_id)
);

CREATE TABLE IF NOT EXISTS penalizacion_actividades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    servidor_id INTEGER NOT NULL,
    usuario_id INTEGER NOT NULL,
    motivo TEXT NOT NULL,
    origen TEXT NOT NULL,
    fecha_ingreso TEXT NOT NULL,
    activo INTEGER NOT NULL DEFAULT 1,
    removido_por INTEGER,
    fecha_remocion TEXT,
    observaciones TEXT
);

CREATE TABLE IF NOT EXISTS accounts (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    available INTEGER NOT NULL DEFAULT 0,
    retained INTEGER NOT NULL DEFAULT 0,
    seized INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS treasury (
    guild_id INTEGER PRIMARY KEY,
    balance INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS fines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    origin TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    paid_by INTEGER,
    payment_method TEXT,
    movement_id INTEGER,
    created_at TEXT NOT NULL,
    paid_at TEXT,
    cancelled_by INTEGER,
    cancelled_at TEXT,
    cancel_reason TEXT
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    amount_requested INTEGER NOT NULL,
    amount_liquidated INTEGER,
    status TEXT NOT NULL,
    reason TEXT,
    created_at TEXT NOT NULL,
    approved_by INTEGER,
    approved_at TEXT,
    liquidated_by INTEGER,
    liquidated_at TEXT,
    rejected_by INTEGER,
    rejected_at TEXT,
    rejection_reason TEXT
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    activity_id INTEGER,
    caller_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    gross_loot INTEGER NOT NULL DEFAULT 0,
    market_rate_percent REAL NOT NULL DEFAULT 0,
    repairs INTEGER NOT NULL DEFAULT 0,
    other_expenses INTEGER NOT NULL DEFAULT 0,
    guild_percent REAL NOT NULL DEFAULT 0,
    guild_amount INTEGER NOT NULL DEFAULT 0,
    distributable INTEGER NOT NULL DEFAULT 0,
    notes TEXT,
    created_at TEXT NOT NULL,
    sent_to_admin_at TEXT,
    reviewed_by INTEGER,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS payout_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payout_id INTEGER NOT NULL REFERENCES payouts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    participation_percent REAL NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    balance_type TEXT,
    deposited_at TEXT,
    UNIQUE(payout_id, user_id)
);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL UNIQUE,
    guild_id INTEGER NOT NULL,
    type TEXT NOT NULL,
    category TEXT NOT NULL,
    user_id INTEGER,
    counterparty_id INTEGER,
    amount INTEGER NOT NULL,
    source_table TEXT,
    source_id INTEGER,
    description TEXT NOT NULL,
    created_by INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    admin_id INTEGER,
    action TEXT NOT NULL,
    affected_user_id INTEGER,
    amount INTEGER,
    system TEXT NOT NULL,
    observation TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS dm_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER,
    user_id INTEGER NOT NULL,
    action TEXT NOT NULL,
    success INTEGER NOT NULL,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_fines_user_status
ON fines(guild_id, user_id, status);

CREATE INDEX IF NOT EXISTS idx_movements_guild_created
ON movements(guild_id, created_at);

CREATE INDEX IF NOT EXISTS idx_activity_participants_activity
ON activity_participants(activity_id);
"""
