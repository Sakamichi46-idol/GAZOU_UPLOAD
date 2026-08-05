from __future__ import annotations

from typing import Any, Callable

import asyncio
import io
import mimetypes
import os
from urllib.parse import urlparse

import aiohttp
import discord

from person_labels import format_people_for_users
from bucket_storage import bucket_is_configured, create_presigned_get_url
from community_features import FeedbackModal, CollectionAddModal, record_usage_event
from user_experience import add_watch_later, record_recent_view, related_rows, SimplePhotoListView
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
RESULTS_PER_PAGE = 9
PHOTO_SEARCH_DISPLAY_NAME = (
    os.getenv("PHOTO_SEARCH_DISPLAY_NAME", "写真検索Bot").strip()
    or "写真検索Bot"
)


def _split_labels(value: Any, limit: int = 25) -> list[str]:
    text = str(value or "").replace("，", "、").replace(",", "、")
    values: list[str] = []
    for part in text.split("、"):
        clean = part.strip()
        if clean and clean not in values:
            values.append(clean)
    return values[:limit]


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

    embed.set_author(name=PHOTO_SEARCH_DISPLAY_NAME)

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
            value=shorten_text(format_people_for_users(confirmed_people), 1024),
            inline=False,
        )
    elif candidate_people:
        embed.add_field(
            name="🧐 人物候補（未確認）",
            value=shorten_text(format_people_for_users(candidate_people), 1024),
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


async def build_photo_attachment_files(
    results: list[dict[str, Any]],
) -> list[discord.File]:
    """写真をDiscord添付用ファイルへ変換する。

    最大9ファイルを同じメッセージで送り、Discord標準のグリッド表示を使う。
    一覧の順番と添付順を必ず一致させる。ローカル保存済み画像を優先し、
    存在しない場合はBucket／元URLから取得する。
    """
    files: list[discord.File] = []
    timeout = aiohttp.ClientTimeout(total=60)
    headers = {"User-Agent": "Mozilla/5.0 (compatible; PhotoArchiveBot/1.0)"}

    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        for position, result in enumerate(results, 1):
            image_id = int(result.get("id") or 0)
            local_path = str(result.get("local_path") or "").strip()
            filename_base = f"photo_{image_id or position}"

            if local_path and os.path.isfile(local_path):
                extension = os.path.splitext(local_path)[1] or ".jpg"
                files.append(
                    discord.File(
                        local_path,
                        filename=f"{filename_base}{extension}",
                    )
                )
                continue

            image_url = get_display_image_url(result)
            if not image_url:
                continue

            try:
                async with session.get(image_url) as response:
                    response.raise_for_status()
                    data = await response.read()
                    if not data:
                        continue

                    content_type = str(response.headers.get("Content-Type") or "").split(";", 1)[0]
                    extension = mimetypes.guess_extension(content_type) or ""
                    if extension == ".jpe":
                        extension = ".jpg"
                    if not extension:
                        extension = os.path.splitext(urlparse(image_url).path)[1] or ".jpg"

                    files.append(
                        discord.File(
                            io.BytesIO(data),
                            filename=f"{filename_base}{extension}",
                        )
                    )
            except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as error:
                print(f"検索結果画像の添付準備エラー: {image_url} / {error}")

    return files


def close_discord_files(files: list[discord.File]) -> None:
    for file in files:
        try:
            file.close()
        except Exception:
            pass


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

        await asyncio.to_thread(
            record_usage_event,
            interaction.user.id,
            "detail",
            image_id=int(self.parent_view.results[result_index].get("id") or 0),
            query_text=self.parent_view.query,
        )
        await asyncio.to_thread(
            record_recent_view,
            interaction.user.id,
            int(self.parent_view.results[result_index].get("id") or 0),
        )
        view = PhotoSearchDetailView(
            owner_id=self.parent_view.owner_id,
            results=self.parent_view.results,
            query=self.parent_view.query,
            search_label=self.parent_view.search_label,
            index=result_index,
            return_page=self.parent_view.page,
        )
        view.message = interaction.message
        await interaction.response.edit_message(
            content=None,
            embeds=[view.build_embed()],
            attachments=[],
            view=view,
        )
        self.parent_view.stop()


class DetailSearchSelect(discord.ui.Select):
    def __init__(self, parent: "PhotoSearchDetailView", people: list[str], tags: list[str]) -> None:
        self.parent_view = parent
        options: list[discord.SelectOption] = []
        for value in people[:12]:
            options.append(discord.SelectOption(label=f"人物：{value}"[:100], value=f"person:{value}", emoji="👤"))
        for value in tags[:12]:
            options.append(discord.SelectOption(label=f"タグ：{value}"[:100], value=f"tag:{value}", emoji="🏷️"))
        super().__init__(
            placeholder="人物・タグから別の写真を探す",
            options=options[:25],
            row=4,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        mode, query = self.values[0].split(":", 1)
        await interaction.response.defer(ephemeral=True)
        if mode == "person":
            rows = await asyncio.to_thread(search_photo_images_by_person, query, DEFAULT_SEARCH_LIMIT)
            label = "人物検索"
        else:
            rows = await asyncio.to_thread(search_photo_images_by_tag, query, DEFAULT_SEARCH_LIMIT)
            label = "タグ検索"
        if not rows:
            await interaction.followup.send("該当する写真が見つかりませんでした。", ephemeral=True)
            return
        view = PhotoSearchResultsView(owner_id=interaction.user.id, results=rows, query=query, search_label=label, page=0)
        files = await view.current_files()
        if not files:
            await interaction.followup.send("画像を取得できませんでした。", ephemeral=True)
            return
        try:
            await interaction.followup.send(content=view.control_content(), files=files, view=view, ephemeral=True)
        finally:
            close_discord_files(files)


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
        current = self.results[self.index]
        people = _split_labels(format_people_for_users(str(current.get("confirmed_people") or current.get("candidate_people") or "")))
        tags = _split_labels(str(current.get("manual_tags") or "") + "、" + str(current.get("ai_tags") or ""))
        if people or tags:
            self.add_item(DetailSearchSelect(self, people, tags))

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
            value=shorten_text(format_people_for_users(confirmed or candidates) or "未確定", 1024),
            inline=False,
        )
        tag_text = "、".join(_split_labels(
            str(result.get("manual_tags") or "") + "、" + str(result.get("ai_tags") or "")
        )) or "未設定"
        embed.add_field(
            name="🏷️ タグ",
            value=shorten_text(tag_text, 1024),
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
            if added:
                await asyncio.to_thread(record_usage_event, interaction.user.id, "favorite", image_id=image_id)
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

    @discord.ui.button(label="この写真を報告", emoji="⚠️", style=discord.ButtonStyle.secondary, row=1)
    async def report_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        await interaction.response.send_modal(
            FeedbackModal(category="人物名・写真情報の間違い", image_id=image_id)
        )

    @discord.ui.button(label="コレクションに追加", emoji="📚", style=discord.ButtonStyle.secondary, row=2)
    async def collection_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        await interaction.response.send_modal(CollectionAddModal(image_id))

    @discord.ui.button(label="あとで見る", emoji="🔖", style=discord.ButtonStyle.secondary, row=2)
    async def watch_later_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        added = await asyncio.to_thread(add_watch_later, interaction.user.id, image_id)
        await interaction.response.send_message(
            "🔖 あとで見るへ追加しました。" if added else "ℹ️ すでにあとで見るへ追加済みです。",
            ephemeral=True,
        )

    @discord.ui.button(label="関連写真", emoji="🔗", style=discord.ButtonStyle.secondary, row=2)
    async def related_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        rows = await asyncio.to_thread(related_rows, image_id, 9)
        if not rows:
            await interaction.response.send_message("関連写真が見つかりませんでした。", ephemeral=True)
            return
        view = SimplePhotoListView(interaction.user.id, rows, title="🔗 関連写真")
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="写真情報をコピー", emoji="📋", style=discord.ButtonStyle.secondary, row=3)
    async def copy_info_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        result = self.results[self.index]
        text = (
            f"画像ID: {result.get('id') or '不明'}\n"
            f"ブログ: {result.get('title') or '無題'}\n"
            f"投稿者: {result.get('member_name') or '不明'}\n"
            f"人物: {format_people_for_users(str(result.get('confirmed_people') or result.get('candidate_people') or '')) or '未確定'}\n"
            f"URL: {result.get('blog_url') or ''}"
        )
        await interaction.response.send_message(f"```text\n{text[:1800]}\n```", ephemeral=True)

    @discord.ui.button(label="人物・タグを提案", emoji="💡", style=discord.ButtonStyle.secondary, row=3)
    async def suggestion_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        await interaction.response.send_modal(FeedbackModal(category="人物・タグの提案", image_id=image_id))

    @discord.ui.button(label="検索結果へ戻る", emoji="↩️", style=discord.ButtonStyle.primary, row=1)
    async def back_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # 最大9枚の準備には3秒以上かかることがあるため、先に応答を確定する。
        await interaction.response.defer()
        view = PhotoSearchResultsView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            page=self.return_page,
        )
        view.message = interaction.message
        files = await view.current_files()
        if not files:
            await interaction.followup.send(
                "⚠️ 検索結果の画像を取得できませんでした。",
                ephemeral=True,
            )
            return
        try:
            await interaction.edit_original_response(
                content=view.control_content(),
                embeds=[],
                attachments=files,
                view=view,
            )
        finally:
            close_discord_files(files)
        self.stop()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        print(f"写真検索結果Viewエラー: {type(error).__name__}: {error}")
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ 検索結果の操作中にエラーが発生しました。もう一度検索してください。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ 検索結果の操作中にエラーが発生しました。もう一度検索してください。",
                ephemeral=True,
            )

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


class PhotoSearchResultSelect(discord.ui.Select):
    """現在ページの1〜9枚目から詳細表示する写真を選ぶ。"""

    def __init__(self, parent_view: "PhotoSearchResultsView") -> None:
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=f"{offset + 1}枚目の詳細",
                description=f"検索結果の{parent_view.page * RESULTS_PER_PAGE + offset + 1}件目",
                value=str(offset),
                emoji="📷",
            )
            for offset, _result in enumerate(parent_view.current_results())
        ]
        super().__init__(
            placeholder="詳細を見る写真を選択",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            item_offset = int(self.values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "⚠️ 選択した写真を取得できませんでした。",
                ephemeral=True,
            )
            return

        result_index = self.parent_view.page * RESULTS_PER_PAGE + item_offset
        if result_index >= len(self.parent_view.results):
            await interaction.response.send_message(
                "⚠️ 選択した写真を取得できませんでした。",
                ephemeral=True,
            )
            return

        view = PhotoSearchDetailView(
            owner_id=self.parent_view.owner_id,
            results=self.parent_view.results,
            query=self.parent_view.query,
            search_label=self.parent_view.search_label,
            index=result_index,
            return_page=self.parent_view.page,
        )
        view.message = interaction.message
        await interaction.response.edit_message(
            content=None,
            embeds=[view.build_embed()],
            attachments=[],
            view=view,
        )
        self.parent_view.stop()

    @discord.ui.button(label="トップメニュー", emoji="🏠", style=discord.ButtonStyle.secondary, row=3)
    async def home_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from control_panel import UserPanelView
        embed = discord.Embed(
            title="📷 写真検索パネル",
            description="目的に合うカテゴリーを選んでください。",
            color=0x3498DB,
        )
        await interaction.response.send_message(embed=embed, view=UserPanelView(), ephemeral=True)


class PhotoSearchResultsView(discord.ui.View):
    """検索結果を1ページ9枚ずつ、操作メッセージ本体の添付として表示する。"""

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

        self.add_item(PhotoSearchResultSelect(self))

        self.previous_button.disabled = self.page <= 0
        self.next_button.disabled = self.page >= self.max_page

    def current_results(self) -> list[dict[str, Any]]:
        start = self.page * RESULTS_PER_PAGE
        return self.results[start : start + RESULTS_PER_PAGE]

    async def current_files(self) -> list[discord.File]:
        """現在ページの最大9枚を、Discordの同時添付用ファイルに変換する。"""
        return await build_photo_attachment_files(self.current_results())

    def control_content(self) -> str:
        start = self.page * RESULTS_PER_PAGE + 1
        end = min(len(self.results), start + RESULTS_PER_PAGE - 1)
        return (
            f"🔍 **{PHOTO_SEARCH_DISPLAY_NAME}｜{self.search_label}結果**\n"
            f"検索語: `{shorten_text(self.query, 1000)}`\n"
            f"取得件数: **{len(self.results)}件**\n"
            f"現在表示: **{start}〜{end}件目**（最大9枚を1セットで表示）"
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果を操作できるのは、検索を実行した人だけです。",
            ephemeral=True,
        )
        return False

    async def _change_page(self, interaction: discord.Interaction, new_page: int) -> None:
        # 画像のダウンロード中にInteractionの3秒制限を超えないよう、先にdeferする。
        await interaction.response.defer()
        view = PhotoSearchResultsView(
            owner_id=self.owner_id,
            results=self.results,
            query=self.query,
            search_label=self.search_label,
            page=new_page,
        )
        view.message = interaction.message
        files = await view.current_files()
        if not files:
            await interaction.followup.send(
                "⚠️ 次のページの画像を取得できませんでした。",
                ephemeral=True,
            )
            return
        try:
            await interaction.edit_original_response(
                content=view.control_content(),
                embeds=[],
                attachments=files,
                view=view,
            )
        finally:
            close_discord_files(files)
        self.stop()

    @discord.ui.button(label="前の9枚", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="次の9枚", emoji="▶️", style=discord.ButtonStyle.primary, row=1)
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
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item[discord.ui.View],
    ) -> None:
        print(f"写真検索結果Viewエラー: {type(error).__name__}: {error}")
        if interaction.response.is_done():
            await interaction.followup.send(
                "⚠️ 検索結果の操作中にエラーが発生しました。もう一度検索してください。",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "⚠️ 検索結果の操作中にエラーが発生しました。もう一度検索してください。",
                ephemeral=True,
            )

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

    await asyncio.to_thread(
        record_usage_event,
        ctx.author.id,
        "search",
        query_text=f"{search_label}:{clean_query}",
    )
    view = PhotoSearchResultsView(
        owner_id=ctx.author.id,
        results=results,
        query=clean_query,
        search_label=search_label,
    )
    files = await view.current_files()
    if not files:
        await ctx.send("⚠️ 検索結果の画像を取得できませんでした。")
        return
    try:
        message = await ctx.send(
            content=view.control_content(),
            files=files,
            view=view,
        )
    finally:
        close_discord_files(files)
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
