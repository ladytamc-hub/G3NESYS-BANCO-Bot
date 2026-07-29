from __future__ import annotations

import re

import discord

_BAD_TEXT_MARKERS = (chr(0x00F0), chr(0x00C3), chr(0x00C2), chr(0x00E2))
_ALIAS_RE = re.compile(r"^:[A-Za-z0-9_+-]+:$")
_CUSTOM_EMOJI_RE = re.compile(r"^<?a?:[A-Za-z0-9_]+:[0-9]{15,25}>?$")


def validate_component_emoji(emoji: object, *, label: str = "") -> None:
    if emoji is None:
        return
    if isinstance(emoji, discord.PartialEmoji):
        if emoji.id is None and emoji.name:
            validate_component_emoji(emoji.name, label=label)
            return
        if emoji.id is None or not str(emoji.id).isdigit():
            raise ValueError(f"Emoji personalizado invalido en {label!r}: {emoji!r}")
        return
    value = str(emoji)
    if not value:
        return
    if any(marker in value for marker in _BAD_TEXT_MARKERS):
        raise ValueError(f"Emoji con codificacion danada en {label!r}: {value!r}")
    if _ALIAS_RE.match(value):
        raise ValueError(f"Alias de emoji no permitido en {label!r}: {value!r}")
    if "<" in value or ">" in value:
        if not _CUSTOM_EMOJI_RE.match(value):
            raise ValueError(f"Emoji personalizado invalido en {label!r}: {value!r}")
        return
    if any(ch.isspace() for ch in value):
        raise ValueError(f"El emoji contiene texto o espacios en {label!r}: {value!r}")
    if any(ch.isalnum() for ch in value):
        raise ValueError(f"El emoji contiene texto en {label!r}: {value!r}")


def validate_view_components(view: discord.ui.View) -> None:
    components = view.to_components()
    if len(view.children) > 25:
        raise ValueError(f"{type(view).__name__} supera 25 componentes")
    if len(components) > 5:
        raise ValueError(f"{type(view).__name__} supera 5 filas")
    for row in components:
        if len(row.get("components", [])) > 5:
            raise ValueError(f"{type(view).__name__} tiene una fila con mas de 5 componentes")
    for child in view.children:
        label = getattr(child, "label", "") or getattr(child, "placeholder", "") or type(child).__name__
        if any(marker in str(label) for marker in _BAD_TEXT_MARKERS):
            raise ValueError(f"Label con codificacion danada en {type(view).__name__}: {label!r}")
        validate_component_emoji(getattr(child, "emoji", None), label=str(label))
        for option in getattr(child, "options", []) or []:
            if any(marker in str(option.label) for marker in _BAD_TEXT_MARKERS):
                raise ValueError(f"Opcion con codificacion danada en {type(view).__name__}: {option.label!r}")
            validate_component_emoji(getattr(option, "emoji", None), label=str(option.label))
