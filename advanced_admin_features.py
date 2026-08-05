"""低コスト運用を重視した管理者向け高度機能。

有料APIを自動で増やさず、手動解析キュー、AI評価統計、人物セット、
保留理由、仮確定、変更スナップショットの確認導線を提供する。
"""
from __future__ import annotations

import asyncio
import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import discord

from ai_cost_control import get_ai_cost_status, simulate_pending_api_usage
from embed_safety import safe_add_field
from photo_database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_advanced_admin_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_admin_change_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL DEFAULT 0,
                action_type TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                snapshot_json TEXT NOT NULL DEFAULT '{}',
                restored_at TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_person_sets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                set_name TEXT NOT NULL UNIQUE,
                people_json TEXT NOT NULL DEFAULT '[]',
                created_by INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_review_hold_reasons (
                image_id INTEGER PRIMARY KEY,
                reason_code TEXT NOT NULL DEFAULT 'LATER',
                note TEXT NOT NULL DEFAULT '',
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_provisional_people (
                image_id INTEGER NOT NULL,
                person_name TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'local_ai',
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(image_id, person_name)
            );
            CREATE TABLE IF NOT EXISTS photo_admin_work_sessions (
                admin_user_id INTEGER PRIMARY KEY,
                session_kind TEXT NOT NULL DEFAULT '',
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at TEXT NOT NULL
            );
            """
        )
        con.commit()


def _table_exists(con, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def advanced_stats() -> dict[str, int]:
    init_advanced_admin_schema()
    with closing(get_connection()) as con:
        def count(sql: str) -> int:
            try:
                row = con.execute(sql).fetchone()
                return int(row[0] or 0) if row else 0
            except Exception:
                return 0
        return {
            "pending": count("SELECT COUNT(*) FROM photo_images WHERE analysis_status='pending'"),
            "failed": count("SELECT COUNT(*) FROM photo_images WHERE analysis_status='failed'"),
            "person_sets": count("SELECT COUNT(*) FROM photo_person_sets"),
            "holds": count("SELECT COUNT(*) FROM photo_review_hold_reasons"),
            "provisional": count("SELECT COUNT(DISTINCT image_id) FROM photo_provisional_people"),
            "snapshots": count("SELECT COUNT(*) FROM photo_admin_change_snapshots WHERE restored_at=''"),
            "decisions": count("SELECT COUNT(*) FROM photo_ai_decision_log") if _table_exists(con, "photo_ai_decision_log") else 0,
            "accepted": count("SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='accepted'") if _table_exists(con, "photo_ai_decision_log") else 0,
            "corrected": count("SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='corrected'") if _table_exists(con, "photo_ai_decision_log") else 0,
        }


def ai_center_embed() -> discord.Embed:
    stats = advanced_stats()
    cost = get_ai_cost_status()
    e = discord.Embed(
        title="🤖 AI育成センター",
        description=(
            "有料APIは管理者が明示した時だけ使用します。統計・人物セット・保留・仮確定はDBだけで動作します。"
        ),
        color=0x5865F2,
    )
    safe_add_field(e, name="⏳ 解析キュー", value=f"待ち **{stats['pending']:,}**\nエラー **{stats['failed']:,}**", inline=True)
    safe_add_field(e, name="💰 API上限", value=(
        f"今日 **{cost.get('daily_used',0):,}/{cost.get('daily_image_limit',0):,}**\n"
        f"今月 **{cost.get('monthly_used',0):,}/{cost.get('monthly_image_limit',0):,}**\n"
        f"自動API **{'ON' if int(cost.get('auto_api_enabled',0)) else 'OFF'}**"
    ), inline=True)
    rate = round(stats['accepted'] * 100 / stats['decisions'], 1) if stats['decisions'] else None
    safe_add_field(e, name="📊 AI評価", value=(
        f"評価 **{stats['decisions']:,}**\n採用 **{stats['accepted']:,}**\n修正 **{stats['corrected']:,}**\n"
        f"採用率 **{rate:.1f}%**" if rate is not None else "評価データはまだありません。"
    ), inline=True)
    safe_add_field(e, name="🧰 管理用データ", value=(
        f"人物セット **{stats['person_sets']:,}**\n保留理由 **{stats['holds']:,}**\n"
        f"仮確定写真 **{stats['provisional']:,}**\n未復元スナップショット **{stats['snapshots']:,}**"
    ), inline=False)
    e.set_footer(text="手動解析は実行前に送信予定枚数を確認します。")
    return e


class ManualAnalyzeConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, limit: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.limit = max(1, min(int(limit), 500))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この確認画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="上限内で解析する", emoji="▶️", style=discord.ButtonStyle.danger)
    async def run(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        from photo_ai_analyzer import analyze_pending_images
        result = await analyze_pending_images(self.limit, manual_api=True)
        reasons = result.get("blocked_reasons") or {}
        reason_text = "\n".join(f"・{k}: {v}件" for k, v in reasons.items()) or "なし"
        e = discord.Embed(title="🤖 手動AI解析結果", color=0x57F287)
        safe_add_field(e, name="結果", value=(
            f"検出 **{result.get('found',0)}**\nAPI送信 **{result.get('api_sent',0)}**\n"
            f"キャッシュ **{result.get('cache_reused',0)}**\n完了 **{result.get('completed',0)}**\n"
            f"確認待ち **{result.get('review',0)}**\nスキップ **{result.get('blocked',0)}**\n"
            f"失敗 **{result.get('failed',0)}**"
        ), inline=False)
        safe_add_field(e, name="スキップ理由", value=reason_text, inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キャンセルしました。", embed=None, view=None)
        self.stop()


async def send_manual_preview(interaction: discord.Interaction, limit: int) -> None:
    result = await asyncio.to_thread(simulate_pending_api_usage, limit)
    e = discord.Embed(title=f"🧮 手動解析前の確認（最大{limit}件）", color=0xFEE75C)
    safe_add_field(e, name="送信予定", value=(
        f"確認対象 **{result.get('inspected',0)}**\nキャッシュ再利用 **{result.get('cache_reuse',0)}**\n"
        f"API送信候補 **{result.get('api_candidates',0)}**\n現在送信可能 **{result.get('api_sendable_now',0)}**"
    ), inline=False)
    safe_add_field(e, name="残り上限", value=(
        f"本日 **{result.get('daily_remaining',0)}**\n今月 **{result.get('monthly_remaining',0)}**"
    ), inline=True)
    reasons = result.get("reasons") or []
    if reasons:
        safe_add_field(e, name="制限理由", value="\n".join(f"・{x}" for x in reasons), inline=False)
    e.set_footer(text="確定するまでAPIは呼びません。")
    await interaction.followup.send(embed=e, view=ManualAnalyzeConfirmView(interaction.user.id, limit), ephemeral=True)


class PersonSetModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="人物セットを保存", timeout=300)
        self.set_name = discord.ui.TextInput(label="セット名", placeholder="例：金村美玖＋小坂菜緒", max_length=80)
        self.people = discord.ui.TextInput(label="人物名（読点・カンマ区切り）", placeholder="金村美玖、小坂菜緒", style=discord.TextStyle.paragraph, max_length=1000)
        self.add_item(self.set_name); self.add_item(self.people)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        import re
        names = []
        for part in re.split(r"[,、，\n]+", str(self.people.value)):
            name = part.strip()
            if name and name not in names:
                names.append(name)
        if not names:
            await interaction.response.send_message("人物名を1人以上入力してください。", ephemeral=True)
            return
        now = _now()
        with closing(get_connection()) as con:
            con.execute(
                """INSERT INTO photo_person_sets(set_name,people_json,created_by,created_at,updated_at)
                   VALUES(?,?,?,?,?) ON CONFLICT(set_name) DO UPDATE SET
                   people_json=excluded.people_json,created_by=excluded.created_by,updated_at=excluded.updated_at""",
                (str(self.set_name.value).strip(), json.dumps(names, ensure_ascii=False), interaction.user.id, now, now),
            )
            con.commit()
        await interaction.response.send_message(f"✅ 人物セット **{self.set_name.value}** を保存しました。\n" + "、".join(names), ephemeral=True)


def person_sets_embed() -> discord.Embed:
    init_advanced_admin_schema()
    with closing(get_connection()) as con:
        rows = con.execute("SELECT set_name,people_json FROM photo_person_sets ORDER BY updated_at DESC LIMIT 25").fetchall()
    text = []
    for row in rows:
        try:
            people = json.loads(row[1] or "[]")
        except Exception:
            people = []
        text.append(f"**{row[0]}**\n{'、'.join(people) or '人物なし'}")
    return discord.Embed(title="👥 人物セット", description="\n\n".join(text)[:4000] or "まだ登録されていません。", color=0xEB459E)


class AICenterView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="今回1枚だけ", emoji="1️⃣", style=discord.ButtonStyle.primary)
    async def one(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await send_manual_preview(interaction, 1)

    @discord.ui.button(label="先頭5枚", emoji="5️⃣", style=discord.ButtonStyle.primary)
    async def five(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await send_manual_preview(interaction, 5)

    @discord.ui.button(label="先頭20枚", emoji="🔢", style=discord.ButtonStyle.primary)
    async def twenty(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await send_manual_preview(interaction, 20)

    @discord.ui.button(label="人物セット保存", emoji="👥", style=discord.ButtonStyle.secondary)
    async def save_set(self, interaction, _):
        await interaction.response.send_modal(PersonSetModal())

    @discord.ui.button(label="人物セット一覧", emoji="📚", style=discord.ButtonStyle.secondary)
    async def sets(self, interaction, _):
        await interaction.response.send_message(embed=await asyncio.to_thread(person_sets_embed), ephemeral=True)

    @discord.ui.button(label="保留理由", emoji="⏸️", style=discord.ButtonStyle.secondary)
    async def holds(self, interaction, _):
        await _send_ai_db_list(interaction, "holds")

    @discord.ui.button(label="仮確定", emoji="🧪", style=discord.ButtonStyle.secondary)
    async def provisional(self, interaction, _):
        await _send_ai_db_list(interaction, "provisional")

    @discord.ui.button(label="変更履歴", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def snapshots(self, interaction, _):
        await _send_ai_db_list(interaction, "snapshots")

    @discord.ui.button(label="AI判定履歴", emoji="📜", style=discord.ButtonStyle.secondary)
    async def decisions(self, interaction, _):
        await _send_ai_db_list(interaction, "decisions")

    @discord.ui.button(label="更新", emoji="🔄", style=discord.ButtonStyle.success)
    async def refresh(self, interaction, _):
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_center_embed), view=self)


async def send_ai_center(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await asyncio.to_thread(ai_center_embed), view=AICenterView(interaction.user.id), ephemeral=True)

# ---------------------------------------------------------------------------
# AI育成センター Phase 2: DBだけで閲覧できる管理一覧
# ---------------------------------------------------------------------------

def _safe_json_list(value: object) -> list[str]:
    try:
        parsed = json.loads(str(value or "[]"))
    except Exception:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(x).strip() for x in parsed if str(x).strip()]


def _paged_rows(table: str, columns: str, order_by: str, page: int, page_size: int = 20):
    page_size = max(1, min(int(page_size), 25))
    page = max(0, int(page))
    with closing(get_connection()) as con:
        total = int(con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] or 0)
        rows = con.execute(
            f"SELECT {columns} FROM {table} ORDER BY {order_by} LIMIT ? OFFSET ?",
            (page_size, page * page_size),
        ).fetchall()
    return total, rows


class AIDBListView(discord.ui.View):
    """AI育成関連のDB一覧を25件上限で安全にページ表示する。"""

    def __init__(self, owner_id: int, kind: str, page: int = 0) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.kind = str(kind)
        self.page = max(0, int(page))
        self.page_size = 20

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    def _load(self):
        init_advanced_admin_schema()
        if self.kind == "sets":
            return _paged_rows("photo_person_sets", "id,set_name,people_json,updated_at", "updated_at DESC", self.page, self.page_size)
        if self.kind == "holds":
            return _paged_rows("photo_review_hold_reasons", "image_id,reason_code,note,updated_at", "updated_at DESC", self.page, self.page_size)
        if self.kind == "provisional":
            return _paged_rows("photo_provisional_people", "image_id,person_name,source,confidence,created_at", "created_at DESC", self.page, self.page_size)
        if self.kind == "snapshots":
            return _paged_rows("photo_admin_change_snapshots", "id,action_type,target_type,target_id,restored_at,created_at", "id DESC", self.page, self.page_size)
        with closing(get_connection()) as con:
            exists = _table_exists(con, "photo_ai_decision_log")
        if not exists:
            return 0, []
        return _paged_rows("photo_ai_decision_log", "id,image_id,decision,suggested_person,confirmed_person,created_at", "id DESC", self.page, self.page_size)

    def embed(self) -> discord.Embed:
        total, rows = self._load()
        names = {
            "sets": "👥 人物セット",
            "holds": "⏸️ 保留理由",
            "provisional": "🧪 仮確定",
            "snapshots": "↩️ 変更スナップショット",
            "decisions": "📜 AI判定履歴",
        }
        lines: list[str] = []
        for row in rows:
            values = tuple(row)
            if self.kind == "sets":
                lines.append(f"`#{values[0]}` **{values[1]}**\n{'、'.join(_safe_json_list(values[2])) or '人物なし'}")
            elif self.kind == "holds":
                lines.append(f"画像ID `{values[0]}`  **{values[1]}**\n{str(values[2] or 'メモなし')[:160]}")
            elif self.kind == "provisional":
                confidence = float(values[3] or 0)
                lines.append(f"画像ID `{values[0]}`  **{values[1]}**\n出所: {values[2]} / 信頼値: {confidence:.3f}")
            elif self.kind == "snapshots":
                state = "復元済み" if str(values[4] or '') else "未復元"
                lines.append(f"`#{values[0]}` **{values[1]}**  {state}\n{values[2]}:{values[3]}")
            else:
                lines.append(f"`#{values[0]}` 画像ID `{values[1]}` **{values[2]}**\nAI: {str(values[3] or '')[:100]}\n確定: {str(values[4] or '')[:100]}")
        start = self.page * self.page_size + 1 if total else 0
        end = min(total, (self.page + 1) * self.page_size)
        e = discord.Embed(
            title=names.get(self.kind, "AI管理一覧"),
            description="\n\n".join(lines)[:4000] or "該当データはありません。",
            color=0x5865F2,
        )
        e.set_footer(text=f"表示 {start}〜{end}件 / 全{total}件  |  {self.page + 1}/{max(1, (total + self.page_size - 1)//self.page_size)}ページ")
        self.previous.disabled = self.page <= 0
        self.next.disabled = end >= total
        return e

    @discord.ui.button(label="前のページ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous(self, interaction, _):
        self.page = max(0, self.page - 1)
        await interaction.response.edit_message(embed=await asyncio.to_thread(self.embed), view=self)

    @discord.ui.button(label="次のページ", emoji="▶️", style=discord.ButtonStyle.secondary)
    async def next(self, interaction, _):
        self.page += 1
        await interaction.response.edit_message(embed=await asyncio.to_thread(self.embed), view=self)

    @discord.ui.button(label="AI育成センターへ", emoji="↩️", style=discord.ButtonStyle.primary)
    async def back(self, interaction, _):
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_center_embed), view=AICenterView(self.owner_id))


async def _send_ai_db_list(interaction: discord.Interaction, kind: str) -> None:
    await interaction.response.defer(ephemeral=True)
    view = AIDBListView(interaction.user.id, kind)
    await interaction.followup.send(embed=await asyncio.to_thread(view.embed), view=view, ephemeral=True)
