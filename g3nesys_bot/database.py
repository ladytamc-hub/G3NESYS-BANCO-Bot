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
        if "caller_percent" not in payout_columns:
            self._conn.execute(
                "ALTER TABLE payouts ADD COLUMN caller_percent REAL NOT NULL DEFAULT 0"
            )
        if "caller_amount" not in payout_columns:
            self._conn.execute(
                "ALTER TABLE payouts ADD COLUMN caller_amount INTEGER NOT NULL DEFAULT 0"
            )
        if "quick_liquidated_at" not in payout_columns:
            self._conn.execute(
                "ALTER TABLE payouts ADD COLUMN quick_liquidated_at TEXT"
            )
        if "quick_liquidated_by" not in payout_columns:
            self._conn.execute(
                "ALTER TABLE payouts ADD COLUMN quick_liquidated_by INTEGER"
            )

        payout_participant_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(payout_participants)"
            ).fetchall()
        }
        if "liquidated_at" not in payout_participant_columns:
            self._conn.execute(
                "ALTER TABLE payout_participants ADD COLUMN liquidated_at TEXT"
            )
        if "liquidated_by" not in payout_participant_columns:
            self._conn.execute(
                "ALTER TABLE payout_participants ADD COLUMN liquidated_by INTEGER"
            )
        if "liquidation_id" not in payout_participant_columns:
            self._conn.execute(
                "ALTER TABLE payout_participants ADD COLUMN liquidation_id INTEGER"
            )
        if "liquidation_movement_id" not in payout_participant_columns:
            self._conn.execute(
                "ALTER TABLE payout_participants ADD COLUMN liquidation_movement_id INTEGER"
            )

        template_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(templates)").fetchall()
        }
        if "description" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN description TEXT NOT NULL DEFAULT ''"
            )
        if "publica" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN publica INTEGER NOT NULL DEFAULT 0"
            )
        if "voice_channel_id" not in template_columns:
            self._conn.execute(
                "ALTER TABLE templates ADD COLUMN voice_channel_id INTEGER"
            )
        if "image_url" not in template_columns:
            self._conn.execute("ALTER TABLE templates ADD COLUMN image_url TEXT")

        activity_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(activities)").fetchall()
        }
        if "cancelled_by" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN cancelled_by INTEGER")
        if "cancellation_reputation_exempt" not in activity_columns:
            self._conn.execute(
                "ALTER TABLE activities ADD COLUMN cancellation_reputation_exempt INTEGER"
            )
        if "cancellation_reason" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN cancellation_reason TEXT")
        if "check_sent_at" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN check_sent_at TEXT")
        if "activity_type" not in activity_columns:
            self._conn.execute(
                "ALTER TABLE activities ADD COLUMN activity_type TEXT NOT NULL DEFAULT 'regular'"
            )
        if "image_url" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN image_url TEXT")
        if "mandatory_loot_amount" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN mandatory_loot_amount INTEGER")
        if "mandatory_loot_recorded_by" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN mandatory_loot_recorded_by INTEGER")
        if "mandatory_loot_recorded_at" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN mandatory_loot_recorded_at TEXT")
        if "deleted_by" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN deleted_by INTEGER")
        if "deleted_at" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN deleted_at TEXT")
        if "thread_id" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN thread_id INTEGER")
        if "thread_panel_message_id" not in activity_columns:
            self._conn.execute("ALTER TABLE activities ADD COLUMN thread_panel_message_id INTEGER")

        attendance_columns = {
            row["name"]
            for row in self._conn.execute(
                "PRAGMA table_info(asistencia_actividades)"
            ).fetchall()
        }
        if "voice_seconds" not in attendance_columns:
            self._conn.execute(
                "ALTER TABLE asistencia_actividades "
                "ADD COLUMN voice_seconds INTEGER NOT NULL DEFAULT 0"
            )
        if "participation_percent" not in attendance_columns:
            self._conn.execute(
                "ALTER TABLE asistencia_actividades "
                "ADD COLUMN participation_percent REAL NOT NULL DEFAULT 0"
            )

        movement_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(movements)").fetchall()
        }
        if "fee_amount" not in movement_columns:
            self._conn.execute(
                "ALTER TABLE movements ADD COLUMN fee_amount INTEGER NOT NULL DEFAULT 0"
            )
        if "net_amount" not in movement_columns:
            self._conn.execute(
                "ALTER TABLE movements ADD COLUMN net_amount INTEGER"
            )

        withdrawal_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(withdrawals)").fetchall()
        }
        if "approval_admin_message" not in withdrawal_columns:
            self._conn.execute(
                "ALTER TABLE withdrawals ADD COLUMN approval_admin_message TEXT"
            )
        if "liquidation_admin_message" not in withdrawal_columns:
            self._conn.execute(
                "ALTER TABLE withdrawals ADD COLUMN liquidation_admin_message TEXT"
            )

        regear_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(regear_requests)").fetchall()
        }
        if "bot_channel_id" not in regear_columns:
            self._conn.execute("ALTER TABLE regear_requests ADD COLUMN bot_channel_id INTEGER")

        caller_penalty_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(caller_penalties)").fetchall()
        }
        if "id" not in caller_penalty_columns:
            self._conn.execute(
                """
                CREATE TABLE caller_penalties__history_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    score_at_penalty INTEGER NOT NULL,
                    reason TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    penalized_at TEXT NOT NULL,
                    notified_at TEXT,
                    removed_by INTEGER,
                    removed_at TEXT,
                    rearmed INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            self._conn.execute(
                """
                INSERT INTO caller_penalties__history_migration (
                    guild_id, user_id, score_at_penalty, reason, active,
                    penalized_at, notified_at, removed_by, removed_at, rearmed
                )
                SELECT guild_id, user_id, score_at_penalty, reason, active,
                       penalized_at, notified_at, removed_by, removed_at, 0
                FROM caller_penalties
                """
            )
            self._conn.execute("DROP TABLE caller_penalties")
            self._conn.execute(
                "ALTER TABLE caller_penalties__history_migration RENAME TO caller_penalties"
            )
        elif "rearmed" not in caller_penalty_columns:
            self._conn.execute(
                "ALTER TABLE caller_penalties ADD COLUMN rearmed INTEGER NOT NULL DEFAULT 0"
            )

        penalty_columns = {
            row["name"]
            for row in self._conn.execute("PRAGMA table_info(penalizacion_actividades)").fetchall()
        }
        if "servidor_id" in penalty_columns and "guild_id" not in penalty_columns:
            self._conn.execute(
                "ALTER TABLE penalizacion_actividades RENAME COLUMN servidor_id TO guild_id"
            )

        scoped_tables = {
            "activities": (
                """
                CREATE TABLE activities__guild_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
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
                    ended_at TEXT,
                    cancelled_by INTEGER,
                    cancellation_reputation_exempt INTEGER,
                    cancellation_reason TEXT,
                    check_sent_at TEXT,
                    activity_type TEXT NOT NULL DEFAULT 'regular',
                    image_url TEXT,
                    mandatory_loot_amount INTEGER,
                    mandatory_loot_recorded_by INTEGER,
                    mandatory_loot_recorded_at TEXT,
                    deleted_by INTEGER,
                    deleted_at TEXT,
                    thread_id INTEGER,
                    thread_panel_message_id INTEGER,
                    UNIQUE(guild_id, code)
                )
                """,
                "id, code, guild_id, template_id, name, caller_id, horario, "
                "voice_channel_id, notes, status, channel_id, message_id, "
                "created_at, started_at, ended_at, cancelled_by, "
                "cancellation_reputation_exempt, cancellation_reason, check_sent_at, "
                "activity_type, image_url, mandatory_loot_amount, "
                "mandatory_loot_recorded_by, mandatory_loot_recorded_at, "
                "deleted_by, deleted_at, thread_id, thread_panel_message_id",
            ),
            "fines": (
                """
                CREATE TABLE fines__guild_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
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
                    cancel_reason TEXT,
                    UNIQUE(guild_id, code)
                )
                """,
                "id, code, guild_id, user_id, amount, reason, status, origin, "
                "created_by, paid_by, payment_method, movement_id, created_at, "
                "paid_at, cancelled_by, cancelled_at, cancel_reason",
            ),
            "withdrawals": (
                """
                CREATE TABLE withdrawals__guild_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
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
                    rejection_reason TEXT,
                    approval_admin_message TEXT,
                    liquidation_admin_message TEXT,
                    UNIQUE(guild_id, code)
                )
                """,
                "id, code, guild_id, user_id, amount_requested, amount_liquidated, "
                "status, reason, created_at, approved_by, approved_at, "
                "liquidated_by, liquidated_at, rejected_by, rejected_at, rejection_reason, "
                "approval_admin_message, liquidation_admin_message",
            ),
            "payouts": (
                """
                CREATE TABLE payouts__guild_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
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
                    reviewed_at TEXT,
                    caller_percent REAL NOT NULL DEFAULT 0,
                    caller_amount INTEGER NOT NULL DEFAULT 0,
                    quick_liquidated_at TEXT,
                    quick_liquidated_by INTEGER,
                    UNIQUE(guild_id, code)
                )
                """,
                "id, code, guild_id, activity_id, caller_id, status, gross_loot, "
                "market_rate_percent, repairs, other_expenses, guild_percent, "
                "guild_amount, distributable, notes, created_at, sent_to_admin_at, "
                "reviewed_by, reviewed_at, caller_percent, caller_amount, "
                "quick_liquidated_at, quick_liquidated_by",
            ),
            "movements": (
                """
                CREATE TABLE movements__guild_migration (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
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
                    created_at TEXT NOT NULL,
                    fee_amount INTEGER NOT NULL DEFAULT 0,
                    net_amount INTEGER,
                    UNIQUE(guild_id, code)
                )
                """,
                "id, code, guild_id, type, category, user_id, counterparty_id, "
                "amount, source_table, source_id, description, created_by, created_at, "
                "fee_amount, net_amount",
            ),
        }
        rebuild = [
            table
            for table in scoped_tables
            if not self._has_unique_index(table, ("guild_id", "code"))
        ]
        if rebuild:
            self._conn.commit()
            self._conn.execute("PRAGMA foreign_keys = OFF")
            try:
                self._conn.execute("BEGIN IMMEDIATE")
                for table in rebuild:
                    create_sql, columns = scoped_tables[table]
                    temporary = f"{table}__guild_migration"
                    self._conn.execute(f"DROP TABLE IF EXISTS {temporary}")
                    self._conn.execute(create_sql)
                    self._conn.execute(
                        f"INSERT INTO {temporary} ({columns}) SELECT {columns} FROM {table}"
                    )
                    self._conn.execute(f"DROP TABLE {table}")
                    self._conn.execute(f"ALTER TABLE {temporary} RENAME TO {table}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
            finally:
                self._conn.execute("PRAGMA foreign_keys = ON")

        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_fines_user_status "
            "ON fines(guild_id, user_id, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_movements_guild_created "
            "ON movements(guild_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_activities_guild_status "
            "ON activities(guild_id, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payouts_guild_status "
            "ON payouts(guild_id, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_withdrawals_guild_status "
            "ON withdrawals(guild_id, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_penalties_guild_user_active "
            "ON penalizacion_actividades(guild_id, usuario_id, activo)"
        )
        self._conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_caller_penalties_one_active "
            "ON caller_penalties(guild_id, user_id) WHERE active = 1"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_admin_access_guild_authorized "
            "ON admin_access(guild_id, authorized)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_payout_participants_liquidation "
            "ON payout_participants(payout_id, liquidated_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_quick_liquidations_guild_created "
            "ON quick_liquidations(guild_id, created_at)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_regear_requests_guild_status "
            "ON regear_requests(guild_id, status)"
        )
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_regear_requests_message "
            "ON regear_requests(guild_id, message_id)"
        )

    def _has_unique_index(self, table: str, columns: tuple[str, ...]) -> bool:
        for index in self._conn.execute(f"PRAGMA index_list({table})").fetchall():
            if not int(index["unique"]):
                continue
            indexed_columns = tuple(
                row["name"]
                for row in self._conn.execute(
                    f"PRAGMA index_info('{index['name']}')"
                ).fetchall()
            )
            if indexed_columns == columns:
                return True
        return False

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

CREATE TABLE IF NOT EXISTS admin_access (
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    authorized INTEGER NOT NULL,
    updated_by INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (guild_id, user_id)
);

CREATE TABLE IF NOT EXISTS caller_penalties (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    score_at_penalty INTEGER NOT NULL,
    reason TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    penalized_at TEXT NOT NULL,
    notified_at TEXT,
    removed_by INTEGER,
    removed_at TEXT,
    rearmed INTEGER NOT NULL DEFAULT 0
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
    voice_channel_id INTEGER,
    description TEXT NOT NULL,
    image_url TEXT,
    publica INTEGER NOT NULL DEFAULT 0,
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
    code TEXT NOT NULL,
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
    ended_at TEXT,
    cancelled_by INTEGER,
    cancellation_reputation_exempt INTEGER,
    cancellation_reason TEXT,
    check_sent_at TEXT,
    activity_type TEXT NOT NULL DEFAULT 'regular',
    image_url TEXT,
    mandatory_loot_amount INTEGER,
    mandatory_loot_recorded_by INTEGER,
    mandatory_loot_recorded_at TEXT,
    deleted_by INTEGER,
    deleted_at TEXT,
    thread_id INTEGER,
    thread_panel_message_id INTEGER,
    UNIQUE(guild_id, code)
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
    voice_seconds INTEGER NOT NULL DEFAULT 0,
    participation_percent REAL NOT NULL DEFAULT 0,
    UNIQUE(actividad_id, usuario_id)
);

CREATE TABLE IF NOT EXISTS activity_voice_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    joined_at TEXT NOT NULL,
    left_at TEXT,
    seconds INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS activity_join_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    display_name TEXT NOT NULL,
    requested_role TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'Pendiente',
    requested_at TEXT NOT NULL,
    reviewed_by INTEGER,
    reviewed_at TEXT
);

CREATE TABLE IF NOT EXISTS penalizacion_actividades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
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
    code TEXT NOT NULL,
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
    cancel_reason TEXT,
    UNIQUE(guild_id, code)
);

CREATE TABLE IF NOT EXISTS withdrawals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
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
    rejection_reason TEXT,
    approval_admin_message TEXT,
    liquidation_admin_message TEXT,
    UNIQUE(guild_id, code)
);

CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
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
    reviewed_at TEXT,
    caller_percent REAL NOT NULL DEFAULT 0,
    caller_amount INTEGER NOT NULL DEFAULT 0,
    quick_liquidated_at TEXT,
    quick_liquidated_by INTEGER,
    UNIQUE(guild_id, code)
);

CREATE TABLE IF NOT EXISTS payout_audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    payout_id INTEGER NOT NULL REFERENCES payouts(id) ON DELETE CASCADE,
    actor_id INTEGER,
    action TEXT NOT NULL,
    details TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS payout_participants (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    payout_id INTEGER NOT NULL REFERENCES payouts(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    participation_percent REAL NOT NULL,
    amount INTEGER NOT NULL DEFAULT 0,
    balance_type TEXT,
    deposited_at TEXT,
    liquidated_at TEXT,
    liquidated_by INTEGER,
    liquidation_id INTEGER,
    liquidation_movement_id INTEGER,
    UNIQUE(payout_id, user_id)
);

CREATE TABLE IF NOT EXISTS quick_liquidations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
    guild_id INTEGER NOT NULL,
    payout_id INTEGER NOT NULL REFERENCES payouts(id) ON DELETE CASCADE,
    mode TEXT NOT NULL,
    admin_id INTEGER NOT NULL,
    total_amount INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(guild_id, code)
);

CREATE TABLE IF NOT EXISTS quick_liquidation_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    liquidation_id INTEGER NOT NULL REFERENCES quick_liquidations(id) ON DELETE CASCADE,
    payout_participant_id INTEGER NOT NULL REFERENCES payout_participants(id) ON DELETE CASCADE,
    user_id INTEGER NOT NULL,
    amount INTEGER NOT NULL,
    balance_type TEXT NOT NULL,
    movement_id INTEGER NOT NULL,
    UNIQUE(payout_participant_id)
);

CREATE TABLE IF NOT EXISTS regear_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id INTEGER NOT NULL,
    request_code TEXT NOT NULL,
    user_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    bot_message_id INTEGER,
    bot_channel_id INTEGER,
    approved_amount INTEGER,
    review_notes TEXT,
    image_url TEXT NOT NULL,
    message_url TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    reviewed_by INTEGER,
    created_at TEXT NOT NULL,
    reviewed_at TEXT,
    updated_at TEXT,
    UNIQUE(guild_id, request_code),
    UNIQUE(guild_id, message_id)
);

CREATE TABLE IF NOT EXISTS movements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    code TEXT NOT NULL,
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
    created_at TEXT NOT NULL,
    fee_amount INTEGER NOT NULL DEFAULT 0,
    net_amount INTEGER,
    UNIQUE(guild_id, code)
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

CREATE INDEX IF NOT EXISTS idx_voice_sessions_activity_user
ON activity_voice_sessions(guild_id, activity_id, user_id, left_at);

CREATE INDEX IF NOT EXISTS idx_join_requests_pending
ON activity_join_requests(guild_id, activity_id, status);

CREATE INDEX IF NOT EXISTS idx_activities_guild_status
ON activities(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_payouts_guild_status
ON payouts(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_payout_audit_guild_payout
ON payout_audit_logs(guild_id, payout_id, created_at);

CREATE INDEX IF NOT EXISTS idx_withdrawals_guild_status
ON withdrawals(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_regear_requests_guild_status
ON regear_requests(guild_id, status);

CREATE INDEX IF NOT EXISTS idx_regear_requests_message
ON regear_requests(guild_id, message_id);

"""
