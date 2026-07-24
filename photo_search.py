import os
from typing import Any

import discord

from photo_database import search_photo_images, search_photo_images_by_author
from bucket_storage import bucket_is_configured, create_presigned_get_url


# =========================
# 検索設定
# =========================

DEFAULT_SEARCH_LIMIT = 10
MAX_SEARCH_LIMIT = 20


# =========================
# 共通処理
# =========================

def shorten_text(
    value: Any,
    max_length: int,
) -> str:
    """
    Discord表示用に文字列を指定文字数以内へ収める。
    """

    text = str(
        value or ""
    ).strip()

    if len(text) <= max_length:

        return text

    return (
        text[: max_length - 1]
        + "…"
    )


def build_search_embed(
    result: dict[str, Any],
    *,
    query: str,
    index: int,
    total: int,
) -> discord.Embed:
    """
    写真検索結果1件分のEmbedを作成する。
    """

    title = (
        result.get("title")
        or "無題"
    )

    blog_url = str(
        result.get("blog_url")
        or ""
    ).strip()

    embed = discord.Embed(
        title=shorten_text(
            title,
            256,
        ),
        url=blog_url or None,
        color=0x00AAFF,
    )

    group_name = (
        result.get("group_name")
        or "不明"
    )

    member_name = (
        result.get("member_name")
        or "不明"
    )

    published_at = (
        result.get("published_at")
        or "不明"
    )

    embed.add_field(
        name="🏷️ グループ",
        value=shorten_text(
            group_name,
            1024,
        ),
        inline=True,
    )

    embed.add_field(
        name="👤 ブログメンバー",
        value=shorten_text(
            member_name,
            1024,
        ),
        inline=True,
    )

    embed.add_field(
        name="📅 投稿日時",
        value=shorten_text(
            published_at,
            1024,
        ),
        inline=False,
    )

    confirmed_people = str(result.get("confirmed_people") or "").strip()
    candidate_people = str(result.get("candidate_people") or "").strip()

    if confirmed_people:
        embed.add_field(
            name="✅ 写っている人物（確定）",
            value=shorten_text(confirmed_people, 1024),
            inline=False,
        )
    elif candidate_people:
        embed.add_field(
            name="🧐 人物候補（未確認）",
            value=shorten_text(candidate_people, 1024),
            inline=False,
        )

    details = []

    for label, key in (
        ("服装", "clothing"),
        ("表情", "expression"),
        ("背景", "background"),
        ("ポーズ", "pose"),
        ("物", "objects"),
    ):

        value = str(
            result.get(key)
            or ""
        ).strip()

        if value:

            details.append(
                f"**{label}:** {value}"
            )

    if details:

        embed.add_field(
            name="🔎 AI解析",
            value=shorten_text(
                "\n".join(details),
                1024,
            ),
            inline=False,
        )

    ai_tags = str(
        result.get("ai_tags")
        or ""
    ).strip()

    manual_tags = str(
        result.get("manual_tags")
        or ""
    ).strip()

    tag_lines = []

    if ai_tags:

        tag_lines.append(
            f"AI: {ai_tags}"
        )

    if manual_tags:

        tag_lines.append(
            f"手動: {manual_tags}"
        )

    if tag_lines:

        embed.add_field(
            name="🏷️ タグ",
            value=shorten_text(
                "\n".join(tag_lines),
                1024,
            ),
            inline=False,
        )

    image_id = result.get(
        "id",
        0,
    )

    image_index = result.get(
        "image_index",
        0,
    )

    embed.set_footer(
        text=(
            f"検索: {query}"
            f" • {index}/{total}"
            f" • 画像ID {image_id}"
            f" • 記事内 {image_index}枚目"
        )
    )

    return embed



def get_display_image_url(result: dict[str, Any]) -> str:
    """Prefer durable Bucket content; fall back to the original source URL."""
    bucket_key = str(result.get("bucket_key") or "").strip()
    if bucket_key and bucket_is_configured():
        try:
            return create_presigned_get_url(bucket_key)
        except Exception as error:
            print("Bucket署名付きURL作成エラー:", error)
    image_url = str(result.get("image_url") or "").strip()
    return image_url if image_url.startswith(("http://", "https://")) else ""

# =========================
# 検索結果送信
# =========================

async def send_photo_search_results(
    ctx,
    query: str,
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> None:
    """
    写真DBを検索し、Discordへ結果を送信する。

    使用例:
        await send_photo_search_results(
            ctx,
            "菅原咲月 浴衣",
        )
    """

    clean_query = str(
        query
    ).strip()

    if not clean_query:

        await ctx.send(
            "⚠️ 検索キーワードを入力してください。\n"
            "例: `!photo_search 菅原咲月 浴衣`"
        )

        return

    try:

        search_limit = max(
            1,
            min(
                int(limit),
                MAX_SEARCH_LIMIT,
            ),
        )

    except (
        TypeError,
        ValueError,
    ):

        search_limit = (
            DEFAULT_SEARCH_LIMIT
        )

    try:

        results = search_photo_images(
            clean_query,
            limit=search_limit,
        )

    except Exception as error:

        print(
            "写真検索DBエラー:",
            error,
        )

        await ctx.send(
            "⚠️ 写真検索中にエラーが発生しました。\n"
            f"`{shorten_text(error, 1500)}`"
        )

        return

    if not results:

        await ctx.send(
            "🔍 該当する写真が見つかりませんでした。\n"
            f"検索: `{shorten_text(clean_query, 1000)}`"
        )

        return

    await ctx.send(
        "🔍 写真検索結果\n"
        f"検索: `{shorten_text(clean_query, 1000)}`\n"
        f"表示件数: **{len(results)}件**"
    )

    total = len(
        results
    )

    for index, result in enumerate(
        results,
        start=1,
    ):

        embed = build_search_embed(
            result,
            query=clean_query,
            index=index,
            total=total,
        )

        # 通信量節約のため、保存済みローカル画像を再アップロードせず、
        # 元画像URLをDiscordに直接表示させる。
        image_url = get_display_image_url(result)

        if image_url:
            embed.set_image(url=image_url)
        else:
            embed.description = (embed.description or "") + (
                "\n\n⚠️ 表示用URLがないため画像を表示できません。"
            )

        await ctx.send(embed=embed)


async def send_photo_author_search_results(ctx, author_name: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    clean_name = str(author_name or "").strip()
    if not clean_name:
        await ctx.send("⚠️ 投稿者名を入力してください。\n例: `!photo_search_author 菅原咲月`")
        return
    try:
        results = search_photo_images_by_author(clean_name, limit=max(1, min(int(limit), MAX_SEARCH_LIMIT)))
    except Exception as error:
        await ctx.send(f"⚠️ 投稿者検索中にエラーが発生しました。\n`{shorten_text(error, 1500)}`")
        return
    if not results:
        await ctx.send(f"🔍 **{clean_name}** のブログ画像は見つかりませんでした。")
        return
    await ctx.send(f"📝 **ブログ投稿者検索**: `{shorten_text(clean_name, 1000)}`\n表示件数: **{len(results)}件**")
    for index, result in enumerate(results, start=1):
        embed = build_search_embed(result, query=f"投稿者:{clean_name}", index=index, total=len(results))
        image_url = get_display_image_url(result)
        if image_url:
            embed.set_image(url=image_url)
        await ctx.send(embed=embed)
