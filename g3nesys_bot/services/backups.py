from __future__ import annotations

import asyncio
import shutil
from pathlib import Path

from ..database import Database
from ..utils import utc_now_iso


async def backup_loop(db: Database, backup_dir: Path, every_minutes: int) -> None:
    backup_dir.mkdir(parents=True, exist_ok=True)
    interval = max(5, every_minutes) * 60
    while True:
        await asyncio.sleep(interval)
        timestamp = utc_now_iso().replace(":", "-")
        target = backup_dir / f"g3nesys-{timestamp}.sqlite3"
        shutil.copy2(db.path, target)
