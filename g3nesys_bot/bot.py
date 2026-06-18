from __future__ import annotations

import asyncio
import logging
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import discord
from discord.ext import commands

from .config import AppConfig, load_config
from .database import Database
from .services.backups import backup_loop

LOGGER = logging.getLogger("g3nesys")


class BotAlreadyRunningError(RuntimeError):
    pass


@contextmanager
def single_instance_lock(database_path: Path) -> Iterator[None]:
    lock_path = database_path.with_name(f"{database_path.name}.instance.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"0")
        handle.flush()
    handle.seek(0)

    try:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.close()
        raise BotAlreadyRunningError(
            "Ya existe otra instancia local de G3NESYS usando esta base de datos."
        ) from exc

    try:
        yield
    finally:
        handle.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class G3NBot(commands.Bot):
    def __init__(self, config: AppConfig | None = None):
        self.config = config or load_config()
        intents = discord.Intents.default()
        intents.members = True
        intents.guilds = True
        intents.message_content = True
        intents.voice_states = True
        super().__init__(
            command_prefix=self.config.command_prefix,
            intents=intents,
            help_command=None,
            case_insensitive=True,
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
        original = getattr(error, "original", error)
        if isinstance(original, commands.CommandNotFound):
            attempted = (ctx.invoked_with or "").strip()
            if attempted and self.get_command(attempted) is not None:
                LOGGER.warning(
                    "Se ignoro un CommandNotFound incorrecto para un comando registrado: %s",
                    attempted,
                )
                return
            shown_command = f"{ctx.prefix or self.config.command_prefix}{attempted}" if attempted else "ese comando"
            await ctx.reply(
                f"No reconozco `{shown_command}`. Revisa cómo está escrito o usa "
                f"`{self.config.command_prefix}ayuda` para ver los comandos disponibles.",
                mention_author=False,
            )
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
    config = load_config()
    try:
        with single_instance_lock(config.database_path):
            bot = G3NBot(config)
            bot.run(config.token)
    except BotAlreadyRunningError as exc:
        LOGGER.error("%s Cierra la otra ventana del bot antes de volver a iniciarlo.", exc)
