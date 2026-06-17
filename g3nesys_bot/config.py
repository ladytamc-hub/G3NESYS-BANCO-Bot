from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class AppConfig:
    token: str
    database_path: Path
    command_prefix: str
    backup_dir: Path
    backup_every_minutes: int


def load_config() -> AppConfig:
    load_dotenv()
    token = os.getenv("DISCORD_TOKEN", "").strip()
    if not token:
        raise RuntimeError("Falta DISCORD_TOKEN en las variables de entorno.")

    database_path = Path(os.getenv("DATABASE_PATH", "data/g3nesys.sqlite3"))
    backup_dir = Path(os.getenv("BACKUP_DIR", "data/backups"))
    backup_every_minutes = int(os.getenv("BACKUP_EVERY_MINUTES", "360"))

    return AppConfig(
        token=token,
        database_path=database_path,
        command_prefix=os.getenv("COMMAND_PREFIX", "!"),
        backup_dir=backup_dir,
        backup_every_minutes=backup_every_minutes,
    )
