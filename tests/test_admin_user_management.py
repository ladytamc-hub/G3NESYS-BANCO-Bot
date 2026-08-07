
import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.cogs.admin import (
    Admin,
    AdminPanelView,
    LegacyAdminPanelCallbacksView,
    MembersAdminView,
    UserManagementAdminView,
)
from g3nesys_bot.database import Database, SCHEMA


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.edits = []
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))
        self._done = True

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))


class FakeInteraction:
    def __init__(self, user_id=10, guild_id=1):
        self.guild = SimpleNamespace(id=guild_id)
        self.guild_id = guild_id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class AdminUserManagementTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.admin = Admin(SimpleNamespace(db=self.db))

    def tearDown(self):
        self.db.close()

    def labels(self, view):
        return [item.label for item in view.children if getattr(item, "label", None)]

    def custom_ids(self, view):
        return [item.custom_id for item in view.children if getattr(item, "custom_id", None)]

    def test_main_panel_replaces_user_buttons_with_user_management(self):
        labels = self.labels(AdminPanelView(self.admin))
        self.assertIn("Gesti\u00f3n de usuarios", labels)
        self.assertNotIn("Callers", labels)
        self.assertNotIn("Reclutadores", labels)
        self.assertNotIn("Delegados de pago", labels)
        self.assertNotIn("Admins", labels)

    def test_user_management_and_members_subpanels_show_expected_buttons(self):
        self.assertEqual(
            self.labels(UserManagementAdminView(self.admin)),
            ["Callers", "Reclutadores", "Delegados de pago", "Admins", "Miembros", "Volver"],
        )
        self.assertEqual(
            self.labels(MembersAdminView(self.admin)),
            ["Ver penalizaciones", "Eliminar penalizaci\u00f3n", "Roles de miembro", "Volver"],
        )

    def test_legacy_panel_keeps_old_custom_ids_for_published_messages(self):
        ids = set(self.custom_ids(LegacyAdminPanelCallbacksView(self.admin)))
        self.assertIn("g3n:admin:callers", ids)
        self.assertIn("g3n:admin:recruiters", ids)
        self.assertIn("g3n:admin:payment_delegates", ids)
        self.assertIn("g3n:admin:admins", ids)

    async def test_user_management_routes_existing_modules_to_admin_panel_callbacks(self):
        called = []

        async def fake_callers(self, interaction, button):
            called.append("callers")

        async def fake_recruiters(self, interaction, button):
            called.append("recruiters")

        async def fake_delegates(self, interaction, button):
            called.append("payment_delegates")

        async def fake_admins(self, interaction, button):
            called.append("admins")

        view = UserManagementAdminView(self.admin)
        patches = [
            patch.object(AdminPanelView, "callers", fake_callers),
            patch.object(AdminPanelView, "recruiters", fake_recruiters),
            patch.object(AdminPanelView, "payment_delegates", fake_delegates),
            patch.object(AdminPanelView, "admins", fake_admins),
        ]
        for manager in patches:
            manager.start()
        try:
            interaction = FakeInteraction()
            for custom_id in (
                "g3n:admin:user_management:callers",
                "g3n:admin:user_management:recruiters",
                "g3n:admin:user_management:payment_delegates",
                "g3n:admin:user_management:admins",
            ):
                button = next(item for item in view.children if item.custom_id == custom_id)
                await button.callback(interaction)
        finally:
            for manager in reversed(patches):
                manager.stop()
        self.assertEqual(called, ["callers", "recruiters", "payment_delegates", "admins"])

    async def test_members_penalty_permissions_allow_admin_or_caller_and_reject_normal_user(self):
        view = MembersAdminView(self.admin)
        interaction = FakeInteraction()
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=True), patch(
            "g3nesys_bot.cogs.admin.is_caller_panel_subject", return_value=False
        ):
            self.assertTrue(await view.require_penalty_manager(interaction))
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=False), patch(
            "g3nesys_bot.cogs.admin.is_caller_panel_subject", return_value=True
        ):
            self.assertTrue(await view.require_penalty_manager(interaction))
        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=False), patch(
            "g3nesys_bot.cogs.admin.is_caller_panel_subject", return_value=False
        ):
            denied = await view.require_penalty_manager(FakeInteraction())
        self.assertFalse(denied)

    def test_activity_penalty_text_and_specific_logical_removal(self):
        self.db.execute(
            """
            INSERT INTO penalizacion_actividades (guild_id, usuario_id, motivo, origen, fecha_ingreso, activo)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (1, 42, "3 inasistencias seguidas", "Actividades", "2026-08-02T00:00:00+00:00"),
        )
        second_id = self.db.execute(
            """
            INSERT INTO penalizacion_actividades (guild_id, usuario_id, motivo, origen, fecha_ingreso, activo)
            VALUES (?, ?, ?, ?, ?, 1)
            """,
            (1, 42, "10 inasistencias acumuladas", "Actividades", "2026-08-02T01:00:00+00:00"),
        )
        member = SimpleNamespace(id=42, mention="<@42>")
        text = self.admin.activity_penalties_text(1, member)
        self.assertIn("3 inasistencias seguidas", text)
        self.assertIn("10 inasistencias acumuladas", text)

        removed = self.admin.remove_activity_penalty(
            1,
            penalty_id=second_id,
            user_id=42,
            removed_by=7,
            observation="test",
        )
        self.assertIsNotNone(removed)
        rows = self.db.fetch_all(
            "SELECT id, activo, removido_por FROM penalizacion_actividades WHERE guild_id = ? AND usuario_id = ? ORDER BY id",
            (1, 42),
        )
        self.assertEqual([int(row["activo"]) for row in rows], [1, 0])
        self.assertEqual(int(rows[1]["removido_por"]), 7)
        active = self.admin.active_activity_penalties(1, 42)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["motivo"], "3 inasistencias seguidas")
        audit = self.db.fetch_one(
            "SELECT * FROM audit_logs WHERE guild_id = ? AND affected_user_id = ? AND action = ?",
            (1, 42, "Quitar penalizacion de actividad"),
        )
        self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()
