import sqlite3
import threading
import unittest
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.constants import OFFICIAL_MEMBER_ROLE_ID
from g3nesys_bot.cogs.admin import Admin, MemberAccessRolesView, MembersAdminView
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.permissions import (
    add_member_access_role,
    configured_member_role_ids,
    has_bank_access,
    is_admin_subject,
    is_bot_member,
    is_full_member,
    remove_member_access_role,
)


INITIAL_EQUIVALENT_ROLE_ID = 1533482540223168553


class FakePermissions:
    administrator = False


class FakeRole:
    def __init__(self, role_id: int, name: str = "Rol"):
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"


class FakeGuild:
    def __init__(self, guild_id: int, roles: list[FakeRole] | None = None):
        self.id = guild_id
        self.owner_id = 999999
        self._roles = {role.id: role for role in roles or []}

    def get_role(self, role_id: int):
        return self._roles.get(role_id)


class FakeMember:
    def __init__(self, guild: FakeGuild, roles: list[FakeRole] | None = None, user_id: int = 10):
        self.id = user_id
        self.guild = guild
        self.roles = roles or []
        self.guild_permissions = FakePermissions()


class MemberAccessRoleTests(unittest.TestCase):
    def create_db(self) -> Database:
        db = Database.__new__(Database)
        db._lock = threading.RLock()
        db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        db._conn.row_factory = sqlite3.Row
        db._conn.execute("PRAGMA foreign_keys = ON")
        db._conn.executescript(SCHEMA)
        db._apply_migrations()
        db._conn.commit()
        self.addCleanup(db.close)
        return db

    def member(self, db: Database, guild_id: int = 1, role_ids: list[int] | None = None) -> FakeMember:
        roles = [FakeRole(role_id, f"Rol {role_id}") for role_id in role_ids or []]
        return FakeMember(FakeGuild(guild_id, roles), roles)

    def test_official_member_role_is_member(self):
        db = self.create_db()
        self.assertTrue(is_bot_member(db, self.member(db, role_ids=[OFFICIAL_MEMBER_ROLE_ID])))
        self.assertTrue(is_full_member(db, self.member(db, role_ids=[OFFICIAL_MEMBER_ROLE_ID])))

    def test_configured_equivalent_role_is_member(self):
        db = self.create_db()
        member = self.member(db, role_ids=[INITIAL_EQUIVALENT_ROLE_ID])
        self.assertTrue(is_bot_member(db, member))
        self.assertTrue(has_bank_access(db, member))

    def test_member_with_both_roles_is_member_once_logically(self):
        db = self.create_db()
        member = self.member(db, role_ids=[OFFICIAL_MEMBER_ROLE_ID, INITIAL_EQUIVALENT_ROLE_ID])
        self.assertTrue(is_bot_member(db, member))

    def test_user_without_member_roles_is_not_member(self):
        db = self.create_db()
        self.assertFalse(is_bot_member(db, self.member(db, role_ids=[777])))

    def test_equivalent_role_does_not_grant_admin(self):
        db = self.create_db()
        guild = FakeGuild(1, [FakeRole(INITIAL_EQUIVALENT_ROLE_ID, "ROL BASE")])
        member = FakeMember(guild, list(guild._roles.values()))
        subject = SimpleNamespace(guild=guild, user=member)
        with patch("g3nesys_bot.permissions.discord.Member", FakeMember):
            self.assertFalse(is_admin_subject(db, subject))

    def test_add_member_access_role(self):
        db = self.create_db()
        self.assertTrue(add_member_access_role(db, 1, 888))
        self.assertIn(888, configured_member_role_ids(db, 1))

    def test_add_member_access_role_avoids_duplicate(self):
        db = self.create_db()
        self.assertTrue(add_member_access_role(db, 1, 888))
        self.assertFalse(add_member_access_role(db, 1, 888))
        self.assertEqual(
            sum(1 for role_id in configured_member_role_ids(db, 1) if role_id == 888),
            1,
        )

    def test_remove_member_access_role(self):
        db = self.create_db()
        add_member_access_role(db, 1, 888)
        self.assertTrue(remove_member_access_role(db, 1, 888))
        self.assertNotIn(888, configured_member_role_ids(db, 1))

    def test_cannot_remove_official_member_role(self):
        db = self.create_db()
        with self.assertRaises(ValueError):
            remove_member_access_role(db, 1, OFFICIAL_MEMBER_ROLE_ID)

    def test_member_access_roles_persist_by_guild(self):
        db_path = Path(__file__).resolve().parent.parent / f".member_roles_{uuid.uuid4().hex}.sqlite"
        db = Database(db_path)
        db.init_schema()
        add_member_access_role(db, 1, 888)
        db.close()

        reopened = Database(db_path)
        reopened.init_schema()
        try:
            self.assertIn(888, configured_member_role_ids(reopened, 1))
        finally:
            reopened.close()
            for suffix in ("", "-wal", "-shm"):
                path = Path(f"{db_path}{suffix}")
                if path.exists():
                    path.unlink()

    def test_member_access_roles_do_not_leak_between_guilds(self):
        db = self.create_db()
        add_member_access_role(db, 1, 888)
        self.assertTrue(is_bot_member(db, self.member(db, guild_id=1, role_ids=[888])))
        self.assertFalse(is_bot_member(db, self.member(db, guild_id=2, role_ids=[888])))

    def test_admin_panel_exposes_member_roles_controls_and_state(self):
        db = self.create_db()
        admin = Admin(SimpleNamespace(db=db))
        guild = FakeGuild(
            1,
            [
                FakeRole(OFFICIAL_MEMBER_ROLE_ID, "MIEMBRO G3NESYS"),
                FakeRole(INITIAL_EQUIVALENT_ROLE_ID, "ROL BASE"),
            ],
        )
        self.assertIn("Roles de miembro", [item.label for item in MembersAdminView(admin).children])
        self.assertEqual(
            [item.label for item in MemberAccessRolesView(admin).children],
            ["Añadir rol de miembro", "Quitar rol de miembro", "Ver roles de miembro", "Volver"],
        )
        text = admin.member_access_roles_text(guild)
        self.assertIn("Rol principal:", text)
        self.assertIn(f"<@&{OFFICIAL_MEMBER_ROLE_ID}>", text)
        self.assertIn(f"<@&{INITIAL_EQUIVALENT_ROLE_ID}>", text)
