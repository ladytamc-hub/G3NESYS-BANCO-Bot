import sqlite3
import threading
import unittest
import zipfile
from io import BytesIO
from types import SimpleNamespace

from g3nesys_bot.cogs.admin import AdminPanelView
from g3nesys_bot.constants import (
    WITHDRAWAL_PAID,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.withdrawal_audit import (
    AUDIT_PAID,
    AUDIT_PARTIAL,
    AUDIT_PENDING,
    AUDIT_REJECTED,
    build_withdrawal_audit_report_files,
    get_withdrawal_audit_dataset,
    normalize_user_search,
    normalize_withdrawal_code,
    search_withdrawal_records,
)


class WithdrawalAuditTests(unittest.TestCase):
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

    def create_withdrawal(
        self,
        code,
        user_id,
        amount,
        status,
        *,
        paid=None,
        reason="Pago semanal",
        created_at="2026-08-03T01:00:00+00:00",
        approved_by=None,
        approved_at=None,
        rejected_by=None,
        rejected_at=None,
        rejection_reason=None,
        notification_channel_id=None,
        notification_message_id=None,
    ):
        return self.db.execute(
            """
            INSERT INTO withdrawals (
                code, guild_id, user_id, amount_requested, amount_liquidated,
                status, reason, created_at, approved_by, approved_at,
                rejected_by, rejected_at, rejection_reason,
                notification_channel_id, notification_message_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                code, 10, user_id, amount, paid, status, reason, created_at,
                approved_by, approved_at, rejected_by, rejected_at, rejection_reason,
                notification_channel_id, notification_message_id,
            ),
        )

    def add_payment(self, withdrawal_id, *, amount, author_id, status=WITHDRAWAL_PARTIAL, created_at="2026-08-03T02:00:00+00:00"):
        self.db.execute(
            """
            INSERT INTO withdrawal_action_logs (
                withdrawal_id, action_type, author_id, amount,
                old_status, new_status, note, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (withdrawal_id, "pago_parcial", author_id, amount, WITHDRAWAL_PENDING, status, "Pago registrado", created_at),
        )
        self.db.execute(
            """
            INSERT INTO movements (
                code, guild_id, type, category, user_id, amount,
                source_table, source_id, description, created_by, created_at,
                fee_amount, net_amount
            ) VALUES (?, ?, 'LIQUIDACION', 'Cobro de saldo', ?, ?, 'withdrawals', ?, ?, ?, ?, 0, ?)
            """,
            (f"MOV-{withdrawal_id}", 10, 1000 + withdrawal_id, amount, withdrawal_id, f"Pago COBRO-{withdrawal_id:06d}", author_id, created_at, amount),
        )

    def seed_dataset(self):
        self.create_withdrawal("COBRO-000006", 101, 300000, WITHDRAWAL_PENDING)
        paid_id = self.create_withdrawal(
            "COBRO-000001",
            102,
            100000,
            WITHDRAWAL_PAID,
            paid=100000,
            approved_by=201,
            approved_at="2026-08-03T00:10:00+00:00",
            notification_channel_id=500,
            notification_message_id=600,
        )
        self.add_payment(paid_id, amount=100000, author_id=201, status=WITHDRAWAL_PAID)
        partial_id = self.create_withdrawal(
            "COBRO-000008",
            103,
            500000,
            WITHDRAWAL_PARTIAL,
            paid=250000,
            reason="Reembolso de reparaciones",
            created_at="2026-08-02T23:35:00+00:00",
            approved_by=202,
            approved_at="2026-08-03T00:10:00+00:00",
        )
        self.add_payment(partial_id, amount=250000, author_id=203)
        self.create_withdrawal(
            "COBRO-000009",
            104,
            200000,
            WITHDRAWAL_REJECTED,
            created_at="2026-08-01T20:00:00+00:00",
            rejected_by=204,
            rejected_at="2026-08-01T21:00:00+00:00",
            rejection_reason="Falta evidencia",
        )

    def test_code_and_name_normalization(self):
        self.assertEqual(normalize_withdrawal_code("COBRO-6"), "COBRO-000006")
        self.assertEqual(normalize_withdrawal_code("000006"), "COBRO-000006")
        self.assertEqual(normalize_user_search("@[G3N] Cometeelpan"), "cometeelpan")

    def test_dataset_summary_and_filters_include_partial_as_pending(self):
        self.seed_dataset()
        dataset = get_withdrawal_audit_dataset(self.db, 10)

        self.assertEqual(dataset.get_record("COBRO-000006").audit_status, AUDIT_PENDING)
        self.assertEqual(dataset.get_record("COBRO-000001").audit_status, AUDIT_PAID)
        self.assertEqual(dataset.get_record("COBRO-000008").audit_status, AUDIT_PARTIAL)
        self.assertEqual(dataset.get_record("COBRO-000009").audit_status, AUDIT_REJECTED)
        self.assertEqual(dataset.summary.total_requested, 1100000)
        self.assertEqual(dataset.summary.total_paid, 350000)
        self.assertEqual(dataset.summary.total_pending, 550000)
        self.assertEqual(dataset.summary.total_rejected, 200000)
        self.assertEqual([row.code for row in dataset.filter_records("pending")], ["COBRO-000006", "COBRO-000008"])
        self.assertEqual([row.code for row in dataset.filter_records("partial")], ["COBRO-000008"])

    def test_search_accepts_code_digits_and_tagged_names(self):
        self.seed_dataset()
        dataset = get_withdrawal_audit_dataset(self.db, 10)
        names = {101: "[G3N] Cometeelpan", 102: "[G3N] evilofsanto", 103: "ColdByte", 104: "Franciscuous"}

        self.assertEqual(search_withdrawal_records(dataset, "000006")[0].code, "COBRO-000006")
        self.assertEqual(search_withdrawal_records(dataset, "@cometeelpan", name_resolver=names.get)[0].code, "COBRO-000006")
        self.assertEqual(search_withdrawal_records(dataset, "evilofsanto", name_resolver=names.get)[0].code, "COBRO-000001")

    def test_detail_history_and_message_url_are_read_from_existing_data(self):
        self.seed_dataset()
        dataset = get_withdrawal_audit_dataset(self.db, 10)
        record = dataset.get_record("COBRO-000001")
        history = dataset.movements_for("COBRO-000001")

        self.assertEqual(record.message_url, "https://discord.com/channels/10/500/600")
        self.assertTrue(any(item.action == "Solicitud creada" for item in history))
        self.assertTrue(any(item.action == "Solicitud aprobada" for item in history))
        self.assertTrue(any(item.action == "Pago parcial registrado" for item in history))
        self.assertTrue(any(item.action == "Movimiento financiero" for item in history))

    def test_report_zip_contains_requests_and_history_csv(self):
        self.seed_dataset()
        files = build_withdrawal_audit_report_files(
            self.db,
            10,
            today="2026-08-03",
            name_resolver=lambda user_id: f"Usuario {user_id}" if user_id else "",
        )

        self.assertEqual(len(files), 1)
        with zipfile.ZipFile(BytesIO(files[0].data)) as archive:
            names = set(archive.namelist())
            self.assertIn("auditoria_cobros.csv", names)
            self.assertIn("historial_cobros.csv", names)
            withdrawals_csv = archive.read("auditoria_cobros.csv").decode("utf-8-sig")
            history_csv = archive.read("historial_cobros.csv").decode("utf-8-sig")
        self.assertIn("COBRO-000008", withdrawals_csv)
        self.assertIn("Reembolso de reparaciones", withdrawals_csv)
        self.assertIn("Pago parcial registrado", history_csv)

    def test_queries_do_not_modify_database(self):
        self.seed_dataset()
        before = self.db._conn.total_changes

        get_withdrawal_audit_dataset(self.db, 10)
        build_withdrawal_audit_report_files(self.db, 10, today="2026-08-03")

        self.assertEqual(before, self.db._conn.total_changes)

    def test_admin_panel_contains_payment_audit_button(self):
        cog = SimpleNamespace(db=self.db)
        view = AdminPanelView(cog)
        labels = [item.label for item in view.children if getattr(item, "label", None)]

        self.assertIn("Auditoría pagos/cobros", labels)
        self.assertIn("Solicitudes de Cobro", labels)


if __name__ == "__main__":
    unittest.main()
