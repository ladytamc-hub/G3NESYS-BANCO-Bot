import sqlite3
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from g3nesys_bot.cogs.admin import Admin
from g3nesys_bot.cogs.bank import Bank
from g3nesys_bot.constants import (
    WITHDRAWAL_APPROVED,
    WITHDRAWAL_CANCELLED,
    WITHDRAWAL_PAID,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
    WITHDRAWAL_UNPAID,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.economy import ActiveWithdrawalError, create_withdrawal_request


class FakeMember:
    def __init__(self, user_id, *, admin=False):
        self.id = user_id
        self.mention = f"<@{user_id}>"
        self.display_name = f"User {user_id}"
        self.bot = False
        self.roles = []
        self.guild_permissions = SimpleNamespace(administrator=admin)

    async def send(self, **kwargs):
        return None


class FakeGuild:
    def __init__(self):
        self.id = 10
        self.name = "G3NESYS"
        self.owner_id = 999
        self.emojis = []
        self.members = {100: FakeMember(100), 200: FakeMember(200, admin=True)}

    def get_member(self, user_id):
        return self.members.get(user_id)

    def get_channel(self, channel_id):
        return None


class FakeBot:
    def __init__(self, db):
        self.db = db
        self.bank = SimpleNamespace(refresh_withdrawal_admin_message=AsyncMock(return_value=""))

    def get_cog(self, name):
        return self.bank if name == "Bank" else None

    def add_view(self, _view):
        return None


class FakeResponse:
    def __init__(self):
        self.messages = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class WithdrawalActiveRequestTests(unittest.IsolatedAsyncioTestCase):
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
        self.bot = FakeBot(self.db)
        self.admin = Admin(self.bot)
        self.bank = Bank(self.bot)
        self.add_account(100, available=10000)

    def tearDown(self):
        self.db.close()

    def add_account(self, user_id, *, available=0):
        self.db.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (10, ?, ?, 0, 0, '2026-08-07T00:00:00+00:00')
            ON CONFLICT(guild_id, user_id)
            DO UPDATE SET available = excluded.available
            """,
            (user_id, available),
        )

    def withdrawal(self, code="COBRO-000001"):
        return self.db.fetch_one("SELECT * FROM withdrawals WHERE guild_id = 10 AND code = ?", (code,))

    def create_request(self, *, user_id=100, amount=3000, status=None):
        code = create_withdrawal_request(
            self.db,
            10,
            user_id=user_id,
            amount=amount,
            reason="Pago semanal",
        )
        if status is not None:
            self.db.execute(
                "UPDATE withdrawals SET status = ?, amount_liquidated = ? WHERE guild_id = 10 AND code = ?",
                (status, 1000 if status == WITHDRAWAL_PARTIAL else None, code),
            )
        return code

    def balance(self, user_id=100):
        return self.db.fetch_one("SELECT * FROM accounts WHERE guild_id = 10 AND user_id = ?", (user_id,))

    def test_user_without_active_request_can_create_request_without_balance_change(self):
        before = tuple(self.balance()[key] for key in ("available", "retained", "seized"))

        code = self.create_request()

        self.assertEqual(code, "COBRO-000001")
        self.assertEqual(self.withdrawal()["status"], WITHDRAWAL_PENDING)
        after = tuple(self.balance()[key] for key in ("available", "retained", "seized"))
        self.assertEqual(after, before)

    def test_pending_request_blocks_new_request(self):
        self.create_request()

        with self.assertRaises(ActiveWithdrawalError) as ctx:
            self.create_request(amount=2000)

        self.assertEqual(ctx.exception.withdrawal["code"], "COBRO-000001")
        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM withdrawals")["total"], 1)

    def test_partial_request_blocks_new_request(self):
        self.create_request(status=WITHDRAWAL_PARTIAL)

        with self.assertRaises(ActiveWithdrawalError):
            self.create_request(amount=2000)

        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM withdrawals")["total"], 1)

    def test_closed_statuses_allow_new_request(self):
        for status in (WITHDRAWAL_PAID, WITHDRAWAL_REJECTED, WITHDRAWAL_UNPAID, WITHDRAWAL_CANCELLED):
            with self.subTest(status=status):
                self.db.execute("DELETE FROM withdrawals")
                self.db.execute("DELETE FROM audit_logs")
                self.db.execute("DELETE FROM id_counters")
                code = self.create_request(status=status)
                self.assertEqual(code, "COBRO-000001")

                new_code = self.create_request(amount=2000)

                self.assertEqual(new_code, "COBRO-000002")
                self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM withdrawals")["total"], 2)

    def test_two_simultaneous_attempts_create_only_one_active_request(self):
        def attempt():
            try:
                return create_withdrawal_request(self.db, 10, user_id=100, amount=3000, reason="Pago semanal")
            except ActiveWithdrawalError as exc:
                return exc.withdrawal["code"]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(lambda _: attempt(), range(2)))

        self.assertEqual(results, ["COBRO-000001", "COBRO-000001"])
        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM withdrawals")["total"], 1)

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_mark_paid_discounts_balance_once(self, send_dm, send_admin):
        send_dm.return_value = True
        code = self.create_request(status=WITHDRAWAL_APPROVED)

        result = await self.admin.pay_withdrawal_full(self.guild, code, 200)

        self.assertIn("Movimiento #", result)
        self.assertEqual(self.withdrawal(code)["status"], WITHDRAWAL_PAID)
        self.assertEqual(self.balance()["available"], 7000)
        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM movements")["total"], 1)
        send_admin.assert_awaited()

    @patch("g3nesys_bot.cogs.admin.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.admin.send_dm_safe", new_callable=AsyncMock)
    async def test_cannot_pay_when_current_balance_is_insufficient(self, send_dm, send_admin):
        code = self.create_request(status=WITHDRAWAL_APPROVED)
        self.add_account(100, available=1000)

        with self.assertRaisesRegex(ValueError, "Saldo actual"):
            await self.admin.pay_withdrawal_full(self.guild, code, 200)

        self.assertEqual(self.withdrawal(code)["status"], WITHDRAWAL_APPROVED)
        self.assertEqual(self.balance()["available"], 1000)
        self.assertEqual(self.db.fetch_one("SELECT COUNT(*) AS total FROM movements")["total"], 0)
        send_dm.assert_not_awaited()
        send_admin.assert_not_awaited()

    def test_paid_withdrawal_embed_is_green(self):
        code = self.create_request(status=WITHDRAWAL_PAID)
        self.db.execute("UPDATE withdrawals SET amount_liquidated = amount_requested WHERE code = ?", (code,))

        embed = self.bank.withdrawal_admin_embed(self.guild, self.withdrawal(code))

        self.assertEqual(embed.color.value, discord.Color.green().value)

    @patch("g3nesys_bot.cogs.bank.send_admin_notification", new_callable=AsyncMock)
    @patch("g3nesys_bot.cogs.bank.has_bank_access", return_value=True)
    @patch("g3nesys_bot.cogs.bank.discord.Member", FakeMember)
    async def test_withdrawal_confirmation_includes_sunday_reminder(self, _access, send_admin):
        interaction = FakeInteraction(self.guild, self.guild.members[100])
        send_admin.return_value = None

        await self.bank.withdraw_interaction(interaction, "3000", "Pago semanal")

        content, ephemeral, _kwargs = interaction.response.messages[0]
        self.assertTrue(ephemeral)
        self.assertIn("Tu solicitud de cobro fue enviada correctamente", content)
        self.assertIn("los pagos se realizan los domingos", content)

    @patch("g3nesys_bot.cogs.bank.has_bank_access", return_value=True)
    @patch("g3nesys_bot.cogs.bank.discord.Member", FakeMember)
    async def test_duplicate_interaction_shows_existing_request_details(self, _access):
        self.create_request()
        interaction = FakeInteraction(self.guild, self.guild.members[100])

        await self.bank.withdraw_interaction(interaction, "2000", "Otra")

        content, ephemeral, _kwargs = interaction.response.messages[0]
        self.assertTrue(ephemeral)
        self.assertIn("Ya tienes una solicitud de cobro activa", content)
        self.assertIn("COBRO-000001", content)
        self.assertIn("Monto solicitado: 3,000 plata", content)
        self.assertIn(f"Estado: {WITHDRAWAL_PENDING}", content)


if __name__ == "__main__":
    unittest.main()
