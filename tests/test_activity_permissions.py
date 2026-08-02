import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.constants import ACTIVITY_OPEN
from g3nesys_bot.cogs.activities import Activities
from g3nesys_bot.database import Database, SCHEMA


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
    def __init__(self, guild, user_id):
        self.guild = guild
        self.guild_id = guild.id
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class ActivityPermissionTests(unittest.IsolatedAsyncioTestCase):
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
        self.bot = SimpleNamespace(db=self.db)
        self.cog = Activities(self.bot)

    def tearDown(self):
        self.db.close()

    def create_activity(self, *, creator_id=100, caller_id=200, guild_id=10):
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, pinged_by_id, horario, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"ACT-{creator_id}-{caller_id}",
                guild_id,
                "CTA",
                caller_id,
                creator_id,
                "20:00",
                ACTIVITY_OPEN,
                "2026-08-02T00:00:00+00:00",
            ),
        )
        return self.db.fetch_one("SELECT * FROM activities WHERE id = ?", (activity_id,))

    def create_historical_activity_without_creator(self, *, caller_id=200, guild_id=10):
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, horario, status, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "ACT-HIST",
                guild_id,
                "CTA vieja",
                caller_id,
                "20:00",
                ACTIVITY_OPEN,
                "2026-08-02T00:00:00+00:00",
            ),
        )
        return self.db.fetch_one("SELECT * FROM activities WHERE id = ?", (activity_id,))

    def test_real_creator_can_operate_and_delete_when_not_assigned_caller(self):
        activity = self.create_activity(creator_id=100, caller_id=200)
        interaction = FakeInteraction(self.guild, 100)

        self.assertTrue(self.cog.can_manage_activity_operational(interaction, activity))
        self.assertTrue(self.cog.can_delete_activity_ping(interaction, activity))

    def test_assigned_caller_can_operate_but_cannot_delete(self):
        activity = self.create_activity(creator_id=100, caller_id=200)
        interaction = FakeInteraction(self.guild, 200)

        self.assertTrue(self.cog.can_manage_activity_operational(interaction, activity))
        self.assertFalse(self.cog.can_delete_activity_ping(interaction, activity))
        self.assertEqual(
            self.cog.activity_permission_denial_reason(interaction, activity, delete=True),
            "caller asignado sin permiso para eliminar",
        )

    def test_unassigned_caller_and_common_user_cannot_operate(self):
        activity = self.create_activity(creator_id=100, caller_id=200)

        other_caller = FakeInteraction(self.guild, 300)
        common_user = FakeInteraction(self.guild, 400)

        self.assertFalse(self.cog.can_manage_activity_operational(other_caller, activity))
        self.assertFalse(self.cog.can_delete_activity_ping(other_caller, activity))
        self.assertFalse(self.cog.can_manage_activity_operational(common_user, activity))
        self.assertFalse(self.cog.can_delete_activity_ping(common_user, activity))

    def test_authorized_admin_can_operate_and_delete(self):
        activity = self.create_activity(creator_id=100, caller_id=200)
        interaction = FakeInteraction(self.guild, 500)

        with patch("g3nesys_bot.cogs.activities.is_admin_subject", return_value=True):
            self.assertTrue(self.cog.can_manage_activity_operational(interaction, activity))
            self.assertTrue(self.cog.can_delete_activity_ping(interaction, activity))

    def test_database_authorized_admin_can_operate_and_delete(self):
        self.db.execute(
            """
            INSERT INTO admin_access (guild_id, user_id, authorized, updated_by, updated_at)
            VALUES (?, ?, 1, ?, ?)
            """,
            (10, 500, 1, "2026-08-02T00:00:00+00:00"),
        )
        activity = self.create_activity(creator_id=100, caller_id=200)
        interaction = FakeInteraction(self.guild, 500)

        self.assertTrue(self.cog.can_manage_activity_operational(interaction, activity))
        self.assertTrue(self.cog.can_delete_activity_ping(interaction, activity))

    async def test_blocked_attempt_logs_precise_reason(self):
        activity = self.create_activity(creator_id=100, caller_id=200)
        interaction = FakeInteraction(self.guild, 300)

        allowed = await self.cog.require_activity_manager(interaction, activity, "verify")

        self.assertFalse(allowed)
        self.assertEqual(len(interaction.response.messages), 1)
        audit = self.db.fetch_one(
            "SELECT observation FROM audit_logs WHERE guild_id = ? ORDER BY id DESC LIMIT 1",
            (10,),
        )
        self.assertIsNotNone(audit)
        self.assertIn("usuario no es creador ni caller asignado", audit["observation"])
        self.assertNotIn("caller no creador", audit["observation"])

    def test_historical_activity_without_creator_falls_back_to_assigned_caller(self):
        activity = self.create_historical_activity_without_creator(caller_id=200)
        interaction = FakeInteraction(self.guild, 200)

        self.assertTrue(self.cog.can_manage_activity_operational(interaction, activity))
        self.assertTrue(self.cog.can_delete_activity_ping(interaction, activity))

    def test_admin_from_other_guild_is_not_authorized_by_activity_helpers(self):
        activity = self.create_activity(creator_id=100, caller_id=200, guild_id=10)
        other_guild = SimpleNamespace(id=99, owner_id=999, emojis=[])
        interaction = FakeInteraction(other_guild, 100)

        with patch("g3nesys_bot.cogs.activities.is_admin_subject", return_value=True):
            self.assertFalse(self.cog.can_manage_activity_operational(interaction, activity))
            self.assertFalse(self.cog.can_delete_activity_ping(interaction, activity))


if __name__ == "__main__":
    unittest.main()