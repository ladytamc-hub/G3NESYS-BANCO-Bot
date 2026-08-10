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
        self.guild_permissions = SimpleNamespace(administrator=False)
        self.roles = []


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


class FakeResponse:
    def __init__(self):
        self._done = False
        self.deferred = False
        self.messages = []

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        self.deferred = ephemeral
        self._done = True

    async def send_message(self, content=None, *, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "ephemeral": ephemeral, **kwargs})
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False, **kwargs):
        self.messages.append({"content": content, "ephemeral": ephemeral, **kwargs})


class FakeInteraction:
    def __init__(self, guild, user):
        self.guild = guild
        self.guild_id = guild.id
        self.user = user
        self.response = FakeResponse()
        self.followup = FakeFollowup()


async def press_outside_balances_button(view: GuildEconomyView, interaction: FakeInteraction):
    button = next(item for item in view.children if item.custom_id == "g3n:admin:guild_economy:outside_balances")
    await button.callback(interaction)


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

    def add_movement(self, user_id: int, *, code: str = "MOV-000001", amount: int = 500):
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, counterparty_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, ?, ?, ?, NULL, ?, NULL, NULL, ?, ?, ?, 0, NULL)
            """,
            (
                code,
                10,
                "DEPOSITO",
                "Prueba",
                user_id,
                amount,
                "Movimiento de prueba",
                900,
                "2026-08-10T00:00:00+00:00",
            ),
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

    def test_outside_balances_include_registered_departure_and_exclude_zero_balance(self):
        self.add_account(100, available=5000)
        self.add_account(200, available=0)
        self.db.execute(
            """
            INSERT INTO member_departures (
                guild_id, user_id, display_name, left_at, last_alerted_at, in_server, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 0, ?)
            """,
            (10, 100, "Fuera", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )
        self.db.execute(
            """
            INSERT INTO member_departures (
                guild_id, user_id, display_name, left_at, last_alerted_at, in_server, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 0, ?)
            """,
            (10, 200, "Sin saldo", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )

        rows, total = list_outside_users_with_balance(self.db, self.guild)

        self.assertEqual(total, 1)
        self.assertEqual(rows[0].user_id, 100)
        self.assertEqual(rows[0].display_name, "Fuera")
        self.assertEqual(rows[0].left_at, "2026-08-01T00:00:00+00:00")

    def test_outside_balances_works_without_member_departures_table(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row

        class LegacyDb:
            def fetch_all(self, query, params=()):
                return list(con.execute(query, tuple(params)).fetchall())

            def fetch_one(self, query, params=()):
                return con.execute(query, tuple(params)).fetchone()

        try:
            con.execute(
                """
                CREATE TABLE accounts (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    available INTEGER NOT NULL DEFAULT 0,
                    retained INTEGER NOT NULL DEFAULT 0,
                    seized INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (guild_id, user_id)
                )
                """
            )
            con.execute(
                """
                INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
                VALUES (?, ?, ?, 0, 0, ?)
                """,
                (10, 100, 5000, "2026-08-10T00:00:00+00:00"),
            )

            rows, total = list_outside_users_with_balance(LegacyDb(), self.guild)

            self.assertEqual(total, 1)
            self.assertEqual(rows[0].user_id, 100)
            self.assertIsNone(rows[0].left_at)
        finally:
            con.close()

    def test_outside_balances_tolerates_null_display_name_without_historical_tables(self):
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row

        class MinimalDb:
            def fetch_all(self, query, params=()):
                return list(con.execute(query, tuple(params)).fetchall())

            def fetch_one(self, query, params=()):
                return con.execute(query, tuple(params)).fetchone()

        try:
            con.executescript(
                """
                CREATE TABLE accounts (
                    guild_id INTEGER,
                    user_id INTEGER,
                    available INTEGER,
                    retained INTEGER,
                    seized INTEGER,
                    updated_at TEXT
                );
                CREATE TABLE member_departures (
                    guild_id INTEGER,
                    user_id INTEGER,
                    display_name TEXT,
                    left_at TEXT,
                    last_alerted_at TEXT,
                    in_server INTEGER,
                    updated_at TEXT
                );
                INSERT INTO accounts VALUES (10, 100, 5000, 0, 0, 'now');
                INSERT INTO member_departures VALUES (10, 100, NULL, NULL, NULL, 0, 'now');
                """
            )

            rows, total = list_outside_users_with_balance(MinimalDb(), self.guild)

            self.assertEqual(total, 1)
            self.assertEqual(rows[0].display_name, "No disponible")
            self.assertIsNone(rows[0].left_at)
        finally:
            con.close()

    def test_user_statement_inside_member_with_balance_works(self):
        user_id = 111111111111111111
        member = FakeMember(user_id, "Dentro")
        self.guild.members[user_id] = member
        self.add_account(user_id, available=5000)
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))

        text = admin.user_statement_text(10, member=member, user_id=user_id)

        self.assertIn("Estado de cuenta de Dentro", text)
        self.assertIn(f"Discord ID: `{user_id}`", text)
        self.assertIn("Presencia Discord: Dentro del servidor", text)
        self.assertIn("Disponible: 5,000 plata", text)

    async def test_user_statement_outside_member_with_balance_works(self):
        user_id = 111111111111111112
        self.add_account(user_id, available=7000)
        self.db.execute(
            """
            INSERT INTO member_departures (
                guild_id, user_id, display_name, left_at, last_alerted_at, in_server, updated_at
            ) VALUES (?, ?, ?, ?, NULL, 0, ?)
            """,
            (10, user_id, "Fuera", "2026-08-01T00:00:00+00:00", "2026-08-01T00:00:00+00:00"),
        )
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await admin.user_statement_interaction(interaction, str(user_id))

        content = interaction.response.messages[0]["content"]
        self.assertIn("Estado de cuenta de Fuera", content)
        self.assertIn("Presencia Discord: Fuera del servidor", content)
        self.assertIn("Disponible: 7,000 plata", content)

    def test_user_statement_outside_member_with_movements_shows_movements(self):
        user_id = 111111111111111113
        self.add_movement(user_id, code="MOV-000113", amount=900)
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))

        text = admin.user_statement_text(10, user_id=user_id)

        self.assertIn("Estado de cuenta de No disponible", text)
        self.assertIn("MOV-000113", text)
        self.assertIn("Movimiento de prueba", text)

    def test_user_statement_outside_member_without_name_does_not_break(self):
        user_id = 111111111111111114
        self.add_account(user_id, available=1)
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))

        text = admin.user_statement_text(10, user_id=user_id)

        self.assertIn("Estado de cuenta de No disponible", text)
        self.assertIn(f"Discord ID: `{user_id}`", text)

    async def test_user_statement_unknown_economic_user_returns_no_information(self):
        user_id = 111111111111111115
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await admin.user_statement_interaction(interaction, str(user_id))

        self.assertEqual(
            interaction.response.messages[0]["content"],
            "No se encontró información económica para este usuario.",
        )
        self.assertEqual(
            self.db.fetch_one("SELECT COUNT(*) AS total FROM accounts WHERE guild_id = ? AND user_id = ?", (10, user_id))["total"],
            0,
        )

    @patch("builtins.print")
    @patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True)
    async def test_outside_balances_button_defers_and_reports_empty_result(self, _is_admin, print_log):
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await press_outside_balances_button(GuildEconomyView(admin), interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(len(interaction.followup.messages), 1)
        message = interaction.followup.messages[0]
        self.assertTrue(message["ephemeral"])
        self.assertIn("Revisión completada", message["content"])
        self.assertIn("no hay usuarios fuera", message["content"])
        print_log.assert_any_call("[OUTSIDE_BALANCES] callback_start")
        print_log.assert_any_call("[OUTSIDE_BALANCES] query_start")
        print_log.assert_any_call("[OUTSIDE_BALANCES] query_ok count=0")
        print_log.assert_any_call("[OUTSIDE_BALANCES] build_start")
        print_log.assert_any_call("[OUTSIDE_BALANCES] build_ok")
        print_log.assert_any_call("[OUTSIDE_BALANCES] send_start")
        print_log.assert_any_call("[OUTSIDE_BALANCES] send_ok")

    @patch("builtins.print")
    @patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True)
    async def test_outside_balances_button_reports_unknown_departure_user(self, _is_admin, print_log):
        self.add_account(100, available=5000)
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await press_outside_balances_button(GuildEconomyView(admin), interaction)

        content = interaction.followup.messages[0]["content"]
        self.assertIn("USUARIOS FUERA CON SALDO", content)
        self.assertIn("Usuario: `100`", content)
        self.assertIn("Fecha de salida: No disponible / anterior al registro", content)
        print_log.assert_any_call("[OUTSIDE_BALANCES] query_ok count=1")

    @patch("builtins.print")
    @patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True)
    async def test_outside_balances_button_with_four_results_does_not_send_none_view(self, _is_admin, print_log):
        for user_id in (100, 101, 102, 103):
            self.add_account(user_id, available=5000)
        admin = Admin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await press_outside_balances_button(GuildEconomyView(admin), interaction)

        message = interaction.followup.messages[0]
        self.assertTrue(message["ephemeral"])
        self.assertNotIn("view", message)
        self.assertIn("Usuario: `100`", message["content"])
        self.assertIn("Usuario: `103`", message["content"])
        print_log.assert_any_call("[OUTSIDE_BALANCES] query_ok count=4")
        print_log.assert_any_call("[OUTSIDE_BALANCES] send_ok")

    @patch("g3nesys_bot.cogs.admin.traceback.print_exc")
    @patch("builtins.print")
    @patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True)
    async def test_outside_balances_button_returns_controlled_error(self, _is_admin, print_log, print_exc):
        class BrokenAdmin(Admin):
            def outside_balances_text(self, guild, *, page=0):
                raise RuntimeError("boom")

        admin = BrokenAdmin(SimpleNamespace(db=self.db, add_view=lambda _view: None))
        interaction = FakeInteraction(self.guild, FakeMember(900, "Admin"))

        await press_outside_balances_button(GuildEconomyView(admin), interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertIn("No se pudo consultar", interaction.followup.messages[0]["content"])
        print_log.assert_any_call("[OUTSIDE_BALANCES] callback_start")
        print_log.assert_any_call("[OUTSIDE_BALANCES_ERROR] RuntimeError: boom")
        print_exc.assert_called_once()

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
