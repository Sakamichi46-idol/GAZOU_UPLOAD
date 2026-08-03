from __future__ import annotations

import os
from typing import Any

import aiohttp
import discord

from photo_database import search_photo_images
from photo_search import get_display_image_url, shorten_text

API_URL = str(os.getenv('INSTAGRAM_SEARCH_API_URL', '') or '').rstrip('/')
API_TOKEN = str(os.getenv('INSTAGRAM_SEARCH_API_TOKEN', '') or '').strip()
SEARCH_LIMIT = max(1, min(int(os.getenv('COMBINED_SEARCH_LIMIT', '20')), 50))


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
            'author': '@' + str(row.get('owner_username') or '不明'),
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
        self._refresh()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message('この検索結果は操作した本人専用です。', ephemeral=True)
            return False
        return True

    def _refresh(self) -> None:
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
