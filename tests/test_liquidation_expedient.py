import csv
import hashlib
import json
import os
import sqlite3
import threading
import unittest
import zipfile
from io import BytesIO, StringIO
from types import SimpleNamespace

from g3nesys_bot.cogs.admin import ActivityAuditRecordView
from g3nesys_bot.constants import (
    ACTIVITY_FINISHED,
    ACTIVITY_PAYOUT_CREATED,
    ACTIVITY_TYPE_MANDATORY,
    ATTENDANCE_CONFIRMED,
    PAYOUT_DEPOSITED,
    WITHDRAWAL_PAID,
    WITHDRAWAL_PARTIAL,
    WITHDRAWAL_PENDING,
    WITHDRAWAL_REJECTED,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.liquidation_expedient import (
    ActivityWithoutLiquidationError,
    build_liquidation_expedient_file,
    liquidation_expedient_tempfile,
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

    async def send_message(self, content, *, ephemeral=False, **kwargs):
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


class LiquidationExpedientTests(unittest.IsolatedAsyncioTestCase):
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
            owner_id=999,
            get_member=lambda user_id: SimpleNamespace(display_name=f"User {user_id}", mention=f"<@{user_id}>")
            if user_id in {100, 101, 102, 103, 104, 200, 201, 202, 300, 301, 302, 303}
            else None,
        )
        self.cog = SimpleNamespace(db=self.db)

    def tearDown(self):
        self.db.close()

    def create_activity(self, code="ACT-001458", *, activity_type="regular", status=ACTIVITY_PAYOUT_CREATED):
        activity_id = self.db.execute(
            """
            INSERT INTO activities (
                code, guild_id, name, caller_id, pinged_by_id, horario,
                voice_channel_id, status, channel_id, message_id, created_at,
                started_at, ended_at, activity_type, thread_id, thread_panel_message_id
            ) VALUES (?, 10, ?, 200, 100, '20:00', 700, ?, 500, 600, ?,
                      ?, ?, ?, 800, 900)
            """,
            (
                code,
                f"Actividad {code}",
                status,
                "2026-08-01T00:00:00+00:00",
                "2026-08-01T01:00:00+00:00",
                "2026-08-01T01:10:00+00:00",
                activity_type,
            ),
        )
        role_id = self.db.execute(
            "INSERT INTO activity_roles (activity_id, key, name, slots, emoji, position) VALUES (?, 'dps', 'DPS', 10, '', 0)",
            (activity_id,),
        )
        for user_id, name, percent in [(101, "FullUser", 100), (102, "LateUser", 70), (103, "PartialUser", 50)]:
            self.db.execute(
                "INSERT INTO activity_participants (activity_id, role_id, user_id, display_name, joined_at) VALUES (?, ?, ?, ?, ?)",
                (activity_id, role_id, user_id, name, "2026-08-01T01:00:00+00:00"),
            )
            self.db.execute(
                """
                INSERT INTO asistencia_actividades (
                    actividad_id, usuario_id, estado, confirmo_boton, confirmo_voz,
                    voice_seconds, participation_percent
                ) VALUES (?, ?, ?, 1, 1, ?, ?)
                """,
                (activity_id, user_id, ATTENDANCE_CONFIRMED, int(600 * percent / 100), percent),
            )
        self.db.execute(
            """
            INSERT INTO activity_join_requests (
                guild_id, activity_id, user_id, display_name, requested_role,
                status, requested_at, reviewed_by, reviewed_at
            ) VALUES (10, ?, 102, 'LateUser', 'DPS', 'Aceptada',
                      '2026-08-01T01:02:00+00:00', 200, '2026-08-01T01:03:00+00:00')
            """,
            (activity_id,),
        )
        self.db.execute(
            """
            INSERT INTO activity_voice_stats (
                guild_id, activity_id, user_id, display_name, monitor_started_at,
                monitor_ended_at, first_join_at, last_join_at, last_leave_at,
                total_present_seconds, total_absent_seconds, leave_count, rejoin_count,
                attendance_percentage, final_voice_status, monitoring_duration_seconds,
                created_at, updated_at
            ) VALUES
            (10, ?, 101, 'FullUser', '2026-08-01T01:00:00+00:00', '2026-08-01T01:10:00+00:00',
             '2026-08-01T01:00:00+00:00', '2026-08-01T01:00:00+00:00', '2026-08-01T01:10:00+00:00',
             600, 0, 0, 0, 100, 'Permanecio hasta el final', 600, 'x', 'x'),
            (10, ?, 102, 'LateUser', '2026-08-01T01:00:00+00:00', '2026-08-01T01:10:00+00:00',
             '2026-08-01T01:03:00+00:00', '2026-08-01T01:03:00+00:00', '2026-08-01T01:10:00+00:00',
             420, 180, 0, 0, 70, 'Entro tarde', 600, 'x', 'x'),
            (10, ?, 103, 'PartialUser', '2026-08-01T01:00:00+00:00', '2026-08-01T01:10:00+00:00',
             '2026-08-01T01:00:00+00:00', '2026-08-01T01:00:00+00:00', '2026-08-01T01:05:00+00:00',
             300, 300, 1, 0, 50, 'Salio antes', 600, 'x', 'x')
            """,
            (activity_id, activity_id, activity_id),
        )
        return activity_id

    def create_payout(self, activity_id):
        payout_id = self.db.execute(
            """
            INSERT INTO payouts (
                code, guild_id, activity_id, caller_id, status, gross_loot,
                market_rate_percent, repairs, other_expenses, guild_percent,
                guild_amount, distributable, notes, created_at, reviewed_by,
                reviewed_at, caller_percent, caller_amount, quick_liquidated_at,
                quick_liquidated_by
            ) VALUES ('SPLIT-000001', 10, ?, 200, ?, 1000000, 2.5, 10000, 5000,
                      10, 98500, 886500, 'Observacion split',
                      '2026-08-01T01:11:00+00:00', 201, '2026-08-01T01:20:00+00:00',
                      0, 0, '2026-08-01T01:30:00+00:00', 201)
            """,
            (activity_id, PAYOUT_DEPOSITED),
        )
        for user_id, percent, amount in [(101, 100, 443250), (102, 70, 310275), (103, 50, 232975)]:
            self.db.execute(
                """
                INSERT INTO payout_participants (
                    payout_id, user_id, participation_percent, amount, deposited_at,
                    liquidated_at, liquidated_by
                ) VALUES (?, ?, ?, ?, '2026-08-01T01:20:00+00:00',
                          '2026-08-01T01:30:00+00:00', 201)
                """,
                (payout_id, user_id, percent, amount),
            )
        self.db.execute(
            "INSERT INTO payout_audit_logs (guild_id, payout_id, actor_id, action, details, created_at) VALUES (10, ?, 200, 'Split generado', 'ACT-001458', '2026-08-01T01:11:00+00:00')",
            (payout_id,),
        )
        return payout_id

    def create_withdrawal(self, code, user_id, amount, status, *, paid=None, reason="Pago ACT-001458"):
        return self.db.execute(
            """
            INSERT INTO withdrawals (
                code, guild_id, user_id, amount_requested, amount_liquidated,
                status, reason, created_at, approved_by, approved_at,
                rejected_by, rejected_at, rejection_reason
            ) VALUES (?, 10, ?, ?, ?, ?, ?, '2026-08-01T02:00:00+00:00',
                      201, '2026-08-01T02:05:00+00:00', ?, ?, ?)
            """,
            (
                code,
                user_id,
                amount,
                paid,
                status,
                reason,
                202 if status == WITHDRAWAL_REJECTED else None,
                "2026-08-01T02:06:00+00:00" if status == WITHDRAWAL_REJECTED else None,
                "Falta evidencia ACT-001458" if status == WITHDRAWAL_REJECTED else None,
            ),
        )

    def add_payment_log(self, withdrawal_id, amount, *, action="pago_parcial", new_status=WITHDRAWAL_PARTIAL, at="2026-08-01T02:10:00+00:00"):
        self.db.execute(
            """
            INSERT INTO withdrawal_action_logs (
                withdrawal_id, action_type, author_id, amount, old_status,
                new_status, note, created_at
            ) VALUES (?, ?, 201, ?, ?, ?, 'Movimiento ACT-001458', ?)
            """,
            (withdrawal_id, action, amount, WITHDRAWAL_PENDING, new_status, at),
        )

    def build_seeded_expedient(self):
        activity_id = self.create_activity()
        self.create_payout(activity_id)
        paid = self.create_withdrawal("COBRO-000001", 101, 1000, WITHDRAWAL_PAID, paid=1000)
        self.add_payment_log(paid, 500, at="2026-08-01T02:10:00+00:00")
        self.add_payment_log(paid, 500, action="pago_completo", new_status=WITHDRAWAL_PAID, at="2026-08-01T02:20:00+00:00")
        pending = self.create_withdrawal("COBRO-000002", 102, 2000, WITHDRAWAL_PENDING, paid=0)
        rejected = self.create_withdrawal("COBRO-000003", 103, 3000, WITHDRAWAL_REJECTED, paid=0)
        return build_liquidation_expedient_file(
            self.db,
            10,
            "ACT-001458",
            generated_at="2026-08-04T00:00:00+00:00",
            name_resolver=lambda user_id: f"User {user_id}" if user_id else "",
        )

    def test_act_liquidated_zip_contains_required_files_manifest_and_hashes(self):
        expedient = self.build_seeded_expedient()

        self.assertEqual(expedient.filename, "EXPEDIENTE_ACT-001458.zip")
        self.assertEqual(expedient.participant_count, 3)
        self.assertEqual(expedient.request_count, 3)
        with zipfile.ZipFile(BytesIO(expedient.data)) as archive:
            names = set(archive.namelist())
            self.assertEqual(
                names,
                {
                    "resumen_liquidacion.txt",
                    "participantes.csv",
                    "pagos_cobros.csv",
                    "historial_auditoria.csv",
                    "informacion_tecnica.txt",
                    "manifest.json",
                },
            )
            manifest = json.loads(archive.read("manifest.json").decode("utf-8"))
            for item in manifest["archivos"]:
                data = archive.read(item["nombre"])
                self.assertEqual(hashlib.sha256(data).hexdigest(), item["sha256"])
            resumen = archive.read("resumen_liquidacion.txt").decode("utf-8")
        self.assertIn("Codigo de actividad: ACT-001458", resumen)
        self.assertIn("Botin bruto: 1,000,000 plata", resumen)

    def test_participants_include_late_and_partial_percent(self):
        expedient = self.build_seeded_expedient()
        with zipfile.ZipFile(BytesIO(expedient.data)) as archive:
            rows = list(csv.DictReader(StringIO(archive.read("participantes.csv").decode("utf-8-sig"))))
        by_id = {row["Discord ID"]: row for row in rows}

        self.assertEqual(by_id["102"]["Se unio despues del inicio"], "Si")
        self.assertEqual(by_id["103"]["Porcentaje de participacion"], "50.00")
        self.assertEqual(by_id["102"]["Fue incluido en el split"], "Si")

    def test_payments_include_partial_pending_and_rejected(self):
        expedient = self.build_seeded_expedient()
        with zipfile.ZipFile(BytesIO(expedient.data)) as archive:
            rows = list(csv.DictReader(StringIO(archive.read("pagos_cobros.csv").decode("utf-8-sig"))))
        text = "\n".join(",".join(row.values()) for row in rows)

        self.assertIn("COBRO-000001", text)
        self.assertIn("Pago parcial registrado", text)
        self.assertIn("Pago total registrado", text)
        self.assertIn("COBRO-000002", text)
        self.assertIn("Pendiente", text)
        self.assertIn("COBRO-000003", text)
        self.assertIn("Rechazado", text)

    def test_historical_mand_activity_is_supported_with_registered_loot(self):
        activity_id = self.create_activity(
            "MAND-000013",
            activity_type=ACTIVITY_TYPE_MANDATORY,
            status=ACTIVITY_FINISHED,
        )
        self.db.execute(
            """
            UPDATE activities
            SET mandatory_loot_amount = 400000,
                mandatory_loot_recorded_by = 200,
                mandatory_loot_recorded_at = '2026-08-01T03:00:00+00:00'
            WHERE id = ?
            """,
            (activity_id,),
        )

        expedient = build_liquidation_expedient_file(self.db, 10, "MAND-13")

        self.assertEqual(expedient.filename, "EXPEDIENTE_MAND-000013.zip")
        with zipfile.ZipFile(BytesIO(expedient.data)) as archive:
            resumen = archive.read("resumen_liquidacion.txt").decode("utf-8")
        self.assertIn("Botin registrado: 400,000 plata", resumen)

    def test_activity_without_liquidation_raises_clear_error(self):
        self.create_activity("ACT-001459", status=ACTIVITY_FINISHED)

        with self.assertRaises(ActivityWithoutLiquidationError):
            build_liquidation_expedient_file(self.db, 10, "ACT-001459")

    def test_missing_optional_data_uses_no_registrado(self):
        activity_id = self.create_activity("ACT-001460")
        self.db.execute("UPDATE activities SET message_id = NULL, thread_id = NULL WHERE id = ?", (activity_id,))
        self.create_payout(activity_id)

        expedient = build_liquidation_expedient_file(self.db, 10, "ACT-001460")

        with zipfile.ZipFile(BytesIO(expedient.data)) as archive:
            resumen = archive.read("resumen_liquidacion.txt").decode("utf-8")
        self.assertIn("No registrado", resumen)

    def test_temp_file_is_removed_and_generation_does_not_modify_database(self):
        expedient = self.build_seeded_expedient()
        before = self.db._conn.total_changes

        with liquidation_expedient_tempfile(expedient) as path:
            self.assertTrue(os.path.exists(path))
            saved_path = path
        self.assertFalse(os.path.exists(saved_path))
        build_liquidation_expedient_file(self.db, 10, "ACT-001458")

        self.assertEqual(before, self.db._conn.total_changes)

    async def test_unauthorized_user_is_deferred_and_denied_from_button(self):
        activity_id = self.create_activity()
        self.create_payout(activity_id)
        record = SimpleNamespace(
            has_split_details=True,
            payout_ids=(1,),
            guild_id=10,
            channel_id=None,
            message_id=None,
            thread_id=None,
            thread_panel_message_id=None,
        )
        view = ActivityAuditRecordView(self.cog, "ACT-001458", has_details=True, record=record)
        button = next(item for item in view.children if item.label == "Expediente de Liquidacion")
        interaction = FakeInteraction(self.guild, 123)

        await button.callback(interaction)

        self.assertTrue(interaction.response.deferred)
        self.assertTrue(interaction.followup.messages)
        self.assertIn("Solo admins autorizados", interaction.followup.messages[0][0])


if __name__ == "__main__":
    unittest.main()
