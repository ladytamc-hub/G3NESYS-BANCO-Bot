import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from g3nesys_bot.cogs.admin import Admin
from g3nesys_bot.cogs.bank import Bank, WithdrawalReviewView
from g3nesys_bot.constants import (
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
)
from g3nesys_bot.database import Database, SCHEMA


class FakeMember:
    def __init__(self, user_id: int):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.display_name = f"User {user_id}"
        self.bot = False

    async def send(self, **kwargs):
        return None


class FakeGuild:
    def __init__(self):
        self.id = 10
        self.owner_id = 999
        self.emojis = []
        self.members = {100: FakeMember(100), 200: FakeMember(200)}

    def get_member(self, user_id: int):
        return self.members.get(user_id)

    def get_channel(self, channel_id: int):
        return None


class WithdrawalRejectionTests(unittest.IsolatedAsyncioTestCase):
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
        self.bot = SimpleNamespace(db=self.db, get_cog=lambda name: None)
        self.admin = Admin(self.bot)
        self.insert_account()
        self.insert_withdrawal()

    def tearDown(self):
        self.db.close()

    def insert_account(self):
        self.db.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (10, 100, 5000, 200, 0, "2026-08-02T00:00:00+00:00"),
        )

    def insert_withdrawal(self, *, status: str = WITHDRAWAL_PENDING):
        self.db.execute(
            """
            INSERT INTO withdrawals (
                code, guild_id, user_id, amount_requested, amount_liquidated,
                status, reason, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            ("COBRO-000001", 10, 100, 3000, None, status, "Pago semanal", "2026-08-02T01:00:00+00:00"),
        )

    def withdrawal(self):
        return self.db.fetch_one(
            "SELECT * FROM withdrawals WHERE guild_id = ? AND code = ?",
            (10, "COBRO-000001"),
        )

    def count(self, table: str) -> int:
        return int(self.db.fetch_one(f"SELECT COUNT(*) AS total FROM {table}")["total"])

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_reject_pending_without_reason_is_historical_only(self, send_dm, send_admin):
        send_dm.return_value = True

        result = await self.admin.reject_withdrawal(self.guild, "cobro-000001", 200, "")

        self.assertEqual(result, "Solicitud `COBRO-000001` no aprobada.")
        withdrawal = self.withdrawal()
        self.assertEqual(withdrawal["status"], WITHDRAWAL_REJECTED)
        self.assertEqual(withdrawal["rejected_by"], 200)
        self.assertIsNotNone(withdrawal["rejected_at"])
        self.assertIsNone(withdrawal["rejection_reason"])
        self.assertIsNone(withdrawal["amount_liquidated"])
        self.assertIsNone(withdrawal["approved_by"])
        self.assertIsNone(withdrawal["assigned_officer_id"])
        self.assertIsNone(withdrawal["payment_place"])
        self.assertIsNone(withdrawal["payment_schedule"])
        account = self.db.fetch_one("SELECT * FROM accounts WHERE guild_id = ? AND user_id = ?", (10, 100))
        self.assertEqual((account["available"], account["retained"], account["seized"]), (5000, 200, 0))
        self.assertEqual(self.count("movements"), 0)
        self.assertEqual(self.count("withdrawal_action_logs"), 1)
        self.assertEqual(self.count("audit_logs"), 1)
        dm_content = send_dm.await_args.kwargs["content"]
        self.assertEqual(dm_content, "Tu solicitud de cobro COBRO-000001 no fue aprobada.")
        self.assertNotIn("Motivo:", dm_content)
        send_admin.assert_awaited_once()

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_reject_pending_with_reason_records_and_notifies_reason(self, send_dm, send_admin):
        send_dm.return_value = True

        await self.admin.reject_withdrawal(self.guild, "COBRO-000001", 200, "Falta evidencia")

        withdrawal = self.withdrawal()
        self.assertEqual(withdrawal["rejection_reason"], "Falta evidencia")
        log = self.db.fetch_one("SELECT * FROM withdrawal_action_logs")
        self.assertEqual(log["action_type"], "no_aprobada")
        self.assertEqual(log["old_status"], WITHDRAWAL_PENDING)
        self.assertEqual(log["new_status"], WITHDRAWAL_REJECTED)
        self.assertEqual(log["note"], "Falta evidencia")
        audit = self.db.fetch_one("SELECT * FROM audit_logs")
        self.assertEqual(audit["action"], "Solicitud de cobro no aprobada")
        self.assertIn("Falta evidencia", audit["observation"])
        self.assertEqual(
            send_dm.await_args.kwargs["content"],
            "Tu solicitud de cobro COBRO-000001 no fue aprobada.\nMotivo: Falta evidencia",
        )
        self.assertIn("Falta evidencia", send_admin.await_args.kwargs["content"])

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_rejecting_twice_is_idempotent(self, send_dm, send_admin):
        send_dm.return_value = True

        await self.admin.reject_withdrawal(self.guild, "COBRO-000001", 200, "")
        with self.assertRaisesRegex(ValueError, "ya no esta pendiente"):
            await self.admin.reject_withdrawal(self.guild, "COBRO-000001", 200, "otro motivo")

        self.assertEqual(self.count("withdrawal_action_logs"), 1)
        self.assertEqual(self.count("audit_logs"), 1)
        self.assertEqual(self.count("movements"), 0)
        send_dm.assert_awaited_once()
        send_admin.assert_awaited_once()

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_non_pending_request_is_not_rejected(self, send_dm, send_admin):
        self.db.execute("UPDATE withdrawals SET status = ? WHERE code = ?", (WITHDRAWAL_APPROVED, "COBRO-000001"))

        with self.assertRaisesRegex(ValueError, "ya no esta pendiente"):
            await self.admin.reject_withdrawal(self.guild, "COBRO-000001", 200, "")

        self.assertEqual(self.withdrawal()["status"], WITHDRAWAL_APPROVED)
        self.assertEqual(self.count("withdrawal_action_logs"), 0)
        self.assertEqual(self.count("audit_logs"), 0)
        self.assertEqual(self.count("movements"), 0)
        send_dm.assert_not_awaited()
        send_admin.assert_not_awaited()

    def test_reject_button_only_exists_for_pending_requests(self):
        bank = Bank(self.bot)
        labels = [item.label for item in WithdrawalReviewView(bank, 10, "COBRO-000001").children]
        self.assertEqual(labels, ["Aprobar cobro", "Rechazar", "Delegar pago"])

        self.db.execute("UPDATE withdrawals SET status = ? WHERE code = ?", (WITHDRAWAL_REJECTED, "COBRO-000001"))
        labels = [item.label for item in WithdrawalReviewView(bank, 10, "COBRO-000001").children]
        self.assertEqual(labels, [])

    def test_embed_omits_empty_rejection_reason(self):
        bank = Bank(self.bot)
        self.db.execute(
            """
            UPDATE withdrawals
            SET status = ?, rejected_by = ?, rejected_at = ?, rejection_reason = NULL
            WHERE code = ?
            """,
            (WITHDRAWAL_REJECTED, 200, "2026-08-02T02:00:00+00:00", "COBRO-000001"),
        )

        embed = bank.withdrawal_admin_embed(self.guild, self.withdrawal())
        fields = {field.name: field.value for field in embed.fields}
        self.assertEqual(embed.description, f"Estado: {WITHDRAWAL_REJECTED}")
        self.assertEqual(fields["No aprobada por"], "<@200>")
        self.assertEqual(fields["Fecha de rechazo"], "2026-08-02T02:00:00+00:00")
        self.assertEqual(fields["Pendiente"], "0 plata")
        self.assertNotIn("Motivo del rechazo", fields)


if __name__ == "__main__":
    unittest.main()

