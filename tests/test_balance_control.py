import sqlite3
import threading
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from g3nesys_bot.cogs.admin import Admin, GuildEconomyView
from g3nesys_bot.constants import WITHDRAWAL_CANCELLED, WITHDRAWAL_PENDING
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.balance_control import (
    BALANCE_SEIZURE_TYPE,
    WithdrawalCancellationCooldown,
    cancel_pending_withdrawal_by_user,
    list_outside_users_with_balance,
    seize_user_balance,
)
from g3nesys_bot.services.economy import create_withdrawal_request


class FakeMember:
    def __init__(self, user_id: int, display_name: str | None = None):
        self.id = user_id
        self.display_name = display_name or f"User {user_id}"
        self.mention = f"<@{user_id}>"
        self.guild = None


class FakeGuild:
    def __init__(self):
        self.id = 10
        self.owner_id = 999
        self.members = {}
        self.emojis = []

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_channel(self, _channel_id: int):
        return None


class BalanceControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.guild = FakeGuild()

    def tearDown(self):
        self.db.close()

    def add_account(self, user_id: int, *, available: int, seized: int = 0):
        self.db.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (?, ?, ?, 0, ?, ?)
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET available = excluded.available, seized = excluded.seized
            """,
            (10, user_id, available, seized, "2026-08-10T00:00:00+00:00"),
        )

    def withdrawal(self, code: str):
        return self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (10, code),
        )

    def account(self, user_id: int):
        return self.db.fetch_one(
            "SELECT * FROM accounts WHERE guild_id = ? AND user_id = ?",
            (10, user_id),
        )

    def test_user_can_cancel_pending_withdrawal_without_balance_change(self):
        self.add_account(100, available=8000)
        code = create_withdrawal_request(self.db, 10, user_id=100, amount=3000, reason="Pago semanal")

        cancelled = cancel_pending_withdrawal_by_user(self.db, 10, user_id=100, code=code)

        self.assertEqual(cancelled, code)
        withdrawal = self.withdrawal(code)
        self.assertEqual(withdrawal["status"], WITHDRAWAL_CANCELLED)
        self.assertIsNotNone(withdrawal["closed_at"])
        self.assertEqual(withdrawal["return_reason"], "Cancelada por el usuario")
        self.assertEqual(self.account(100)["available"], 8000)
        log = self.db.fetch_one("SELECT * FROM withdrawal_action_logs")
        self.assertEqual(log["action_type"], "cancelada_usuario")
        self.assertEqual(log["old_status"], WITHDRAWAL_PENDING)
        self.assertEqual(log["new_status"], WITHDRAWAL_CANCELLED)

    def test_user_cancellation_has_24_hour_cooldown(self):
        self.add_account(100, available=8000)
        first = create_withdrawal_request(self.db, 10, user_id=100, amount=1000, reason="Uno")
        cancel_pending_withdrawal_by_user(self.db, 10, user_id=100, code=first)
        second = create_withdrawal_request(self.db, 10, user_id=100, amount=1000, reason="Dos")

        with self.assertRaises(WithdrawalCancellationCooldown):
            cancel_pending_withdrawal_by_user(self.db, 10, user_id=100, code=second)

        older = (datetime.now(timezone.utc) - timedelta(hours=25)).isoformat()
        self.db.execute(
            "UPDATE withdrawal_user_cancellations SET last_cancelled_at = ? WHERE guild_id = ? AND user_id = ?",
            (older, 10, 100),
        )
        self.assertEqual(cancel_pending_withdrawal_by_user(self.db, 10, user_id=100, code=second), second)

    def test_outside_balances_exclude_current_members_and_keep_unknown_left_date(self):
        self.add_account(100, available=5000)
        self.add_account(200, available=7000)
        self.guild.members[200] = FakeMember(200)

        rows, total = list_outside_users_with_balance(self.db, self.guild)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0].user_id, 100)
        self.assertEqual(rows[0].left_at, None)
        self.assertEqual(rows[0].days_out, None)

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    async def test_member_remove_records_departure_and_alerts_once_when_balance_is_positive(self, send_admin):
        self.add_account(100, available=5000)
        bot = SimpleNamespace(db=self.db)
        admin = Admin(bot)
        member = FakeMember(100, "Salida")
        member.guild = self.guild

        await admin.on_member_remove(member)
        await admin.on_member_remove(member)

        departure = self.db.fetch_one(
            "SELECT * FROM member_departures WHERE guild_id = ? AND user_id = ?",
            (10, 100),
        )
        self.assertEqual(departure["in_server"], 0)
        self.assertIsNotNone(departure["left_at"])
        self.assertIsNotNone(departure["last_alerted_at"])
        send_admin.assert_awaited_once()

    def test_seize_user_balance_moves_available_to_seized_and_audits(self):
        self.add_account(100, available=10000, seized=250)

        result = seize_user_balance(
            self.db,
            10,
            user_id=100,
            amount=4000,
            admin_id=900,
            reason="Pago manual realizado fuera del sistema.",
            origin="pago manual",
            known_name="User 100",
        )

        account = self.account(100)
        self.assertEqual(account["available"], 6000)
        self.assertEqual(account["seized"], 4250)
        movement = self.db.fetch_one("SELECT * FROM movements WHERE id = ?", (result.movement_id,))
        self.assertEqual(movement["type"], BALANCE_SEIZURE_TYPE)
        self.assertEqual(movement["amount"], 4000)
        seizure = self.db.fetch_one("SELECT * FROM balance_seizure_logs")
        self.assertEqual(seizure["balance_before"], 10000)
        self.assertEqual(seizure["balance_after"], 6000)
        self.assertEqual(seizure["origin"], "pago manual")
        audit = self.db.fetch_one("SELECT * FROM audit_logs WHERE action = ?", ("Decomiso administrativo de balance",))
        self.assertIsNotNone(audit)

    def test_seize_user_balance_rejects_amount_above_available(self):
        self.add_account(100, available=1000)

        with self.assertRaisesRegex(ValueError, "saldo disponible"):
            seize_user_balance(
                self.db,
                10,
                user_id=100,
                amount=2000,
                admin_id=900,
                reason="Correccion administrativa",
                origin="correccion",
                known_name="User 100",
            )

        self.assertEqual(self.account(100)["available"], 1000)
        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM movements")["total"], 0)

    def test_guild_economy_view_exposes_balance_control_buttons(self):
        labels = [item.label for item in GuildEconomyView(SimpleNamespace(db=self.db)).children]

        self.assertIn("Saldos de usuarios fuera", labels)
        self.assertIn("Decomisar balance", labels)


if __name__ == "__main__":
    unittest.main()
