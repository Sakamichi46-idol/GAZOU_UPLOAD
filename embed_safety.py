"""Discord Embed limit helpers.

Discord imposes strict limits on embed titles, descriptions, fields and total
text length. These helpers make admin/user views resilient to unexpectedly long
or corrupted database values instead of failing the entire interaction.
"""
from __future__ import annotations

import logging
from typing import Any

import discord

LOGGER = logging.getLogger(__name__)

EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096
EMBED_FIELD_NAME_LIMIT = 256
EMBED_FIELD_VALUE_LIMIT = 1024
EMBED_FIELD_COUNT_LIMIT = 25
EMBED_FOOTER_LIMIT = 2048
EMBED_TOTAL_LIMIT = 6000


def safe_text(
    value: Any,
    limit: int,
    *,
    empty: str = "なし",
    suffix: str = "\n…省略しました",
    context: str = "",
) -> str:
    text = str(value or "").strip()
    if not text:
        return empty
    if len(text) <= limit:
        return text

    usable = max(0, limit - len(suffix))
    result = text[:usable] + suffix
    LOGGER.warning(
        "Discord Embed文字列を省略しました: context=%s original_length=%s limit=%s",
        context or "unknown",
        len(text),
        limit,
    )
    return result


def _embed_text_length(embed: discord.Embed) -> int:
    total = len(str(embed.title or "")) + len(str(embed.description or ""))
    for field in embed.fields:
        total += len(str(field.name or "")) + len(str(field.value or ""))
    if embed.footer:
        total += len(str(embed.footer.text or ""))
    if embed.author:
        total += len(str(embed.author.name or ""))
    return total


def safe_embed(
    *,
    title: Any = "",
    description: Any = "",
    color: discord.Color | int | None = None,
    url: str | None = None,
    context: str = "",
) -> discord.Embed:
    return discord.Embed(
        title=safe_text(
            title,
            EMBED_TITLE_LIMIT,
            empty="",
            suffix="…",
            context=f"{context}.title",
        ) or None,
        description=safe_text(
            description,
            EMBED_DESCRIPTION_LIMIT,
            empty="",
            context=f"{context}.description",
        ) or None,
        color=color,
        url=url,
    )


def safe_add_field(
    embed: discord.Embed,
    *,
    name: Any,
    value: Any,
    inline: bool = False,
    context: str = "",
) -> bool:
    """Add a field while respecting per-field, field-count and total limits."""
    if len(embed.fields) >= EMBED_FIELD_COUNT_LIMIT:
        LOGGER.warning("Embedフィールド上限のため追加を省略: context=%s", context or "unknown")
        return False

    field_name = safe_text(
        name,
        EMBED_FIELD_NAME_LIMIT,
        empty="項目",
        suffix="…",
        context=f"{context}.name",
    )

    remaining_total = EMBED_TOTAL_LIMIT - _embed_text_length(embed) - len(field_name)
    if remaining_total <= 0:
        LOGGER.warning("Embed総文字数上限のためフィールド追加を省略: context=%s", context or "unknown")
        return False

    value_limit = min(EMBED_FIELD_VALUE_LIMIT, remaining_total)
    if value_limit < 1:
        return False

    field_value = safe_text(
        value,
        value_limit,
        empty="なし",
        context=f"{context}.value",
    )
    embed.add_field(name=field_name, value=field_value, inline=inline)
    return True


def safe_set_footer(embed: discord.Embed, *, text: Any, context: str = "") -> None:
    remaining = max(0, EMBED_TOTAL_LIMIT - _embed_text_length(embed))
    limit = min(EMBED_FOOTER_LIMIT, remaining)
    if limit <= 0:
        return
    embed.set_footer(
        text=safe_text(
            text,
            limit,
            empty="",
            context=f"{context}.footer",
        )
    )
