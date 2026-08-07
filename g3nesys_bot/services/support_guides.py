from __future__ import annotations

from dataclasses import dataclass

import discord


@dataclass(frozen=True)
class SupportGuide:
    key: str
    title: str
    emoji: str
    description: str
    instructions: tuple[str, ...] = ()
    image_url: str = ""
    video_url: str = ""


GUIDES: dict[str, SupportGuide] = {
    "bank": SupportGuide(
        key="bank",
        title="Banco",
        emoji="💰",
        description="Guía próximamente disponible.",
    ),
    "activities": SupportGuide(
        key="activities",
        title="Pings y actividades",
        emoji="📣",
        description="Guía próximamente disponible.",
    ),
    "splits": SupportGuide(
        key="splits",
        title="Splits",
        emoji="💸",
        description="Guía próximamente disponible.",
    ),
    "withdrawals": SupportGuide(
        key="withdrawals",
        title="Solicitudes de cobro",
        emoji="🧾",
        description="Guía próximamente disponible.",
    ),
    "claims": SupportGuide(
        key="claims",
        title="Reclamaciones",
        emoji="🎫",
        description="Guía próximamente disponible.",
    ),
    "media": SupportGuide(
        key="media",
        title="Audio visual",
        emoji="🎧",
        description=(
            "Guía próximamente disponible. Esta sección está preparada para "
            "tutoriales en video, material gráfico y recursos multimedia."
        ),
    ),
    "panels": SupportGuide(
        key="panels",
        title="Paneles",
        emoji="🧩",
        description=(
            "Guía próximamente disponible. Esta sección explicará qué función "
            "cumple cada panel del bot y cómo acceder a sus opciones."
        ),
    ),
}


def build_guides_home_embed() -> discord.Embed:
    embed = discord.Embed(
        title="📚 Guías de usuario",
        description="Selecciona una categoría para consultar su guía.",
        color=discord.Color.blurple(),
    )
    for guide in GUIDES.values():
        embed.add_field(
            name=f"{guide.emoji} {guide.title}",
            value=guide.description[:180],
            inline=False,
        )
    return embed


def build_guide_embed(guide: SupportGuide) -> discord.Embed:
    embed = discord.Embed(
        title=f"{guide.emoji} {guide.title}",
        description=guide.description,
        color=discord.Color.blurple(),
    )
    if guide.instructions:
        embed.add_field(
            name="Pasos",
            value="\n".join(f"{index}. {step}" for index, step in enumerate(guide.instructions, start=1))[:1024],
            inline=False,
        )
    else:
        embed.add_field(name="Estado", value="Guía próximamente disponible.", inline=False)
    if guide.image_url:
        embed.set_image(url=guide.image_url)
    return embed

