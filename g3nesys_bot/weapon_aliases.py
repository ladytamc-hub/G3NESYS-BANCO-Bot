from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class WeaponAlias:
    key: str
    emoji: str
    display_name: str
    aliases: tuple[str, ...]


WEAPON_ALIASES: dict[str, dict[str, Any]] = {
    "arbol": {
        "emoji": "<:arbol:1520621224391217253>",
        "display_name": "Árbol",
        "aliases": ["arbol"],
    },
    "ballesta": {
        "emoji": "<:ballesta:1521364639584096468>",
        "display_name": "Ballesta",
        "aliases": ["ballesta"],
    },
    "caido": {
        "emoji": "<:caido:1521365082854920344>",
        "display_name": "Caído",
        "aliases": ["caido", "caído"],
    },
    "cancion": {
        "emoji": "<:cancion:1512964764681240676>",
        "display_name": "Canción",
        "aliases": ["cancion"],
    },
    "caza": {
        "emoji": "<:caza:1512963821134680305>",
        "display_name": "Caza Espíritus",
        "aliases": ["caza", "caza espiritus"],
    },
    "cobra": {
        "emoji": "<:cobra:1520164986222153798>",
        "display_name": "Cobra",
        "aliases": ["cobra"],
    },
    "escarcha": {
        "emoji": "<:escarcha:1512965418036236428>",
        "display_name": "Escarcha",
        "aliases": ["escarcha"],
    },
    "exaltado": {
        "emoji": "<:exaltado:1520621560577392780>",
        "display_name": "Bastón Exaltado",
        "aliases": ["exaltado", "baston exaltado"],
    },
    "falce": {
        "emoji": "<:falce:1520164912997863454>",
        "display_name": "Falce",
        "aliases": ["falce"],
    },
    "golem": {
        "emoji": "<:golem:1512956276940865687>",
        "display_name": "Gólem",
        "aliases": ["golem"],
    },
    "gritogelido": {
        "emoji": "<:gritogelido:1521364990412722439>",
        "display_name": "Grito Gélido",
        "aliases": ["gritogelido", "grito gelido", "grito gélido"],
    },
    "guja": {
        "emoji": "<:guja:1512964112924016781>",
        "display_name": "Guja",
        "aliases": ["guja"],
    },
    "incu": {
        "emoji": "<:incu:1512956472189911122>",
        "display_name": "Íncubo",
        "aliases": ["incu", "incubo"],
    },
    "infortunio": {
        "emoji": "<:infortunio:1520621438359306310>",
        "display_name": "Infortunio",
        "aliases": ["infortunio"],
    },
    "jura": {
        "emoji": "<:jura:1512963578754236547>",
        "display_name": "Juradores",
        "aliases": ["jura", "juradores"],
    },
    "lecho": {
        "emoji": "<:lecho:1512964711409385473>",
        "display_name": "Lecho",
        "aliases": ["lecho"],
    },
    "looter": {
        "emoji": "<:looter:1521365127406817290>",
        "display_name": "Looter",
        "aliases": ["looter"],
    },
    "maldivida": {
        "emoji": "<:maldivida:1512956599348625449>",
        "display_name": "Maldición de Vida",
        "aliases": ["maldivida", "maldi de vida"],
    },
    "manojusticia": {
        "emoji": "<:manojusticia:1512964658535993384>",
        "display_name": "Mano de la Justicia",
        "aliases": ["manojusticia", "mano de la justicia"],
    },
    "martillo1h": {
        "emoji": "<:martillo1h:1521314781729001642>",
        "display_name": "Martillo 1H",
        "aliases": ["martillo", "martillo una mano", "martillo 1h"],
    },
    "martillolargo": {
        "emoji": "<:martillolargo:1512964207388262460>",
        "display_name": "Martillo Largo",
        "aliases": ["martillolargo", "martillo largo"],
    },
    "martillorelampago": {
        "emoji": "<:martillorelampago:1520165145073029242>",
        "display_name": "Martillo Relámpago",
        "aliases": ["martillorelampago", "martillo relampago"],
    },
    "maza1h": {
        "emoji": "<:maza1h:1521314729203597453>",
        "display_name": "Maza 1H",
        "aliases": ["maza", "maza una mano", "maza 1h"],
    },
    "monarca": {
        "emoji": "<:monarca:1520621356083839079>",
        "display_name": "Monarca",
        "aliases": ["monarca"],
    },
    "paratiempo": {
        "emoji": "<:paratiempo:1512963641551360121>",
        "display_name": "Paratiempo",
        "aliases": ["paratiempo"],
    },
    "pasillo": {
        "emoji": "<:pasillo:1512963775169298492>",
        "display_name": "Pasillo",
        "aliases": ["pasillo"],
    },
    "patas": {
        "emoji": "<:patas:1520621292020039821>",
        "display_name": "Patas de Oso",
        "aliases": ["patas", "patas de oso"],
    },
    "perfora": {
        "emoji": "<:perfora:1512964916217381006>",
        "display_name": "Perfora",
        "aliases": ["perfora"],
    },
    "prisma": {
        "emoji": "<:prisma:1512956537440698520>",
        "display_name": "Prisma",
        "aliases": ["prisma"],
    },
    "puas": {
        "emoji": "<:puas:1512964604806823956>",
        "display_name": "Púas",
        "aliases": ["puas"],
    },
    "putre": {
        "emoji": "<:putre:1512956658031132865>",
        "display_name": "Putrefacto",
        "aliases": ["putre", "putrefacto", "baston putrefacto"],
    },
    "raiz": {
        "emoji": "<:raiz:1520165299612029030>",
        "display_name": "Raíz",
        "aliases": ["raiz"],
    },
    "redencion": {
        "emoji": "<:redencion:1512963986998693950>",
        "display_name": "Redención",
        "aliases": ["redencion"],
    },
    "rompe": {
        "emoji": "<:rompe:1512956702880960603>",
        "display_name": "Rompereinos",
        "aliases": ["rompe", "rompereinos"],
    },
    "santi": {
        "emoji": "<:santi:1512963729388601364>",
        "display_name": "Santificador",
        "aliases": ["santi", "santificador"],
    },
    "sc": {
        "emoji": "<:sc:1520621883848921208>",
        "display_name": "Shadowcaller",
        "aliases": ["sc", "shadowcaller", "invocador"],
    },
}


def normalize_weapon_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKD", str(value).casefold().strip())
    without_accents = "".join(
        character for character in normalized if not unicodedata.combining(character)
    )
    return re.sub(r"\s+", " ", without_accents).strip()


def _build_alias_index() -> dict[str, WeaponAlias]:
    index: dict[str, WeaponAlias] = {}
    for key, data in WEAPON_ALIASES.items():
        weapon = WeaponAlias(
            key=key,
            emoji=str(data["emoji"]),
            display_name=str(data["display_name"]),
            aliases=tuple(str(alias) for alias in data["aliases"]),
        )
        aliases = {normalize_weapon_text(key)}
        aliases.update(normalize_weapon_text(alias) for alias in weapon.aliases)
        for alias in aliases:
            if not alias:
                continue
            if alias in index:
                raise ValueError(f"Alias de arma duplicado: {alias}")
            index[alias] = weapon
    return index


WEAPON_ALIAS_INDEX = _build_alias_index()


def resolve_weapon_alias(value: str) -> WeaponAlias | None:
    return WEAPON_ALIAS_INDEX.get(normalize_weapon_text(value))
