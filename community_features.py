"""Community feedback, personal collections, AI-learning stats and operations tools.

This module deliberately keeps its schema additive so existing Railway volumes and
photo_archive.db files can be upgraded without destructive migrations.
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import shutil
import sqlite3
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from photo_database import PHOTO_DB_PATH, get_connection, get_photo_image


logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _anon_key(user_id: int) -> str:
    salt = os.getenv("PHOTO_FEEDBACK_HASH_SALT", "photo-feedback-v1")
    return hashlib.sha256(f"{salt}:{int(user_id)}".encode()).hexdigest()[:16]


def _table_columns(connection: sqlite3.Connection, table_name: str) -> set[str]:
    """Return the actual column names for an existing SQLite table."""

    rows = connection.execute(f"PRAGMA table_info({table_name})").fetchall()
    return {str(row["name"] if isinstance(row, sqlite3.Row) else row[1]) for row in rows}


def _validate_community_dependencies(connection: sqlite3.Connection) -> None:
    """Log schema mismatches without preventing the bot from starting."""

    required = {
        "photo_images": {"id", "blog_id", "image_url", "download_status"},
        "photo_blogs": {"id", "blog_url", "group_name", "member_name", "title"},
    }

    for table_name, expected_columns in required.items():
        actual_columns = _table_columns(connection, table_name)
        if not actual_columns:
            logger.warning("Community features: table %s does not exist yet.", table_name)
            continue

        missing = sorted(expected_columns - actual_columns)
        if missing:
            logger.warning(
                "Community features: table %s is missing columns: %s",
                table_name,
                ", ".join(missing),
            )


def init_community_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS community_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ticket_no TEXT NOT NULL UNIQUE,
                category TEXT NOT NULL,
                body TEXT NOT NULL,
                image_id INTEGER,
                anonymous INTEGER NOT NULL DEFAULT 1,
                reporter_user_id TEXT NOT NULL DEFAULT '',
                reporter_anon_key TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                admin_note TEXT NOT NULL DEFAULT '',
                admin_reply TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_feedback_status ON community_feedback(status, id DESC);
            CREATE INDEX IF NOT EXISTS idx_feedback_anon ON community_feedback(reporter_anon_key, created_at);

            CREATE TABLE IF NOT EXISTS user_photo_collections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id TEXT NOT NULL,
                collection_name TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(discord_user_id, collection_name)
            );
            CREATE TABLE IF NOT EXISTS user_photo_collection_items (
                collection_id INTEGER NOT NULL,
                image_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY(collection_id, image_id),
                FOREIGN KEY(collection_id) REFERENCES user_photo_collections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS user_collection_shares (
                share_token TEXT PRIMARY KEY,
                collection_id INTEGER NOT NULL,
                owner_user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                FOREIGN KEY(collection_id) REFERENCES user_photo_collections(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS photo_usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_hash TEXT NOT NULL,
                event_type TEXT NOT NULL,
                image_id INTEGER,
                query_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_usage_event ON photo_usage_events(event_type, created_at);
            CREATE INDEX IF NOT EXISTS idx_usage_image ON photo_usage_events(image_id, event_type);
            """
        )
        _validate_community_dependencies(con)
        con.commit()


def record_usage_event(user_id: int, event_type: str, *, image_id: int | None = None, query_text: str = "") -> None:
    init_community_schema()
    with closing(get_connection()) as con:
        con.execute(
            "INSERT INTO photo_usage_events(discord_user_hash,event_type,image_id,query_text,created_at) VALUES(?,?,?,?,?)",
            (_anon_key(user_id), str(event_type)[:40], image_id, str(query_text)[:500], _now()),
        )
        con.commit()


def create_feedback(user_id: int, category: str, body: str, *, image_id: int | None, anonymous: bool) -> str:
    init_community_schema()
    now = _now()
    with closing(get_connection()) as con:
        cutoff = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
        count = con.execute(
            "SELECT COUNT(*) FROM community_feedback WHERE reporter_anon_key=? AND created_at>=?",
            (_anon_key(user_id), cutoff),
        ).fetchone()[0]
        if int(count) >= 5:
            raise ValueError("短時間の送信上限（10分で5件）に達しました。")
        next_id = int(con.execute("SELECT COALESCE(MAX(id),0)+1 FROM community_feedback").fetchone()[0])
        ticket = f"REQ-{datetime.now().strftime('%Y%m%d')}-{next_id:05d}"
        con.execute(
            """INSERT INTO community_feedback(
                ticket_no,category,body,image_id,anonymous,reporter_user_id,reporter_anon_key,status,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                ticket, category, body.strip(), image_id, 1 if anonymous else 0,
                str(user_id), _anon_key(user_id), "open", now, now,
            ),
        )
        con.commit()
    return ticket


class FeedbackModal(discord.ui.Modal):
    def __init__(self, *, category: str = "不具合・要望", image_id: int | None = None) -> None:
        super().__init__(title="不具合・要望を送る", timeout=600)
        self.category_default = category
        self.image_id_default = image_id
        self.category = discord.ui.TextInput(label="種類", default=category, max_length=40)
        self.image_id = discord.ui.TextInput(
            label="写真ID（写真に関する場合）", required=False,
            default=str(image_id or ""), max_length=20,
        )
        self.body = discord.ui.TextInput(
            label="内容", style=discord.TextStyle.paragraph,
            placeholder="何が起きたか、期待する動作、人物名の訂正などを詳しく書いてください。",
            max_length=1800,
        )
        self.anonymous = discord.ui.TextInput(
            label="匿名で送る？（はい / いいえ）", default="はい", max_length=5,
        )
        self.add_item(self.category)
        self.add_item(self.image_id)
        self.add_item(self.body)
        self.add_item(self.anonymous)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        image_id: int | None = None
        raw_id = str(self.image_id.value or "").strip()
        if raw_id:
            if not raw_id.isdigit():
                await interaction.followup.send("⚠️ 写真IDは数字で入力してください。", ephemeral=True)
                return
            image_id = int(raw_id)
            if await asyncio.to_thread(get_photo_image, image_id) is None:
                await interaction.followup.send("⚠️ その写真IDは見つかりません。", ephemeral=True)
                return
        anonymous = str(self.anonymous.value or "はい").strip().lower() not in {"いいえ", "no", "false", "0"}
        try:
            ticket = await asyncio.to_thread(
                create_feedback, interaction.user.id, str(self.category.value), str(self.body.value),
                image_id=image_id, anonymous=anonymous,
            )
        except ValueError as exc:
            await interaction.followup.send(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.followup.send(
            f"📮 送信しました。受付番号は **{ticket}** です。\n"
            f"送信方法: **{'匿名' if anonymous else '記名'}**\n"
            "管理者が確認できる要望箱へ保存されました。",
            ephemeral=True,
        )


def _feedback_rows(status: str = "open", limit: int = 25) -> list[dict[str, Any]]:
    init_community_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            "SELECT * FROM community_feedback WHERE status=? ORDER BY id DESC LIMIT ?",
            (status, max(1, min(limit, 25))),
        ).fetchall()
        return [dict(row) for row in rows]


class CollectionCreateModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="コレクションを作成", timeout=300)
        self.name = discord.ui.TextInput(label="コレクション名", placeholder="例：ライブ、制服、夏", max_length=60)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        added = await asyncio.to_thread(_collection_create, interaction.user.id, str(self.name.value))
        await interaction.response.send_message(
            "✅ コレクションを作成しました。" if added else "ℹ️ 同じ名前のコレクションがあります。",
            ephemeral=True,
        )


class CollectionAddModal(discord.ui.Modal):
    def __init__(self, image_id: int | None = None) -> None:
        super().__init__(title="写真をコレクションへ追加", timeout=300)
        self.image_id = discord.ui.TextInput(
            label="写真ID", placeholder="例：733", default=str(image_id or ""), max_length=20
        )
        self.name = discord.ui.TextInput(label="コレクション名", placeholder="例：ライブ", max_length=60)
        self.add_item(self.image_id)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = str(self.image_id.value).strip()
        if not raw.isdigit():
            await interaction.response.send_message("⚠️ 写真IDは数字で入力してください。", ephemeral=True)
            return
        try:
            added = await asyncio.to_thread(_collection_add, interaction.user.id, str(self.name.value), int(raw))
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        if added:
            await asyncio.to_thread(record_usage_event, interaction.user.id, "collection", image_id=int(raw))
        await interaction.response.send_message(
            "✅ コレクションへ追加しました。" if added else "ℹ️ すでに追加済みです。",
            ephemeral=True,
        )


class CollectionHubView(discord.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=300)

    @discord.ui.button(label="新規作成", emoji="➕", style=discord.ButtonStyle.success)
    async def create(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CollectionCreateModal())

    @discord.ui.button(label="写真を追加", emoji="📷", style=discord.ButtonStyle.primary)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CollectionAddModal())

    @discord.ui.button(label="共有コード", emoji="🔗", style=discord.ButtonStyle.secondary)
    async def share(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(CollectionShareModal())


class FeedbackStatusSelect(discord.ui.Select):
    def __init__(self, ticket_no: str) -> None:
        self.ticket_no = ticket_no
        super().__init__(
            placeholder="対応状態を変更",
            options=[
                discord.SelectOption(label="未対応", value="open", emoji="📥"),
                discord.SelectOption(label="対応中", value="working", emoji="🛠️"),
                discord.SelectOption(label="対応済み", value="done", emoji="✅"),
                discord.SelectOption(label="却下", value="rejected", emoji="🚫"),
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        with closing(get_connection()) as con:
            con.execute(
                "UPDATE community_feedback SET status=?,updated_at=? WHERE ticket_no=?",
                (self.values[0], _now(), self.ticket_no),
            )
            con.commit()
        await interaction.response.send_message("✅ 状態を更新しました。", ephemeral=True)


class FeedbackReplyModal(discord.ui.Modal):
    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(title=f"{row['ticket_no']} へ返信", timeout=600)
        self.row = row
        self.reply = discord.ui.TextInput(label="返信内容", style=discord.TextStyle.paragraph, max_length=1500)
        self.add_item(self.reply)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        text = str(self.reply.value).strip()
        with closing(get_connection()) as con:
            con.execute(
                "UPDATE community_feedback SET admin_reply=?,status='done',updated_at=? WHERE ticket_no=?",
                (text, _now(), str(self.row['ticket_no'])),
            )
            con.commit()
        delivered = False
        raw_user_id = str(self.row.get('reporter_user_id') or '')
        if raw_user_id.isdigit():
            try:
                user = interaction.client.get_user(int(raw_user_id)) or await interaction.client.fetch_user(int(raw_user_id))
                await user.send(f"📮 **{self.row['ticket_no']} への管理者返信**\n{text}")
                delivered = True
            except (discord.Forbidden, discord.HTTPException, discord.NotFound):
                delivered = False
        await interaction.response.send_message(
            "✅ 返信を保存し、DMで送信しました。" if delivered else "✅ 返信を保存しました。DM送信はできませんでした。",
            ephemeral=True,
        )


class FeedbackAdminDetailView(discord.ui.View):
    def __init__(self, row: dict[str, Any]) -> None:
        super().__init__(timeout=None)
        self.row = row
        self.add_item(FeedbackStatusSelect(str(row["ticket_no"])))

    @discord.ui.button(label="匿名返信", emoji="✉️", style=discord.ButtonStyle.primary)
    async def reply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FeedbackReplyModal(self.row))


class FeedbackTicketSelect(discord.ui.Select):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = {str(row["ticket_no"]): row for row in rows}
        options = []
        for row in rows:
            title = f"{row['ticket_no']} | {row['category']}"
            desc = str(row["body"]).replace("\n", " ")[:95]
            options.append(discord.SelectOption(label=title[:100], value=str(row["ticket_no"]), description=desc))
        super().__init__(placeholder="報告を選択", options=options or [discord.SelectOption(label="未対応はありません", value="none")])
        self.disabled = not bool(rows)

    async def callback(self, interaction: discord.Interaction) -> None:
        row = self.rows.get(self.values[0])
        if not row:
            await interaction.response.send_message("未対応の報告はありません。", ephemeral=True)
            return
        reporter = "匿名" if int(row["anonymous"]) else f"ユーザーID {row['reporter_user_id']}"
        embed = discord.Embed(title=f"📮 {row['ticket_no']}", color=0xE67E22)
        embed.add_field(name="種類", value=str(row["category"]), inline=True)
        embed.add_field(name="投稿", value=reporter, inline=True)
        embed.add_field(name="写真ID", value=str(row["image_id"] or "なし"), inline=True)
        embed.add_field(name="内容", value=str(row["body"])[:1024], inline=False)
        embed.set_footer(text=f"状態: {row['status']} / {row['created_at']}")
        await interaction.response.send_message(embed=embed, view=FeedbackAdminDetailView(row), ephemeral=True)


class FeedbackAdminListView(discord.ui.View):
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        super().__init__(timeout=None)
        self.add_item(FeedbackTicketSelect(rows))


def get_ai_learning_stats(limit: int = 25) -> dict[str, Any]:
    """Return real local-face reference counts. No fabricated model accuracy."""
    with closing(get_connection()) as con:
        totals = con.execute(
            """SELECT
                COUNT(*) AS faces,
                SUM(CASE WHEN face_embedding<>'' THEN 1 ELSE 0 END) AS embeddings,
                SUM(CASE WHEN confirmed_person_id IS NOT NULL THEN 1 ELSE 0 END) AS confirmed
              FROM photo_faces"""
        ).fetchone()
        people = con.execute(
            """SELECT p.person_name,p.group_name,COUNT(*) AS reference_count
               FROM photo_faces f JOIN photo_people p ON p.id=f.confirmed_person_id
               WHERE f.face_embedding<>'' AND f.confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
               GROUP BY p.id ORDER BY reference_count DESC,p.person_name LIMIT ?""",
            (max(1, min(limit, 50)),),
        ).fetchall()
    return {
        "faces": int(totals["faces"] or 0),
        "embeddings": int(totals["embeddings"] or 0),
        "confirmed": int(totals["confirmed"] or 0),
        "people": [dict(row) for row in people],
    }


def operations_summary() -> dict[str, Any]:
    init_community_schema()
    with closing(get_connection()) as con:
        feedback = con.execute("SELECT status,COUNT(*) c FROM community_feedback GROUP BY status").fetchall()
        collections = int(con.execute("SELECT COUNT(*) FROM user_photo_collections").fetchone()[0])
        events = int(con.execute("SELECT COUNT(*) FROM photo_usage_events").fetchone()[0])
    return {"feedback": {r["status"]: int(r["c"]) for r in feedback}, "collections": collections, "events": events}



def _collection_rows(user_id: int) -> list[dict[str, Any]]:
    init_community_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            """SELECT c.id,c.collection_name,COUNT(i.image_id) AS item_count
               FROM user_photo_collections c
               LEFT JOIN user_photo_collection_items i ON i.collection_id=c.id
               WHERE c.discord_user_id=? GROUP BY c.id ORDER BY c.collection_name""",
            (str(user_id),),
        ).fetchall()
        return [dict(row) for row in rows]


def _collection_create(user_id: int, name: str) -> bool:
    clean = str(name or "").strip()[:60]
    if not clean:
        raise ValueError("コレクション名を入力してください。")
    init_community_schema()
    with closing(get_connection()) as con:
        cur = con.execute(
            "INSERT OR IGNORE INTO user_photo_collections(discord_user_id,collection_name,created_at) VALUES(?,?,?)",
            (str(user_id), clean, _now()),
        )
        con.commit()
        return cur.rowcount > 0


def _collection_add(user_id: int, name: str, image_id: int) -> bool:
    if get_photo_image(int(image_id)) is None:
        raise ValueError("写真IDが見つかりません。")
    _collection_create(user_id, name)
    with closing(get_connection()) as con:
        row = con.execute(
            "SELECT id FROM user_photo_collections WHERE discord_user_id=? AND collection_name=?",
            (str(user_id), str(name).strip()[:60]),
        ).fetchone()
        cur = con.execute(
            "INSERT OR IGNORE INTO user_photo_collection_items(collection_id,image_id,created_at) VALUES(?,?,?)",
            (int(row[0]), int(image_id), _now()),
        )
        con.commit()
        return cur.rowcount > 0



def _search_history_rows(user_id: int, limit: int = 15) -> list[dict[str, Any]]:
    init_community_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            """SELECT query_text,MAX(created_at) AS last_used,COUNT(*) AS used_count
               FROM photo_usage_events
               WHERE discord_user_hash=? AND event_type='search' AND query_text<>''
               GROUP BY query_text ORDER BY last_used DESC LIMIT ?""",
            (_anon_key(user_id), max(1, min(limit, 25))),
        ).fetchall()
        return [dict(r) for r in rows]

def _popular_rows(limit: int = 10, period: str = "all") -> list[dict[str, Any]]:
    init_community_schema()
    period = str(period or "all").lower()
    cutoff_clause = ""
    params: list[Any] = []
    if period == "week":
        cutoff_clause = "AND e.created_at>=?"
        params.append((datetime.now(timezone.utc) - timedelta(days=7)).isoformat())
    elif period == "month":
        cutoff_clause = "AND e.created_at>=?"
        params.append((datetime.now(timezone.utc) - timedelta(days=30)).isoformat())
    params.append(max(1, min(limit, 25)))
    with closing(get_connection()) as con:
        rows = con.execute(
            f"""SELECT i.*, COUNT(*) AS score,
                       b.title, b.member_name, b.group_name,
                       b.blog_url, b.published_at
                FROM photo_usage_events e
                JOIN photo_images i ON i.id=e.image_id
                JOIN photo_blogs b ON b.id=i.blog_id
                WHERE e.image_id IS NOT NULL
                  AND e.event_type IN ('detail','favorite','collection')
                  {cutoff_clause}
                GROUP BY i.id
                ORDER BY score DESC, i.id DESC
                LIMIT ?""",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


def _create_collection_share(user_id: int, collection_name: str) -> str:
    import secrets
    init_community_schema()
    clean = str(collection_name or "").strip()[:60]
    with closing(get_connection()) as con:
        row = con.execute(
            "SELECT id FROM user_photo_collections WHERE discord_user_id=? AND collection_name=?",
            (str(user_id), clean),
        ).fetchone()
        if not row:
            raise ValueError("指定したコレクションが見つかりません。")
        token = secrets.token_urlsafe(8)
        con.execute(
            "INSERT INTO user_collection_shares(share_token,collection_id,owner_user_id,created_at,is_active) VALUES(?,?,?,?,1)",
            (token, int(row[0]), str(user_id), _now()),
        )
        con.commit()
        return token


def _shared_collection_rows(token: str, limit: int = 25) -> tuple[str, list[dict[str, Any]]]:
    init_community_schema()
    with closing(get_connection()) as con:
        head = con.execute(
            """SELECT c.collection_name FROM user_collection_shares s
               JOIN user_photo_collections c ON c.id=s.collection_id
               WHERE s.share_token=? AND s.is_active=1""", (str(token),)
        ).fetchone()
        if not head:
            return "", []
        rows = con.execute(
            """SELECT i.id,b.title,b.member_name FROM user_collection_shares s
               JOIN user_photo_collection_items ci ON ci.collection_id=s.collection_id
               JOIN photo_images i ON i.id=ci.image_id
               JOIN photo_blogs b ON b.id=i.blog_id
               WHERE s.share_token=? AND s.is_active=1
               ORDER BY ci.created_at DESC LIMIT ?""",
            (str(token), max(1,min(limit,25))),
        ).fetchall()
        return str(head[0]), [dict(r) for r in rows]


class CollectionShareModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="コレクションを共有", timeout=300)
        self.name = discord.ui.TextInput(label="コレクション名", max_length=60)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            token = await asyncio.to_thread(_create_collection_share, interaction.user.id, str(self.name.value))
        except ValueError as exc:
            await interaction.response.send_message(f"⚠️ {exc}", ephemeral=True)
            return
        await interaction.response.send_message(
            "🔗 共有コードを発行しました。共有相手は次のコマンドで閲覧できます。\n"
            f"`!collection_shared {token}`\n"
            "共有画面は閲覧専用で、元のコレクションを変更できません。",
            ephemeral=True,
        )


def register_community_commands(bot: commands.Bot) -> None:
    init_community_schema()

    @bot.command(name="feedback_box")
    async def feedback_box(ctx: commands.Context) -> None:
        if ctx.interaction:
            await ctx.interaction.response.send_modal(FeedbackModal())
        else:
            await ctx.send("一般ユーザーパネルの『不具合・要望』ボタンから送信してください。")

    @bot.command(name="collection_list")
    async def collection_list(ctx: commands.Context) -> None:
        rows = await asyncio.to_thread(_collection_rows, ctx.author.id)
        if not rows:
            await ctx.send("📚 コレクションはまだありません。", view=CollectionHubView())
            return
        await ctx.send(
            "📚 **あなたのコレクション**\n"
            + "\n".join(f"・{r['collection_name']}：{r['item_count']}枚" for r in rows),
            view=CollectionHubView(),
        )

    @bot.command(name="collection_create")
    async def collection_create(ctx: commands.Context, *, name: str) -> None:
        added = await asyncio.to_thread(_collection_create, ctx.author.id, name)
        await ctx.send("✅ コレクションを作成しました。" if added else "ℹ️ 同じ名前のコレクションがあります。")

    @bot.command(name="collection_add")
    async def collection_add(ctx: commands.Context, image_id: int, *, name: str) -> None:
        try:
            added = await asyncio.to_thread(_collection_add, ctx.author.id, name, image_id)
        except ValueError as exc:
            await ctx.send(f"⚠️ {exc}")
            return
        if added:
            await asyncio.to_thread(record_usage_event, ctx.author.id, "collection", image_id=image_id)
        await ctx.send("✅ コレクションへ追加しました。" if added else "ℹ️ すでに追加済みです。")

    @bot.command(name="search_history")
    async def search_history(ctx: commands.Context, limit: int = 15) -> None:
        rows = await asyncio.to_thread(_search_history_rows, ctx.author.id, limit)
        if not rows:
            await ctx.send("🕘 検索履歴はまだありません。")
            return
        await ctx.send(
            "🕘 **最近の検索**\n"
            + "\n".join(f"・{r['query_text']}（{r['used_count']}回）" for r in rows)
        )

    @bot.command(name="popular_photos")
    async def popular_photos(ctx: commands.Context, period: str = "all", limit: int = 10) -> None:
        if str(period).isdigit():
            limit, period = int(period), "all"
        period = str(period).lower()
        if period not in {"week", "month", "all"}:
            await ctx.send("期間は `week` / `month` / `all` から選んでください。")
            return
        try:
            rows = await asyncio.to_thread(_popular_rows, limit, period)
        except sqlite3.OperationalError as exc:
            logger.exception(
                "Popular photos query failed (period=%s, limit=%s)",
                period,
                limit,
            )
            await ctx.send(
                "⚠️ 人気写真の取得に失敗しました。"
                "データベース構成をログで確認してください。"
            )
            return

        if not rows:
            await ctx.send("📈 人気データはまだありません。")
            return
        await ctx.send(
            f"📈 **人気写真（{'今週' if period=='week' else '今月' if period=='month' else '全期間'}）**\n"
            + "\n".join(
                f"{n}. 画像ID {r['image_id']}（{r['score']}） {r['member_name']} / {r['title']}"[:190]
                for n, r in enumerate(rows, 1)
            )
        )


    @bot.command(name="collection_shared")
    async def collection_shared(ctx: commands.Context, token: str) -> None:
        name, rows = await asyncio.to_thread(_shared_collection_rows, token, 25)
        if not rows:
            await ctx.send("⚠️ 共有コードが無効、またはコレクションが空です。")
            return
        await ctx.send(
            f"🔗 **共有コレクション：{name}**\n" +
            "\n".join(f"・画像ID {r['id']}：{r['member_name']} / {r['title']}"[:190] for r in rows)
        )

    @bot.command(name="feedback_admin")
    @commands.is_owner()
    async def feedback_admin(ctx: commands.Context) -> None:
        rows = await asyncio.to_thread(_feedback_rows, "open", 25)
        embed = discord.Embed(
            title="📬 報告・要望箱",
            description=f"未対応 **{len(rows)}件**（最新25件）\n報告を選ぶと詳細と対応状態を変更できます。",
            color=0xE67E22,
        )
        await ctx.send(embed=embed, view=FeedbackAdminListView(rows))

    @bot.command(name="ai_learning_status")
    @commands.is_owner()
    async def ai_learning_status(ctx: commands.Context) -> None:
        stats = await asyncio.to_thread(get_ai_learning_stats, 25)
        lines = [f"・{r['person_name']}（{r['group_name'] or 'その他'}）: {r['reference_count']}顔" for r in stats["people"]]
        embed = discord.Embed(
            title="🧠 AI学習データ状況",
            description=(
                f"検出顔: **{stats['faces']}**\n特徴量あり: **{stats['embeddings']}**\n"
                f"人物確定済み顔: **{stats['confirmed']}**\n\n"
                "確定済みの顔特徴は、次回のローカル類似顔候補生成で参照されます。\n"
                "※ここでは実測できない『精度%』は表示しません。"
            ),
            color=0x9B59B6,
        )
        embed.add_field(name="学習元が多い人物", value="\n".join(lines[:25]) or "まだありません。", inline=False)
        await ctx.send(embed=embed)

    @bot.command(name="operations_dashboard")
    @commands.is_owner()
    async def operations_dashboard(ctx: commands.Context) -> None:
        summary, ai = await asyncio.gather(
            asyncio.to_thread(operations_summary), asyncio.to_thread(get_ai_learning_stats, 10)
        )
        feedback = summary["feedback"]
        embed = discord.Embed(title="📊 運営・AIダッシュボード", color=0x2C3E50)
        embed.add_field(
            name="報告・要望",
            value=f"未対応 {feedback.get('open',0)} / 対応中 {feedback.get('working',0)} / 対応済み {feedback.get('done',0)}",
            inline=False,
        )
        embed.add_field(
            name="AI学習基盤",
            value=f"特徴量 {ai['embeddings']} / 人物確定済み顔 {ai['confirmed']}",
            inline=False,
        )
        embed.add_field(name="ユーザー機能", value=f"コレクション {summary['collections']} / 利用イベント {summary['events']}", inline=False)
        await ctx.send(embed=embed)

    @bot.command(name="photo_db_backup")
    @commands.is_owner()
    async def photo_db_backup(ctx: commands.Context) -> None:
        source = Path(PHOTO_DB_PATH)
        if not source.exists():
            await ctx.send("⚠️ 写真DBが見つかりません。")
            return
        target = Path("/tmp") / f"photo_archive_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.db"

        def _sqlite_backup() -> None:
            # 稼働中DBは単純コピーせず、SQLiteのオンラインバックアップAPIを使う。
            with sqlite3.connect(source) as src, sqlite3.connect(target) as dst:
                src.backup(dst)

        await asyncio.to_thread(_sqlite_backup)
        if target.stat().st_size > 24 * 1024 * 1024:
            await ctx.send(f"✅ バックアップを作成しましたが、Discord添付上限を超えています。\n`{target}`")
            return
        await ctx.send("✅ 写真DBバックアップです。", file=discord.File(target))
        try:
            target.unlink(missing_ok=True)
        except OSError:
            pass
