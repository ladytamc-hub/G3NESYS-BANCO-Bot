import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from g3nesys_bot.cogs.activities import (
    Activities,
    CreatePingOptionsView,
    PingsLegacyPanelCallbacksView,
    PingsPanelView,
)
from g3nesys_bot.constants import AUTOMATIC_PENALTIES_ENABLED, FINE_PENDING
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.callers import evaluate_caller_penalties
from g3nesys_bot.services.fines import create_fine


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.deferred = None
        self.modal = None
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        if self._done:
            raise AssertionError("response.defer called after response was done")
        self.deferred = ephemeral
        self._done = True

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        if self._done:
            raise AssertionError("response.send_message called after response was done")
        self.messages.append((content, ephemeral, kwargs))
        self._done = True

    async def send_modal(self, modal):
        if self._done:
            raise AssertionError("response.send_modal called after response was done")
        self.modal = modal
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))


class FakeGuild:
    def __init__(self, guild_id=10):
        self.id = guild_id
        self.name = "G3NESYS"
        self.owner_id = 999
        self.emojis = []

    def get_channel(self, channel_id: int):
        return None

    def get_member(self, user_id: int):
        return None


class FakeUser:
    def __init__(self, user_id=100, guild=None):
        self.id = user_id
        self.guild = guild
        self.mention = f"<@{user_id}>"
        self.display_name = f"User {user_id}"
        self.bot = False

    async def send(self, **kwargs):
        return None


class FakeInteraction:
    def __init__(self, guild, user_id=100):
        self.guild = guild
        self.guild_id = guild.id
        self.user = FakeUser(user_id, guild)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class PingsPanelAndPenaltyTests(unittest.IsolatedAsyncioTestCase):
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
        self.bot = SimpleNamespace(db=self.db)
        self.cog = Activities(self.bot)

    def tearDown(self):
        self.db.close()

    def labels(self, view):
        return [item.label for item in view.children if getattr(item, "label", None)]

    def rows(self, view):
        return [item.row for item in view.children if getattr(item, "label", None)]

    def custom_ids(self, view):
        return [item.custom_id for item in view.children if getattr(item, "custom_id", None)]

    def test_pings_panel_hides_cta_and_keeps_requested_order(self):
        view = PingsPanelView(self.cog)

        self.assertNotIn("Ping CTA (Sin Split)", self.labels(view))
        self.assertEqual(
            self.labels(view)[:3],
            ["Crear Ping", "Crear Plantilla", "Editar plantilla"],
        )
        self.assertEqual(
            self.labels(view)[3:6],
            ["Ver mis Plantillas", "Mis Actividades", "Mis Penalizaciones"],
        )
        self.assertEqual(
            self.labels(view)[6:],
            ["Mi Ranking", "Mi Reporte", "Config"],
        )
        self.assertEqual(self.rows(view), [0, 0, 0, 1, 1, 1, 2, 2, 2])
        styles = {item.label: item.style for item in view.children if getattr(item, "label", None)}
        self.assertEqual(styles["Crear Plantilla"].name, "primary")
        self.assertEqual(styles["Mis Penalizaciones"].name, "danger")

    def test_cta_flow_still_exists_for_legacy_persistent_messages(self):
        self.assertTrue(callable(getattr(PingsPanelView, "create_mandatory", None)))
        legacy = PingsLegacyPanelCallbacksView(self.cog)

        self.assertIn("g3n:pings:create_mandatory", self.custom_ids(legacy))
        self.assertIn("Ping CTA (Sin Split)", self.labels(legacy))

    async def test_create_ping_opens_secondary_view(self):
        view = PingsPanelView(self.cog)
        button = next(item for item in view.children if item.custom_id == "g3n:pings:create_ping")
        interaction = FakeInteraction(self.guild)

        await button.callback(interaction)

        self.assertEqual(len(interaction.response.messages), 1)
        content, ephemeral, kwargs = interaction.response.messages[0]
        self.assertTrue(ephemeral)
        self.assertIn("tipo de ping", content)
        self.assertIsInstance(kwargs["view"], CreatePingOptionsView)

    def test_secondary_view_shows_existing_ping_options(self):
        secondary = CreatePingOptionsView(self.cog)

        self.assertEqual(self.labels(secondary), ["Crear Ping Rápido", "Crear Ping (Act. Split)"])

    async def test_secondary_quick_ping_opens_modal_without_defer(self):
        self.db.execute(
            """
            INSERT INTO callers (guild_id, user_id, added_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (self.guild.id, 100, 200, "2026-08-02T00:00:00+00:00"),
        )
        secondary = CreatePingOptionsView(self.cog)
        interaction = FakeInteraction(self.guild)

        with patch("g3nesys_bot.cogs.activities.is_caller_panel_subject", return_value=True):
            await secondary.children[0].callback(interaction)

        self.assertIsNotNone(interaction.response.modal)
        self.assertIsNone(interaction.response.deferred)
        self.assertEqual(interaction.response.messages, [])
        self.assertEqual(interaction.followup.messages, [])

    async def test_secondary_split_ping_defers_and_opens_template_selector(self):
        self.db.execute(
            """
            INSERT INTO callers (guild_id, user_id, added_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (self.guild.id, 100, 200, "2026-08-02T00:00:00+00:00"),
        )
        self.db.execute(
            """
            INSERT INTO templates (
                guild_id, name, activity_name, default_time, description,
                publica, created_by, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self.guild.id, "Plantilla", "Avalon", "20:00", "Desc", 1, 100, "2026-08-02T00:00:00+00:00"),
        )
        secondary = CreatePingOptionsView(self.cog)
        interaction = FakeInteraction(self.guild)

        with patch("g3nesys_bot.cogs.activities.is_caller_panel_subject", return_value=True):
            await secondary.children[1].callback(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.messages, [])
        self.assertEqual(len(interaction.followup.messages), 1)
        content, ephemeral, kwargs = interaction.followup.messages[0]
        self.assertTrue(ephemeral)
        self.assertIn("Elige la plantilla", content)
        self.assertEqual(kwargs["view"].__class__.__name__, "TemplateSelectView")

    async def test_legacy_and_secondary_buttons_share_internal_ping_methods(self):
        with patch.object(self.cog, "open_quick_ping_from_panel", new_callable=AsyncMock) as quick,              patch.object(self.cog, "open_split_ping_from_panel", new_callable=AsyncMock) as split:
            legacy = PingsLegacyPanelCallbacksView(self.cog)
            secondary = CreatePingOptionsView(self.cog)
            interaction = FakeInteraction(self.guild)

            await next(item for item in legacy.children if item.custom_id == "g3n:pings:create_activity").callback(interaction)
            await next(item for item in legacy.children if item.custom_id == "g3n:pings:select_template").callback(interaction)
            await secondary.children[0].callback(interaction)
            await secondary.children[1].callback(interaction)

        self.assertEqual(quick.await_count, 2)
        self.assertEqual(split.await_count, 2)

    async def test_manual_fines_still_work_when_automatic_penalties_are_disabled(self):
        self.assertFalse(AUTOMATIC_PENALTIES_ENABLED)
        user = FakeUser(100, self.guild)

        code = await create_fine(
            self.db,
            guild_id=self.guild.id,
            user=user,
            amount=1500,
            reason="Penalizacion manual",
            origin="Manual",
            created_by=200,
        )

        fine = self.db.fetch_one("SELECT * FROM fines WHERE guild_id = ? AND code = ?", (self.guild.id, code))
        self.assertIsNotNone(fine)
        self.assertEqual(fine["status"], FINE_PENDING)
        self.assertEqual(fine["origin"], "Manual")

    def test_automatic_activity_penalties_do_not_create_records_or_modify_balances(self):
        self.assertFalse(AUTOMATIC_PENALTIES_ENABLED)
        self.db.execute(
            """
            INSERT INTO accounts (guild_id, user_id, available, retained, seized, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (self.guild.id, 100, 9000, 500, 0, "2026-08-02T00:00:00+00:00"),
        )
        for index in range(3):
            self.db.execute(
                """
                INSERT INTO fines (
                    code, guild_id, user_id, amount, reason, status, origin,
                    created_by, created_at
                ) VALUES (?, ?, ?, ?, ?, 'Pendiente', ?, ?, ?)
                """,
                (
                    f"MULTA-{index}",
                    self.guild.id,
                    100,
                    1000,
                    "Prueba",
                    "Manual",
                    200,
                    "2026-08-02T00:00:00+00:00",
                ),
            )

        result = self.cog.ensure_penalty_for_user(self.guild.id, 100)

        self.assertIsNone(result)
        penalties = self.db.fetch_one("SELECT COUNT(*) AS total FROM penalizacion_actividades")
        self.assertEqual(int(penalties["total"]), 0)
        account = self.db.fetch_one("SELECT * FROM accounts WHERE guild_id = ? AND user_id = ?", (self.guild.id, 100))
        self.assertEqual((account["available"], account["retained"], account["seized"]), (9000, 500, 0))

    async def test_automatic_caller_penalties_do_not_create_records(self):
        self.assertFalse(AUTOMATIC_PENALTIES_ENABLED)
        self.db.execute(
            """
            INSERT INTO callers (guild_id, user_id, added_by, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (self.guild.id, 100, 200, "2026-08-02T00:00:00+00:00"),
        )

        result = await evaluate_caller_penalties(self.db, self.guild)

        self.assertEqual(result, [])
        penalties = self.db.fetch_one("SELECT COUNT(*) AS total FROM caller_penalties")
        self.assertEqual(int(penalties["total"]), 0)


if __name__ == "__main__":
    unittest.main()
