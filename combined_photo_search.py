from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
import os
from contextlib import closing
from typing import Any

import aiohttp
import discord

from photo_database import get_connection, search_photo_images
from photo_search import (
    build_photo_attachment_files,
    close_discord_files,
    get_display_image_url,
    shorten_text,
)

API_URL = str(os.getenv('INSTAGRAM_SEARCH_API_URL', '') or '').rstrip('/')
API_TOKEN = str(os.getenv('INSTAGRAM_SEARCH_API_TOKEN', '') or '').strip()
SEARCH_LIMIT = max(1, min(int(os.getenv('COMBINED_SEARCH_LIMIT', '20')), 50))
BLOG_GROUP_PAGE_SIZE = 5
BLOG_IMAGE_BATCH_SIZE = 10


def _blog_results(query: str, limit: int) -> list[dict[str, Any]]:
    results = search_photo_images(query, limit=limit)
    normalized: list[dict[str, Any]] = []
    for row in results:
        normalized.append({
            'source': 'blog',
            'id': row.get('id'),
            'image_url': get_display_image_url(row),
            'title': row.get('title') or 'ブログ写真',
            'author': row.get('member_name') or '不明',
            'people': row.get('confirmed_people') or row.get('candidate_people') or '',
            'date': row.get('published_at') or '',
            'source_url': row.get('blog_url') or '',
            'message_url': row.get('discord_message_url') or '',
        })
    return normalized


def _normalize_period_bounds(value: str, *, end: bool) -> str | None:
    text = str(value or "").strip().replace("/", "-").replace(".", "-")
    if not text:
        return None
    parts = text.split("-")
    if len(parts) == 1 and len(parts[0]) == 4 and parts[0].isdigit():
        year = int(parts[0])
        return f"{year:04d}-12-31" if end else f"{year:04d}-01-01"
    if (
        len(parts) == 2
        and len(parts[0]) == 4
        and parts[0].isdigit()
        and parts[1].isdigit()
    ):
        year = int(parts[0])
        month = int(parts[1])
        if not 1 <= month <= 12:
            return None
        if end:
            if month == 12:
                return f"{year:04d}-12-31"
            next_month = month + 1
            return f"{year:04d}-{next_month:02d}-01"
        return f"{year:04d}-{month:02d}-01"
    return None


def _blog_person_grouped_results(
    person_name: str,
    *,
    match_mode: str = "poster",
    sort_order: str = "latest",
    start_period: str = "",
    end_period: str = "",
) -> list[dict[str, Any]]:
    """人物検索結果をブログ単位でまとめて返す。

    poster: 選択メンバーが投稿したブログの全保存写真。
    subject: 選択メンバーが「確認済み人物」として登録された写真のみ。
    """
    clean_name = str(person_name or "").strip()
    if not clean_name:
        return []

    clean_mode = "subject" if str(match_mode).lower() == "subject" else "poster"
    start_bound = _normalize_period_bounds(start_period, end=False) if start_period else None
    end_bound = _normalize_period_bounds(end_period or start_period, end=True) if (end_period or start_period) else None
    date_key = (
        "substr(photo_blogs.published_at,1,4) || '-' || "
        "substr(photo_blogs.published_at,6,2) || '-' || "
        "substr(photo_blogs.published_at,9,2)"
    )
    where_parts = [
        "photo_images.download_status = 'completed'",
        "(photo_images.local_path != '' OR photo_images.bucket_key != '')",
        "COALESCE(photo_blogs.is_hidden, 0) = 0",
    ]
    params: list[Any] = []
    if clean_mode == "poster":
        where_parts.append("photo_blogs.member_name = ?")
        params.append(clean_name)
    else:
        where_parts.append(
            """
            EXISTS (
                SELECT 1
                FROM photo_image_people pip
                WHERE pip.image_id = photo_images.id
                  AND pip.person_name = ?
                  AND pip.relation_status = 'confirmed'
            )
            """
        )
        params.append(clean_name)
    if start_bound:
        where_parts.append(f"{date_key} >= ?")
        params.append(start_bound)
    if end_bound:
        if end_bound.endswith("-31"):
            where_parts.append(f"{date_key} <= ?")
        else:
            where_parts.append(f"{date_key} < ?")
        params.append(end_bound)
    direction = "ASC" if str(sort_order).lower() == "oldest" else "DESC"
    with closing(get_connection()) as con:
        rows = con.execute(
            f"""
            SELECT
                photo_images.*,
                photo_blogs.id AS blog_id,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'confirmed'
                ), '') AS confirmed_people
            FROM photo_images
            INNER JOIN photo_blogs
              ON photo_blogs.id = photo_images.blog_id
            WHERE {" AND ".join(where_parts)}
            ORDER BY
                {date_key} {direction},
                photo_blogs.id {direction},
                photo_images.image_index ASC,
                photo_images.id ASC
            """,
            tuple(params),
        ).fetchall()
    grouped: OrderedDict[int, dict[str, Any]] = OrderedDict()
    for row in rows:
        data = dict(row)
        blog_id = int(data.get("blog_id") or 0)
        if blog_id not in grouped:
            grouped[blog_id] = {
                "blog_id": blog_id,
                "title": data.get("title") or "ブログ",
                "author": data.get("member_name") or "不明",
                "group_name": data.get("group_name") or "",
                "date": data.get("published_at") or "",
                "source_url": data.get("blog_url") or "",
                "images": [],
            }
        grouped[blog_id]["images"].append({
            "id": data.get("id"),
            "image_index": int(data.get("image_index") or 0),
            "image_url": data.get("image_url") or "",
            "local_path": data.get("local_path") or "",
            "bucket_key": data.get("bucket_key") or "",
            "people": data.get("confirmed_people") or "",
            "message_url": data.get("discord_message_url") or "",
        })
    return list(grouped.values())


def _blog_group_embed(
    group: dict[str, Any],
    *,
    person_name: str,
    match_mode: str,
    blog_index: int,
    total_blogs: int,
) -> discord.Embed:
    mode_text = "投稿者" if match_mode == "poster" else "写っている人物"
    embed = discord.Embed(
        title=str(group.get("title") or "ブログ")[:256],
        description=(
            f"**投稿者:** {group.get('author') or '不明'}\n"
            f"**投稿日:** {group.get('date') or '不明'}\n"
            f"**検索:** {mode_text} = {person_name}\n"
            f"**該当写真:** {len(group.get('images') or []):,}枚"
        ),
        color=0x5865F2,
        url=(group.get("source_url") or None),
    )
    embed.set_footer(text=f"ブログ {blog_index + 1}/{total_blogs}")
    return embed


class BlogPageSelect(discord.ui.Select):
    def __init__(self, parent: "GroupedBlogResultView") -> None:
        self.parent_view = parent
        start_no = parent.page * BLOG_GROUP_PAGE_SIZE
        options: list[discord.SelectOption] = []
        for offset, group in enumerate(parent.page_groups):
            title = str(group.get("title") or "ブログ")
            date = str(group.get("date") or "日付不明")
            count = len(group.get("images") or [])
            options.append(
                discord.SelectOption(
                    label=f"{start_no + offset + 1}. {title}"[:100],
                    value=str(start_no + offset),
                    description=f"{date} / {count}枚"[:100],
                )
            )
        super().__init__(
            placeholder="表示するブログを選択",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        index = int(self.values[0])
        detail = BlogDetailView(
            self.parent_view.owner_id,
            self.parent_view.groups,
            person_name=self.parent_view.person_name,
            match_mode=self.parent_view.match_mode,
            sort_order=self.parent_view.sort_order,
            start_period=self.parent_view.start_period,
            end_period=self.parent_view.end_period,
            index=index,
        )
        await interaction.response.edit_message(embed=detail.embed(), view=detail)


class GroupedBlogResultView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        groups: list[dict[str, Any]],
        *,
        person_name: str,
        match_mode: str,
        sort_order: str,
        start_period: str,
        end_period: str,
        page: int = 0,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.groups = groups
        self.person_name = person_name
        self.match_mode = match_mode
        self.sort_order = sort_order
        self.start_period = start_period
        self.end_period = end_period
        total_pages = max(1, math.ceil(len(groups) / BLOG_GROUP_PAGE_SIZE))
        self.page = max(0, min(int(page), total_pages - 1))
        if self.page_groups:
            self.add_item(BlogPageSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= total_pages - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果は検索した本人だけが操作できます。",
            ephemeral=True,
        )
        return False

    @property
    def page_groups(self) -> list[dict[str, Any]]:
        start = self.page * BLOG_GROUP_PAGE_SIZE
        return self.groups[start:start + BLOG_GROUP_PAGE_SIZE]

    def summary_embed(self) -> discord.Embed:
        total_pages = max(1, math.ceil(len(self.groups) / BLOG_GROUP_PAGE_SIZE))
        total_images = sum(len(group.get("images") or []) for group in self.groups)
        mode_text = "投稿者で探す" if self.match_mode == "poster" else "写っている人物で探す"
        order_text = "古い順" if self.sort_order == "oldest" else "最新順"
        period_text = "全期間"
        if self.start_period:
            period_text = self.start_period
            if self.end_period and self.end_period != self.start_period:
                period_text += f"〜{self.end_period}"
        start_no = self.page * BLOG_GROUP_PAGE_SIZE
        lines = []
        for offset, group in enumerate(self.page_groups):
            lines.append(
                f"**{start_no + offset + 1}. {group.get('title') or 'ブログ'}**\n"
                f"{group.get('date') or '日付不明'} / "
                f"{group.get('author') or '投稿者不明'} / "
                f"{len(group.get('images') or []):,}枚"
            )
        embed = discord.Embed(
            title=f"👤 {self.person_name} のブログ写真",
            description=(
                f"**検索方法:** {mode_text}\n"
                f"**並び順:** {order_text}\n"
                f"**期間:** {period_text}\n"
                f"**合計:** {len(self.groups):,}ブログ / {total_images:,}枚\n\n"
                + "\n\n".join(lines)
                + "\n\n下のメニューから、表示するブログを1件選んでください。"
            ),
            color=0x57F287,
        )
        embed.set_footer(
            text=f"{self.page + 1}/{total_pages}ページ ・ 1ページ最大{BLOG_GROUP_PAGE_SIZE}ブログ"
        )
        return embed

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        new_view = GroupedBlogResultView(
            self.owner_id, self.groups,
            person_name=self.person_name, match_mode=self.match_mode,
            sort_order=self.sort_order, start_period=self.start_period,
            end_period=self.end_period, page=self.page - 1,
        )
        await interaction.response.edit_message(embed=new_view.summary_embed(), view=new_view)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        new_view = GroupedBlogResultView(
            self.owner_id, self.groups,
            person_name=self.person_name, match_mode=self.match_mode,
            sort_order=self.sort_order, start_period=self.start_period,
            end_period=self.end_period, page=self.page + 1,
        )
        await interaction.response.edit_message(embed=new_view.summary_embed(), view=new_view)


class BlogDetailView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        groups: list[dict[str, Any]],
        *,
        person_name: str,
        match_mode: str,
        sort_order: str,
        start_period: str,
        end_period: str,
        index: int,
    ) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.groups = groups
        self.person_name = person_name
        self.match_mode = match_mode
        self.sort_order = sort_order
        self.start_period = start_period
        self.end_period = end_period
        self.index = max(0, min(int(index), len(groups) - 1))
        self.previous_blog.disabled = self.index <= 0
        self.next_blog.disabled = self.index >= len(groups) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message(
            "この検索結果は検索した本人だけが操作できます。",
            ephemeral=True,
        )
        return False

    @property
    def group(self) -> dict[str, Any]:
        return self.groups[self.index]

    def embed(self) -> discord.Embed:
        return _blog_group_embed(
            self.group,
            person_name=self.person_name,
            match_mode=self.match_mode,
            blog_index=self.index,
            total_blogs=len(self.groups),
        )

    def _new(self, index: int) -> "BlogDetailView":
        return BlogDetailView(
            self.owner_id, self.groups,
            person_name=self.person_name,
            match_mode=self.match_mode,
            sort_order=self.sort_order,
            start_period=self.start_period,
            end_period=self.end_period,
            index=index,
        )

    @discord.ui.button(label="写真をまとめて表示", emoji="🖼️", style=discord.ButtonStyle.primary, row=0)
    async def show_images(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        images = list(self.group.get("images") or [])
        if not images:
            await interaction.followup.send("このブログには表示できる写真がありません。", ephemeral=True)
            return

        sent = 0
        # Discordの添付グリッドを使うため9枚ずつ。同一ブログ内では連続して送る。
        for start in range(0, len(images), 9):
            files = await build_photo_attachment_files(images[start:start + 9])
            if not files:
                continue
            try:
                await interaction.followup.send(files=files, ephemeral=True)
                sent += len(files)
            finally:
                close_discord_files(files)

        if sent == 0:
            await interaction.followup.send(
                "写真を取得できませんでした。保存先または画像URLを確認してください。",
                ephemeral=True,
            )

    @discord.ui.button(label="前のブログ", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_blog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = self._new(self.index - 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="次のブログ", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_blog(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = self._new(self.index + 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="一覧へ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back_to_list(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        page = self.index // BLOG_GROUP_PAGE_SIZE
        view = GroupedBlogResultView(
            self.owner_id, self.groups,
            person_name=self.person_name,
            match_mode=self.match_mode,
            sort_order=self.sort_order,
            start_period=self.start_period,
            end_period=self.end_period,
            page=page,
        )
        await interaction.response.edit_message(embed=view.summary_embed(), view=view)


async def send_blog_person_search(
    interaction: discord.Interaction,
    person_name: str,
    *,
    match_mode: str = "poster",
    sort_order: str = "latest",
    start_period: str = "",
    end_period: str = "",
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)
    groups = await asyncio.to_thread(
        _blog_person_grouped_results,
        person_name,
        match_mode=match_mode,
        sort_order=sort_order,
        start_period=start_period,
        end_period=end_period,
    )
    if not groups:
        await interaction.followup.send("該当するブログ写真が見つかりませんでした。", ephemeral=True)
        return
    view = GroupedBlogResultView(
        interaction.user.id,
        groups,
        person_name=person_name,
        match_mode=match_mode,
        sort_order=sort_order,
        start_period=start_period,
        end_period=end_period,
    )
    await interaction.followup.send(embed=view.summary_embed(), view=view, ephemeral=True)

async def _instagram_results(query: str, limit: int) -> tuple[list[dict[str, Any]], str]:
    if not API_URL or not API_TOKEN:
        return [], 'Instagram検索APIが未設定です。'
    timeout = aiohttp.ClientTimeout(total=15)
    headers = {'Authorization': f'Bearer {API_TOKEN}'}
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(
                f'{API_URL}/api/search',
                params={'q': query, 'limit': limit},
                headers=headers,
            ) as response:
                if response.status != 200:
                    return [], f'Instagram検索API HTTP {response.status}'
                payload = await response.json()
    except (aiohttp.ClientError, TimeoutError) as error:
        return [], f'Instagram検索APIへ接続できません: {type(error).__name__}'

    normalized: list[dict[str, Any]] = []
    for row in payload.get('results', []):
        if str(row.get('media_type') or '') != 'image':
            continue
        normalized.append({
            'source': 'instagram',
            'id': row.get('id'),
            'image_url': row.get('attachment_url') or '',
            'title': shorten_text(row.get('caption') or 'Instagram投稿', 120),
            'author': str(row.get('display_name') or ('@' + str(row.get('owner_username') or '不明'))),
            'people': row.get('people_text') or '',
            'date': row.get('created_at') or '',
            'source_url': row.get('post_url') or '',
            'message_url': row.get('discord_message_url') or '',
        })
    return normalized, ''


async def collect_combined_results(source: str, query: str) -> tuple[list[dict[str, Any]], list[str]]:
    source = source.lower().strip()
    notices: list[str] = []
    results: list[dict[str, Any]] = []
    if source in {'blog', 'all'}:
        results.extend(_blog_results(query, SEARCH_LIMIT))
    if source in {'instagram', 'all'}:
        instagram, error = await _instagram_results(query, SEARCH_LIMIT)
        results.extend(instagram)
        if error:
            notices.append(error)
    return results[:SEARCH_LIMIT], notices


class CombinedSearchView(discord.ui.View):
    def __init__(self, *, owner_id: int, results: list[dict[str, Any]], query: str, index: int = 0):
        super().__init__(timeout=300)
        self.owner_id = owner_id
        self.results = results
        self.query = query
        self.index = index % len(results)
        self._sync_navigation_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('この検索結果は操作した本人専用です。', ephemeral=True)
            return False
        return True

    def _sync_navigation_buttons(self) -> None:
        """検索結果の前へ/次へボタン状態だけを同期する。

        discord.py の View 内部メソッド `_refresh(components)` と
        名前が衝突しないよう、アプリ専用のメソッド名を使用する。
        """
        self.previous.disabled = len(self.results) <= 1
        self.next.disabled = len(self.results) <= 1

    def embed(self) -> discord.Embed:
        row = self.results[self.index]
        source = '📚 ブログ' if row['source'] == 'blog' else '📸 Instagram'
        embed = discord.Embed(
            title=f'{source}｜{row.get("author") or "不明"}',
            description=shorten_text(row.get('title') or '', 1000),
            url=row.get('source_url') or None,
            color=0x3498DB if row['source'] == 'blog' else 0xE1306C,
        )
        embed.add_field(name='人物', value=shorten_text(row.get('people') or '未設定', 1024), inline=False)
        embed.add_field(name='画像ID', value=f'{row["source"]}:{row.get("id")}', inline=True)
        embed.add_field(name='保存日時', value=shorten_text(row.get('date') or '不明', 1024), inline=True)
        if row.get('message_url'):
            embed.add_field(name='Discord保存先', value=f'[メッセージを開く]({row["message_url"]})', inline=False)
        if row.get('image_url'):
            embed.set_image(url=row['image_url'])
        embed.set_footer(text=f'検索: {self.query} • {self.index + 1}/{len(self.results)}')
        return embed

    @discord.ui.button(label='前へ', emoji='◀️', style=discord.ButtonStyle.secondary)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index - 1) % len(self.results)
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label='次へ', emoji='▶️', style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.index = (self.index + 1) % len(self.results)
        await interaction.response.edit_message(embed=self.embed(), view=self)


async def send_combined_search(interaction: discord.Interaction, source: str, query: str) -> None:
    await interaction.response.defer(ephemeral=True, thinking=True)
    results, notices = await collect_combined_results(source, query)
    if not results:
        text = '該当する写真が見つかりませんでした。'
        if notices:
            text += '\n' + '\n'.join(f'⚠️ {notice}' for notice in notices)
        await interaction.followup.send(text, ephemeral=True)
        return
    view = CombinedSearchView(owner_id=interaction.user.id, results=results, query=query)
    content = '\n'.join(f'⚠️ {notice}' for notice in notices) or None
    await interaction.followup.send(content=content, embed=view.embed(), view=view, ephemeral=True)
