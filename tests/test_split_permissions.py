import sqlite3
import threading
import unittest
from types import SimpleNamespace

from g3nesys_bot.cogs.activities import Activities
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.permissions import is_authorized_admin


class SplitPermissionTests(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.cog = Activities(SimpleNamespace(db=self.db))

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


if __name__ == "__main__":
    unittest.main()
