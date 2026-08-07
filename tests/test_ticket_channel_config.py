import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import discord

from g3nesys_bot.cogs.admin import Admin, TicketChannelConfigView
from g3nesys_bot.cogs.bank import Bank
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.ticket_channels import (
    TICKET_CHANNEL_LABEL,
    TICKET_CHANNEL_SETTING_KEY,
    TICKET_CONVERSATION_CHANNEL_LABEL,
    TICKET_CONVERSATION_CHANNEL_SETTING_KEY,
)
from g3nesys_bot.services.tickets import create_ticket, get_ticket, set_ticket_thread


class FakePermissions:
    def __init__(self, **overrides):
        values = {
            "view_channel": True,
            "send_messages": True,
            "create_public_threads": True,
            "create_private_threads": True,
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
        self.added_users = []

    async def add_user(self, user):
        self.added_users.append(user)

    async def send(self, content=None, **kwargs):
        self.messages.append((content, kwargs))
        return SimpleNamespace(id=901, channel=self)


class FakeChannel:
    def __init__(self, channel_id, name="tickets", permissions=None):
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.type = discord.ChannelType.text
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
        message = FakeMessage(700 + len(self.sent_messages), self, **kwargs)
        self.sent_messages.append(message)
        return message


class FakeMessage:
    def __init__(self, message_id, channel, **kwargs):
        self.id = message_id
        self.channel = channel
        self.edits = []
        for key, value in kwargs.items():
            setattr(self, key, value)

    async def edit(self, **kwargs):
        self.edits.append(kwargs)
        for key, value in kwargs.items():
            setattr(self, key, value)


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

    def notification_config_view(self, admin):
        return TicketChannelConfigView(
            admin,
            setting_key=TICKET_CHANNEL_SETTING_KEY,
            label=TICKET_CHANNEL_LABEL,
        )

    def conversation_config_view(self, admin):
        return TicketChannelConfigView(
            admin,
            setting_key=TICKET_CONVERSATION_CHANNEL_SETTING_KEY,
            label=TICKET_CONVERSATION_CHANNEL_LABEL,
            conversation=True,
        )

    async def test_admin_can_configure_ticket_channel_and_setting_persists(self):
        channel = FakeChannel(500)
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)

        await self.notification_config_view(admin).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "500")
        self.assertIn("Canal de notificaciones de tickets configurado correctamente", interaction.response.messages[0][0])
        self.assertTrue(interaction.response.messages[0][1])

    async def test_admin_can_configure_conversation_channel_independently(self):
        channel = FakeChannel(600)
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)

        await self.conversation_config_view(admin).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CONVERSATION_CHANNEL_SETTING_KEY), "600")
        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "")
        self.assertIn("Canal de conversaciones de tickets configurado correctamente", interaction.response.messages[0][0])
        self.assertTrue(interaction.response.messages[0][1])

    async def test_normal_member_cannot_change_ticket_channel(self):
        channel = FakeChannel(500)
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)
        view = self.notification_config_view(admin)
        button = next(item for item in view.children if getattr(item, "label", None) == "Usar canal actual")

        with patch("g3nesys_bot.cogs.admin.is_admin_subject", return_value=False):
            await button.callback(interaction)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CHANNEL_SETTING_KEY), "")
        self.assertIn("Solo admins autorizados", interaction.response.messages[0][0])

    async def test_missing_bot_permissions_are_rejected_when_configuring_conversation_channel(self):
        channel = FakeChannel(500, permissions=FakePermissions(create_private_threads=False))
        guild = FakeGuild([channel])
        admin = Admin(FakeBot(self.db, guild=guild))
        interaction = FakeInteraction(guild, channel)

        await self.conversation_config_view(admin).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, TICKET_CONVERSATION_CHANNEL_SETTING_KEY), "")
        self.assertIn("Crear hilos privados", interaction.response.messages[0][0])

    def test_configuration_text_warns_when_channel_is_missing(self):
        guild = FakeGuild([])
        admin = Admin(FakeBot(self.db, guild=guild))

        self.assertIn("No configurado", admin.ticket_channel_status_text(guild.id))

        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, "999")
        text = admin.ticket_channel_status_text(guild.id)

        self.assertIn("canal no disponible", text)
        self.assertIn("Advertencia", text)

    async def test_new_ticket_uses_separate_admin_and_conversation_channels(self):
        fallback = FakeChannel(100, name="banco")
        admin_channel = FakeChannel(500, name="tickets-admin")
        conversation_channel = FakeChannel(600, name="tickets-usuarios")
        guild = FakeGuild([fallback, admin_channel, conversation_channel])
        bot = FakeBot(self.db, guild=guild, channels=[admin_channel, conversation_channel])
        bank = Bank(bot)
        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, str(admin_channel.id))
        self.db.set_setting(guild.id, TICKET_CONVERSATION_CHANNEL_SETTING_KEY, str(conversation_channel.id))
        ticket = create_ticket(self.db, guild.id, 100, "Ayuda", "Necesito soporte")
        interaction = FakeInteraction(guild, fallback)

        admin_target = await bank.resolve_ticket_notification_channel(guild)
        conversation_target = await bank.resolve_ticket_conversation_channel(guild)
        admin_message = await bank.notify_ticket_created(guild, ticket, admin_target)
        thread = await bank.try_create_ticket_thread(interaction, str(ticket["code"]), conversation_target)
        set_ticket_thread(self.db, int(ticket["id"]), thread.id)
        ticket = get_ticket(self.db, guild.id, str(ticket["code"]))

        self.assertIs(admin_target, admin_channel)
        self.assertIs(conversation_target, conversation_channel)
        self.assertEqual(len(admin_channel.sent_messages), 1)
        self.assertIs(admin_message.channel, admin_channel)
        self.assertEqual(conversation_channel.created_threads[0][0]["name"], f"{ticket['code']}-evidencias")
        self.assertEqual(conversation_channel.created_threads[0][0]["type"], discord.ChannelType.private_thread)
        self.assertFalse(conversation_channel.created_threads[0][0]["invitable"])
        self.assertEqual(conversation_channel.created_threads[0][0]["auto_archive_duration"], 1440)
        self.assertEqual(thread.added_users, [interaction.user])
        self.assertEqual(thread.messages[0][0].split()[0], "Ticket")
        self.assertEqual(self.db.fetch_one("SELECT notification_message_id FROM tickets WHERE id = ?", (ticket["id"],))["notification_message_id"], 700)

    async def test_missing_conversation_channel_does_not_fall_back_to_admin_channel(self):
        fallback = FakeChannel(100, name="banco")
        admin_channel = FakeChannel(500, name="tickets-admin")
        guild = FakeGuild([fallback, admin_channel])
        bank = Bank(FakeBot(self.db, guild=guild, channels=[admin_channel]))
        self.db.set_setting(guild.id, TICKET_CHANNEL_SETTING_KEY, str(admin_channel.id))

        admin_target = await bank.resolve_ticket_notification_channel(guild)
        conversation_target = await bank.resolve_ticket_conversation_channel(guild)
        thread = await bank.try_create_ticket_thread(FakeInteraction(guild, fallback), "TKT-000001", conversation_target)

        self.assertIs(admin_target, admin_channel)
        self.assertIsNone(conversation_target)
        self.assertIsNone(thread)
        self.assertEqual(admin_channel.created_threads, [])
        warning = self.db.fetch_one("SELECT * FROM audit_logs WHERE action = ?", ("Advertencia canal de tickets",))
        self.assertIsNotNone(warning)
        self.assertIn("ticket_conversation_channel_id no configurado", warning["observation"])


if __name__ == "__main__":
    unittest.main()
