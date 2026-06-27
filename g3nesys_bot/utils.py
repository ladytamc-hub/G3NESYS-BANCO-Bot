from __future__ import annotations

import re
from datetime import datetime, timezone

import discord


CUSTOM_EMOJI_PLACEHOLDER_RE = re.compile(r"(?<!<)(?<!<a):([A-Za-z0-9_]{2,32}):")
CUSTOM_EMOJI_TOKEN_RE = re.compile(r":([A-Za-z0-9_]{2,32}):\Z")


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


def is_custom_emoji_placeholder(value: str) -> bool:
    return bool(CUSTOM_EMOJI_TOKEN_RE.fullmatch((value or "").strip()))


def resolve_custom_emojis(text: str | None, guild: discord.Guild | None) -> str | None:
    if text is None or guild is None or ":" not in text:
        return text
    emoji_by_name = {emoji.name: emoji for emoji in getattr(guild, "emojis", [])}
    emoji_by_lower_name = {emoji.name.lower(): emoji for emoji in getattr(guild, "emojis", [])}
    if not emoji_by_name:
        return text

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        emoji = emoji_by_name.get(name) or emoji_by_lower_name.get(name.lower())
        return str(emoji) if emoji is not None else match.group(0)

    return CUSTOM_EMOJI_PLACEHOLDER_RE.sub(replace, text)


def resolve_custom_emojis_in_embed(
    embed: discord.Embed | None,
    guild: discord.Guild | None,
) -> discord.Embed | None:
    if embed is None or guild is None:
        return embed
    data = embed.to_dict()
    for key in ("title", "description"):
        if key in data:
            data[key] = resolve_custom_emojis(data[key], guild)
    if "footer" in data and "text" in data["footer"]:
        data["footer"]["text"] = resolve_custom_emojis(data["footer"]["text"], guild)
    if "author" in data and "name" in data["author"]:
        data["author"]["name"] = resolve_custom_emojis(data["author"]["name"], guild)
    for field in data.get("fields", []):
        field["name"] = resolve_custom_emojis(field["name"], guild)
        field["value"] = resolve_custom_emojis(field["value"], guild)
    return discord.Embed.from_dict(data)


def resolve_custom_emojis_in_send_kwargs(
    kwargs: dict,
    guild: discord.Guild | None,
) -> dict:
    if guild is None:
        return kwargs
    resolved = dict(kwargs)
    if resolved.get("embed") is not None:
        resolved["embed"] = resolve_custom_emojis_in_embed(resolved["embed"], guild)
    if resolved.get("embeds") is not None:
        resolved["embeds"] = [
            resolve_custom_emojis_in_embed(embed, guild)
            for embed in resolved["embeds"]
        ]
    return resolved


def parse_channel_id(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"(\d{15,25})", raw)
    if not match:
        return None
    return int(match.group(1))
