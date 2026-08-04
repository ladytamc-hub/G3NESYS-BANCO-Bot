import asyncio
import csv
import sqlite3
import threading
import unittest
import zipfile
from datetime import datetime
from io import BytesIO, StringIO
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.cogs.activities import Activities, ActivityVoiceStatsDownloadView
from g3nesys_bot.constants import ACTIVITY_CANCELLED, ACTIVITY_FINISHED, ACTIVITY_IN_PROGRESS
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.activity_audit import build_activity_audit_report_files
from g3nesys_bot.services.voice_monitoring import (
    VOICE_STATUS_LATE,
    VOICE_STATUS_LEFT_EARLY,
    VOICE_STATUS_MULTIPLE,
    VOICE_STATUS_NEVER,
    VOICE_STATUS_STAYED,
    build_full_voice_stats_report_files,
    build_short_voice_stats_report_files,
    get_persisted_activity_voice_stats,
    persist_activity_voice_stats,
)


START = "2026-08-03T00:00:00+00:00"
END = "2026-08-03T00:10:00+00:00"


class VoiceChannel:
    def __init__(self, channel_id):
        self.id = channel_id


class Member:
    def __init__(self, user_id, channel_id=None):
        self.id = user_id
        self.display_name = f"Member {user_id}"
        self.voice = SimpleNamespace(channel=VoiceChannel(channel_id)) if channel_id is not None else None


class Guild:
    def __init__(self, guild_id=10, channel_by_user=None):
        self.id = guild_id
        self.owner_id = 999
        self.emojis = []
        self.channel_by_user = channel_by_user or {}

    def get_member(self, user_id):
        if user_id not in self.channel_by_user:
            return None
        return Member(user_id, self.channel_by_user[user_id])


class VoiceMonitoringTests(unittest.TestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()

    def tearDown(self):
        self.db.close()

    def create_activity(self, *, guild_id=10, status=ACTIVITY_FINISHED, started_at=START, ended_at=END, code="ACT-000100", channel_id=700, caller_id=900, name="CTA"):
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, horario, status,
                voice_channel_id, created_at, started_at, ended_at
            ) VALUES (?, ?, ?, ?, '20:00', ?, ?, ?, ?, ?)
            """,
            (code, guild_id, name, caller_id, status, channel_id, START, started_at, ended_at),
        )
        role_id = self.db.execute(
            """
            INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position)
            VALUES (?, 'dps', 'DPS', 20, '', 0)
            """,
            (activity_id,),
        )
        return activity_id, role_id

    def add_participant(self, activity_id, role_id, user_id, name=None):
        self.db.execute(
            """
            INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (activity_id, role_id, user_id, name or f"User {user_id}", START),
        )

    def add_session(self, guild_id, activity_id, user_id, joined, left=None):
        seconds = 0
        if left is not None:
            seconds = int((datetime.fromisoformat(left) - datetime.fromisoformat(joined)).total_seconds())
        self.db.execute(
            """
            INSERT INTO activity_voice_sessions (guild_id, activity_id, user_id, joined_at, left_at, seconds)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (guild_id, activity_id, user_id, joined, left, seconds),
        )

    def stat_for(self, activity_id, user_id, guild_id=10):
        persist_activity_voice_stats(self.db, guild_id, activity_id, ended_at=END)
        return {item.user_id: item for item in get_persisted_activity_voice_stats(self.db, guild_id, activity_id)}[user_id]

    def test_full_presence_late_entry_left_and_never_joined(self):
        activity_id, role_id = self.create_activity()
        for user_id in (101, 102, 103, 104):
            self.add_participant(activity_id, role_id, user_id)
        self.add_session(10, activity_id, 101, START, END)
        self.add_session(10, activity_id, 102, "2026-08-03T00:03:00+00:00", END)
        self.add_session(10, activity_id, 103, START, "2026-08-03T00:05:00+00:00")

        stayed = self.stat_for(activity_id, 101)
        late = self.stat_for(activity_id, 102)
        left = self.stat_for(activity_id, 103)
        never = self.stat_for(activity_id, 104)

        self.assertEqual(stayed.total_present_seconds, 600)
        self.assertEqual(stayed.attendance_percentage, 100)
        self.assertEqual(stayed.final_voice_status, VOICE_STATUS_STAYED)
        self.assertEqual(late.total_present_seconds, 420)
        self.assertEqual(late.final_voice_status, VOICE_STATUS_LATE)
        self.assertEqual(left.leave_count, 1)
        self.assertEqual(left.final_voice_status, VOICE_STATUS_LEFT_EARLY)
        self.assertEqual(never.total_present_seconds, 0)
        self.assertEqual(never.final_voice_status, VOICE_STATUS_NEVER)

    def test_leave_return_and_multiple_exits(self):
        activity_id, role_id = self.create_activity()
        self.add_participant(activity_id, role_id, 201)
        self.add_participant(activity_id, role_id, 202)
        self.add_session(10, activity_id, 201, START, "2026-08-03T00:02:00+00:00")
        self.add_session(10, activity_id, 201, "2026-08-03T00:04:00+00:00", END)
        self.add_session(10, activity_id, 202, START, "2026-08-03T00:01:00+00:00")
        self.add_session(10, activity_id, 202, "2026-08-03T00:02:00+00:00", "2026-08-03T00:03:00+00:00")
        self.add_session(10, activity_id, 202, "2026-08-03T00:04:00+00:00", END)

        one_return = self.stat_for(activity_id, 201)
        several = self.stat_for(activity_id, 202)

        self.assertEqual(one_return.leave_count, 1)
        self.assertEqual(one_return.rejoin_count, 1)
        self.assertEqual(one_return.final_voice_status, VOICE_STATUS_MULTIPLE)
        self.assertEqual(several.leave_count, 2)
        self.assertEqual(several.rejoin_count, 2)
        self.assertEqual(several.final_voice_status, VOICE_STATUS_MULTIPLE)

    def test_open_session_at_finish_counts_until_end_without_leave(self):
        activity_id, role_id = self.create_activity()
        self.add_participant(activity_id, role_id, 301)
        self.add_session(10, activity_id, 301, START, None)

        stat = self.stat_for(activity_id, 301)

        self.assertEqual(stat.total_present_seconds, 600)
        self.assertEqual(stat.leave_count, 0)
        self.assertEqual(stat.final_voice_status, VOICE_STATUS_STAYED)

    def test_cancelled_activity_keeps_collected_stats(self):
        activity_id, role_id = self.create_activity(status=ACTIVITY_CANCELLED)
        self.add_participant(activity_id, role_id, 401)
        self.add_session(10, activity_id, 401, START, "2026-08-03T00:04:00+00:00")

        stat = self.stat_for(activity_id, 401)

        self.assertEqual(stat.monitor_ended_at, END)
        self.assertEqual(stat.total_present_seconds, 240)
        self.assertEqual(stat.final_voice_status, VOICE_STATUS_LEFT_EARLY)

    def test_recover_keeps_active_session_open_when_member_is_still_in_channel(self):
        activity_id, role_id = self.create_activity(status=ACTIVITY_IN_PROGRESS, ended_at=None)
        self.add_participant(activity_id, role_id, 501)
        self.add_session(10, activity_id, 501, START, None)
        guild = Guild(10, {501: 700})
        bot = SimpleNamespace(db=self.db, get_guild=lambda guild_id: guild if guild_id == 10 else None, guilds=[guild])
        cog = Activities(bot)

        cog.recover_voice_tracking(guild)

        row = self.db.fetch_one("SELECT left_at FROM activity_voice_sessions WHERE activity_id = ? AND user_id = ?", (activity_id, 501))
        self.assertIsNone(row["left_at"])

    def test_recover_closes_active_session_when_member_changed_channel(self):
        activity_id, role_id = self.create_activity(status=ACTIVITY_IN_PROGRESS, ended_at=None)
        self.add_participant(activity_id, role_id, 502)
        self.add_session(10, activity_id, 502, START, None)
        guild = Guild(10, {502: 999})
        bot = SimpleNamespace(db=self.db, get_guild=lambda guild_id: guild if guild_id == 10 else None, guilds=[guild])
        cog = Activities(bot)

        cog.recover_voice_tracking(guild)

        row = self.db.fetch_one("SELECT left_at FROM activity_voice_sessions WHERE activity_id = ? AND user_id = ?", (activity_id, 502))
        self.assertIsNotNone(row["left_at"])

    def test_simultaneous_activities_and_guilds_are_separate(self):
        first_id, first_role = self.create_activity(code="ACT-000101", channel_id=700)
        second_id, second_role = self.create_activity(code="ACT-000102", channel_id=800)
        other_id, other_role = self.create_activity(guild_id=99, code="ACT-000103", channel_id=900)
        self.add_participant(first_id, first_role, 601)
        self.add_participant(second_id, second_role, 601)
        self.add_participant(other_id, other_role, 601)
        self.add_session(10, first_id, 601, START, END)
        self.add_session(10, second_id, 601, START, "2026-08-03T00:05:00+00:00")
        self.add_session(99, other_id, 601, START, "2026-08-03T00:02:00+00:00")

        first = self.stat_for(first_id, 601)
        second = self.stat_for(second_id, 601)
        other = self.stat_for(other_id, 601, guild_id=99)

        self.assertEqual(first.attendance_percentage, 100)
        self.assertEqual(second.attendance_percentage, 50)
        self.assertEqual(other.attendance_percentage, 20)

    def test_display_name_is_historical_and_report_survives_deleted_message(self):
        activity_id, role_id = self.create_activity()
        self.add_participant(activity_id, role_id, 701, name="OldNick")
        self.add_session(10, activity_id, 701, START, END)

        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END, name_resolver=lambda user_id: "NewNick")
        stat = get_persisted_activity_voice_stats(self.db, 10, activity_id)[0]
        files = build_activity_audit_report_files(self.db, 10, today="2026-08-03")

        self.assertEqual(stat.display_name, "OldNick")
        with zipfile.ZipFile(BytesIO(files[0].data)) as archive:
            stats_csv = archive.read("estadisticas_voz_actividades.csv").decode("utf-8-sig")
        self.assertIn("OldNick", stats_csv)
        self.assertIn("activity_id", stats_csv)

    def test_short_voice_stats_report_orders_percentages_and_includes_zero(self):
        activity_id, role_id = self.create_activity()
        self.add_participant(activity_id, role_id, 801, name="FullUser")
        self.add_participant(activity_id, role_id, 802, name="HalfUser")
        self.add_participant(activity_id, role_id, 803, name="NeverUser")
        self.add_session(10, activity_id, 801, START, END)
        self.add_session(10, activity_id, 802, START, "2026-08-03T00:05:00+00:00")
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END)

        files = build_short_voice_stats_report_files(self.db, 10, activity_id)
        txt = next(item for item in files if item.filename.endswith(".txt")).data.decode("utf-8")
        csv_text = next(item for item in files if item.filename.endswith(".csv")).data.decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(csv_text)))

        self.assertLess(txt.index("FullUser"), txt.index("HalfUser"))
        self.assertLess(txt.index("HalfUser"), txt.index("NeverUser"))
        self.assertIn("FullUser \u2014 100.00 %", txt)
        self.assertIn("NeverUser \u2014 0.00 %", txt)
        self.assertEqual(rows[0], {"nombre": "FullUser", "porcentaje_participacion": "100.00"})
        self.assertEqual(rows[-1], {"nombre": "NeverUser", "porcentaje_participacion": "0.00"})

    def test_full_voice_stats_report_includes_audit_fields_late_multiple_and_cancelled(self):
        activity_id, role_id = self.create_activity(status=ACTIVITY_CANCELLED)
        self.add_participant(activity_id, role_id, 901, name="LateUser")
        self.add_participant(activity_id, role_id, 902, name="MultiUser")
        self.add_session(10, activity_id, 901, "2026-08-03T00:03:00+00:00", END)
        self.add_session(10, activity_id, 902, START, "2026-08-03T00:02:00+00:00")
        self.add_session(10, activity_id, 902, "2026-08-03T00:04:00+00:00", END)
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END)

        files = build_full_voice_stats_report_files(self.db, 10, activity_id)
        csv_text = next(item for item in files if item.filename.endswith(".csv")).data.decode("utf-8-sig")
        txt = next(item for item in files if item.filename.endswith(".txt")).data.decode("utf-8")
        rows = {int(row["user_id"]): row for row in csv.DictReader(StringIO(csv_text))}

        self.assertIn("Estado de actividad", txt)
        self.assertEqual(rows[901]["entro_tarde"], "si")
        self.assertEqual(rows[901]["estado_final"], VOICE_STATUS_LATE)
        self.assertEqual(rows[902]["salidas"], "1")
        self.assertEqual(rows[902]["reingresos"], "1")
        self.assertEqual(rows[902]["estado_final"], VOICE_STATUS_MULTIPLE)

    def test_voice_stats_report_uses_historical_names_safe_filenames_and_csv_formula_guard(self):
        activity_id, role_id = self.create_activity(name="\U0001F525 CTA / Raid, \"Test\" =SUM")
        self.add_participant(activity_id, role_id, 1001, name="=SUM(A1:A2)")
        self.add_session(10, activity_id, 1001, START, END)
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END, name_resolver=lambda user_id: "ChangedNick")

        files = build_short_voice_stats_report_files(self.db, 10, activity_id)
        csv_text = next(item for item in files if item.filename.endswith(".csv")).data.decode("utf-8-sig")
        rows = list(csv.DictReader(StringIO(csv_text)))

        for item in files:
            self.assertNotIn("/", item.filename)
            self.assertNotIn(" ", item.filename)
            self.assertTrue(item.filename.startswith("stats_cortas_CTA_Raid_Test_SUM_2026-08-03_"))
        self.assertEqual(rows[0]["nombre"], "'=SUM(A1:A2)")

    def test_voice_stats_download_view_sends_short_files_ephemerally(self):
        activity_id, role_id = self.create_activity(caller_id=900)
        self.add_participant(activity_id, role_id, 1101, name="CallerStat")
        self.add_session(10, activity_id, 1101, START, END)
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END)
        cog = Activities(SimpleNamespace(db=self.db, get_guild=lambda guild_id: None, guilds=[]))
        interaction = self.fake_interaction(user_id=900)
        view = ActivityVoiceStatsDownloadView(cog, activity_id)
        button = next(child for child in view.children if child.custom_id == "g3n:activity:voice_stats_download_short")

        asyncio.run(button.callback(interaction))

        self.assertTrue(interaction.response.deferred_ephemeral)
        self.assertEqual(len(interaction.followup.messages), 1)
        sent = interaction.followup.messages[0]
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(len(sent["files"]), 2)
        self.assertTrue(sent["files"][0].filename.startswith("stats_cortas_"))

    def test_voice_stats_download_view_blocks_full_report_without_admin_permission(self):
        activity_id, role_id = self.create_activity(caller_id=900)
        self.add_participant(activity_id, role_id, 1201, name="DeniedUser")
        self.add_session(10, activity_id, 1201, START, END)
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END)
        cog = Activities(SimpleNamespace(db=self.db, get_guild=lambda guild_id: None, guilds=[]))
        interaction = self.fake_interaction(user_id=900)
        view = ActivityVoiceStatsDownloadView(cog, activity_id)
        button = next(child for child in view.children if child.custom_id == "g3n:activity:voice_stats_download_full")

        with patch("g3nesys_bot.cogs.activities.is_admin_subject", return_value=False):
            asyncio.run(button.callback(interaction))

        self.assertTrue(interaction.response.deferred_ephemeral)
        self.assertEqual(len(interaction.followup.messages), 1)
        sent = interaction.followup.messages[0]
        self.assertTrue(sent["ephemeral"])
        self.assertIn("No tienes permiso", sent["content"])
        self.assertNotIn("files", sent)

    def test_voice_stats_download_view_sends_full_report_for_admin(self):
        activity_id, role_id = self.create_activity(caller_id=900)
        self.add_participant(activity_id, role_id, 1301, name="AdminFile")
        self.add_session(10, activity_id, 1301, START, END)
        persist_activity_voice_stats(self.db, 10, activity_id, ended_at=END)
        cog = Activities(SimpleNamespace(db=self.db, get_guild=lambda guild_id: None, guilds=[]))
        interaction = self.fake_interaction(user_id=777)
        view = ActivityVoiceStatsDownloadView(cog, activity_id)
        button = next(child for child in view.children if child.custom_id == "g3n:activity:voice_stats_download_full")

        with patch("g3nesys_bot.cogs.activities.is_admin_subject", return_value=True):
            asyncio.run(button.callback(interaction))

        self.assertTrue(interaction.response.deferred_ephemeral)
        self.assertEqual(len(interaction.followup.messages), 1)
        sent = interaction.followup.messages[0]
        self.assertTrue(sent["ephemeral"])
        self.assertEqual(len(sent["files"]), 2)
        self.assertTrue(sent["files"][0].filename.startswith("stats_completas_"))

    def fake_interaction(self, *, user_id=900, guild_id=10):
        class FakeResponse:
            def __init__(self):
                self.deferred_ephemeral = None

            async def defer(self, *, ephemeral=False):
                self.deferred_ephemeral = ephemeral

        class FakeFollowup:
            def __init__(self):
                self.messages = []

            async def send(self, content=None, **kwargs):
                payload = {"content": content, **kwargs}
                self.messages.append(payload)

        return SimpleNamespace(
            guild=SimpleNamespace(id=guild_id),
            guild_id=guild_id,
            user=SimpleNamespace(id=user_id),
            response=FakeResponse(),
            followup=FakeFollowup(),
        )
