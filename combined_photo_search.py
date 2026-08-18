from __future__ import annotations

import asyncio
import os
from contextlib import closing
from typing import Any

import aiohttp
import discord

from photo_database import get_connection, search_photo_images
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


def _blog_person_results(
    person_name: str,
    *,
    sort_order: str = "latest",
    start_period: str = "",
    end_period: str = "",
    limit: int = SEARCH_LIMIT,
) -> list[dict[str, Any]]:
    clean_name = str(person_name or "").strip()
    if not clean_name:
        return []

    where_parts = [
        """
        (
            EXISTS (
                SELECT 1
                FROM photo_image_people pip
                WHERE pip.image_id = photo_images.id
                  AND pip.person_name = ?
                  AND pip.relation_status IN ('confirmed', 'candidate')
            )
            OR EXISTS (
                SELECT 1
                FROM photo_faces pf
                JOIN photo_face_candidates pfc ON pfc.face_id = pf.id
                JOIN photo_people pp ON pp.id = pfc.person_id
                WHERE pf.image_id = photo_images.id
                  AND pp.person_name = ?
                  AND pfc.candidate_rank = 1
            )
        )
        """
    ]
    params: list[Any] = [clean_name, clean_name]

    start_bound = _normalize_period_bounds(start_period, end=False) if start_period else None
    end_bound = _normalize_period_bounds(end_period or start_period, end=True) if (end_period or start_period) else None

    # published_at は YYYY年MM月DD日 / YYYY-MM-DD / YYYY/MM/DD / YYYY.MM.DD の
    # いずれでも年・月・日の開始位置が同じため、substrで比較用キーを作れる。
    date_key = (
        "substr(photo_blogs.published_at,1,4) || '-' || "
        "substr(photo_blogs.published_at,6,2) || '-' || "
        "substr(photo_blogs.published_at,9,2)"
    )

    if start_bound:
        where_parts.append(f"{date_key} >= ?")
        params.append(start_bound)

    if end_bound:
        # 年月指定の終了境界が翌月1日の場合もあるので <= ではなく < に寄せるため、
        # 12月末/年指定のような明示末日は 23:59 相当としてその日を含める。
        if end_bound.endswith("-31"):
            where_parts.append(f"{date_key} <= ?")
        else:
            where_parts.append(f"{date_key} < ?")
        params.append(end_bound)

    direction = "ASC" if str(sort_order).lower() == "oldest" else "DESC"
    safe_limit = max(1, min(int(limit), 50))

    with closing(get_connection()) as con:
        rows = con.execute(
            f"""
            SELECT
                photo_images.*,
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
                ), '') AS confirmed_people,
                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'candidate'
                ), '') AS candidate_people
            FROM photo_images
            INNER JOIN photo_blogs
              ON photo_blogs.id = photo_images.blog_id
            WHERE
                photo_images.download_status = 'completed'
                AND (photo_images.local_path != '' OR photo_images.bucket_key != '')
                AND {" AND ".join(where_parts)}
            ORDER BY
                {date_key} {direction},
                photo_images.image_index {"ASC" if direction == "ASC" else "DESC"},
                photo_images.id {direction}
            LIMIT ?
            """,
            tuple(params + [safe_limit]),
        ).fetchall()

    normalized: list[dict[str, Any]] = []
    for row in rows:
        data = dict(row)
        normalized.append({
            'source': 'blog',
            'id': data.get('id'),
            'image_url': get_display_image_url(data),
            'title': data.get('title') or 'ブログ写真',
            'author': data.get('member_name') or '不明',
            'people': data.get('confirmed_people') or data.get('candidate_people') or '',
            'date': data.get('published_at') or '',
            'source_url': data.get('blog_url') or '',
            'message_url': data.get('discord_message_url') or '',
        })
    return normalized


async def send_blog_person_search(
    interaction: discord.Interaction,
    person_name: str,
    *,
    sort_order: str = "latest",
    start_period: str = "",
    end_period: str = "",
) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True, thinking=True)

    results = await asyncio.to_thread(
        _blog_person_results,
        person_name,
        sort_order=sort_order,
        start_period=start_period,
        end_period=end_period,
        limit=SEARCH_LIMIT,
    )

    if not results:
        await interaction.followup.send(
            "該当するブログ写真が見つかりませんでした。",
            ephemeral=True,
        )
        return

    period_text = "全期間"
    if start_period:
        period_text = start_period
        if end_period and end_period != start_period:
            period_text += f"〜{end_period}"

    order_text = "古い順" if sort_order == "oldest" else "最新順"
    query_text = f"{person_name} / {order_text} / {period_text}"
    view = CombinedSearchView(
        owner_id=interaction.user.id,
        results=results,
        query=query_text,
    )

    await interaction.followup.send(
        embed=view.embed(),
        view=view,
        ephemeral=True,
    )


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
