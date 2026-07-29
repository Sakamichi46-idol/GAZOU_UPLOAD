from __future__ import annotations

from typing import Any, Callable

import asyncio
import discord

from bucket_storage import bucket_is_configured, create_presigned_get_url
from photo_database import (
    search_photo_images,
    search_photo_images_by_author,
    search_photo_images_by_blog,
    search_photo_images_by_person,
    search_photo_images_by_tag,
)


DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
VIEW_TIMEOUT_SECONDS = 300


def shorten_text(value: Any, max_length: int) -> str:
    text = str(value or "").strip()
    if len(text) <= max_length:
        return text
    return text[: max_length - 1] + "…"


def build_search_embed(
    result: dict[str, Any],
    *,
    query: str,
    index: int,
    total: int,
    search_label: str = "検索",
) -> discord.Embed:
    title = result.get("title") or "無題"
    blog_url = str(result.get("blog_url") or "").strip()

    embed = discord.Embed(
        title=shorten_text(title, 256),
        url=blog_url or None,
        color=0x00AAFF,
    )

    embed.add_field(
        name="🏷️ グループ",
        value=shorten_text(result.get("group_name") or "不明", 1024),
        inline=True,
    )
    embed.add_field(
        name="👤 ブログ投稿者",
        value=shorten_text(result.get("member_name") or "不明", 1024),
        inline=True,
    )
    embed.add_field(
        name="📅 投稿日時",
        value=shorten_text(result.get("published_at") or "不明", 1024),
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

    details: list[str] = []
    for label, key in (
        ("服装", "clothing"),
        ("表情", "expression"),
        ("背景", "background"),
        ("ポーズ", "pose"),
        ("物", "objects"),
    ):
        value = str(result.get(key) or "").strip()
        if value:
            details.append(f"**{label}:** {value}")

    if details:
        embed.add_field(
            name="🔎 AI解析",
            value=shorten_text("\n".join(details), 1024),
            inline=False,
        )

    tag_lines: list[str] = []
    ai_tags = str(result.get("ai_tags") or "").strip()
    manual_tags = str(result.get("manual_tags") or "").strip()
    if ai_tags:
        tag_lines.append(f"AI: {ai_tags}")
    if manual_tags:
        tag_lines.append(f"手動: {manual_tags}")
    if tag_lines:
        embed.add_field(
            name="🏷️ タグ",
            value=shorten_text("\n".join(tag_lines), 1024),
            inline=False,
        )

    image_id = result.get("id", 0)
    image_index = result.get("image_index", 0)
    embed.set_footer(
        text=(
            f"{search_label}: {query} • {index}/{total}"
            f" • 画像ID {image_id} • 記事内 {image_index}枚目"
        )
    )
    return embed


def get_display_image_url(result: dict[str, Any]) -> str:
    """Bucketを優先し、失敗時だけ元URLへフォールバックする。"""
    bucket_key = str(result.get("bucket_key") or "").strip()
    if bucket_key and bucket_is_configured():
        try:
            return create_presigned_get_url(bucket_key)
        except Exception as error:
            print("Bucket署名付きURL作成エラー:", error)

    image_url = str(result.get("image_url") or "").strip()
    return image_url if image_url.startswith(("http://", "https://")) else ""


class PhotoSearchResultsView(discord.ui.View):
    """1メッセージを編集して検索結果を切り替え、通信量と投稿数を抑える。"""

    def __init__(
        self,
        *,
        owner_id: int,
        results: list[dict[str, Any]],
        query: str,
        search_label: str,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.results = results
        self.query = query
        self.search_label = search_label
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.results) - 1

    def current_embed(self) -> discord.Embed:
        result = self.results[self.index]
        embed = build_search_embed(
            result,
            query=self.query,
            index=self.index + 1,
            total=len(self.results),
            search_label=self.search_label,
        )
        image_url = get_display_image_url(result)
        if image_url:
            embed.set_image(url=image_url)
        else:
            embed.description = (embed.description or "") + "\n\n⚠️ 表示用URLがありません。"
        return embed

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果を操作できるのは、コマンドを実行した人だけです。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.index = min(len(self.results) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="終了", emoji="⏹️", style=discord.ButtonStyle.danger)
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


async def _send_search(
    ctx,
    *,
    query: str,
    search_label: str,
    usage: str,
    search_function: Callable[[str, int], list[dict[str, Any]]],
    limit: int = DEFAULT_SEARCH_LIMIT,
) -> None:
    clean_query = str(query or "").strip()
    if not clean_query:
        await ctx.send(f"⚠️ 検索語を入力してください。\n使い方: `{usage}`")
        return

    try:
        safe_limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
    except (TypeError, ValueError):
        safe_limit = DEFAULT_SEARCH_LIMIT

    try:
        results = await asyncio.to_thread(search_function, clean_query, safe_limit)
    except Exception as error:
        print(f"{search_label}DBエラー:", error)
        await ctx.send(
            f"⚠️ {search_label}中にエラーが発生しました。\n"
            f"`{shorten_text(error, 1500)}`"
        )
        return

    if not results:
        await ctx.send(
            f"🔍 該当する写真が見つかりませんでした。\n"
            f"{search_label}: `{shorten_text(clean_query, 1000)}`"
        )
        return

    view = PhotoSearchResultsView(
        owner_id=ctx.author.id,
        results=results,
        query=clean_query,
        search_label=search_label,
    )
    message = await ctx.send(
        content=(
            f"🔍 **{search_label}結果**\n"
            f"検索語: `{shorten_text(clean_query, 1000)}`\n"
            f"取得件数: **{len(results)}件**（ボタンで切り替え）"
        ),
        embed=view.current_embed(),
        view=view,
    )
    view.message = message


async def send_photo_search_results(ctx, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    await _send_search(
        ctx,
        query=query,
        search_label="横断検索",
        usage="!search 菅原咲月 浴衣",
        search_function=search_photo_images,
        limit=limit,
    )


async def send_photo_person_search_results(ctx, person_name: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    await _send_search(
        ctx,
        query=person_name,
        search_label="確定人物検索",
        usage="!person 賀喜遥香",
        search_function=search_photo_images_by_person,
        limit=limit,
    )


async def send_photo_tag_search_results(ctx, tag: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    await _send_search(
        ctx,
        query=tag,
        search_label="タグ検索",
        usage="!tag 制服",
        search_function=search_photo_images_by_tag,
        limit=limit,
    )


async def send_photo_blog_search_results(ctx, query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    await _send_search(
        ctx,
        query=query,
        search_label="ブログ検索",
        usage="!blog 井上和",
        search_function=search_photo_images_by_blog,
        limit=limit,
    )


async def send_photo_author_search_results(ctx, author_name: str, limit: int = DEFAULT_SEARCH_LIMIT) -> None:
    await _send_search(
        ctx,
        query=author_name,
        search_label="ブログ投稿者検索",
        usage="!photo_search_author 菅原咲月",
        search_function=search_photo_images_by_author,
        limit=limit,
    )
