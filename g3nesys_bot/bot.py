from __future__ import annotations

import asyncio
import logging

import discord
from discord.ext import commands

from .config import load_config
from .database import Database
from .services.backups import backup_loop

LOGGER = logging.getLogger("g3nesys")


class G3NBot(commands.Bot):
    def __init__(self):
        self.config = load_config()
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=self.config.command_prefix,
            intents=intents,
            help_command=None,
        )
        self.db = Database(self.config.database_path)
        self.backup_task: asyncio.Task | None = None

    async def setup_hook(self) -> None:
        self.db.init_schema()
        await self.load_extension("g3nesys_bot.cogs.settings")
        await self.load_extension("g3nesys_bot.cogs.activities")
        await self.load_extension("g3nesys_bot.cogs.fines")
        await self.load_extension("g3nesys_bot.cogs.bank")
        await self.load_extension("g3nesys_bot.cogs.admin")
        self.backup_task = asyncio.create_task(
            backup_loop(
                self.db,
                self.config.backup_dir,
                self.config.backup_every_minutes,
            )
        )

    async def on_ready(self) -> None:
        LOGGER.info("Bot conectado como %s", self.user)

    async def on_command_error(self, ctx: commands.Context, error: Exception) -> None:
        if isinstance(error, commands.CommandNotFound):
            return
        if isinstance(error, commands.MissingRequiredArgument):
            await ctx.reply(
                f"Falta un dato: `{error.param.name}`.",
                mention_author=False,
            )
            return
        if isinstance(error, commands.BadArgument):
            await ctx.reply("No pude leer uno de los datos del comando.", mention_author=False)
            return
        LOGGER.exception("Error en comando", exc_info=error)
        await ctx.reply(
            "Ocurrio un error interno. Lo deje registrado para revision.",
            mention_author=False,
        )

    async def close(self) -> None:
        if self.backup_task:
            self.backup_task.cancel()
        self.db.close()
        await super().close()


def run() -> None:
    logging.basicConfig(level=logging.INFO)
    bot = G3NBot()
    bot.run(bot.config.token)
