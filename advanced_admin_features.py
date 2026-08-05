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

    @discord.ui.button(label="更新", emoji="🔄", style=discord.ButtonStyle.success)
    async def refresh(self, interaction, _):
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_center_embed), view=self)


async def send_ai_center(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    await interaction.followup.send(embed=await asyncio.to_thread(ai_center_embed), view=AICenterView(interaction.user.id), ephemeral=True)
