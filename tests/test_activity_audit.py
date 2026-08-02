import sqlite3
import threading
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace

from g3nesys_bot.cogs.admin import (
    ActivityAuditDetailsView,
    ActivityAuditHomeView,
    ActivityAuditRecordView,
    AdminPanelView,
    build_activity_audit_details_embed,
    activity_audit_channel_text,
    activity_audit_ping_url,
    activity_audit_thread_url,
    activity_audit_user_label,
)
from g3nesys_bot.constants import (
    ACTIVITY_CANCELLED,
    ACTIVITY_FINISHED,
    ACTIVITY_OPEN,
    ACTIVITY_TYPE_MANDATORY,
    PAYOUT_DEPOSITED,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.activity_audit import (
    AUDIT_CANCELLED,
    AUDIT_NO_SPLIT,
    AUDIT_PENDING,
    AUDIT_SPLIT,
    build_activity_audit_report_files,
    get_activity_audit_dataset,
    movement_mentions_activity_code,
    normalize_activity_code,
)


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


class ActivityAuditTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.db = Database.__new__(Database)
        self.db._lock = threading.RLock()
        self.db._conn = sqlite3.connect(":memory:", check_same_thread=False)
        self.db._conn.row_factory = sqlite3.Row
        self.db._conn.execute("PRAGMA foreign_keys = ON")
        self.db._conn.executescript(SCHEMA)
        self.db._apply_migrations()
        self.db._conn.commit()
        self.guild = SimpleNamespace(id=10, owner_id=999, emojis=[], get_member=lambda user_id: None)
        self.cog = SimpleNamespace(db=self.db)

    def tearDown(self):
        self.db.close()

    def create_activity(
        self,
        code,
        *,
        pinged_by_id=100,
        caller_id=100,
        status=ACTIVITY_FINISHED,
        activity_type="regular",
        created_at="2026-08-01T00:00:00+00:00",
        channel_id=None,
        message_id=None,
        thread_id=None,
        thread_panel_message_id=None,
    ):
        return self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, pinged_by_id, horario,
                status, created_at, activity_type, channel_id, message_id,
                thread_id, thread_panel_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code, 10, f"Actividad {code}", caller_id, pinged_by_id, "20:00",
                status, created_at, activity_type, channel_id, message_id,
                thread_id, thread_panel_message_id,
            ),
        )

    def create_payout_with_deposit(self, activity_id, *, amount=1000, user_id=300, caller_id=200):
        payout_id = self.db.execute(
            """
            INSERT INTO payouts (
                code, guild_id, activity_id, caller_id, status, gross_loot,
                market_rate_percent, repairs, other_expenses, guild_percent,
                guild_amount, distributable, caller_percent, caller_amount, created_at, reviewed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"SPLIT-{activity_id}", 10, activity_id, caller_id, PAYOUT_DEPOSITED,
                amount, 0, 0, 0, 0, 0, amount, 0, 0,
                "2026-08-01T01:00:00+00:00", "2026-08-01T02:00:00+00:00",
            ),
        )
        self.db.execute(
            """
            INSERT INTO payout_participants (payout_id, user_id, participation_percent, amount, deposited_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (payout_id, user_id, 100, amount, "2026-08-01T02:00:00+00:00"),
        )
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, 'DEPOSITO', 'Split de actividad', ?, ?, 'payouts', ?, ?, ?, ?, 0, ?)
            """,
            (
                f"MOV-{activity_id}", 10, user_id, amount, payout_id,
                f"Deposito por Split SPLIT-{activity_id}", 1, "2026-08-01T02:00:00+00:00", amount,
            ),
        )
        return payout_id

    def create_fallback_deposit(self, code, *, amount=500, movement_code="MOV-FB", user_id=301):
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, 'DEPOSITO', 'Deposito administrativo', ?, ?, NULL, NULL, ?, ?, ?, 0, ?)
            """,
            (movement_code, 10, user_id, amount, f"Pago {code}", 1, "2026-08-01T03:00:00+00:00", amount),
        )

    def seed_dataset(self):
        self.create_activity("ACT-000049", pinged_by_id=49)
        pending_id = self.create_activity("ACT-000050", pinged_by_id=100, caller_id=100, channel_id=5000, message_id=6000, thread_id=7000, thread_panel_message_id=8000)
        split_id = self.create_activity("ACT-000060", pinged_by_id=101, caller_id=202, channel_id=5001, message_id=6001)
        self.create_payout_with_deposit(split_id, amount=1000, user_id=300, caller_id=202)
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, 'DEPOSITO', 'Deposito administrativo', ?, ?, NULL, NULL, ?, ?, ?, 0, ?)
            """,
            ("MOV-NOMATCH", 10, 301, 9999, "Pago ACT-0000600", 1, "2026-08-01T03:00:00+00:00", 9999),
        )
        self.create_activity("ACT-000061", pinged_by_id=102, activity_type=ACTIVITY_TYPE_MANDATORY)
        self.create_activity("ACT-000062", pinged_by_id=103, status=ACTIVITY_CANCELLED)
        fallback_id = self.create_activity("ACT-000063", pinged_by_id=104, caller_id=205, channel_id=9999)
        self.create_fallback_deposit("ACT-000063", amount=700, movement_code="MOV-FB63", user_id=302)
        outside_id = self.create_activity("ACT-000064", pinged_by_id=999, caller_id=205, status=ACTIVITY_OPEN)
        return pending_id, split_id, fallback_id, outside_id

    def test_dataset_includes_from_act_000050_and_keeps_pinged_by(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        codes = [record.code for record in dataset.records]

        self.assertNotIn("ACT-000049", codes)
        self.assertIn("ACT-000050", codes)
        pending = dataset.get_record("60")
        self.assertEqual(pending.code, "ACT-000060")
        self.assertEqual(pending.pinged_by_id, 101)
        self.assertEqual(pending.caller_id, 202)

    def test_filters_classify_split_pending_cancelled_and_no_split(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)

        self.assertEqual(dataset.get_record("ACT-000050").audit_status, AUDIT_PENDING)
        self.assertEqual(dataset.get_record("ACT-000060").audit_status, AUDIT_SPLIT)
        self.assertEqual(dataset.get_record("ACT-000061").audit_status, AUDIT_NO_SPLIT)
        self.assertEqual(dataset.get_record("ACT-000062").audit_status, AUDIT_CANCELLED)
        self.assertEqual(dataset.get_record("ACT-000063").audit_status, AUDIT_SPLIT)
        self.assertNotIn("ACT-000061", [row.code for row in dataset.filter_records("pending")])
        self.assertNotIn("ACT-000062", [row.code for row in dataset.filter_records("pending")])

    def test_split_details_total_and_no_false_positive_with_longer_code(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000060")
        movements = dataset.movements_for("ACT-000060")

        self.assertEqual(record.total_deposited, 1000)
        self.assertEqual(record.beneficiaries, 1)
        self.assertEqual(len(movements), 1)
        self.assertEqual(sum(item.amount for item in movements), 1000)
        self.assertFalse(movement_mentions_activity_code("Pago ACT-0000600", "ACT-000060"))

    def test_search_normalization(self):
        self.assertEqual(normalize_activity_code("ACT-000060"), "ACT-000060")
        self.assertEqual(normalize_activity_code("000060"), "ACT-000060")
        self.assertEqual(normalize_activity_code("60"), "ACT-000060")
        self.assertEqual(normalize_activity_code("MAND-000013"), "MAND-000013")
        self.assertEqual(normalize_activity_code("mand-13"), "MAND-000013")

    def test_act_sequence_ignores_historical_mand_counter(self):
        self.db.execute(
            "INSERT INTO id_counters (guild_id, prefix, last_value) VALUES (?, ?, ?)",
            (10, "ACT", 144),
        )
        self.db.execute(
            "INSERT INTO id_counters (guild_id, prefix, last_value) VALUES (?, ?, ?)",
            (10, "MAND", 13),
        )

        self.assertEqual(self.db.next_code(10, "ACT"), "ACT-000145")
        self.assertEqual(self.db.next_code(10, "ACT"), "ACT-000146")
        mand_counter = self.db.fetch_one(
            "SELECT last_value FROM id_counters WHERE guild_id = ? AND prefix = ?",
            (10, "MAND"),
        )
        self.assertEqual(int(mand_counter["last_value"]), 13)

    def test_audit_keeps_historical_mand_codes_searchable_and_reported(self):
        self.create_activity(
            "MAND-000013",
            pinged_by_id=106,
            activity_type=ACTIVITY_TYPE_MANDATORY,
        )
        self.create_fallback_deposit(
            "MAND-000013",
            amount=400,
            movement_code="MOV-MAND13",
            user_id=303,
        )

        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("MAND-000013")
        movements = dataset.movements_for("MAND-000013")

        self.assertIsNotNone(record)
        self.assertEqual(record.code, "MAND-000013")
        self.assertEqual(record.activity_type, ACTIVITY_TYPE_MANDATORY)
        self.assertEqual(record.audit_status, AUDIT_NO_SPLIT)
        self.assertEqual(len(movements), 1)
        self.assertTrue(movement_mentions_activity_code("Pago MAND-000013", "MAND-000013"))
        self.assertFalse(movement_mentions_activity_code("Pago MAND-0000130", "MAND-000013"))

        files = build_activity_audit_report_files(self.db, 10, today="2026-08-02")
        with zipfile.ZipFile(BytesIO(files[0].data)) as archive:
            activities_csv = archive.read("actividades_desde_ACT-000050.csv").decode("utf-8-sig")
        self.assertIn("MAND-000013", activities_csv)

    def test_creator_outside_server_label_keeps_id(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000064")

        self.assertEqual(record.pinged_by_id, 999)
        self.assertIn("Usuario fuera del servidor", activity_audit_user_label(self.guild, record.pinged_by_id))
        self.assertIn("999", activity_audit_user_label(self.guild, record.pinged_by_id))


    def test_activity_navigation_links_use_saved_ids(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000050")

        self.assertEqual(
            activity_audit_ping_url(record),
            "https://discord.com/channels/10/5000/6000",
        )
        self.assertEqual(
            activity_audit_thread_url(record),
            "https://discord.com/channels/10/7000/8000",
        )
        view = ActivityAuditRecordView(self.cog, record.code, has_details=record.has_split_details, record=record)
        labels = [item.label for item in view.children if getattr(item, "label", None)]
        urls = [getattr(item, "url", None) for item in view.children]

        self.assertIn("Abrir ping", labels)
        self.assertIn("Abrir hilo", labels)
        self.assertIn("https://discord.com/channels/10/5000/6000", urls)
        self.assertIn("https://discord.com/channels/10/7000/8000", urls)

    def test_activity_without_thread_has_no_thread_button(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000060")
        view = ActivityAuditRecordView(self.cog, record.code, has_details=record.has_split_details, record=record)
        labels = [item.label for item in view.children if getattr(item, "label", None)]

        self.assertIn("Abrir ping", labels)
        self.assertNotIn("Abrir hilo", labels)

    def test_historical_activity_without_message_has_no_ping_button(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000063")
        view = ActivityAuditRecordView(self.cog, record.code, has_details=record.has_split_details, record=record)
        labels = [item.label for item in view.children if getattr(item, "label", None)]

        self.assertIsNone(activity_audit_ping_url(record))
        self.assertNotIn("Abrir ping", labels)
        self.assertIn("Canal no disponible", activity_audit_channel_text(self.guild, record.channel_id))
        self.assertIn("9999", activity_audit_channel_text(self.guild, record.channel_id))

    def test_details_view_keeps_pagination_and_adds_links_without_changing_totals(self):
        self.seed_dataset()
        dataset = get_activity_audit_dataset(self.db, 10)
        record = dataset.get_record("ACT-000060")
        before_total = record.total_deposited
        view = ActivityAuditDetailsView(self.cog, record.code, 0, back_mode="split", back_page=0, record=record)
        labels = [item.label for item in view.children if getattr(item, "label", None)]
        after = get_activity_audit_dataset(self.db, 10).get_record("ACT-000060")
        embed, _ = build_activity_audit_details_embed(self.cog, self.guild, record.code, 0)
        fields = {field.name: field.value for field in embed.fields}

        self.assertIn("Anterior", labels)
        self.assertIn("Siguiente", labels)
        self.assertIn("Abrir ping", labels)
        self.assertIn("Publicaci\u00f3n", fields)
        self.assertIn("Publicado en", fields["Publicaci\u00f3n"])
        self.assertEqual(before_total, after.total_deposited)

    def test_report_zip_contains_activity_and_detail_csv(self):
        self.seed_dataset()
        files = build_activity_audit_report_files(
            self.db,
            10,
            today="2026-08-02",
            name_resolver=lambda user_id: f"Usuario {user_id}" if user_id else "",
        )

        self.assertEqual(len(files), 1)
        self.assertEqual(files[0].filename, "auditoria_actividades_G3NESYS_2026-08-02.zip")
        with zipfile.ZipFile(BytesIO(files[0].data)) as archive:
            names = set(archive.namelist())
            self.assertIn("actividades_desde_ACT-000050.csv", names)
            self.assertIn("detalle_splits_desde_ACT-000050.csv", names)
            activities_csv = archive.read("actividades_desde_ACT-000050.csv").decode("utf-8-sig")
            details_csv = archive.read("detalle_splits_desde_ACT-000050.csv").decode("utf-8-sig")
        self.assertIn("ACT-000050", activities_csv)
        self.assertIn("Usuario 100", activities_csv)
        self.assertIn("ACT-000060", details_csv)
        self.assertIn("1000", details_csv)

    def test_queries_do_not_modify_database(self):
        self.seed_dataset()
        before = self.db._conn.total_changes
        get_activity_audit_dataset(self.db, 10)
        build_activity_audit_report_files(self.db, 10, today="2026-08-02")
        after = self.db._conn.total_changes

        self.assertEqual(before, after)

    async def test_unauthorized_user_cannot_access_home_view(self):
        view = ActivityAuditHomeView(self.cog)
        interaction = FakeInteraction(self.guild, 123)

        allowed = await view.require_admin(interaction)

        self.assertFalse(allowed)
        self.assertEqual(len(interaction.response.messages), 1)
        self.assertIn("Solo admins autorizados", interaction.response.messages[0][0])

    def test_admin_panel_contains_activity_audit_button(self):
        view = AdminPanelView(self.cog)
        labels = [item.label for item in view.children if getattr(item, "label", None)]

        self.assertIn("Auditoría de actividades", labels)


if __name__ == "__main__":
    unittest.main()