[1mdiff --git a/g3nesys_bot/cogs/activities.py b/g3nesys_bot/cogs/activities.py[m
[1mindex 67a1361..5afaf00 100644[m
[1m--- a/g3nesys_bot/cogs/activities.py[m
[1m+++ b/g3nesys_bot/cogs/activities.py[m
[36m@@ -1554,9 +1554,9 @@[m [mclass CallerConfigPanelView(discord.ui.View):[m
 [m
 [m
 class CreatePingOptionsView(discord.ui.View):[m
[31m-    def __init__(self, panel: "PingsPanelView"):[m
[32m+[m[32m    def __init__(self, cog: "Activities"):[m
         super().__init__(timeout=180)[m
[31m-        self.panel = panel[m
[32m+[m[32m        self.cog = cog[m
 [m
     @discord.ui.button([m
         label="Crear Ping Rápido",[m
[36m@@ -1565,8 +1565,8 @@[m [mclass CreatePingOptionsView(discord.ui.View):[m
         custom_id="g3n:pings:create_activity:options",[m
         row=0,[m
     )[m
[31m-    async def create_activity(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:[m
[31m-        await self.panel.create_activity(interaction, button)[m
[32m+[m[32m    async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[32m+[m[32m        await self.cog.open_quick_ping_from_panel(interaction)[m
 [m
     @discord.ui.button([m
         label="Crear Ping (Act. Split)",[m
[36m@@ -1575,8 +1575,8 @@[m [mclass CreatePingOptionsView(discord.ui.View):[m
         custom_id="g3n:pings:select_template:options",[m
         row=0,[m
     )[m
[31m-    async def select_template(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:[m
[31m-        await self.panel.select_template(interaction, button)[m
[32m+[m[32m    async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[32m+[m[32m        await self.cog.open_split_ping_from_panel(interaction)[m
 [m
 [m
 class PingsPanelView(discord.ui.View):[m
[36m@@ -1619,7 +1619,7 @@[m [mclass PingsPanelView(discord.ui.View):[m
         await private_response([m
             interaction,[m
             "Selecciona el tipo de ping que quieres crear:",[m
[31m-            view=CreatePingOptionsView(self),[m
[32m+[m[32m            view=CreatePingOptionsView(self.cog),[m
         )[m
 [m
     @discord.ui.button([m
[36m@@ -1644,10 +1644,8 @@[m [mclass PingsPanelView(discord.ui.View):[m
         row=0,[m
     )[m
     async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[31m-        if not is_caller_panel_subject(self.cog.db, interaction):[m
[31m-            await reject_caller_access(self.cog.db, interaction, "crear pings")[m
[31m-            return[m
[31m-        await self.cog.prompt_activity_creation(interaction, template_id=None)[m
[32m+[m[32m        await self.cog.open_quick_ping_from_panel(interaction)[m
[32m+[m
 [m
     @discord.ui.button([m
         label="Crear Ping (Act. Split)",[m
[36m@@ -1657,33 +1655,8 @@[m [mclass PingsPanelView(discord.ui.View):[m
         row=0,[m
     )[m
     async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[31m-        if not is_caller_panel_subject(self.cog.db, interaction):[m
[31m-            await reject_caller_access(self.cog.db, interaction, "crear pings")[m
[31m-            return[m
[31m-        if is_admin_subject(self.cog.db, interaction):[m
[31m-            templates = self.cog.db.fetch_all([m
[31m-                "SELECT * FROM templates WHERE guild_id = ? ORDER BY created_at DESC LIMIT 25",[m
[31m-                (interaction.guild.id,),[m
[31m-            )[m
[31m-        else:[m
[31m-            templates = self.cog.db.fetch_all([m
[31m-                """[m
[31m-                SELECT *[m
[31m-                FROM templates[m
[31m-                WHERE guild_id = ? AND (created_by = ? OR publica = 1)[m
[31m-                ORDER BY CASE WHEN created_by = ? THEN 0 ELSE 1 END, created_at DESC[m
[31m-                LIMIT 25[m
[31m-                """,[m
[31m-                (interaction.guild.id, interaction.user.id, interaction.user.id),[m
[31m-            )[m
[31m-        if not templates:[m
[31m-            await private_response(interaction, "Aun no hay plantillas disponibles. Crea una con `Crear Plantilla`.")[m
[31m-            return[m
[31m-        await private_response([m
[31m-            interaction,[m
[31m-            "Elige la plantilla que quieres usar:",[m
[31m-            view=TemplateSelectView(self.cog, templates),[m
[31m-        )[m
[32m+[m[32m        await self.cog.open_split_ping_from_panel(interaction)[m
[32m+[m
 [m
     @discord.ui.button([m
         label="Crear Plantilla",[m
[36m@@ -1918,8 +1891,8 @@[m [mclass PingsLegacyPanelCallbacksView(discord.ui.View):[m
         style=discord.ButtonStyle.success,[m
         custom_id="g3n:pings:create_activity",[m
     )[m
[31m-    async def create_activity(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:[m
[31m-        await PingsPanelView(self.cog).create_activity(interaction, button)[m
[32m+[m[32m    async def create_activity(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[32m+[m[32m        await self.cog.open_quick_ping_from_panel(interaction)[m
 [m
     @discord.ui.button([m
         label="Crear Ping (Act. Split)",[m
[36m@@ -1927,8 +1900,8 @@[m [mclass PingsLegacyPanelCallbacksView(discord.ui.View):[m
         style=discord.ButtonStyle.secondary,[m
         custom_id="g3n:pings:select_template",[m
     )[m
[31m-    async def select_template(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:[m
[31m-        await PingsPanelView(self.cog).select_template(interaction, button)[m
[32m+[m[32m    async def select_template(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:[m
[32m+[m[32m        await self.cog.open_split_ping_from_panel(interaction)[m
 [m
 [m
 class ActivityView(discord.ui.View):[m
[36m@@ -3595,6 +3568,62 @@[m [mclass Activities(commands.Cog):[m
             raise ValueError("El canal de pings configurado ya no existe o no permite publicar.")[m
         return channel[m
 [m
[32m+[m[32m    async def _ping_open_error_response(self, interaction: discord.Interaction, content: str) -> None:[m
[32m+[m[32m        if interaction.response.is_done():[m
[32m+[m[32m            await interaction.followup.send(content, ephemeral=True)[m
[32m+[m[32m        else:[m
[32m+[m[32m            await interaction.response.send_message(content, ephemeral=True)[m
[32m+[m
[32m+[m[32m    async def open_quick_ping_from_panel(self, interaction: discord.Interaction) -> None:[m
[32m+[m[32m        try:[m
[32m+[m[32m            if not is_caller_panel_subject(self.db, interaction):[m
[32m+[m[32m                await reject_caller_access(self.db, interaction, "crear pings")[m
[32m+[m[32m                return[m
[32m+[m[32m            await self.prompt_activity_creation(interaction, template_id=None)[m
[32m+[m[32m        except Exception:[m
[32m+[m[32m            LOGGER.exception("Error al abrir creacion de ping rapido")[m
[32m+[m[32m            await self._ping_open_error_response([m
[32m+[m[32m                interaction,[m
[32m+[m[32m                "Ocurri? un error al abrir el formulario de ping.",[m
[32m+[m[32m            )[m
[32m+[m
[32m+[m[32m    async def open_split_ping_from_panel(self, interaction: discord.Interaction) -> None:[m
[32m+[m[32m        try:[m
[32m+[m[32m            await defer_private_response(interaction)[m
[32m+[m[32m            if not is_caller_panel_subject(self.db, interaction):[m
[32m+[m[32m                await reject_caller_access(self.db, interaction, "crear pings")[m
[32m+[m[32m                return[m
[32m+[m[32m            if is_admin_subject(self.db, interaction):[m
[32m+[m[32m                templates = self.db.fetch_all([m
[32m+[m[32m                    "SELECT * FROM templates WHERE guild_id = ? ORDER BY created_at DESC LIMIT 25",[m
[32m+[m[32m                    (interaction.guild.id,),[m
[32m+[m[32m                )[m
[32m+[m[32m            else:[m
[32m+[m[32m                templates = self.db.fetch_all([m
[32m+[m[32m                    """[m
[32m+[m[32m                    SELECT *[m
[32m+[m[32m                    FROM templates[m
[32m+[m[32m                    WHERE guild_id = ? AND (created_by = ? OR publica = 1)[m
[32m+[m[32m                    ORDER BY CASE WHEN created_by = ? THEN 0 ELSE 1 END, created_at DESC[m
[32m+[m[32m                    LIMIT 25[m
[32m+[m[32m                    """,[m
[32m+[m[32m                    (interaction.guild.id, interaction.user.id, interaction.user.id),[m
[32m+[m[32m                )[m
[32m+[m[32m            if not templates:[m
[32m+[m[32m                await private_response(interaction, "Aun no hay plantillas disponibles. Crea una con `Crear Plantilla`.")[m
[32m+[m[32m                return[m
[32m+[m[32m            await private_response([m
[32m+[m[32m                interaction,[m
[32m+[m[32m                "Elige la plantilla que quieres usar:",[m
[32m+[m[32m                view=TemplateSelectView(self, templates),[m
[32m+[m[32m            )[m
[32m+[m[32m        except Exception:[m
[32m+[m[32m            LOGGER.exception("Error al abrir creacion de ping con split")[m
[32m+[m[32m            await self._ping_open_error_response([m
[32m+[m[32m                interaction,[m
[32m+[m[32m                "Ocurri? un error al abrir el formulario de ping.",[m
[32m+[m[32m            )[m
[32m+[m
     async def prompt_activity_creation([m
         self,[m
         interaction: discord.Interaction,[m
[1mdiff --git a/tests/test_pings_panel_and_penalties.py b/tests/test_pings_panel_and_penalties.py[m
[1mindex 5637e06..bc9c871 100644[m
[1m--- a/tests/test_pings_panel_and_penalties.py[m
[1m+++ b/tests/test_pings_panel_and_penalties.py[m
[36m@@ -19,15 +19,31 @@[m [mfrom g3nesys_bot.services.fines import create_fine[m
 class FakeResponse:[m
     def __init__(self):[m
         self.messages = [][m
[32m+[m[32m        self.deferred = None[m
[32m+[m[32m        self.modal = None[m
         self._done = False[m
 [m
     def is_done(self):[m
         return self._done[m
 [m
[32m+[m[32m    async def defer(self, *, ephemeral=False):[m
[32m+[m[32m        if self._done:[m
[32m+[m[32m            raise AssertionError("response.defer called after response was done")[m
[32m+[m[32m        self.deferred = ephemeral[m
[32m+[m[32m        self._done = True[m
[32m+[m
     async def send_message(self, content, *, ephemeral=False, **kwargs):[m
[32m+[m[32m        if self._done:[m
[32m+[m[32m            raise AssertionError("response.send_message called after response was done")[m
         self.messages.append((content, ephemeral, kwargs))[m
         self._done = True[m
 [m
[32m+[m[32m    async def send_modal(self, modal):[m
[32m+[m[32m        if self._done:[m
[32m+[m[32m            raise AssertionError("response.send_modal called after response was done")[m
[32m+[m[32m        self.modal = modal[m
[32m+[m[32m        self._done = True[m
[32m+[m
 [m
 class FakeFollowup:[m
     def __init__(self):[m
[36m@@ -140,22 +156,73 @@[m [mclass PingsPanelAndPenaltyTests(unittest.IsolatedAsyncioTestCase):[m
         self.assertIsInstance(kwargs["view"], CreatePingOptionsView)[m
 [m
     def test_secondary_view_shows_existing_ping_options(self):[m
[31m-        secondary = CreatePingOptionsView(PingsPanelView(self.cog))[m
[32m+[m[32m        secondary = CreatePingOptionsView(self.cog)[m
 [m
         self.assertEqual(self.labels(secondary), ["Crear Ping Rápido", "Crear Ping (Act. Split)"])[m
 [m
[31m-    async def test_secondary_buttons_reuse_existing_panel_callbacks(self):[m
[31m-        panel = PingsPanelView(self.cog)[m
[31m-        panel.create_activity = AsyncMock()[m
[31m-        panel.select_template = AsyncMock()[m
[31m-        secondary = CreatePingOptionsView(panel)[m
[32m+[m[32m    async def test_secondary_quick_ping_opens_modal_without_defer(self):[m
[32m+[m[32m        self.db.execute([m
[32m+[m[32m            """[m
[32m+[m[32m            INSERT INTO callers (guild_id, user_id, added_by, created_at)[m
[32m+[m[32m            VALUES (?, ?, ?, ?)[m
[32m+[m[32m            """,[m
[32m+[m[32m            (self.guild.id, 100, 200, "2026-08-02T00:00:00+00:00"),[m
[32m+[m[32m        )[m
[32m+[m[32m        secondary = CreatePingOptionsView(self.cog)[m
         interaction = FakeInteraction(self.guild)[m
 [m
[31m-        await secondary.children[0].callback(interaction)[m
[31m-        await secondary.children[1].callback(interaction)[m
[32m+[m[32m        with patch("g3nesys_bot.cogs.activities.is_caller_panel_subject", return_value=True):[m
[32m+[m[32m            await secondary.children[0].callback(interaction)[m
 [m
[31m-        panel.create_activity.assert_awaited_once()[m
[31m-        panel.select_template.assert_awaited_once()[m
[32m+[m[32m        self.assertIsNotNone(interaction.response.modal)[m
[32m+[m[32m        self.assertIsNone(interaction.response.deferred)[m
[32m+[m[32m        self.assertEqual(interaction.response.messages, [])[m
[32m+[m[32m        self.assertEqual(interaction.followup.messages, [])[m
[32m+[m
[32m+[m[32m    async def test_secondary_split_ping_defers_and_opens_template_selector(self):[m
[32m+[m[32m        self.db.execute([m
[32m+[m[32m            """[m
[32m+[m[32m            INSERT INTO callers (guild_id, user_id, added_by, created_at)[m
[32m+[m[32m            VALUES (?, ?, ?, ?)[m
[32m+[m[32m            """,[m
[32m+[m[32m            (self.guild.id, 100, 200, "2026-08-02T00:00:00+00:00"),[m
[32m+[m[32m        )[m
[32m+[m[32m        self.db.execute([m
[32m+[m[32m            """[m
[32m+[m[32m            INSERT INTO templates ([m
[32m+[m[32m                guild_id, name, activity_name, default_time, description,[m
[32m+[m[32m                publica, created_by, created_at[m
[32m+[m[32m            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)[m
[32m+[m[32m            """,[m
[32m+[m[32m            (self.guild.id, "Plantilla", "Avalon", "20:00", "Desc", 1, 100, "2026-08-02T00:00:00+00:00"),[m
[32m+[m[32m        )[m
[32m+[m[32m        secondary = CreatePingOptionsView(self.cog)[m
[32m+[m[32m        interaction = FakeInteraction(self.guild)[m
[32m+[m
[32m+[m[32m        with patch("g3nesys_bot.cogs.activities.is_caller_panel_subject", return_value=True):[m
[32m+[m[32m            await secondary.children[1].callback(interaction)[m
[32m+[m
[32m+[m[32m        self.assertTrue(interaction.response.deferred)[m
[32m+[m[32m        self.assertEqual(interaction.response.messages, [])[m
[32m+[m[32m        self.assertEqual(len(interaction.followup.messages), 1)[m
[32m+[m[32m        content, ephemeral, kwargs = interaction.followup.messages[0][m
[32m+[m[32m        self.assertTrue(ephemeral)[m
[32m+[m[32m        self.assertIn("Elige la plantilla", content)[m
[32m+[m[32m        self.assertEqual(kwargs["view"].__class__.__name__, "TemplateSelectView")[m
[32m+[m
[32m+[m[32m    async def test_legacy_and_secondary_buttons_share_internal_ping_methods(self):[m
[32m+[m[32m        with patch.object(self.cog, "open_quick_ping_from_panel", new_callable=AsyncMock) as quick,              patch.object(self.cog, "open_split_ping_from_panel", new_callable=AsyncMock) as split:[m
[32m+[m[32m            legacy = PingsLegacyPanelCallbacksView(self.cog)[m
[32m+[m[32m            secondary = CreatePingOptionsView(self.cog)[m
[32m+[m[32m            interaction = FakeInteraction(self.guild)[m
[32m+[m
[32m+[m[32m            await next(item for item in legacy.children if item.custom_id == "g3n:pings:create_activity").callback(interaction)[m
[32m+[m[32m            await next(item for item in legacy.children if item.custom_id == "g3n:pings:select_template").callback(interaction)[m
[32m+[m[32m            await secondary.children[0].callback(interaction)[m
[32m+[m[32m            await secondary.children[1].callback(interaction)[m
[32m+[m
[32m+[m[32m        self.assertEqual(quick.await_count, 2)[m
[32m+[m[32m        self.assertEqual(split.await_count, 2)[m
 [m
     async def test_manual_fines_still_work_when_automatic_penalties_are_disabled(self):[m
         self.assertFalse(AUTOMATIC_PENALTIES_ENABLED)[m
