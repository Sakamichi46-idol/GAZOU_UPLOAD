"""General-user experience features for the photo archive bot.

Additive, backward-compatible utilities: interactive help, recently viewed photos,
watch-later, random/today/recommended photos, simple person profiles, and related
photo browsing. All entry points are intended for ephemeral panel use.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands

from person_labels import format_people_for_users
from photo_database import get_connection, get_photo_image


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _user_hash(user_id: int) -> str:
    salt = os.getenv("PHOTO_FEEDBACK_HASH_SALT", "photo-archive-user-state")
    return hashlib.sha256(f"{salt}:{int(user_id)}".encode()).hexdigest()


def init_user_experience_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS user_recent_views (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_hash TEXT NOT NULL,
                image_id INTEGER NOT NULL,
                viewed_at TEXT NOT NULL,
                UNIQUE(discord_user_hash, image_id)
            );
            CREATE INDEX IF NOT EXISTS idx_user_recent_views_user_time
              ON user_recent_views(discord_user_hash, viewed_at DESC);

            CREATE TABLE IF NOT EXISTS user_watch_later (
                discord_user_hash TEXT NOT NULL,
                image_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(discord_user_hash, image_id)
            );

            CREATE TABLE IF NOT EXISTS user_guide_preferences (
                discord_user_hash TEXT PRIMARY KEY,
                beginner_mode INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            );
            """
        )
        con.commit()


def should_show_beginner_guide(user_id: int) -> bool:
    init_user_experience_schema()
    with closing(get_connection()) as con:
        row = con.execute(
            "SELECT beginner_mode FROM user_guide_preferences WHERE discord_user_hash=?",
            (_user_hash(user_id),),
        ).fetchone()
        return row is None or bool(int(row[0]))


def set_beginner_guide(user_id: int, enabled: bool) -> None:
    init_user_experience_schema()
    with closing(get_connection()) as con:
        con.execute(
            """INSERT INTO user_guide_preferences(discord_user_hash,beginner_mode,updated_at)
               VALUES(?,?,?)
               ON CONFLICT(discord_user_hash) DO UPDATE SET
                 beginner_mode=excluded.beginner_mode, updated_at=excluded.updated_at""",
            (_user_hash(user_id), 1 if enabled else 0, _now()),
        )
        con.commit()


def record_recent_view(user_id: int, image_id: int) -> None:
    if image_id <= 0:
        return

    init_user_experience_schema()

    with closing(get_connection()) as con:
        con.execute(
            """
            INSERT INTO user_recent_views(
                discord_user_hash,
                image_id,
                viewed_at
            )
            VALUES(?,?,?)
            ON CONFLICT(discord_user_hash,image_id)
            DO UPDATE SET viewed_at=excluded.viewed_at
            """,
            (
                _user_hash(user_id),
                int(image_id),
                _now(),
            ),
        )
        con.commit()


def add_watch_later(user_id: int, image_id: int) -> bool:
    if image_id <= 0:
        return False

    init_user_experience_schema()

    with closing(get_connection()) as con:
        cursor = con.execute(
            """
            INSERT OR IGNORE INTO user_watch_later(
                discord_user_hash,
                image_id,
                created_at
            )
            VALUES(?,?,?)
            """,
            (
                _user_hash(user_id),
                int(image_id),
                _now(),
            ),
        )
        con.commit()
        return cursor.rowcount > 0


def remove_watch_later(user_id: int, image_id: int) -> bool:
    init_user_experience_schema()

    with closing(get_connection()) as con:
        cursor = con.execute(
            """
            DELETE FROM user_watch_later
            WHERE discord_user_hash=?
              AND image_id=?
            """,
            (
                _user_hash(user_id),
                int(image_id),
            ),
        )
        con.commit()
        return cursor.rowcount > 0


def _photo_rows(
    sql: str,
    params: tuple[Any, ...] = (),
) -> list[dict[str, Any]]:
    with closing(get_connection()) as con:
        rows = con.execute(sql, params).fetchall()
        return [dict(row) for row in rows]


def recent_rows(
    user_id: int,
    limit: int = 20,
) -> list[dict[str, Any]]:
    return _photo_rows(
        """
        SELECT
            i.*,
            b.blog_url,
            b.group_name,
            b.member_name,
            b.title,
            b.published_at
        FROM user_recent_views r
        JOIN photo_images i
          ON i.id=r.image_id
        JOIN photo_blogs b
          ON b.id=i.blog_id
        WHERE r.discord_user_hash=?
        ORDER BY r.viewed_at DESC
        LIMIT ?
        """,
        (
            _user_hash(user_id),
            max(1, min(limit, 25)),
        ),
    )


def watch_later_rows(
    user_id: int,
    limit: int = 25,
) -> list[dict[str, Any]]:
    return _photo_rows(
        """
        SELECT
            i.*,
            b.blog_url,
            b.group_name,
            b.member_name,
            b.title,
            b.published_at
        FROM user_watch_later w
        JOIN photo_images i
          ON i.id=w.image_id
        JOIN photo_blogs b
          ON b.id=i.blog_id
        WHERE w.discord_user_hash=?
        ORDER BY w.created_at DESC
        LIMIT ?
        """,
        (
            _user_hash(user_id),
            max(1, min(limit, 25)),
        ),
    )


def random_rows(
    limit: int = 9,
    group_name: str = "",
) -> list[dict[str, Any]]:
    where = "WHERE i.download_status='completed'"
    params: list[Any] = []

    if group_name:
        where += " AND b.group_name=?"
        params.append(group_name)

    params.append(max(1, min(limit, 9)))

    return _photo_rows(
        f"""
        SELECT
            i.*,
            b.blog_url,
            b.group_name,
            b.member_name,
            b.title,
            b.published_at
        FROM photo_images i
        JOIN photo_blogs b
          ON b.id=i.blog_id
        {where}
        ORDER BY RANDOM()
        LIMIT ?
        """,
        tuple(params),
    )


def today_row() -> dict[str, Any] | None:
    """UTCの日付ごとに固定された「今日の1枚」を返す。"""

    day = datetime.now(timezone.utc).strftime("%Y%m%d")

    with closing(get_connection()) as con:
        total_row = con.execute(
            """
            SELECT COUNT(*)
            FROM photo_images
            WHERE download_status='completed'
            """
        ).fetchone()

        total = int(total_row[0]) if total_row else 0

        if total <= 0:
            return None

        offset = int(
            hashlib.sha256(day.encode()).hexdigest()[:12],
            16,
        ) % total

        row = con.execute(
            """
            SELECT
                i.*,
                b.blog_url,
                b.group_name,
                b.member_name,
                b.title,
                b.published_at
            FROM photo_images i
            JOIN photo_blogs b
              ON b.id=i.blog_id
            WHERE i.download_status='completed'
            ORDER BY i.id
            LIMIT 1 OFFSET ?
            """,
            (offset,),
        ).fetchone()

        return dict(row) if row else None


def related_rows(
    image_id: int,
    limit: int = 9,
) -> list[dict[str, Any]]:
    """人物・タグ・同じ記事を使って関連写真を返す。

    AI特徴量の比較が利用できない環境でも軽く動き、同じ記事、確定人物、
    AI/手動タグ、投稿者、グループの順に関連度を付ける。
    """
    source = get_photo_image(int(image_id))
    if not source:
        return []

    blog_id = int(source.get("blog_id") or 0)
    member_name = str(source.get("member_name") or "")
    group_name = str(source.get("group_name") or "")
    image_index = int(source.get("image_index") or 0)

    return _photo_rows(
        """
        WITH source_people AS (
            SELECT person_name FROM photo_image_people
            WHERE image_id=? AND relation_status='confirmed'
        ),
        source_tags AS (
            SELECT tag FROM photo_ai_tags WHERE image_id=?
            UNION
            SELECT tag FROM photo_manual_tags WHERE image_id=?
        )
        SELECT i.*, b.blog_url, b.group_name, b.member_name, b.title, b.published_at,
               (CASE WHEN i.blog_id=? THEN 100 ELSE 0 END) +
               25 * (SELECT COUNT(*) FROM photo_image_people pp
                     WHERE pp.image_id=i.id AND pp.relation_status='confirmed'
                       AND pp.person_name IN (SELECT person_name FROM source_people)) +
               12 * ((SELECT COUNT(*) FROM photo_ai_tags at
                      WHERE at.image_id=i.id AND at.tag IN (SELECT tag FROM source_tags)) +
                     (SELECT COUNT(*) FROM photo_manual_tags mt
                      WHERE mt.image_id=i.id AND mt.tag IN (SELECT tag FROM source_tags))) +
               (CASE WHEN b.member_name=? AND ?<>'' THEN 8 ELSE 0 END) +
               (CASE WHEN b.group_name=? AND ?<>'' THEN 3 ELSE 0 END) AS related_score
        FROM photo_images i
        JOIN photo_blogs b ON b.id=i.blog_id
        WHERE i.id<>? AND i.download_status='completed'
        ORDER BY related_score DESC, ABS(i.image_index-?) ASC, i.id DESC
        LIMIT ?
        """,
        (int(image_id), int(image_id), int(image_id), blog_id,
         member_name, member_name, group_name, group_name, int(image_id), image_index,
         max(1, min(limit, 25))),
    )


def person_profile(name: str) -> dict[str, Any]:
    clean = str(name or "").strip()

    if not clean:
        return {
            "name": "",
            "photo_count": 0,
            "person": {},
            "co": [],
        }

    with closing(get_connection()) as con:
        photo_count_row = con.execute(
            """
            SELECT COUNT(DISTINCT image_id)
            FROM photo_image_people
            WHERE person_name=?
              AND relation_status='confirmed'
            """,
            (clean,),
        ).fetchone()

        photo_count = int(photo_count_row[0]) if photo_count_row else 0

        person_row = con.execute(
            """
            SELECT
                group_name,
                generation_name,
                is_active
            FROM photo_people
            WHERE person_name=?
            LIMIT 1
            """,
            (clean,),
        ).fetchone()

        co_rows = con.execute(
            """
            SELECT
                b.person_name,
                COUNT(*) AS c
            FROM photo_image_people a
            JOIN photo_image_people b
              ON b.image_id=a.image_id
             AND b.person_name<>a.person_name
            WHERE a.person_name=?
              AND a.relation_status='confirmed'
              AND b.relation_status='confirmed'
            GROUP BY b.person_name
            ORDER BY c DESC
            LIMIT 5
            """,
            (clean,),
        ).fetchall()

    return {
        "name": clean,
        "photo_count": photo_count,
        "person": dict(person_row) if person_row else {},
        "co": [dict(item) for item in co_rows],
    }


def _display_url(row: dict[str, Any]) -> str:
    """Bucketの公開URLを優先し、なければ元画像URLを使う。"""

    return str(
        row.get("bucket_public_url")
        or row.get("public_url")
        or row.get("image_url")
        or ""
    ).strip()


def photo_embed(
    row: dict[str, Any],
    *,
    title: str = "📷 写真",
) -> discord.Embed:
    blog_url = str(row.get("blog_url") or "").strip()

    embed = discord.Embed(
        title=title,
        url=blog_url or None,
        color=0x5865F2,
    )

    people_text = format_people_for_users(
        str(
            row.get("confirmed_people")
            or row.get("candidate_people")
            or ""
        )
    )

    embed.add_field(
        name="人物",
        value=people_text or "未確定",
        inline=False,
    )

    embed.add_field(
        name="投稿者",
        value=str(row.get("member_name") or "不明"),
        inline=True,
    )

    embed.add_field(
        name="グループ",
        value=str(row.get("group_name") or "不明"),
        inline=True,
    )

    embed.add_field(
        name="画像ID",
        value=str(row.get("id") or "不明"),
        inline=True,
    )

    image_url = _display_url(row)

    if image_url:
        embed.set_image(url=image_url)

    embed.set_footer(
        text=str(row.get("title") or "無題")[:200]
    )

    return embed


class SimplePhotoListView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        rows: list[dict[str, Any]],
        *,
        title: str,
        watch_later: bool = False,
    ) -> None:
        super().__init__(timeout=900)

        self.owner_id = int(owner_id)
        self.rows = rows
        self.title = title
        self.index = 0
        self.watch_later = watch_later

        self._sync()

    def _sync(self) -> None:
        self.previous.disabled = self.index <= 0
        self.next.disabled = self.index >= len(self.rows) - 1
        self.remove.disabled = not self.watch_later

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この画面は、操作した本人だけが使えます。",
            ephemeral=True,
        )
        return False

    def embed(self) -> discord.Embed:
        embed = photo_embed(
            self.rows[self.index],
            title=self.title,
        )
        embed.description = f"{self.index + 1}/{len(self.rows)}"
        return embed

    @discord.ui.button(
        label="前へ",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if self.index <= 0:
            await interaction.response.defer()
            return

        self.index -= 1
        self._sync()

        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )

    @discord.ui.button(
        label="次へ",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if self.index >= len(self.rows) - 1:
            await interaction.response.defer()
            return

        self.index += 1
        self._sync()

        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )

    @discord.ui.button(
        label="あとで見るから削除",
        emoji="🗑️",
        style=discord.ButtonStyle.danger,
    )
    async def remove(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not self.watch_later:
            await interaction.response.send_message(
                "この一覧では削除できません。",
                ephemeral=True,
            )
            return

        image_id = int(
            self.rows[self.index].get("id") or 0
        )

        await asyncio.to_thread(
            remove_watch_later,
            interaction.user.id,
            image_id,
        )

        self.rows.pop(self.index)

        if not self.rows:
            await interaction.response.edit_message(
                content="🔖 あとで見る写真はありません。",
                embed=None,
                view=None,
            )
            return

        self.index = min(
            self.index,
            len(self.rows) - 1,
        )

        self._sync()

        await interaction.response.edit_message(
            embed=self.embed(),
            view=self,
        )


HELP_TOPICS: dict[str, tuple[str, str, str]] = {
    "photo": (
        "🔍 写真検索",
        "① 「写真検索」を押します。\n"
        "② 検索対象を選びます。\n"
        "③ メンバー名・タイトルなどを入力します。\n"
        "④ 検索結果の写真が9枚ずつ表示されます。\n"
        "⑤ 下の「次の9枚」で次のページへ進みます。\n"
        "⑥ 写真を選ぶと、詳しい情報を確認できます。",
        "検索結果は最大20件です。"
        "表記揺れがある場合は、短い語句で検索してください。",
    ),
    "person": (
        "👤 人物検索",
        "① 「人物で探す」を押します。\n"
        "② グループを選びます。\n"
        "③ 期・区分を選びます。\n"
        "④ 人物を選びます。\n"
        "⑤ 検索結果の写真が9枚ずつ表示されます。\n"
        "⑥ 下のボタンで次のページへ進みます。\n"
        "⑦ 写真詳細から、お気に入り・コレクション・報告を利用できます。",
        "検索結果は最大20件です。"
        "人物が未確定の写真は表示されない場合があります。",
    ),
    "tag": (
        "🏷️ タグ検索",
        "① 「タグで探す」を押します。\n"
        "② カテゴリーを選びます。\n"
        "③ タグや人物条件を選びます。\n"
        "④ 条件を確認して検索します。\n"
        "⑤ 検索結果が9枚ずつ表示されます。\n"
        "⑥ 写真詳細から、お気に入りやコレクションへ追加できます。",
        "条件を増やしすぎると0件になることがあります。"
        "少ない条件から試してください。",
    ),
    "favorite": (
        "⭐ お気に入り",
        "① 写真詳細を開きます。\n"
        "② 「お気に入り登録」を押します。\n"
        "③ 写真検索パネルの「お気に入り」を開きます。\n"
        "④ 「前へ」「次へ」で登録写真を見ます。\n"
        "⑤ 不要な写真だけ個別に削除できます。",
        "お気に入りはユーザーごとに保存されます。"
        "表示が消えても登録内容は残ります。",
    ),
    "collection": (
        "📚 コレクション",
        "① 写真詳細を開きます。\n"
        "② 「コレクションに追加」を押します。\n"
        "③ 新しいコレクションを作るか、既存の追加先を選びます。\n"
        "④ 必要に応じてコレクション名を入力します。\n"
        "⑤ 写真検索パネルの「コレクション」から登録写真を確認します。",
        "コレクションを削除しても元の写真は消えません。"
        "同じ写真を複数のコレクションへ登録できます。",
    ),
    "history": (
        "🕘 履歴・最近見た写真",
        "① 写真詳細を開くと閲覧履歴へ記録されます。\n"
        "② 「最近見た」を開きます。\n"
        "③ 「前へ」「次へ」で見直します。\n"
        "④ 「検索履歴」では過去の検索内容を確認できます。",
        "履歴は本人だけに表示されます。",
    ),
    "support": (
        "📮 不具合・要望",
        "① 「不具合・要望」を押します。\n"
        "② 匿名または記名を選びます。\n"
        "③ 種類・内容・必要であれば画像IDを入力します。\n"
        "④ 内容を確認して送信します。\n"
        "⑤ 管理者からの返信を待ちます。",
        "匿名でも、荒らし対策と返信配送のため、"
        "Bot内部では送信先情報を保持します。"
        "管理者画面では匿名表示されます。",
    ),
}


class BeginnerGuideView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この案内は本人だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="使い方を見る", emoji="📖", style=discord.ButtonStyle.primary)
    async def help_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=help_home_embed(), view=HelpHomeView(self.owner_id))

    @discord.ui.button(label="トップメニューを使う", emoji="✅", style=discord.ButtonStyle.success)
    async def disable_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(set_beginner_guide, self.owner_id, False)
        from control_panel import UserPanelView
        embed = discord.Embed(
            title="📷 写真検索パネル",
            description="目的に合うカテゴリーを選んでください。",
            color=0x3498DB,
        )
        await interaction.edit_original_response(embed=embed, view=UserPanelView(), content=None)


def beginner_guide_embed() -> discord.Embed:
    embed = discord.Embed(
        title="👋 はじめての方へ",
        description=(
            "このBotでは、写真を検索・保存・整理できます。\n\n"
            "① **🔍 写真を探す** から検索方法を選ぶ\n"
            "② 写真の詳細を開く\n"
            "③ お気に入り・コレクション・あとで見るへ保存\n"
            "④ 困ったときは **📖 使い方** を確認"
        ),
        color=0x57F287,
    )
    embed.add_field(
        name="💡 ポイント",
        value="検索結果や保存一覧は本人だけに表示されます。初回ガイドは下のボタンで非表示にできます。",
        inline=False,
    )
    return embed


class HelpTopicView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        topic: str,
    ) -> None:
        super().__init__(timeout=600)

        self.owner_id = int(owner_id)
        self.topic = topic

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この案内は本人だけが操作できます。",
            ephemeral=True,
        )
        return False

    def embed(self) -> discord.Embed:
        title, steps, note = HELP_TOPICS[self.topic]

        embed = discord.Embed(
            title=title,
            description=steps,
            color=0x57F287,
        )

        embed.add_field(
            name="💡 注意点",
            value=note,
            inline=False,
        )

        embed.set_footer(
            text="下のボタンから、そのまま機能を開けます。"
        )

        return embed

    @discord.ui.button(
        label="この機能を開く",
        emoji="🚀",
        style=discord.ButtonStyle.success,
    )
    async def open_feature(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if self.topic == "photo":
            from control_panel import send_source_selector
            await send_source_selector(interaction, person_only=False)
            return
        if self.topic == "person":
            from control_panel import send_source_selector
            await send_source_selector(interaction, person_only=True)
            return
        from control_panel import invoke_existing_command
        command_map = {
            "tag": ("photo_tags", ""),
            "favorite": ("favorite_list", "100"),
            "collection": ("collection_list", ""),
            "history": ("recently_viewed", "20"),
            "support": ("feedback_box", ""),
        }
        command_name, arguments = command_map.get(self.topic, ("", ""))
        if not command_name:
            await interaction.response.send_message("この機能を開けませんでした。", ephemeral=True)
            return
        await invoke_existing_command(interaction, command_name, arguments)

    @discord.ui.button(
        label="使い方メニューへ",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
    )
    async def back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.edit_message(
            embed=help_home_embed(),
            view=HelpHomeView(self.owner_id),
        )


class HelpSelect(discord.ui.Select):
    def __init__(self, owner_id: int) -> None:
        self.owner_id = int(owner_id)

        options = [
            discord.SelectOption(
                label=value[0].split(" ", 1)[-1],
                emoji=value[0].split(" ", 1)[0],
                value=key,
            )
            for key, value in HELP_TOPICS.items()
        ]

        super().__init__(
            placeholder="知りたい機能を選んでください",
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        topic = self.values[0]
        view = HelpTopicView(
            self.owner_id,
            topic,
        )

        await interaction.response.edit_message(
            embed=view.embed(),
            view=view,
        )


class HelpHomeView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)

        self.owner_id = int(owner_id)
        self.add_item(HelpSelect(owner_id))

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "この案内は本人だけが操作できます。",
            ephemeral=True,
        )
        return False


def help_home_embed() -> discord.Embed:
    return discord.Embed(
        title="📖 写真検索パネルの使い方",
        description=(
            "知りたい機能を下のメニューから選んでください。\n"
            "説明は **①→②→③** の操作手順、注意点、"
            "機能への進み方で表示します。"
        ),
        color=0x3498DB,
    )


class ExploreView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)

        self.owner_id = int(owner_id)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "本人だけが操作できます。",
            ephemeral=True,
        )
        return False

    async def _show(
        self,
        interaction: discord.Interaction,
        rows: list[dict[str, Any]],
        title: str,
    ) -> None:
        if not rows:
            await interaction.response.send_message(
                "表示できる写真がありません。",
                ephemeral=True,
            )
            return

        view = SimplePhotoListView(
            self.owner_id,
            rows,
            title=title,
        )

        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="ランダム",
        emoji="🎲",
        style=discord.ButtonStyle.primary,
    )
    async def random(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(
            random_rows,
            9,
            "",
        )

        await self._show(
            interaction,
            rows,
            "🎲 ランダム写真",
        )

    @discord.ui.button(
        label="今日の1枚",
        emoji="📅",
        style=discord.ButtonStyle.primary,
    )
    async def today(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        row = await asyncio.to_thread(today_row)

        await self._show(
            interaction,
            [row] if row else [],
            "📅 今日の1枚",
        )

    @discord.ui.button(
        label="最近見た",
        emoji="🕘",
        style=discord.ButtonStyle.secondary,
    )
    async def recent(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(
            recent_rows,
            self.owner_id,
            20,
        )

        await self._show(
            interaction,
            rows,
            "🕘 最近見た写真",
        )

    @discord.ui.button(
        label="あとで見る",
        emoji="🔖",
        style=discord.ButtonStyle.secondary,
    )
    async def later(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer(ephemeral=True)
        rows = await asyncio.to_thread(
            watch_later_rows,
            self.owner_id,
            25,
        )

        if not rows:
            await interaction.followup.send(
                "🔖 あとで見る写真はありません。",
                ephemeral=True,
            )
            return

        view = SimplePhotoListView(
            self.owner_id,
            rows,
            title="🔖 あとで見る",
            watch_later=True,
        )

        await interaction.followup.send(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )


class PersonProfileModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(
            title="人物プロフィール",
            timeout=300,
        )

        self.name_input = discord.ui.TextInput(
            label="人物名",
            placeholder="例：賀喜遥香",
            max_length=80,
        )

        self.add_item(self.name_input)

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:
        data = await asyncio.to_thread(
            person_profile,
            str(self.name_input.value),
        )

        if data["photo_count"] <= 0:
            await interaction.response.send_message(
                "人物情報が見つかりませんでした。",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            embed=_build_person_profile_embed(data),
            ephemeral=True,
        )


def _build_person_profile_embed(
    data: dict[str, Any],
) -> discord.Embed:
    person = data["person"]

    if person.get("generation_name"):
        generation_text = str(
            person.get("generation_name")
        )
    elif person:
        generation_text = (
            "在籍"
            if person.get("is_active")
            else "卒業・その他"
        )
    else:
        generation_text = "未設定"

    embed = discord.Embed(
        title=f"👤 {data['name']}",
        color=0xEB459E,
    )

    embed.add_field(
        name="登録写真",
        value=f"{data['photo_count']}枚",
        inline=True,
    )

    embed.add_field(
        name="所属",
        value=str(
            person.get("group_name")
            or "その他"
        ),
        inline=True,
    )

    embed.add_field(
        name="期・区分",
        value=generation_text,
        inline=True,
    )

    co_people = "\n".join(
        f"・{item['person_name']}（{item['c']}枚）"
        for item in data["co"]
    )

    embed.add_field(
        name="よく一緒に写る人物",
        value=co_people or "データなし",
        inline=False,
    )

    return embed


def register_user_experience_commands(
    bot: commands.Bot,
) -> None:
    init_user_experience_schema()

    @bot.command(name="user_help")
    async def user_help(
        ctx: commands.Context,
    ) -> None:
        await ctx.send(
            embed=help_home_embed(),
            view=HelpHomeView(ctx.author.id),
        )

    @bot.command(name="user_explore")
    async def user_explore(
        ctx: commands.Context,
    ) -> None:
        await ctx.send(
            "✨ 見たい機能を選んでください。",
            view=ExploreView(ctx.author.id),
        )

    @bot.command(name="recently_viewed")
    async def recently_viewed(
        ctx: commands.Context,
        limit: int = 20,
    ) -> None:
        rows = await asyncio.to_thread(
            recent_rows,
            ctx.author.id,
            limit,
        )

        if not rows:
            await ctx.send(
                "🕘 最近見た写真はありません。"
            )
            return

        view = SimplePhotoListView(
            ctx.author.id,
            rows,
            title="🕘 最近見た写真",
        )

        await ctx.send(
            embed=view.embed(),
            view=view,
        )

    @bot.command(name="watch_later")
    async def watch_later(
        ctx: commands.Context,
        limit: int = 25,
    ) -> None:
        rows = await asyncio.to_thread(
            watch_later_rows,
            ctx.author.id,
            limit,
        )

        if not rows:
            await ctx.send(
                "🔖 あとで見る写真はありません。"
            )
            return

        view = SimplePhotoListView(
            ctx.author.id,
            rows,
            title="🔖 あとで見る",
            watch_later=True,
        )

        await ctx.send(
            embed=view.embed(),
            view=view,
        )

    @bot.command(name="person_profile")
    async def person_profile_command(
        ctx: commands.Context,
        *,
        name: str,
    ) -> None:
        data = await asyncio.to_thread(
            person_profile,
            name,
        )

        if data["photo_count"] <= 0:
            await ctx.send(
                "人物情報が見つかりませんでした。"
            )
            return

        await ctx.send(
            embed=_build_person_profile_embed(data)
        )
