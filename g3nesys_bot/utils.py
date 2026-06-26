from __future__ import annotations

import re
from datetime import datetime, timezone


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_int_amount(raw: str) -> int:
    cleaned = re.sub(r"[^0-9]", "", raw or "")
    if not cleaned:
        raise ValueError("Monto invalido.")
    amount = int(cleaned)
    if amount <= 0:
        raise ValueError("El monto debe ser mayor que cero.")
    return amount


def format_amount(amount: int, currency: str = "plata") -> str:
    return f"{int(amount):,} {currency}".replace(",", ".")


def split_csv_ids(value: str | None) -> set[int]:
    result: set[int] = set()
    if not value:
        return result
    for match in re.findall(r"\d+", value):
        result.add(int(match))
    return result


def join_csv_ids(values: set[int]) -> str:
    return ",".join(str(value) for value in sorted(values))


def normalize_key(value: str) -> str:
    key = re.sub(r"[^a-zA-Z0-9_]+", "_", value.strip().lower())
    key = re.sub(r"_+", "_", key).strip("_")
    return key or "rol"


def parse_channel_id(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"(\d{15,25})", raw)
    if not match:
        return None
    return int(match.group(1))
