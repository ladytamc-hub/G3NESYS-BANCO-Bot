import sqlite3
import threading
import unittest
from types import MethodType, SimpleNamespace

from g3nesys_bot.constants import ACTIVITY_PAYOUT_CREATED, ATTENDANCE_CONFIRMED, PAYOUT_PENDING
from g3nesys_bot.cogs.activities import Activities, ActivityView, PayoutEditView
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.permissions import is_authorized_admin


class FakeResponse:
    def __init__(self):
        self.deferred = False
        self.send_message_calls = []
        self.modal = None
        self._done = False

    def is_done(self):
        return self._done

    async def defer(self, *, ephemeral=False):
        self.deferred = ephemeral
        self._done = True

    async def send_message(self, content, *, ephemeral=False, **kwargs):
        if self._done:
            raise AssertionError("response.send_message called after defer")
        self.send_message_calls.append((content, ephemeral, kwargs))
        self._done = True

    async def send_modal(self, modal):
        if self._done:
            raise AssertionError("response.send_modal called after defer")
        self.modal = modal
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))


class FakeInteraction:
    def __init__(self, guild, user_id):
        self.guild = guild
        self.guild_id = guild.id
        async def send(**kwargs):
            return None
        self.user = SimpleNamespace(id=user_id, send=send)
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.message = SimpleNamespace()



class SplitPermissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.guild = SimpleNamespace(id=10, owner_id=999, emojis=[])
        self.bot = SimpleNamespace(db=self.db, get_guild=lambda guild_id: self.guild if guild_id == 10 else None)
        self.cog = Activities(self.bot)

    def tearDown(self):
        self.db.close()

    @staticmethod
    def ctx(guild_id: int, user_id: int, owner_id: int = 999):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id, owner_id=owner_id),
            author=SimpleNamespace(id=user_id),
        )

    @staticmethod
    def interaction(guild_id: int, user_id: int, owner_id: int = 999):
        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id, owner_id=owner_id),
            user=SimpleNamespace(id=user_id),
        )

    def test_caller_can_manage_own_split(self):
        payout = {"guild_id": 10, "caller_id": 100}
        self.assertTrue(self.cog.can_manage_payout(self.ctx(10, 100), payout))
        self.assertTrue(self.cog.can_manage_payout_interaction(self.interaction(10, 100), payout))

    def test_guild_owner_can_manage_foreign_split(self):
        payout = {"guild_id": 10, "caller_id": 100}
        self.assertTrue(self.cog.can_manage_payout(self.ctx(10, 400, owner_id=400), payout))
        self.assertTrue(self.cog.can_manage_payout_interaction(self.interaction(10, 400, owner_id=400), payout))

    def test_authorized_admin_can_manage_foreign_split_in_same_guild(self):
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (10, 200, 1, "2026-08-01T00:00:00+00:00"),
        )
        payout = {"guild_id": 10, "caller_id": 100}
        self.assertTrue(is_authorized_admin(self.db, 10, 200))
        self.assertTrue(self.cog.can_manage_payout(self.ctx(10, 200), payout))
        self.assertTrue(self.cog.can_manage_payout_interaction(self.interaction(10, 200), payout))

    def test_normal_user_and_admin_from_other_guild_cannot_manage_split(self):
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (99, 200, 1, "2026-08-01T00:00:00+00:00"),
        )
        payout = {"guild_id": 10, "caller_id": 100}
        self.assertFalse(self.cog.can_manage_payout(self.ctx(10, 200), payout))
        self.assertFalse(self.cog.can_manage_payout(self.ctx(10, 300), payout))

    def test_duplicate_authorized_admin_rows_are_deduplicated_and_blocked(self):
        with self.db._lock:
            self.db._conn.execute(
                """
                CREATE TABLE authorized_admins (
                    guild_id INTEGER NOT NULL,
                    user_id INTEGER NOT NULL,
                    added_by INTEGER,
                    added_at TEXT
                )
                """
            )
            self.db._conn.execute(
                "INSERT INTO authorized_admins (guild_id, user_id, added_by, added_at) VALUES (10, 200, 1, 'a')"
            )
            self.db._conn.execute(
                "INSERT INTO authorized_admins (guild_id, user_id, added_by, added_at) VALUES (10, 200, 2, 'b')"
            )
            self.db._ensure_authorized_admin_uniqueness()
            rows = self.db._conn.execute(
                "SELECT guild_id, user_id FROM authorized_admins WHERE guild_id = 10 AND user_id = 200"
            ).fetchall()
            self.assertEqual(len(rows), 1)
            with self.assertRaises(sqlite3.IntegrityError):
                self.db._conn.execute(
                    "INSERT INTO authorized_admins (guild_id, user_id, added_by, added_at) VALUES (10, 200, 3, 'c')"
                )

    def create_activity(self, *, status=ACTIVITY_PAYOUT_CREATED, caller_id=100):
        return self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, horario, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            ("CTA-1", 10, "CTA", caller_id, "20:00", status, "2026-08-01T00:00:00+00:00"),
        )

    def create_payout(self, *, activity_id=None, caller_id=100):
        return self.db.execute(
            """
            INSERT INTO payouts (
                code, guild_id, activity_id, caller_id, status, gross_loot,
                market_rate_percent, repairs, other_expenses, guild_percent,
                guild_amount, distributable, caller_percent, caller_amount, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "SPLIT-1", 10, activity_id, caller_id, PAYOUT_PENDING, 1000,
                0, 0, 0, 0, 0, 1000, 0, 0, "2026-08-01T00:00:00+00:00",
            ),
        )

    def test_ping_with_previous_split_shows_resplit_button(self):
        activity_id = self.create_activity(status=ACTIVITY_PAYOUT_CREATED)
        view = ActivityView(self.cog, activity_id)
        labels = [item.label for item in view.children if getattr(item, "label", None)]
        self.assertIn("Volver a splitear", labels)

    async def test_authorized_admin_sends_split_to_review_with_defer_and_followup(self):
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (10, 200, 1, "2026-08-01T00:00:00+00:00"),
        )
        self.create_payout(caller_id=100)

        async def fake_send_to_admins(_self, guild, payout_id):
            return True

        self.cog.send_payout_to_admins = MethodType(fake_send_to_admins, self.cog)
        interaction = FakeInteraction(self.guild, 200)
        await self.cog.send_payout_to_review_interaction(interaction, 10, "SPLIT-1")

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.send_message_calls, [])
        self.assertTrue(any("enviado a revision" in msg[0] for msg in interaction.followup.messages))
        payout = self.db.fetch_one("SELECT sent_to_admin_at FROM payouts WHERE guild_id = ? AND code = ?", (10, "SPLIT-1"))
        self.assertIsNotNone(payout["sent_to_admin_at"])

    async def test_unauthorized_split_review_rejects_with_defer_and_followup(self):
        self.create_payout(caller_id=100)
        interaction = FakeInteraction(self.guild, 300)
        await self.cog.send_payout_to_review_interaction(interaction, 10, "SPLIT-1")

        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.send_message_calls, [])
        self.assertTrue(any("Solo el caller del Split" in msg[0] for msg in interaction.followup.messages))

    async def test_resplit_creates_new_split_without_modifying_previous_one(self):
        activity_id = self.create_activity(status=ACTIVITY_PAYOUT_CREATED, caller_id=100)
        role_id = self.db.execute(
            """
            INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (activity_id, "dps", "DPS", 10, "", 0),
        )
        self.db.execute(
            """
            INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (activity_id, role_id, 501, "User", "2026-08-01T00:00:00+00:00"),
        )
        self.db.execute(
            """
            INSERT INTO asistencia_actividades (
                actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz,
                voice_seconds, participation_percent
            ) VALUES (?, ?, ?, 1, 1, 600, 100)
            """,
            (activity_id, 501, ATTENDANCE_CONFIRMED),
        )
        old_id = self.create_payout(activity_id=activity_id, caller_id=100)
        old_before = self.db.fetch_one("SELECT * FROM payouts WHERE id = ?", (old_id,))

        async def no_update(activity_id):
            return None

        self.cog.update_activity_message = no_update
        modal = SimpleNamespace(
            gross_loot=SimpleNamespace(value="100000"),
            market_rate=SimpleNamespace(value="0"),
            costs=SimpleNamespace(value="0 | 0"),
            guild_percent=SimpleNamespace(value="0"),
            caller_percent=SimpleNamespace(value="0"),
        )
        interaction = FakeInteraction(self.guild, 100)
        await self.cog.create_payout_from_modal(interaction, activity_id, modal)

        payouts = self.db.fetch_all(
            "SELECT * FROM payouts WHERE guild_id = ? AND activity_id = ? ORDER BY id ASC",
            (10, activity_id),
        )
        self.assertEqual(len(payouts), 2)
        self.assertEqual(int(payouts[0]["id"]), old_id)
        self.assertEqual(payouts[0]["code"], old_before["code"])
        self.assertEqual(int(payouts[0]["gross_loot"]), int(old_before["gross_loot"]))
        self.assertNotEqual(int(payouts[1]["id"]), old_id)
        self.assertTrue(interaction.response.deferred)
        self.assertEqual(interaction.response.send_message_calls, [])

    def test_payout_edit_view_uses_valid_unicode_emojis(self):
        view = PayoutEditView(self.cog, 10, "SPLIT-1")
        emojis = {item.label: str(item.emoji) for item in view.children if getattr(item, "label", None)}
        self.assertEqual(emojis["Ver lista"], "\U0001f4cb")
        self.assertEqual(emojis["Editar %"], "\u270f\ufe0f")
        self.assertEqual(emojis["Corregir Split"], "\U0001f6e0\ufe0f")
        self.assertEqual(emojis["A\u00f1adir Usuario"], "\u2795")
        self.assertEqual(emojis["Eliminar Usuario"], "\u2796")
        self.assertEqual(emojis["Enviar a revision"], "\U0001f4e4")

    async def test_caller_edit_percent_responds_with_modal(self):
        self.create_payout(caller_id=100)
        view = PayoutEditView(self.cog, 10, "SPLIT-1")
        button = next(item for item in view.children if getattr(item, "custom_id", None) == "g3n:payout:edit_percent")
        interaction = FakeInteraction(self.guild, 100)
        await button.callback(interaction)

        self.assertIsNotNone(interaction.response.modal)
        self.assertFalse(interaction.response.deferred)
        self.assertEqual(interaction.response.send_message_calls, [])
        self.assertEqual(interaction.followup.messages, [])

    async def test_authorized_admin_edit_percent_foreign_split_responds_with_modal(self):
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (10, 200, 1, "2026-08-01T00:00:00+00:00"),
        )
        self.create_payout(caller_id=100)
        view = PayoutEditView(self.cog, 10, "SPLIT-1")
        button = next(item for item in view.children if getattr(item, "custom_id", None) == "g3n:payout:edit_percent")
        interaction = FakeInteraction(self.guild, 200)
        await button.callback(interaction)

        self.assertIsNotNone(interaction.response.modal)
        self.assertFalse(interaction.response.deferred)
        self.assertEqual(interaction.response.send_message_calls, [])
        self.assertEqual(interaction.followup.messages, [])


if __name__ == "__main__":
    unittest.main()
