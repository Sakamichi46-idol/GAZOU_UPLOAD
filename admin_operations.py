"""管理者向け運用ダッシュボード。

既存の管理ワークフローを壊さず、状態把握、前回作業の再開、
エラー再試行、AI候補の実測状況、監査ログ、人物マスター管理、
DB健全性確認を一か所へ集約する。
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import discord
from discord.ext import commands
from embed_safety import safe_add_field
from advanced_admin_features import (
    init_advanced_admin_schema,
    send_ai_center,
)
from ai_cost_control import (
    get_ai_cost_status,
    simulate_pending_api_usage,
    update_ai_cost_settings,
)

from photo_database import (
    get_photo_blog_for_admin_edit,
    HIDDEN_REASON_LABELS,
    delete_hidden_photo_blog,
    get_connection,
    get_hidden_photo_blog,
    list_hidden_photo_blogs,
    queue_hidden_blog_reanalysis,
    restore_hidden_photo_blog,
)

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(con, table: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(con, table: str) -> set[str]:
    if not _table_exists(con, table):
        return set()
    return {str(row[1]) for row in con.execute(f"PRAGMA table_info({table})").fetchall()}


def init_admin_operations_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_admin_audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL DEFAULT 0,
                action_type TEXT NOT NULL,
                target_type TEXT NOT NULL DEFAULT '',
                target_id TEXT NOT NULL DEFAULT '',
                detail TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photo_admin_audit_time
              ON photo_admin_audit_log(created_at DESC);

            CREATE TABLE IF NOT EXISTS photo_ai_decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL DEFAULT 0,
                image_id INTEGER NOT NULL DEFAULT 0,
                face_id INTEGER NOT NULL DEFAULT 0,
                decision TEXT NOT NULL,
                suggested_person TEXT NOT NULL DEFAULT '',
                confirmed_person TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photo_ai_decision_time
              ON photo_ai_decision_log(created_at DESC);

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
            CREATE INDEX IF NOT EXISTS idx_photo_admin_snapshot_time
              ON photo_admin_change_snapshots(created_at DESC);

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
            """
        )
        con.commit()


def write_audit(
    admin_user_id: int,
    action_type: str,
    *,
    target_type: str = "",
    target_id: str | int = "",
    detail: str = "",
) -> None:
    init_admin_operations_schema()
    with closing(get_connection()) as con:
        con.execute(
            """INSERT INTO photo_admin_audit_log(
                   admin_user_id,action_type,target_type,target_id,detail,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (
                int(admin_user_id),
                str(action_type)[:100],
                str(target_type)[:100],
                str(target_id)[:200],
                str(detail)[:2000],
                _now(),
            ),
        )
        con.commit()


def _scalar(con, sql: str, params: tuple[Any, ...] = ()) -> int:
    try:
        row = con.execute(sql, params).fetchone()
        return int(row[0] or 0) if row else 0
    except Exception:
        return 0


def get_admin_dashboard_stats() -> dict[str, int]:
    init_admin_operations_schema()
    with closing(get_connection()) as con:
        stats = {
            "images": _scalar(con, "SELECT COUNT(*) FROM photo_images") if _table_exists(con, "photo_images") else 0,
            "blogs": _scalar(con, "SELECT COUNT(*) FROM photo_blogs") if _table_exists(con, "photo_blogs") else 0,
            "hidden_blogs": _scalar(con, "SELECT COUNT(*) FROM photo_blogs WHERE COALESCE(is_hidden,0)=1") if "is_hidden" in _columns(con, "photo_blogs") else 0,
            "people": _scalar(con, "SELECT COUNT(*) FROM photo_people") if _table_exists(con, "photo_people") else 0,
            "faces": _scalar(con, "SELECT COUNT(*) FROM photo_faces") if _table_exists(con, "photo_faces") else 0,
            "embeddings": _scalar(con, "SELECT COUNT(*) FROM photo_faces WHERE TRIM(COALESCE(face_embedding,''))<>''") if _table_exists(con, "photo_faces") else 0,
            "confirmed_faces": _scalar(con, "SELECT COUNT(*) FROM photo_faces WHERE confirmed_person_id IS NOT NULL") if _table_exists(con, "photo_faces") else 0,
            "pending_reviews": _scalar(con, "SELECT COUNT(*) FROM photo_review_queue WHERE status='pending'") if _table_exists(con, "photo_review_queue") else 0,
            "skipped_reviews": _scalar(con, "SELECT COUNT(*) FROM photo_review_queue WHERE status='skipped'") if _table_exists(con, "photo_review_queue") else 0,
            "download_errors": _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE download_status='failed'") if _table_exists(con, "photo_images") else 0,
            "analysis_errors": _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status='failed'") if _table_exists(con, "photo_images") else 0,
            "face_errors": _scalar(con, "SELECT COUNT(*) FROM photo_face_scans WHERE status='failed'") if _table_exists(con, "photo_face_scans") else 0,
            "storage_pending": _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE download_status='pending'") if _table_exists(con, "photo_images") else 0,
            "analysis_pending": _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status='pending'") if _table_exists(con, "photo_images") else 0,
            "face_pending": _scalar(con, "SELECT COUNT(*) FROM photo_face_scans WHERE status='pending'") if _table_exists(con, "photo_face_scans") else 0,
        }
        if _table_exists(con, "community_feedback"):
            cols = _columns(con, "community_feedback")
            status_col = "status" if "status" in cols else None
            stats["feedback_pending"] = _scalar(con, "SELECT COUNT(*) FROM community_feedback WHERE status='pending'") if status_col else 0
        else:
            stats["feedback_pending"] = 0
    stats["errors"] = stats["download_errors"] + stats["analysis_errors"] + stats["face_errors"]
    return stats


def dashboard_embed() -> discord.Embed:
    s = get_admin_dashboard_stats()
    embed = discord.Embed(
        title="📊 管理者運用ダッシュボード",
        description="優先度の高い作業とシステム状態をまとめて確認できます。",
        color=0x5865F2,
        timestamp=datetime.now(timezone.utc),
    )
    safe_add_field(embed, 
        name="📷 アーカイブ",
        value=f"記事 **{s['blogs']:,}**（除外 **{s['hidden_blogs']:,}**）\n画像 **{s['images']:,}**\n人物 **{s['people']:,}**",
        inline=True,
    )
    safe_add_field(embed, 
        name="✅ 人物確認",
        value=f"未確認 **{s['pending_reviews']:,}**\nスキップ **{s['skipped_reviews']:,}**\n確定顔 **{s['confirmed_faces']:,}**",
        inline=True,
    )
    safe_add_field(embed, 
        name="⚠️ エラー",
        value=(f"画像取得 **{s['download_errors']:,}**\n"
               f"AI解析 **{s['analysis_errors']:,}**\n"
               f"顔認証 **{s['face_errors']:,}**"),
        inline=True,
    )
    safe_add_field(embed, 
        name="⏳ 待機中",
        value=(f"保存 **{s['storage_pending']:,}**\n"
               f"AI解析 **{s['analysis_pending']:,}**\n"
               f"顔認証 **{s['face_pending']:,}**"),
        inline=True,
    )
    safe_add_field(embed, 
        name="🧠 AI学習元",
        value=f"検出顔 **{s['faces']:,}**\n特徴量 **{s['embeddings']:,}**\n確定済み **{s['confirmed_faces']:,}**",
        inline=True,
    )
    safe_add_field(embed, 
        name="📬 要望箱",
        value=f"未対応 **{s['feedback_pending']:,}**",
        inline=True,
    )
    embed.set_footer(text="数字は現在のDB状態から取得しています。")
    return embed


def get_ai_quality_stats() -> dict[str, Any]:
    init_admin_operations_schema()
    with closing(get_connection()) as con:
        decisions = _scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log")
        accepted = _scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='accepted'")
        corrected = _scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='corrected'")
        no_candidate = _scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='no_candidate'")
        people = []
        if _table_exists(con, "photo_faces") and _table_exists(con, "photo_people"):
            people = [dict(r) for r in con.execute(
                """SELECT pp.person_name,
                          COUNT(pf.id) AS confirmed_faces,
                          SUM(CASE WHEN TRIM(COALESCE(pf.face_embedding,''))<>'' THEN 1 ELSE 0 END) AS embeddings,
                          MAX(pf.confirmed_at) AS last_confirmed_at
                   FROM photo_people pp
                   LEFT JOIN photo_faces pf ON pf.confirmed_person_id=pp.id
                   GROUP BY pp.id
                   HAVING COUNT(pf.id)>0
                   ORDER BY confirmed_faces DESC LIMIT 20"""
            ).fetchall()]
    rate = round(accepted * 100 / decisions, 1) if decisions else None
    return {"decisions": decisions, "accepted": accepted, "corrected": corrected,
            "no_candidate": no_candidate, "acceptance_rate": rate, "people": people}


def ai_quality_embed() -> discord.Embed:
    d = get_ai_quality_stats()
    rate_text = f"{d['acceptance_rate']:.1f}%" if d["acceptance_rate"] is not None else "未計測"
    embed = discord.Embed(title="🧠 AI学習・候補品質", color=0x9B59B6)
    embed.description = (
        f"候補判定記録 **{d['decisions']:,}件**\n"
        f"候補どおり **{d['accepted']:,}件** / 修正 **{d['corrected']:,}件** / 候補なし **{d['no_candidate']:,}件**\n"
        f"実測採用率 **{rate_text}**\n\n"
        "※ 採用率は記録開始後の管理者判断だけから計算します。"
    )
    lines = [
        f"・{p['person_name']}: 確定 {int(p['confirmed_faces'] or 0):,} / 特徴量 {int(p['embeddings'] or 0):,}"
        for p in d["people"][:15]
    ]
    safe_add_field(embed, name="人物別の学習元", value="\n".join(lines) or "まだデータがありません。", inline=False)
    return embed


def get_health_report() -> dict[str, Any]:
    required: dict[str, set[str]] = {
        "photo_images": {"id", "blog_id", "download_status", "analysis_status"},
        "photo_blogs": {"id", "title", "member_name", "is_hidden", "hidden_reason"},
        "photo_people": {"id", "person_name"},
        "photo_faces": {"id", "image_id", "confirmed_person_id", "face_embedding"},
    }
    with closing(get_connection()) as con:
        missing_tables = [t for t in required if not _table_exists(con, t)]
        missing_columns: list[str] = []
        for table, cols in required.items():
            actual = _columns(con, table)
            for col in sorted(cols - actual):
                missing_columns.append(f"{table}.{col}")
        orphan_images = _scalar(con, "SELECT COUNT(*) FROM photo_images i LEFT JOIN photo_blogs b ON b.id=i.blog_id WHERE b.id IS NULL") if _table_exists(con, "photo_images") and _table_exists(con, "photo_blogs") else 0
        orphan_faces = _scalar(con, "SELECT COUNT(*) FROM photo_faces f LEFT JOIN photo_images i ON i.id=f.image_id WHERE i.id IS NULL") if _table_exists(con, "photo_faces") and _table_exists(con, "photo_images") else 0
        orphan_people = _scalar(con, "SELECT COUNT(*) FROM photo_faces f LEFT JOIN photo_people p ON p.id=f.confirmed_person_id WHERE f.confirmed_person_id IS NOT NULL AND p.id IS NULL") if _table_exists(con, "photo_faces") and _table_exists(con, "photo_people") else 0
        duplicate_urls = _scalar(con, "SELECT COUNT(*) FROM (SELECT image_url FROM photo_images WHERE TRIM(COALESCE(image_url,''))<>'' GROUP BY image_url HAVING COUNT(*)>1)") if "image_url" in _columns(con, "photo_images") else 0
        stale_download = _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE download_status<>'failed' AND TRIM(COALESCE(download_error,''))<>''") if "download_error" in _columns(con, "photo_images") else 0
        stale_analysis = _scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status<>'failed' AND TRIM(COALESCE(analysis_error,''))<>''") if "analysis_error" in _columns(con, "photo_images") else 0
        hidden_blogs = _scalar(con, "SELECT COUNT(*) FROM photo_blogs WHERE COALESCE(is_hidden,0)=1") if "is_hidden" in _columns(con, "photo_blogs") else 0
        unknown_member_candidates = _scalar(
            con,
            "SELECT COUNT(*) FROM photo_blogs WHERE COALESCE(is_hidden,0)=0 AND TRIM(COALESCE(member_name,'')) IN ('','不明','投稿者不明')",
        ) if _table_exists(con, "photo_blogs") else 0
        integrity_row = con.execute("PRAGMA integrity_check").fetchone()
        integrity = str(integrity_row[0]) if integrity_row else "unknown"
    return {
        "missing_tables": missing_tables,
        "missing_columns": missing_columns,
        "orphan_images": orphan_images,
        "orphan_faces": orphan_faces,
        "orphan_people": orphan_people,
        "duplicate_urls": duplicate_urls,
        "stale_errors": stale_download + stale_analysis,
        "hidden_blogs": hidden_blogs,
        "unknown_member_candidates": unknown_member_candidates,
        "integrity": integrity,
    }


def health_embed() -> discord.Embed:
    r = get_health_report()
    problems = len(r["missing_tables"]) + len(r["missing_columns"]) + r["orphan_images"] + r["orphan_faces"] + r["orphan_people"] + r["stale_errors"]
    embed = discord.Embed(
        title="🩺 DB・保存データ健全性",
        color=0x57F287 if problems == 0 and r["integrity"] == "ok" else 0xFEE75C,
    )
    safe_add_field(embed, name="SQLite整合性", value=r["integrity"], inline=True)
    safe_add_field(embed, name="不足テーブル", value=str(len(r["missing_tables"])), inline=True)
    safe_add_field(embed, name="不足カラム", value=str(len(r["missing_columns"])), inline=True)
    safe_add_field(embed, name="孤立画像", value=f"{r['orphan_images']:,}", inline=True)
    safe_add_field(embed, name="孤立顔", value=f"{r['orphan_faces']:,}", inline=True)
    safe_add_field(embed, name="孤立人物参照", value=f"{r['orphan_people']:,}", inline=True)
    safe_add_field(embed, name="重複URL群", value=f"{r['duplicate_urls']:,}", inline=True)
    safe_add_field(embed, name="古いエラー文字", value=f"{r['stale_errors']:,}", inline=True)
    safe_add_field(embed, name="除外済み記事", value=f"{r['hidden_blogs']:,}", inline=True)
    safe_add_field(embed, name="投稿者不明の除外候補", value=f"{r['unknown_member_candidates']:,}", inline=True)
    details = []
    if r["missing_tables"]:
        details.append("不足テーブル: " + ", ".join(r["missing_tables"][:10]))
    if r["missing_columns"]:
        details.append("不足カラム: " + ", ".join(r["missing_columns"][:15]))
    if details:
        safe_add_field(embed, name="詳細", value="\n".join(details)[:1024], inline=False)
    embed.set_footer(text="この画面は診断のみです。削除や自動修復は行いません。")
    return embed


def record_ai_decision(
    admin_user_id: int,
    image_id: int,
    face_id: int,
    decision: str,
    *,
    suggested_person: str = "",
    confirmed_person: str = "",
    confidence: float = 0.0,
) -> None:
    init_admin_operations_schema()
    with closing(get_connection()) as con:
        con.execute(
            """INSERT INTO photo_ai_decision_log(
                   admin_user_id,image_id,face_id,decision,suggested_person,
                   confirmed_person,confidence,created_at
               ) VALUES(?,?,?,?,?,?,?,?)""",
            (
                int(admin_user_id), int(image_id), int(face_id), str(decision),
                str(suggested_person)[:100], str(confirmed_person)[:100],
                float(confidence or 0), _now(),
            ),
        )
        con.commit()


def list_audit(limit: int = 20) -> list[dict[str, Any]]:
    init_admin_operations_schema()
    with closing(get_connection()) as con:
        return [dict(r) for r in con.execute(
            "SELECT * FROM photo_admin_audit_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ).fetchall()]


def reset_error_type(error_type: str) -> int:
    now = _now()
    with closing(get_connection()) as con:
        if error_type == "download":
            cur = con.execute("UPDATE photo_images SET download_status='pending',download_error='',updated_at=? WHERE download_status='failed'", (now,))
        elif error_type == "analysis":
            cur = con.execute("UPDATE photo_images SET analysis_status='pending',analysis_error='',updated_at=? WHERE analysis_status='failed'", (now,))
        elif error_type == "face":
            count = _scalar(con, "SELECT COUNT(*) FROM photo_face_scans WHERE status='failed'") if _table_exists(con, "photo_face_scans") else 0
            con.execute("DELETE FROM photo_face_scans WHERE status='failed'")
            con.commit()
            return count
        else:
            return 0
        con.commit()
        return max(0, int(cur.rowcount or 0))


class ConfirmErrorResetView(discord.ui.View):
    def __init__(self, owner_id: int, error_type: str, count: int) -> None:
        super().__init__(timeout=120)
        self.owner_id = int(owner_id)
        self.error_type = error_type
        self.count = int(count)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この確認画面は操作した管理者だけが使えます。", ephemeral=True)
        return False

    @discord.ui.button(label="再試行待ちへ戻す", emoji="♻️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        changed = await asyncio.to_thread(reset_error_type, self.error_type)
        await asyncio.to_thread(write_audit, interaction.user.id, "error_retry_reset", target_type=self.error_type, target_id="all", detail=f"対象 {self.count} / 更新 {changed}")
        await interaction.followup.send(f"✅ **{changed:,}件**を再試行待ちへ戻しました。通常ワーカーが順番に処理します。", ephemeral=True)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キャンセルしました。", view=None)
        self.stop()


class ErrorManagementView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("管理者本人だけが操作できます。", ephemeral=True)
        return False

    async def _confirm(self, interaction: discord.Interaction, kind: str, label: str) -> None:
        s = await asyncio.to_thread(get_admin_dashboard_stats)
        count = s[f"{kind}_errors"]
        if count <= 0:
            await interaction.response.send_message(f"✅ {label}はありません。", ephemeral=True)
            return
        await interaction.response.send_message(
            f"⚠️ **{label} {count:,}件**を再試行待ちへ戻します。\n復旧不能・不正URLは対象外です。",
            view=ConfirmErrorResetView(self.owner_id, kind, count),
            ephemeral=True,
        )

    @discord.ui.button(label="画像取得エラー", emoji="💾", style=discord.ButtonStyle.primary)
    async def download(self, interaction, _):
        await self._confirm(interaction, "download", "画像取得エラー")

    @discord.ui.button(label="AI解析エラー", emoji="🤖", style=discord.ButtonStyle.primary)
    async def analysis(self, interaction, _):
        await self._confirm(interaction, "analysis", "AI解析エラー")

    @discord.ui.button(label="顔認証エラー", emoji="👤", style=discord.ButtonStyle.primary)
    async def face(self, interaction, _):
        await self._confirm(interaction, "face", "顔認証エラー")


class PersonMasterModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="人物マスターを追加・更新", timeout=300)
        self.name = discord.ui.TextInput(label="人物名", max_length=80)
        self.group = discord.ui.TextInput(label="グループ", required=False, max_length=80, placeholder="乃木坂46 / その他")
        self.generation = discord.ui.TextInput(label="期・区分", required=False, max_length=80, placeholder="5期生 / 卒業生")
        self.active = discord.ui.TextInput(label="在籍状態", required=False, max_length=10, placeholder="1=在籍、0=卒業・その他", default="1")
        for item in (self.name, self.group, self.generation, self.active):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = str(self.name.value).strip()
        if not name:
            await interaction.response.send_message("人物名を入力してください。", ephemeral=True)
            return
        active = 0 if str(self.active.value).strip() in {"0", "false", "卒業"} else 1
        now = _now()
        def save() -> None:
            with closing(get_connection()) as con:
                con.execute(
                    """INSERT INTO photo_people(person_name,group_name,generation_name,is_active,created_at,updated_at)
                       VALUES(?,?,?,?,?,?)
                       ON CONFLICT(person_name) DO UPDATE SET
                         group_name=excluded.group_name,
                         generation_name=excluded.generation_name,
                         is_active=excluded.is_active,
                         updated_at=excluded.updated_at""",
                    (name, str(self.group.value).strip(), str(self.generation.value).strip(), active, now, now),
                )
                con.commit()
        await asyncio.to_thread(save)
        await asyncio.to_thread(write_audit, interaction.user.id, "person_master_upsert", target_type="person", target_id=name, detail=f"group={self.group.value}, generation={self.generation.value}, active={active}")
        await interaction.response.send_message(f"✅ **{name}** の人物マスターを保存しました。", ephemeral=True)



class ConfirmHiddenDeleteView(discord.ui.View):
    def __init__(self, owner_id: int, blog_id: int) -> None:
        super().__init__(timeout=120)
        self.owner_id = int(owner_id)
        self.blog_id = int(blog_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この確認画面は開いた管理者だけが使えます。", ephemeral=True)
        return False

    @discord.ui.button(label="完全削除する", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        deleted = await asyncio.to_thread(delete_hidden_photo_blog, self.blog_id)
        if deleted:
            await asyncio.to_thread(
                write_audit,
                interaction.user.id,
                "hidden_blog_delete",
                target_type="blog",
                target_id=self.blog_id,
                detail="除外済み記事を完全削除",
            )
            await interaction.response.edit_message(content=f"✅ ブログID **{self.blog_id}** を完全削除しました。", embed=None, view=None)
        else:
            await interaction.response.edit_message(content="対象が見つからないか、除外状態ではありません。", embed=None, view=None)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="削除をキャンセルしました。", view=None)
        self.stop()


class HiddenBlogDetailView(discord.ui.View):
    def __init__(self, owner_id: int, blog_id: int, return_page: int = 0) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.blog_id = int(blog_id)
        self.return_page = max(0, int(return_page))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが使えます。", ephemeral=True)
        return False

    @discord.ui.button(label="投稿者を設定して復元", emoji="✏️", style=discord.ButtonStyle.success)
    async def set_author_restore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from admin_workflow import BlogAuthorGroupSelectView
        await interaction.response.send_message(
            "✏️ **投稿者を設定して復元**\n正しいグループを選択してください。",
            view=BlogAuthorGroupSelectView(self.blog_id, restore_if_hidden=True),
            ephemeral=True,
        )

    @discord.ui.button(label="復元", emoji="👁️", style=discord.ButtonStyle.success)
    async def restore(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        restored = await asyncio.to_thread(restore_hidden_photo_blog, self.blog_id)
        if restored:
            await asyncio.to_thread(write_audit, interaction.user.id, "hidden_blog_restore", target_type="blog", target_id=self.blog_id, detail="除外一覧から復元")
            await interaction.response.edit_message(content=f"✅ ブログID **{self.blog_id}** を人物確認対象へ戻しました。", embed=None, view=None)
        else:
            await interaction.response.send_message("復元対象が見つかりませんでした。", ephemeral=True)

    @discord.ui.button(label="再解析待ちへ", emoji="🔄", style=discord.ButtonStyle.primary)
    async def reanalyze(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        changed = await asyncio.to_thread(queue_hidden_blog_reanalysis, self.blog_id)
        await asyncio.to_thread(write_audit, interaction.user.id, "hidden_blog_reanalysis", target_type="blog", target_id=self.blog_id, detail=str(changed))
        await interaction.followup.send(
            f"✅ 再処理待ちへ戻しました。\n画像取得対象 **{changed['download']}件** / AI解析対象 **{changed['analysis']}件**\n除外状態は維持されます。",
            ephemeral=True,
        )

    @discord.ui.button(label="完全削除", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        blog = await asyncio.to_thread(get_hidden_photo_blog, self.blog_id)
        count = int((blog or {}).get("image_count") or 0)
        await interaction.response.send_message(
            f"⚠️ ブログID **{self.blog_id}** と関連画像 **{count}件**を完全削除します。\nこの操作は元に戻せません。",
            view=ConfirmHiddenDeleteView(self.owner_id, self.blog_id),
            ephemeral=True,
        )

    @discord.ui.button(label="除外一覧へ", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = await HiddenBlogsView.create(self.owner_id, self.return_page)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class HiddenBlogSelect(discord.ui.Select):
    def __init__(self, parent: "HiddenBlogsView", rows: list[dict[str, Any]]) -> None:
        self.parent_view = parent
        options: list[discord.SelectOption] = []
        for row in rows:
            blog_id = int(row.get("id") or 0)
            reason = HIDDEN_REASON_LABELS.get(str(row.get("hidden_reason") or ""), str(row.get("hidden_reason") or "理由不明"))
            member = str(row.get("member_name") or "投稿者不明")
            title = str(row.get("title") or "無題")
            options.append(discord.SelectOption(
                label=f"{blog_id}: {title}"[:100],
                value=str(blog_id),
                description=f"{member} / {reason} / 画像{int(row.get('image_count') or 0)}件"[:100],
                emoji="🚫",
            ))
        if not options:
            options = [discord.SelectOption(label="除外済みデータはありません", value="__none__")]
        super().__init__(placeholder="除外済みデータを選択", options=options, row=0)

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "__none__":
            await interaction.response.send_message("除外済みデータはありません。", ephemeral=True)
            return
        blog_id = int(value)
        blog = await asyncio.to_thread(get_hidden_photo_blog, blog_id)
        if not blog:
            await interaction.response.send_message("対象が見つかりませんでした。", ephemeral=True)
            return
        reason_code = str(blog.get("hidden_reason") or "")
        reason = HIDDEN_REASON_LABELS.get(reason_code, reason_code or "理由不明")
        embed = discord.Embed(title=f"🚫 除外データ #{blog_id}", color=0xED4245)
        safe_add_field(embed, name="グループ", value=str(blog.get("group_name") or "不明"), inline=True)
        safe_add_field(embed, name="投稿者", value=str(blog.get("member_name") or "投稿者不明"), inline=True)
        safe_add_field(embed, name="画像数", value=str(int(blog.get("image_count") or 0)), inline=True)
        safe_add_field(embed, name="タイトル", value=str(blog.get("title") or "無題"), inline=False)
        safe_add_field(embed, name="除外理由", value=f"{reason} (`{reason_code or 'UNKNOWN'}`)", inline=False)
        safe_add_field(embed, name="管理メモ", value=str(blog.get("hidden_note") or "なし"), inline=False)
        safe_add_field(embed, name="処理エラー", value=f"画像取得 {int(blog.get('download_errors') or 0)} / AI解析 {int(blog.get('analysis_errors') or 0)}", inline=False)
        safe_add_field(embed, name="ブログURL", value=str(blog.get("blog_url") or "なし"), inline=False)
        await interaction.response.edit_message(embed=embed, view=HiddenBlogDetailView(self.parent_view.owner_id, blog_id, self.parent_view.page))


class HiddenBlogsView(discord.ui.View):
    PAGE_SIZE = 25

    def __init__(self, owner_id: int, rows: list[dict[str, Any]], total: int, page: int) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.rows = rows
        self.total = int(total)
        self.page_count = max(1, (self.total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.add_item(HiddenBlogSelect(self, rows))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    @classmethod
    async def create(cls, owner_id: int, page: int = 0) -> "HiddenBlogsView":
        safe_page = max(0, int(page))
        rows, total = await asyncio.to_thread(list_hidden_photo_blogs, cls.PAGE_SIZE, safe_page * cls.PAGE_SIZE)
        page_count = max(1, (int(total) + cls.PAGE_SIZE - 1) // cls.PAGE_SIZE)
        safe_page = min(safe_page, page_count - 1)
        if safe_page * cls.PAGE_SIZE and not rows:
            rows, total = await asyncio.to_thread(list_hidden_photo_blogs, cls.PAGE_SIZE, safe_page * cls.PAGE_SIZE)
        return cls(owner_id, rows, total, safe_page)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが使えます。", ephemeral=True)
        return False

    def embed(self) -> discord.Embed:
        start = self.page * self.PAGE_SIZE + 1 if self.total else 0
        end = min((self.page + 1) * self.PAGE_SIZE, self.total)
        embed = discord.Embed(
            title="🚫 除外データ管理",
            description=(
                f"表示 **{start}〜{end}件目 / 全{self.total}件**（{self.page + 1}/{self.page_count}ページ）\n"
                "データとBucket画像は保持されたままです。復元・再解析・完全削除を選べます。"
            ),
            color=0xED4245,
        )
        counts: dict[str, int] = {}
        for row in self.rows:
            code = str(row.get("hidden_reason") or "UNKNOWN")
            counts[code] = counts.get(code, 0) + 1
        if counts:
            safe_add_field(embed, name="このページの内訳", value="\n".join(f"・{HIDDEN_REASON_LABELS.get(k, k)}: {v}件" for k, v in counts.items()), inline=False)
        return embed

    @discord.ui.button(label="前の25件", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = await HiddenBlogsView.create(self.owner_id, self.page - 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="次の25件", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = await HiddenBlogsView.create(self.owner_id, self.page + 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="更新", emoji="🔄", style=discord.ButtonStyle.primary, row=1)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = await HiddenBlogsView.create(self.owner_id, self.page)
        await interaction.response.edit_message(embed=view.embed(), view=view)


def ai_savings_embed() -> discord.Embed:
    status = get_ai_cost_status()
    embed = discord.Embed(
        title="💰 AI節約モード",
        description=(
            "有料APIは補助機能として扱い、キャッシュとローカル処理を優先します。\n"
            f"自動API解析: **{'ON' if int(status.get('auto_api_enabled', 0)) else 'OFF'}**\n"
            f"一時停止: **{'はい' if int(status.get('is_paused', 0)) else 'いいえ'}**"
        ),
        color=0x57F287 if not int(status.get('is_paused', 0)) else 0xED4245,
    )
    safe_add_field(
        embed, name="本日",
        value=f"API送信 **{int(status.get('daily_used', 0))}枚** / 上限 **{int(status.get('daily_image_limit', 0))}枚**\n残り **{int(status.get('daily_remaining', 0))}枚**",
        inline=True,
    )
    safe_add_field(
        embed, name="今月",
        value=f"API送信 **{int(status.get('monthly_used', 0))}枚** / 上限 **{int(status.get('monthly_image_limit', 0))}枚**\n残り **{int(status.get('monthly_remaining', 0))}枚**",
        inline=True,
    )
    safe_add_field(
        embed, name="再利用実績",
        value=f"キャッシュ再利用 **{int(status.get('cache_reuse_total', 0)):,}件**\nAPI記録 **{int(status.get('api_calls_total', 0)):,}件**",
        inline=True,
    )
    if status.get('pause_reason'):
        safe_add_field(embed, name="停止理由", value=str(status.get('pause_reason')), inline=False)
    embed.set_footer(text="人物候補とタグは1回の画像解析で同時取得し、タグ専用API呼び出しは行いません。")
    return embed


def ai_simulation_embed(limit: int = 500) -> discord.Embed:
    result = simulate_pending_api_usage(limit)
    embed = discord.Embed(
        title="🧮 API送信シミュレーション",
        description=(
            "APIは呼び出していません。未解析画像をDB上で分類した結果です。\n"
            f"確認範囲: **{result['inspected']:,}件** / 未解析全体 **{result['total_pending']:,}件**"
        ),
        color=0x3498DB,
    )
    safe_add_field(
        embed,
        name="無料で処理できるもの",
        value=(
            f"キャッシュ再利用 **{result['cache_reuse']:,}件**\n"
            f"ローカル顔判定 **{result['local_face_decision']:,}件**\n"
            "※ローカル顔判定は人物確認用の別工程で、画像タグ解析の代替にはまだ含めていません。"
        ),
        inline=False,
    )
    safe_add_field(
        embed,
        name="API候補",
        value=(
            f"API送信候補 **{result['api_candidates']:,}件**\n"
            f"現在の設定で送信可能 **{result['api_sendable_now']:,}件**\n"
            f"設定・上限により停止 **{result['api_blocked']:,}件**"
        ),
        inline=True,
    )
    safe_add_field(
        embed,
        name="残り上限",
        value=(
            f"本日 **{result['daily_remaining']:,}枚**\n"
            f"今月 **{result['monthly_remaining']:,}枚**"
        ),
        inline=True,
    )
    if result['no_hash']:
        safe_add_field(
            embed,
            name="画像ハッシュ未作成",
            value=f"**{result['no_hash']:,}件**はキャッシュ判定ができないためAPI候補として数えています。",
            inline=False,
        )
    if result['reasons']:
        safe_add_field(
            embed,
            name="現在送信しない理由",
            value="\n".join(f"・{x}" for x in result['reasons']),
            inline=False,
        )
    if result['truncated']:
        embed.set_footer(text=f"安全のため先頭{result['limit']:,}件だけを確認しています。API送信はしていません。")
    else:
        embed.set_footer(text="この画面は確認専用です。API送信はしていません。")
    return embed


class AICostLimitModal(discord.ui.Modal):
    def __init__(self) -> None:
        super().__init__(title="AI API上限を変更", timeout=300)
        status = get_ai_cost_status()
        self.daily = discord.ui.TextInput(label="1日の画像上限", default=str(status.get('daily_image_limit', 20)), max_length=8)
        self.monthly = discord.ui.TextInput(label="1か月の画像上限", default=str(status.get('monthly_image_limit', 300)), max_length=8)
        self.add_item(self.daily); self.add_item(self.monthly)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            daily = max(int(str(self.daily.value).strip()), 0)
            monthly = max(int(str(self.monthly.value).strip()), 0)
        except ValueError:
            await interaction.response.send_message("⚠️ 上限は0以上の整数で入力してください。", ephemeral=True)
            return
        await asyncio.to_thread(update_ai_cost_settings, daily_image_limit=daily, monthly_image_limit=monthly)
        write_audit(interaction.user.id, "ai_cost_limit_update", target_type="settings", detail=f"daily={daily}, monthly={monthly}")
        await interaction.response.send_message(embed=await asyncio.to_thread(ai_savings_embed), ephemeral=True)


class AISavingsView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600); self.owner_id = int(owner_id)
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id: return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True); return False

    @discord.ui.button(label="自動APIを切替", emoji="🔁", style=discord.ButtonStyle.secondary)
    async def toggle_auto(self, interaction, _):
        current = await asyncio.to_thread(get_ai_cost_status)
        new_value = 0 if int(current.get('auto_api_enabled', 0)) else 1
        await asyncio.to_thread(update_ai_cost_settings, auto_api_enabled=new_value)
        write_audit(interaction.user.id, "ai_auto_api_toggle", target_type="settings", detail=f"enabled={new_value}")
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_savings_embed), view=self)

    @discord.ui.button(label="日・月上限", emoji="🧮", style=discord.ButtonStyle.primary)
    async def limits(self, interaction, _):
        await interaction.response.send_modal(AICostLimitModal())

    @discord.ui.button(label="停止解除", emoji="▶️", style=discord.ButtonStyle.success)
    async def resume(self, interaction, _):
        await asyncio.to_thread(update_ai_cost_settings, is_paused=0, pause_reason='')
        write_audit(interaction.user.id, "ai_pause_clear", target_type="settings")
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_savings_embed), view=self)

    @discord.ui.button(label="手動停止", emoji="⏸️", style=discord.ButtonStyle.danger)
    async def pause(self, interaction, _):
        await asyncio.to_thread(update_ai_cost_settings, is_paused=1, pause_reason='管理者が手動停止しました。')
        write_audit(interaction.user.id, "ai_pause", target_type="settings")
        await interaction.response.edit_message(embed=await asyncio.to_thread(ai_savings_embed), view=self)

    @discord.ui.button(label="APIシミュレーション", emoji="🧮", style=discord.ButtonStyle.primary)
    async def simulation(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        embed = await asyncio.to_thread(ai_simulation_embed, 500)
        await interaction.followup.send(embed=embed, ephemeral=True)


class AdminOperationsView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この管理画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="更新", emoji="🔄", style=discord.ButtonStyle.primary)
    async def refresh(self, interaction, _):
        await interaction.response.defer()
        embed = await asyncio.to_thread(dashboard_embed)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="前回の続き", emoji="⏯️", style=discord.ButtonStyle.success)
    async def resume(self, interaction, _):
        from admin_workflow import BlogDashboardView
        await interaction.response.send_message(
            "⏯️ **前回の続きから再開**\n投稿者を選ぶと、保存済みのページ・絞り込み・最後の記事へ戻れます。",
            view=BlogDashboardView(),
            ephemeral=True,
        )

    @discord.ui.button(label="連続人物確認", emoji="✅", style=discord.ButtonStyle.success)
    async def review(self, interaction, _):
        from control_panel import invoke_existing_command
        await invoke_existing_command(interaction, "review_panel", "1", admin_required=True)

    @discord.ui.button(label="エラー管理", emoji="⚠️", style=discord.ButtonStyle.danger)
    async def errors(self, interaction, _):
        await interaction.response.send_message("再試行するエラー種別を選んでください。", view=ErrorManagementView(self.owner_id), ephemeral=True)

    @discord.ui.button(label="AI育成センター", emoji="🤖", style=discord.ButtonStyle.primary)
    async def ai_center(self, interaction, _):
        await send_ai_center(interaction)

    @discord.ui.button(label="AI学習状況", emoji="🧠", style=discord.ButtonStyle.secondary)
    async def ai(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await asyncio.to_thread(ai_quality_embed), ephemeral=True)

    @discord.ui.button(label="AI節約設定", emoji="💰", style=discord.ButtonStyle.success)
    async def ai_savings(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await asyncio.to_thread(ai_savings_embed), view=AISavingsView(self.owner_id), ephemeral=True)

    @discord.ui.button(label="監査ログ", emoji="📜", style=discord.ButtonStyle.secondary)
    async def audit(self, interaction, _):
        rows = await asyncio.to_thread(list_audit, 20)
        text = "\n".join(
            f"`{r['id']}` <@{r['admin_user_id']}> **{r['action_type']}** {r['target_type']}:{r['target_id']}\n{str(r['detail'])[:180]}"
            for r in rows
        ) or "監査ログはまだありません。"
        await interaction.response.send_message(embed=discord.Embed(title="📜 最近の管理操作", description=text[:4000], color=0x95A5A6), ephemeral=True)

    @discord.ui.button(label="人物マスター", emoji="👥", style=discord.ButtonStyle.secondary)
    async def people(self, interaction, _):
        await interaction.response.send_modal(PersonMasterModal())

    @discord.ui.button(label="健全性チェック", emoji="🩺", style=discord.ButtonStyle.secondary)
    async def health(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await asyncio.to_thread(health_embed), ephemeral=True)


    @discord.ui.button(label="除外データ管理", emoji="🚫", style=discord.ButtonStyle.secondary)
    async def hidden_blogs(self, interaction, _):
        await interaction.response.defer(ephemeral=True)
        view = await HiddenBlogsView.create(self.owner_id, 0)
        await interaction.followup.send(embed=view.embed(), view=view, ephemeral=True)


async def send_admin_operations(interaction: discord.Interaction) -> None:
    await interaction.response.defer(ephemeral=True)
    embed = await asyncio.to_thread(dashboard_embed)
    await interaction.followup.send(embed=embed, view=AdminOperationsView(interaction.user.id), ephemeral=True)


def register_admin_operations_commands(bot: commands.Bot) -> None:
    init_admin_operations_schema()
    init_advanced_admin_schema()

    @bot.command(name="admin_dashboard")
    @commands.is_owner()
    async def admin_dashboard(ctx: commands.Context) -> None:
        await ctx.send(embed=await asyncio.to_thread(dashboard_embed), view=AdminOperationsView(ctx.author.id))

    @bot.command(name="admin_health")
    @commands.is_owner()
    async def admin_health(ctx: commands.Context) -> None:
        await ctx.send(embed=await asyncio.to_thread(health_embed))

    @bot.command(name="admin_audit")
    @commands.is_owner()
    async def admin_audit(ctx: commands.Context, limit: int = 20) -> None:
        rows = await asyncio.to_thread(list_audit, limit)
        text = "\n".join(f"{r['id']}: {r['action_type']} {r['target_type']}:{r['target_id']} - {str(r['detail'])[:120]}" for r in rows) or "ログなし"
        await ctx.send(f"```\n{text[:1900]}\n```")
