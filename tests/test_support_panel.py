import sqlite3
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

import discord

from g3nesys_bot.cogs.admin import Admin, ConfigAdminView
from g3nesys_bot.cogs.support import (
    SUPPORT_PANEL_BANNER_SETTING_KEY,
    SUPPORT_PANEL_CHANNEL_SETTING_KEY,
    SUPPORT_PANEL_TYPE,
    Support,
    SupportAdminView,
    SupportGuideDetailView,
    SupportGuidesView,
    SupportPanelChannelConfigView,
    SupportPanelView,
)
from g3nesys_bot.database import Database, SCHEMA
from g3nesys_bot.services.support_guides import GUIDES, build_guide_embed
from g3nesys_bot.services.tickets import create_ticket


class FakePermissions:
    view_channel = True
    send_messages = True
    embed_links = True


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


class FakeChannel:
    def __init__(self, channel_id=500):
        self.id = channel_id
        self.mention = f"<#{channel_id}>"
        self.type = discord.ChannelType.text
        self.sent_messages = []

    def permissions_for(self, _member):
        return FakePermissions()

    async def send(self, **kwargs):
        message = FakeMessage(700 + len(self.sent_messages), self, **kwargs)
        self.sent_messages.append(message)
        return message

    async def fetch_message(self, message_id):
        for message in self.sent_messages:
            if message.id == message_id:
                return message
        raise discord.NotFound(response=SimpleNamespace(status=404, reason="not found"), message="not found")


class FakeGuild:
    def __init__(self, guild_id=10, channels=None):
        self.id = guild_id
        self.name = "G3NESYS"
        self.me = SimpleNamespace(id=999)
        self._channels = {channel.id: channel for channel in channels or []}

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)


class FakeBot:
    def __init__(self, db, guild=None, channels=None, bank=None):
        self.db = db
        self._guild = guild
        self._channels = {channel.id: channel for channel in channels or []}
        self._bank = bank
        self.views = []

    def add_view(self, view):
        self.views.append(view)

    def get_cog(self, name):
        if name == "Bank":
            return self._bank
        if name == "Support":
            return getattr(self, "_support", None)
        return None

    def get_channel(self, channel_id):
        return self._channels.get(channel_id)

    def get_guild(self, guild_id):
        return self._guild if self._guild and self._guild.id == guild_id else None

    async def fetch_channel(self, _channel_id):
        return None


class FakeResponse:
    def __init__(self):
        self.messages = []
        self.edits = []
        self.modals = []
        self.deferred = False
        self._done = False

    def is_done(self):
        return self._done

    async def send_message(self, content=None, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))
        self._done = True

    async def edit_message(self, **kwargs):
        self.edits.append(kwargs)
        self._done = True

    async def send_modal(self, modal):
        self.modals.append(modal)
        self._done = True

    async def defer(self, *, ephemeral=False, **kwargs):
        self.deferred = ephemeral
        self._done = True


class FakeFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, content=None, *, ephemeral=False, **kwargs):
        self.messages.append((content, ephemeral, kwargs))


class FakeInteraction:
    def __init__(self, guild, user_id=100, channel=None):
        self.guild = guild
        self.guild_id = guild.id
        self.channel = channel
        self.user = SimpleNamespace(id=user_id, mention=f"<@{user_id}>")
        self.response = FakeResponse()
        self.followup = FakeFollowup()
        self.data = {}


class SupportPanelTests(unittest.IsolatedAsyncioTestCase):
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

    def support(self, *, bank=None, guild=None, channels=None):
        bot = FakeBot(self.db, guild=guild, channels=channels, bank=bank)
        support = Support(bot)
        bot._support = support
        return support, bot

    async def test_support_panel_registers_persistent_view_and_has_three_buttons(self):
        support, bot = self.support()

        await support.cog_load()

        self.assertTrue(any(isinstance(view, SupportPanelView) for view in bot.views))
        labels = [item.label for item in SupportPanelView(support).children]
        self.assertEqual(labels, ["ABRIR TICKET", "MIS TICKETS", "GUÍAS"])

    async def test_support_panel_publishes_and_stores_panel_message(self):
        channel = FakeChannel(500)
        guild = FakeGuild(channels=[channel])
        support, _bot = self.support(guild=guild, channels=[channel])
        self.db.set_setting(guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY, str(channel.id))
        self.db.set_setting(guild.id, SUPPORT_PANEL_BANNER_SETTING_KEY, "https://example.com/banner.png")

        message = await support.publish_support_panel(guild, admin_id=42)
        row = self.db.fetch_one(
            "SELECT * FROM panel_messages WHERE guild_id = ? AND panel_type = ?",
            (guild.id, SUPPORT_PANEL_TYPE),
        )

        self.assertEqual(message.id, 700)
        self.assertEqual(row["channel_id"], channel.id)
        self.assertEqual(row["message_id"], message.id)
        self.assertEqual(channel.sent_messages[0].embed.title, "🎫 PANEL DE SOPORTE G3NESYS")
        self.assertEqual(channel.sent_messages[0].embed.image.url, "https://example.com/banner.png")

    async def test_open_ticket_reuses_existing_bank_ticket_flow(self):
        bank = SimpleNamespace(open_ticket_modal=AsyncMock())
        guild = FakeGuild()
        support, _bot = self.support(bank=bank, guild=guild)
        interaction = FakeInteraction(guild)
        button = next(item for item in SupportPanelView(support).children if item.custom_id == "support:open_ticket")

        await button.callback(interaction)

        bank.open_ticket_modal.assert_awaited_once_with(interaction)

    async def test_my_tickets_only_shows_current_user_tickets(self):
        guild = FakeGuild()
        support, _bot = self.support(guild=guild)
        own_ticket = create_ticket(self.db, guild.id, 100, "Pago", "Revisar pago")
        create_ticket(self.db, guild.id, 200, "Otro", "No debe verse")
        self.db.execute("UPDATE tickets SET thread_id = ? WHERE id = ?", (900, own_ticket["id"]))
        interaction = FakeInteraction(guild, user_id=100)

        await support.show_my_tickets(interaction, 100)
        embed = interaction.response.messages[0][2]["embed"]

        text = "\n".join(field.value for field in embed.fields)
        self.assertIn("Pago", text)
        self.assertNotIn("Otro", text)
        self.assertIn("https://discord.com/channels/10/900", text)

    async def test_user_cannot_page_other_users_tickets(self):
        guild = FakeGuild()
        support, _bot = self.support(guild=guild)
        interaction = FakeInteraction(guild, user_id=100)

        await support.show_my_tickets(interaction, 200)

        self.assertIn("Solo puedes consultar tus propios tickets", interaction.response.messages[0][0])

    async def test_guides_open_select_and_back(self):
        guild = FakeGuild()
        support, _bot = self.support(guild=guild)
        interaction = FakeInteraction(guild)
        button = next(item for item in SupportPanelView(support).children if item.custom_id == "support:guides")

        await button.callback(interaction)

        guides_view = interaction.response.messages[0][2]["view"]
        self.assertIsInstance(guides_view, SupportGuidesView)
        select = next(item for item in guides_view.children if getattr(item, "custom_id", "") == "support:guides:select")
        select._values = ["bank"]
        await select.callback(interaction)
        self.assertEqual(interaction.response.edits[0]["embed"].title, "💰 Banco")

        detail_view = SupportGuideDetailView(support, "bank")
        back = next(item for item in detail_view.children if getattr(item, "custom_id", "") == "support:guides:back")
        second_interaction = FakeInteraction(guild)
        await back.callback(second_interaction)
        self.assertIsInstance(second_interaction.response.edits[0]["view"], SupportGuidesView)

    def test_guide_structure_supports_video_button(self):
        guide = GUIDES["bank"]
        embed = build_guide_embed(guide)

        self.assertIn("Guía próximamente disponible", embed.fields[0].value)
        self.assertTrue(hasattr(guide, "video_url"))
        self.assertTrue(hasattr(guide, "image_url"))

    def test_admin_config_panel_exposes_support_panel_controls(self):
        guild = FakeGuild()
        support, bot = self.support(guild=guild)
        admin = Admin(bot)
        labels = [item.label for item in ConfigAdminView(admin).children]
        admin_labels = [item.label for item in SupportAdminView(support).children]

        self.assertIn("Panel de Soporte", labels)
        self.assertEqual(
            admin_labels,
            ["Publicar panel", "Actualizar panel", "Configurar canal", "Configurar imagen/banner", "Ver configuración"],
        )

    async def test_support_channel_configuration_persists(self):
        channel = FakeChannel(500)
        guild = FakeGuild(channels=[channel])
        support, _bot = self.support(guild=guild, channels=[channel])
        interaction = FakeInteraction(guild, channel=channel)

        await SupportPanelChannelConfigView(support).save_channel(interaction, channel)

        self.assertEqual(self.db.get_setting(guild.id, SUPPORT_PANEL_CHANNEL_SETTING_KEY), "500")
        self.assertIn("Canal del Panel de Soporte configurado correctamente", interaction.response.messages[0][0])


if __name__ == "__main__":
    unittest.main()

