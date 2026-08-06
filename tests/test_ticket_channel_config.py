import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from g3nesys_bot.cogs.admin import Admin, TicketChannelConfigView
from g3nesys_bot.cogs.bank import Bank
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.ticket_channels import TICKET_CHANNEL_SETTING_KEY
from g3nesys_bot.services.tickets import create_ticket, get_ticket, set_ticket_thread


class FakePermissions:
    def __init__(self, **overrides):
        values = {
            "view_channel": True,
            "send_messages": True,
            "create_public_threads": True,
            "send_messages_in_threads": True,
            "embed_links": True,
            "attach_files": True,
        }
        values.update(overrides)
        self.__dict__.update(values)


class FakeThread:
    def __init__(self, thread_id=900):
        self.id = thread_id
        self.mention = f"<#{thread_id}>"
        self.messages = []

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))
        return SimpleNamespace(id=901, channel=self)


class FakeChannel:
    def __init__(self, channel_id, name="tickets", permissions=None):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self._permissions = permissions or FakePermissions()
        self.created_threads = []
        self.sent_messages = []

    def permissions_for(self, _member):
        return self._permissions

    async def create_thread(self, **kwargs):
        thread = FakeThread(thread_id=900 + len(self.created_threads))
        self.created_threads.append((kwargs, thread))
        return thread

    async def send(self, **kwargs):
        message = SimpleNamespace(id=700 + len(self.sent_messages), channel=self, **kwargs)
        self.sent_messages.append(message)
        return message


class FakeGuild:
    def __init__(self, channels=None):
        self.id = 10
        self.name = "G3NESYS"
        self.me = SimpleNamespace(id=999)
        self._channels = {channel.id: channel for channel in (channels or [])}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_member(self, user_id):
        return self.me if user_id == self.me.id else None


class FakeBot:
    def __init__(self, db, guild=None, channels=None):
        self.db = db
        self._guild = guild
        self._channels = {channel.id: channel for channel in (channels or [])}
        self.views = []

    def get_guild(self, guild_id):
        return self._guild if self._guild and self._guild.id == guild_id else None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    async def fetch_channel(self, _channel_id):
        return None

    def add_view(self, view):
        self.views.append(view)


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
    def __init__(self, guild, channel, user_id=100):
        self.guild = guild
        self.guild_id = guild.id
        self.channel = channel
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.response = FakeResponse()
        self.followup = FakeFollowup()


class TicketChannelConfigTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_admin_can_configure_ticket_channel_and_setting_persists(self):
        channel = FakeChannel(500)
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)

        await TicketChannelConfigView(admin).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "500")
        self.assertIn("Canal de tickets configurado correctamente", interaction.response.messages[0][0])
        self.assertTrue(interaction.response.messages[0][1])

    async def test_normal_member_cannot_change_ticket_channel(self):
        channel = FakeChannel(500)
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)
        view = TicketChannelConfigView(admin)
        button = next(item for item in view.children if getattr(item, "label", None) == "Usar canal actual")

        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=False):
            await button.callback(interaction)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "")
        self.assertIn("Solo admins autorizados", interaction.response.messages[0][0])

    async def test_missing_bot_permissions_are_rejected_when_configuring(self):
        channel = FakeChannel(500, permissions=FakePermissions(create_public_threads=False))
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)

        await TicketChannelConfigView(admin).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "")
        self.assertIn("Crear hilos", interaction.response.messages[0][0])

    def test_configuration_text_warns_when_channel_is_missing(self):
        guild = FakeGuild([])
        admin = Admin(FakeBot(self.db, guild=guild))

        self.assertIn("No configurado", admin.ticket_channel_status_text(guild.id))

        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, "999")
        text = admin.ticket_channel_status_text(guild.id)

        self.assertIn("canal no disponible", text)
        self.assertIn("Advertencia", text)

    async def test_new_ticket_thread_and_main_message_use_configured_channel(self):
        fallback = FakeChannel(100, name="banco")
        ticket_channel = FakeChannel(500, name="tickets")
        guild = FakeGuild([fallback, ticket_channel])
        bot = FakeBot(self.db, guild=guild, channels=[ticket_channel])
        bank = Bank(bot)
        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, str(ticket_channel.id))
        ticket = create_ticket(self.db, guild.id, 100, "Ayuda", "Necesito soporte")
        interaction = FakeInteraction(guild, fallback)

        target = await bank.resolve_ticket_destination_channel(guild, fallback)
        thread = await bank.try_create_ticket_thread(interaction, str(ticket["code"]), target)
        set_ticket_thread(self.db, int(ticket["id"]), thread.id)
        ticket = get_ticket(self.db, guild.id, str(ticket["code"]))
        await bank.notify_ticket_created(guild, ticket, target)

        self.assertIs(target, ticket_channel)
        self.assertEqual(ticket_channel.created_threads[0][0]["name"], f"{ticket['code']}-evidencias")
        self.assertEqual(thread.messages[0][0].split()[0], "Ticket")
        self.assertEqual(len(ticket_channel.sent_messages), 1)
        self.assertEqual(self.db.fetch_one("SELECT notification_message_id FROM tickets WHERE id = ?", (ticket["id"],))["notification_message_id"], 700)

    async def test_deleted_configured_channel_falls_back_to_bank_panel_channel(self):
        fallback = FakeChannel(100, name="banco")
        guild = FakeGuild([fallback])
        bank = Bank(FakeBot(self.db, guild=guild))
        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, "999")

        target = await bank.resolve_ticket_destination_channel(guild, fallback)

        self.assertIs(target, fallback)
        warning = self.db.fetch_one("SELECT * FROM audit_logs WHERE action = ?", ("Advertencia canal de tickets",))
        self.assertIsNotNone(warning)
        self.assertIn("no existe", warning["observation"])


if __name__ == "__main__":
    unittest.main()