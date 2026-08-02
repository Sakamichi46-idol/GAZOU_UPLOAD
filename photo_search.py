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
    search_photo_images_by_person_with_candidates,
    search_photo_images_by_tag,
    add_photo_favorite,
)


DEFAULT_SEARCH_LIMIT = 20
MAX_SEARCH_LIMIT = 50
VIEW_TIMEOUT_SECONDS = 300
RESULTS_PER_PAGE = 5


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


class DetailResultButton(discord.ui.Button):
    """現在のページに表示されている写真の詳細を開く。"""

    def __init__(self, parent_view: "PhotoSearchResultsView", item_offset: int) -> None:
        self.parent_view = parent_view
        self.item_offset = item_offset
        super().__init__(
            label=f"{item_offset + 1}枚目の詳細",
            style=discord.ButtonStyle.primary,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        result_index = self.parent_view.page * RESULTS_PER_PAGE + self.item_offset
        if result_index >= len(self.parent_view.results):
            await interaction.response.send_message(
                "⚠️ 対象の写真を取得できませんでした。",
                ephemeral=True,
            )
            return

        await interaction.response.defer()
        await self.parent_view.delete_page_messages()

        view = PhotoSearchDetailView(
            owner_id=self.parent_view.owner_id,
            results=self.parent_view.results,
            query=self.parent_view.query,
            search_label=self.parent_view.search_label,
            index=result_index,
            return_page=self.parent_view.page,
        )
        view.message = interaction.message
        await interaction.message.edit(
            content=None,
            embeds=[view.build_embed()],
            view=view,
        )
        self.parent_view.stop()


class PhotoSearchDetailView(discord.ui.View):
    """検索結果の写真詳細とお気に入り登録を提供する。"""

    def __init__(
        self,
        *,
        owner_id: int,
        results: list[dict[str, Any]],
        query: str,
        search_label: str,
        index: int,
        return_page: int,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.results = results
        self.query = query
        self.search_label = search_label
        self.index = max(0, min(index, len(results) - 1))
        self.return_page = return_page
        self.message: discord.Message | None = None
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(results) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果を操作できるのは、検索を実行した人だけです。",
            ephemeral=True,
        )
        return False

    def build_embed(self) -> discord.Embed:
        result = self.results[self.index]
        blog_title = str(result.get("title") or "無題")
        blog_url = str(result.get("blog_url") or "").strip()
        confirmed = str(result.get("confirmed_people") or "").strip()
        candidates = str(result.get("candidate_people") or "").strip()

        embed = discord.Embed(
            title="📷 写真の詳細",
            url=blog_url or None,
            color=0xF1C40F,
        )
        embed.add_field(
            name="📝 ブログタイトル",
            value=shorten_text(blog_title, 1024),
            inline=False,
        )
        embed.add_field(
            name="👤 写っている人物",
            value=shorten_text(confirmed or candidates or "未確定", 1024),
            inline=False,
        )
        embed.add_field(
            name="✍️ ブログ投稿者",
            value=shorten_text(result.get("member_name") or "不明", 1024),
            inline=True,
        )
        embed.add_field(
            name="🏷️ グループ",
            value=shorten_text(result.get("group_name") or "不明", 1024),
            inline=True,
        )
        embed.add_field(
            name="📅 投稿日時",
            value=shorten_text(result.get("published_at") or "不明", 1024),
            inline=False,
        )
        embed.add_field(
            name="🖼️ 画像ID",
            value=str(result.get("id") or "不明"),
            inline=True,
        )

        image_url = get_display_image_url(result)
        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(
            text=(
                f"{self.search_label}: {self.query} • "
                f"{self.index + 1}/{len(self.results)} • "
                f"記事内 {result.get('image_index', 0)}枚目"
            )
        )
        return embed

    @discord.ui.button(label="前の画像", emoji="◀️", style=discord.ButtonStyle.secondary, row=0)
    async def previous_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PhotoSearchDetailView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            index=self.index - 1,
            return_page=self.return_page,
        )
        view.message = interaction.message
        await interaction.response.edit_message(embeds=[view.build_embed()], view=view)
        self.stop()

    @discord.ui.button(label="次の画像", emoji="▶️", style=discord.ButtonStyle.secondary, row=0)
    async def next_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PhotoSearchDetailView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            index=self.index + 1,
            return_page=self.return_page,
        )
        view.message = interaction.message
        await interaction.response.edit_message(embeds=[view.build_embed()], view=view)
        self.stop()

    @discord.ui.button(label="お気に入り登録", emoji="⭐", style=discord.ButtonStyle.success, row=1)
    async def favorite_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        result = self.results[self.index]
        try:
            image_id = int(result.get("id", 0))
        except (TypeError, ValueError):
            image_id = 0

        if image_id <= 0:
            await interaction.response.send_message(
                "⚠️ この写真の画像IDを取得できませんでした。",
                ephemeral=True,
            )
            return

        try:
            added = await asyncio.to_thread(add_photo_favorite, image_id, interaction.user.id)
        except Exception as error:
            print("検索詳細画面のお気に入り登録エラー:", error)
            await interaction.response.send_message(
                "⚠️ お気に入り登録中にエラーが発生しました。",
                ephemeral=True,
            )
            return

        text = (
            f"⭐ 画像ID **{image_id}** をお気に入りに登録しました。"
            if added
            else f"⭐ 画像ID **{image_id}** はすでにお気に入り登録済みです。"
        )
        await interaction.response.send_message(text, ephemeral=True)

    @discord.ui.button(label="検索結果へ戻る", emoji="↩️", style=discord.ButtonStyle.primary, row=1)
    async def back_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        view = PhotoSearchResultsView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            page=self.return_page,
        )
        view.message = interaction.message
        await interaction.message.edit(
            content=view.control_content(),
            embeds=[],
            view=view,
        )
        await view.send_page_with_interaction(interaction)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class PhotoSearchResultsView(discord.ui.View):
    """検索結果を1ページ5枚ずつ、写真ごとの個別メッセージで表示する。"""

    def __init__(
        self,
        *,
        owner_id: int,
        results: list[dict[str, Any]],
        query: str,
        search_label: str,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=VIEW_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.results = results
        self.query = query
        self.search_label = search_label
        self.max_page = max(0, (len(results) - 1) // RESULTS_PER_PAGE)
        self.page = max(0, min(page, self.max_page))
        self.message: discord.Message | None = None
        self.page_messages: list[discord.Message] = []

        for item_offset, _result in enumerate(self.current_results()):
            self.add_item(DetailResultButton(self, item_offset))

        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def current_results(self) -> list[dict[str, Any]]:
        start = self.page * RESULTS_PER_PAGE
        return self.results[start : start + RESULTS_PER_PAGE]

    def current_embeds(self) -> list[discord.Embed]:
        """一覧では説明を付けず、写真だけを1枚ずつ表示する。"""
        embeds: list[discord.Embed] = []

        for result in self.current_results():
            image_url = get_display_image_url(result)
            if not image_url:
                continue
            embed = discord.Embed(color=0x00AAFF)
            embed.set_image(url=image_url)
            embeds.append(embed)

        return embeds

    def control_content(self) -> str:
        start = self.page * RESULTS_PER_PAGE + 1
        end = min(len(self.results), start + RESULTS_PER_PAGE - 1)
        return (
            f"🔍 **{self.search_label}結果**\n"
            f"検索語: `{shorten_text(self.query, 1000)}`\n"
            f"取得件数: **{len(self.results)}件**\n"
            f"現在表示: **{start}〜{end}件目**（写真は1枚ずつ表示）"
        )

    async def delete_page_messages(self) -> None:
        for message in self.page_messages:
            try:
                await message.delete()
            except (discord.HTTPException, discord.NotFound):
                pass
        self.page_messages.clear()

    async def send_page_with_context(self, ctx) -> None:
        self.page_messages = []
        for embed in self.current_embeds():
            message = await ctx.send(embed=embed)
            if message is not None:
                self.page_messages.append(message)

    async def send_page_with_interaction(self, interaction: discord.Interaction) -> None:
        is_ephemeral = bool(getattr(getattr(interaction.message, "flags", None), "ephemeral", False))
        self.page_messages = []
        for embed in self.current_embeds():
            message = await interaction.followup.send(
                embed=embed,
                ephemeral=is_ephemeral,
                wait=True,
            )
            self.page_messages.append(message)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果を操作できるのは、検索を実行した人だけです。",
            ephemeral=True,
        )
        return False

    async def _change_page(self, interaction: discord.Interaction, new_page: int) -> None:
        await interaction.response.defer()
        await self.delete_page_messages()

        view = PhotoSearchResultsView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            page=new_page,
        )
        view.message = interaction.message

        await interaction.message.edit(
            content=view.control_content(),
            embeds=[],
            view=view,
        )
        await view.send_page_with_interaction(interaction)
        self.stop()

    @discord.ui.button(label="前の5枚", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="次の5枚", emoji="▶️", style=discord.ButtonStyle.primary, row=1)
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._change_page(interaction, self.page + 1)

    @discord.ui.button(label="終了", emoji="⏹️", style=discord.ButtonStyle.danger, row=1)
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        await self.delete_page_messages()
        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)
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
        content=view.control_content(),
        view=view,
    )
    view.message = message
    await view.send_page_with_context(ctx)


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
        search_label="人物検索（確認済み＋AI推定）",
        usage="!person 賀喜遥香",
        search_function=search_photo_images_by_person_with_candidates,
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
