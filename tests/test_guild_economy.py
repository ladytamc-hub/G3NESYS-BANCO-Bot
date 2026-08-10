import csv
import os
import sqlite3
import threading
import unittest
from io import StringIO
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.cogs.admin import AdminPanelView, GuildEconomyAdminMenuView, GuildEconomyView
from g3nesys_bot.constants import (
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REASSIGNMENT,
    WITHDRAWAL_REJECTED,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.guild_economy import (
    build_guild_economy_csv_report,
    get_guild_economy_summary,
    get_guild_user_balance_report,
    guild_economy_report_tempfile,
)


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        self.deferred = ephemeral
        self._done = True


class FakeFollowup:
    def __init__(self, *, fail_send=False):
        self.messages = []
        self.fail_send = fail_send
        self.calls = 0

    async def send(self, content=None, *, ephemeral=False, **kwargs):
        self.calls += 1
        if self.fail_send and self.calls == 1:
            raise RuntimeError("send failed")
        self.messages.append((content, ephemeral, kwargs))


class FakeInteraction:
    def __init__(self, guild, user_id=100, *, fail_send=False):
        self.guild = guild
        self.guild_id = guild.id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup(fail_send=fail_send)
        self.edits = []

    async def edit_original_response(self, **kwargs):
        self.edits.append(kwargs)


class GuildEconomyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.guild = SimpleNamespace(
            id=10,
            name="G3NESYS",
            get_member=lambda user_id: SimpleNamespace(display_name=f"Member {user_id}") if user_id in {101, 102, 103, 104} else None,
        )
        self.cog = SimpleNamespace(db=self.db)

    def tearDown(self):
        self.db.close()

    def add_account(self, guild_id, user_id, *, available=0, retained=0, seized=0):
        self.db.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (?, ?, ?, ?, ?, '2026-08-04T00:00:00+00:00')
            """,
            (guild_id, user_id, available, retained, seized),
        )

    def add_movement(self, guild_id, user_id, movement_type, amount, *, code, at="2026-08-04T00:00:00+00:00", source_table=None, source_id=None):
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, ?, 'test', ?, ?, ?, ?, ?, 1, ?, 0, ?)
            """,
            (code, guild_id, movement_type, user_id, amount, source_table, source_id, f"{movement_type} {code}", at, amount),
        )

    def add_withdrawal(self, code, user_id, amount, status):
        return self.db.execute(
            """
            INSERT INTO withdrawals (
                code, guild_id, user_id, amount_requested, amount_liquidated,
                status, reason, created_at
            ) VALUES (?, 10, ?, ?, 0, ?, 'test', '2026-08-04T00:00:00+00:00')
            """,
            (code, user_id, amount, status),
        )

    def seed_economy(self):
        self.add_account(10, 101, available=500, retained=900, seized=300)
        self.add_account(10, 102, available=0, retained=700, seized=0)
        self.add_account(10, 103, available=250, retained=0, seized=0)
        self.add_account(10, 104, available=-50, retained=0, seized=0)
        self.add_account(99, 101, available=999999)
        self.add_movement(10, 101, "DEPOSITO", 1000, code="DEP-1", at="2026-08-04T01:00:00+00:00")
        self.add_movement(10, 102, "DEPOSITO", 2000, code="DEP-2", at="2026-08-04T02:00:00+00:00")
        self.add_movement(10, 103, "DEPOSITO", 3000, code="DEP-3", at="2026-08-04T03:00:00+00:00")
        self.add_movement(99, 101, "DEPOSITO", 999999, code="DEP-X")
        self.add_movement(10, 101, "LIQUIDACION", 100, code="LIQ-1", at="2026-08-04T04:00:00+00:00", source_table="withdrawals")
        self.add_movement(10, 101, "LIQUIDACION", 150, code="LIQ-2", at="2026-08-04T05:00:00+00:00", source_table="withdrawals")
        self.add_movement(10, 103, "LIQUIDACION", 50, code="LIQ-3", at="2026-08-04T06:00:00+00:00", source_table="payouts")
        self.add_movement(99, 101, "LIQUIDACION", 888888, code="LIQ-X")
        self.add_withdrawal("COBRO-P", 102, 1000, WITHDRAWAL_PENDING)
        self.add_withdrawal("COBRO-R", 103, 1000, WITHDRAWAL_REJECTED)
        self.add_withdrawal("COBRO-B", 104, 1000, WITHDRAWAL_REASSIGNMENT)

    def test_admin_panel_contains_guild_economy_button(self):
        labels = [item.label for item in AdminPanelView(self.cog).children if getattr(item, "label", None)]
        submenu_labels = [item.label for item in GuildEconomyAdminMenuView(self.cog).children if getattr(item, "label", None)]

        self.assertEqual(labels[0], "Economía Gremial")
        self.assertIn("Economía Gremial", labels)
        self.assertIn("Ver Plata Gremial", submenu_labels)

    def test_summary_totals_use_real_sources_and_guild_scope(self):
        self.seed_economy()

        summary = get_guild_economy_summary(self.db, 10, generated_at="2026-08-04 14:30 UTC")

        self.assertEqual(summary.total_deposited, 6000)
        self.assertEqual(summary.total_paid, 300)
        self.assertEqual(summary.total_pending, 750)
        self.assertEqual(summary.users_with_pending_balance, 2)
        self.assertEqual(summary.generated_at, "2026-08-04 14:30 UTC")

    def test_pending_excludes_retained_seized_zero_and_negative(self):
        self.seed_economy()
        summary, rows = get_guild_user_balance_report(self.db, 10)
        by_user = {row.user_id: row for row in rows}

        self.assertEqual(summary.total_pending, 750)
        self.assertEqual(by_user[102].status, "Sin saldo")
        self.assertEqual(by_user[104].status, "Saldo negativo")
        self.assertEqual(by_user[101].retained, 900)
        self.assertEqual(by_user[101].seized, 300)

    def test_pending_rejected_and_returned_requests_do_not_count_as_paid(self):
        self.seed_economy()

        summary = get_guild_economy_summary(self.db, 10)

        self.assertEqual(summary.total_paid, 300)

    def test_report_orders_by_available_balance_and_includes_csv_bom(self):
        self.seed_economy()
        report = build_guild_economy_csv_report(
            self.db,
            10,
            guild_name="G3NESYS",
            generated_at="2026-08-04 14:30 UTC",
            name_resolver=lambda user_id: f"Member {user_id}" if user_id in {101, 102, 103, 104} else "",
            today="2026-08-04",
        )

        self.assertTrue(report.csv_data.startswith(b"\xef\xbb\xbf"))
        text = report.csv_data.decode("utf-8-sig")
        lines = text.splitlines()
        header_index = next(index for index, line in enumerate(lines) if line.startswith("Discord ID,"))
        rows = list(csv.DictReader(StringIO("\n".join(lines[header_index:]))))
        self.assertEqual([row["Discord ID"] for row in rows[:4]], ["101", "103", "102", "104"])
        self.assertEqual(rows[0]["saldo_disponible_valor"], "500")
        self.assertEqual(rows[1]["Estado"], "Pendiente")
        self.assertEqual(rows[2]["Estado"], "Sin saldo")
        self.assertEqual(report.filename, "economia_gremial_G3NESYS_2026-08-04.csv")

    def test_user_outside_discord_stays_in_report_with_historical_or_fallback_name(self):
        self.add_account(10, 106, available=123)
        self.add_movement(10, 105, "DEPOSITO", 100, code="DEP-105")
        self.db.execute(
            """
            INSERT INTO activities (id, code, guild_id, name, caller_id, horario, status, created_at)
            VALUES (1, 'ACT-000001', 10, 'CTA', 1, '20:00', 'Finalizada', '2026-08-04T00:00:00+00:00')
            """
        )
        role_id = self.db.execute(
            "INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position) VALUES (1, 'dps', 'DPS', 1, '', 0)"
        )
        self.db.execute(
            "INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at) VALUES (1, ?, 105, 'Old User', 'x')",
            (role_id,),
        )

        _summary, rows = get_guild_user_balance_report(self.db, 10, name_resolver=lambda user_id: "")
        names = {row.user_id: row.display_name for row in rows}

        self.assertEqual(names[105], "Old User")
        self.assertEqual(names[106], "Usuario no disponible")

    def test_temp_file_is_removed_after_success_and_exception(self):
        self.seed_economy()
        report = build_guild_economy_csv_report(self.db, 10, guild_name="G3NESYS")

        with guild_economy_report_tempfile(report) as path:
            self.assertTrue(os.path.exists(path))
            saved_path = path
        self.assertFalse(os.path.exists(saved_path))

        with self.assertRaises(RuntimeError):
            with guild_economy_report_tempfile(report) as path:
                saved_error_path = path
                raise RuntimeError("boom")
        self.assertFalse(os.path.exists(saved_error_path))

    def test_service_does_not_modify_financial_records(self):
        self.seed_economy()
        before_changes = self.db._conn.total_changes
        before_accounts = [tuple(row) for row in self.db.fetch_all("SELECT * FROM accounts ORDER BY guild_id, user_id")]
        before_withdrawals = [tuple(row) for row in self.db.fetch_all("SELECT * FROM withdrawals ORDER BY id")]

        get_guild_economy_summary(self.db, 10)
        build_guild_economy_csv_report(self.db, 10, guild_name="G3NESYS")

        self.assertEqual(before_changes, self.db._conn.total_changes)
        self.assertEqual(before_accounts, [tuple(row) for row in self.db.fetch_all("SELECT * FROM accounts ORDER BY guild_id, user_id")])
        self.assertEqual(before_withdrawals, [tuple(row) for row in self.db.fetch_all("SELECT * FROM withdrawals ORDER BY id")])

    async def test_authorized_user_can_open_refresh_download_and_back(self):
        self.seed_economy()
        view = GuildEconomyView(self.cog)
        interaction = FakeInteraction(self.guild)
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True):
            await view.send_summary(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertTrue(interaction.followup.messages)
        self.assertIn("embed", interaction.followup.messages[0][2])

        refresh = next(item for item in view.children if item.label == "Actualizar")
        refresh_interaction = FakeInteraction(self.guild)
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True):
            await refresh.callback(refresh_interaction)
        self.assertEqual(len(refresh_interaction.edits), 1)

        download = next(item for item in view.children if item.label == "Descargar reporte")
        download_interaction = FakeInteraction(self.guild)
        before = self.db._conn.total_changes
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True):
            await download.callback(download_interaction)
        self.assertTrue(any(msg[2].get("file") is not None for msg in download_interaction.followup.messages))
        self.assertGreaterEqual(self.db._conn.total_changes, before)

        back = next(item for item in view.children if item.label == "Volver")
        back_interaction = FakeInteraction(self.guild)
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True):
            await back.callback(back_interaction)
        self.assertIsInstance(back_interaction.edits[0]["view"], AdminPanelView)

    async def test_unauthorized_user_cannot_open(self):
        interaction = FakeInteraction(self.guild, user_id=999)
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=False):
            await GuildEconomyView(self.cog).send_summary(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.followup.messages[0][0], "⛔ No tienes permisos para consultar Economía Gremial.")
        self.assertTrue(interaction.followup.messages[0][1])

    async def test_download_error_does_not_leave_unhandled_traceback_to_user(self):
        self.seed_economy()
        interaction = FakeInteraction(self.guild, fail_send=True)
        button = next(item for item in GuildEconomyView(self.cog).children if item.label == "Descargar reporte")

        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True), patch("g3nesys_bot.cogs.admin.traceback.print_exc"):
            await button.callback(interaction)

        self.assertTrue(any(msg[0] == "No fue posible generar el reporte de Economía Gremial." for msg in interaction.followup.messages))


if __name__ == "__main__":
    unittest.main()
