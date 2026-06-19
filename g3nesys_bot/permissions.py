from __future__ import annotations

import discord
from discord.ext import commands

from .database import Database
from .services.callers import is_caller_penalized
from .utils import split_csv_ids


def _member_from_subject(subject: commands.Context | discord.Interaction) -> discord.Member | None:
    user = getattr(subject, "author", None) or getattr(subject, "user", None)
    if isinstance(user, discord.Member):
        return user
    return None


def _guild_from_subject(subject: commands.Context | discord.Interaction) -> discord.Guild | None:
    return getattr(subject, "guild", None)


def has_named_role(member: discord.Member, role_name: str) -> bool:
    return any(role.name == role_name for role in member.roles)


def has_any_configured_role(member: discord.Member, role_ids_csv: str) -> bool:
    role_ids = split_csv_ids(role_ids_csv)
    if not role_ids:
        return False
    return any(role.id in role_ids for role in member.roles)


def is_admin_subject(db: Database, subject: commands.Context | discord.Interaction) -> bool:
    guild = _guild_from_subject(subject)
    member = _member_from_subject(subject)
    if guild is None or member is None:
        return False
    override = db.fetch_one(
        "SELECT authorized FROM admin_access WHERE guild_id = ? AND user_id = ?",
        (guild.id, member.id),
    )
    if override is not None:
        return bool(override["authorized"])
    if member.guild_permissions.administrator:
        return True
    return has_any_configured_role(member, db.get_setting(guild.id, "admin_role_ids"))


def is_caller_subject(db: Database, subject: commands.Context | discord.Interaction) -> bool:
    guild = _guild_from_subject(subject)
    member = _member_from_subject(subject)
    if guild is None or member is None:
        return False
    if is_admin_subject(db, subject):
        return True
    row = db.fetch_one(
        "SELECT 1 FROM callers WHERE guild_id = ? AND user_id = ?",
        (guild.id, member.id),
    )
    return row is not None and not is_caller_penalized(db, guild.id, member.id)


def can_manage_activity(
    db: Database,
    subject: commands.Context | discord.Interaction,
    caller_id: int,
) -> bool:
    member = _member_from_subject(subject)
    if member is None:
        return False
    if is_admin_subject(db, subject):
        return True
    return member.id == caller_id and is_caller_subject(db, subject)


def has_bank_access(db: Database, member: discord.Member) -> bool:
    member_role = db.get_setting(member.guild.id, "member_role_name")
    guest_role = db.get_setting(member.guild.id, "guest_role_name")
    return has_named_role(member, member_role) or has_named_role(member, guest_role)


def is_full_member(db: Database, member: discord.Member) -> bool:
    member_role = db.get_setting(member.guild.id, "member_role_name")
    return has_named_role(member, member_role)


async def require_admin_context(ctx: commands.Context, db: Database) -> bool:
    if is_admin_subject(db, ctx):
        return True
    await ctx.reply("Solo admins autorizados pueden hacer esto.", mention_author=False)
    return False


async def require_caller_context(ctx: commands.Context, db: Database) -> bool:
    if is_caller_subject(db, ctx):
        return True
    if ctx.guild is not None and is_caller_penalized(db, ctx.guild.id, ctx.author.id):
        await ctx.reply(
            "Tu acceso de caller esta suspendido por reputacion. "
            "Un administrador debe retirar la penalizacion desde el Panel Administrativo.",
            mention_author=False,
        )
        return False
    await ctx.reply("Solo callers autorizados o admins pueden hacer esto.", mention_author=False)
    return False
