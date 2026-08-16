"""管理者向け統合インサイト/AI運用ダッシュボード。

外部APIを呼ばず、既存SQLiteの情報だけで状態・品質・精度・使用量を集計する。
AI解析優先順位の設定だけはローカルDBへ保存し、未解析キューのORDER BYへ反映する。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord

from embed_safety import safe_add_field
from photo_database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_exists(con: sqlite3.Connection, name: str) -> bool:
    return con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _scalar(con: sqlite3.Connection, sql: str, params: tuple[Any, ...] = ()) -> int | float:
    row = con.execute(sql, params).fetchone()
    if row is None or row[0] is None:
        return 0
    return row[0]


def init_insights_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_priority_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                mode TEXT NOT NULL DEFAULT 'oldest',
                updated_by INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            );
            INSERT OR IGNORE INTO photo_ai_priority_settings(id,mode,updated_by,updated_at)
            VALUES(1,'oldest',0,'');

            CREATE TABLE IF NOT EXISTS photo_ai_dashboard_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_type TEXT NOT NULL,
                detail_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_ai_dashboard_events_time
              ON photo_ai_dashboard_events(created_at DESC);
            """
        )
        con.commit()


def get_priority_mode() -> str:
    init_insights_schema()
    with closing(get_connection()) as con:
        row = con.execute("SELECT mode FROM photo_ai_priority_settings WHERE id=1").fetchone()
    return str(row[0] if row else "oldest")


def set_priority_mode(mode: str, user_id: int) -> None:
    allowed = {"oldest", "newest", "reviewed_first", "new_blog_first"}
    if mode not in allowed:
        raise ValueError("未対応の優先順位です。")
    init_insights_schema()
    with closing(get_connection()) as con:
        con.execute(
            "UPDATE photo_ai_priority_settings SET mode=?,updated_by=?,updated_at=? WHERE id=1",
            (mode, int(user_id), _now()),
        )
        con.execute(
            "INSERT INTO photo_ai_dashboard_events(event_type,detail_json,created_at) VALUES(?,?,?)",
            ("priority_changed", json.dumps({"mode": mode, "user_id": int(user_id)}, ensure_ascii=False), _now()),
        )
        con.commit()


def ai_overview() -> dict[str, Any]:
    with closing(get_connection()) as con:
        images = int(_scalar(con, "SELECT COUNT(*) FROM photo_images"))
        analyzed = int(_scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status IN ('completed','review')"))
        pending = int(_scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status='pending'"))
        failed = int(_scalar(con, "SELECT COUNT(*) FROM photo_images WHERE analysis_status='failed'"))
        tags = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_tags"))
        tagged_images = int(_scalar(con, "SELECT COUNT(DISTINCT image_id) FROM photo_ai_tags"))
        faces_scanned = int(_scalar(con, "SELECT COUNT(*) FROM photo_face_scans WHERE status='completed'")) if _table_exists(con, "photo_face_scans") else 0
        reviewed = int(_scalar(con, "SELECT COUNT(*) FROM photo_review_queue WHERE status='completed'")) if _table_exists(con, "photo_review_queue") else 0
        avg_tags = (tags / tagged_images) if tagged_images else 0.0
    return {
        "images": images, "analyzed": analyzed, "pending": pending, "failed": failed,
        "tags": tags, "tagged_images": tagged_images, "avg_tags": avg_tags,
        "faces_scanned": faces_scanned, "reviewed": reviewed,
    }


def queue_stats() -> dict[str, int]:
    with closing(get_connection()) as con:
        rows = con.execute("SELECT analysis_status,COUNT(*) FROM photo_images GROUP BY analysis_status").fetchall()
    result = {str(r[0] or "unknown"): int(r[1] or 0) for r in rows}
    return {
        "pending": result.get("pending", 0),
        "completed": result.get("completed", 0),
        "review": result.get("review", 0),
        "failed": result.get("failed", 0),
        "other": sum(v for k, v in result.items() if k not in {"pending", "completed", "review", "failed"}),
    }


def usage_report() -> dict[str, Any]:
    with closing(get_connection()) as con:
        def summary(days: int | None):
            where = ""
            params: tuple[Any, ...] = ()
            if days is not None:
                where = "WHERE datetime(created_at)>=datetime('now',?)"
                params = (f"-{days} days",)
            row = con.execute(
                f"""SELECT
                    SUM(CASE WHEN request_kind='api' THEN 1 ELSE 0 END),
                    SUM(CASE WHEN request_kind='cache_reuse' THEN 1 ELSE 0 END),
                    COALESCE(SUM(total_tokens),0),
                    COALESCE(SUM(estimated_cost_usd),0)
                    FROM photo_ai_usage {where}""", params
            ).fetchone()
            return {
                "api": int((row[0] if row else 0) or 0),
                "reuse": int((row[1] if row else 0) or 0),
                "tokens": int((row[2] if row else 0) or 0),
                "cost": float((row[3] if row else 0) or 0),
            }
        today = con.execute(
            """SELECT COUNT(*) FROM photo_ai_usage
               WHERE request_kind='api' AND date(created_at,'+9 hours')=date('now','+9 hours')"""
        ).fetchone()[0]
        avg_ms = 0.0
    return {"today": int(today or 0), "week": summary(7), "month": summary(30), "all": summary(None), "avg_ms": avg_ms}


def tag_quality_report() -> dict[str, Any]:
    with closing(get_connection()) as con:
        if not _table_exists(con, "tag_master"):
            return {"available": False}
        total = int(_scalar(con, "SELECT COUNT(*) FROM tag_master"))
        approved = int(_scalar(con, "SELECT COUNT(*) FROM tag_master WHERE status='approved'"))
        pending = int(_scalar(con, "SELECT COUNT(*) FROM tag_master WHERE status='pending'"))
        blocked = int(_scalar(con, "SELECT COUNT(*) FROM tag_master WHERE status='blocked'"))
        searchable = int(_scalar(con, "SELECT COUNT(*) FROM tag_master WHERE searchable=1"))
        aliases = int(_scalar(con, "SELECT COUNT(*) FROM tag_aliases")) if _table_exists(con, "tag_aliases") else 0
        low = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_tags WHERE confidence < 0.6"))
        top = con.execute(
            """SELECT tag,COUNT(*) c FROM photo_ai_tags GROUP BY tag ORDER BY c DESC LIMIT 10"""
        ).fetchall()
        categories = con.execute(
            """SELECT category,COUNT(*) c,AVG(confidence) a FROM photo_ai_tags
               GROUP BY category ORDER BY c DESC LIMIT 12"""
        ).fetchall()
    return {
        "available": True, "total": total, "approved": approved, "pending": pending,
        "blocked": blocked, "searchable": searchable, "aliases": aliases, "low": low,
        "top": [(str(r[0]), int(r[1])) for r in top],
        "categories": [(str(r[0] or "その他"), int(r[1]), float(r[2] or 0)) for r in categories],
    }


def face_accuracy_report() -> dict[str, Any]:
    with closing(get_connection()) as con:
        if not _table_exists(con, "photo_ai_decision_log"):
            return {"available": False}
        total = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log"))
        accepted = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='accepted'"))
        corrected = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_decision_log WHERE decision='corrected'"))
        rows = con.execute(
            """SELECT suggested_person,confirmed_person,COUNT(*) c
               FROM photo_ai_decision_log
               WHERE decision='corrected' AND suggested_person<>'' AND confirmed_person<>''
               GROUP BY suggested_person,confirmed_person ORDER BY c DESC LIMIT 10"""
        ).fetchall()
        people = con.execute(
            """SELECT confirmed_person,
                      SUM(CASE WHEN decision='accepted' THEN 1 ELSE 0 END) a,
                      SUM(CASE WHEN decision='corrected' THEN 1 ELSE 0 END) c,
                      COUNT(*) t
               FROM photo_ai_decision_log WHERE confirmed_person<>''
               GROUP BY confirmed_person HAVING t>=1 ORDER BY t DESC LIMIT 15"""
        ).fetchall()
    rate = (accepted / (accepted + corrected) * 100.0) if accepted + corrected else 0.0
    return {
        "available": True, "total": total, "accepted": accepted, "corrected": corrected, "rate": rate,
        "confusions": [(str(r[0]), str(r[1]), int(r[2])) for r in rows],
        "people": [(str(r[0]), int(r[1]), int(r[2]), int(r[3])) for r in people],
    }


def db_health_report() -> dict[str, Any]:
    with closing(get_connection()) as con:
        integrity = str(con.execute("PRAGMA integrity_check").fetchone()[0])
        fk = con.execute("PRAGMA foreign_key_check").fetchall()
        dup_urls = int(_scalar(con, "SELECT COUNT(*) FROM (SELECT image_url FROM photo_images GROUP BY image_url HAVING COUNT(*)>1)"))
        orphan_reviews = 0
        if _table_exists(con, "photo_review_queue"):
            orphan_reviews = int(_scalar(con, """SELECT COUNT(*) FROM photo_review_queue q LEFT JOIN photo_images i ON i.id=q.image_id WHERE i.id IS NULL"""))
        missing_queue = int(_scalar(con, """SELECT COUNT(*) FROM photo_image_people p
             LEFT JOIN photo_review_queue q ON q.image_id=p.image_id
             WHERE p.relation_status='confirmed' AND q.image_id IS NULL""")) if _table_exists(con, "photo_image_people") else 0
        indexes = int(_scalar(con, "SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"))
    return {
        "integrity": integrity, "foreign_key_errors": len(fk), "duplicate_urls": dup_urls,
        "orphan_reviews": orphan_reviews, "missing_queue": missing_queue, "indexes": indexes,
    }


def system_report() -> dict[str, Any]:
    db_path = Path(os.getenv("PHOTO_DB_PATH", "/data/photo_archive.db"))
    if not db_path.exists():
        try:
            from photo_database import DB_PATH
            db_path = Path(DB_PATH)
        except Exception:
            pass
    db_size = db_path.stat().st_size if db_path.exists() else 0
    with closing(get_connection()) as con:
        tables = int(_scalar(con, "SELECT COUNT(*) FROM sqlite_master WHERE type='table'"))
        images = int(_scalar(con, "SELECT COUNT(*) FROM photo_images"))
        tags = int(_scalar(con, "SELECT COUNT(*) FROM photo_ai_tags"))
        faces = int(_scalar(con, "SELECT COUNT(*) FROM photo_faces")) if _table_exists(con, "photo_faces") else 0
        embeddings = int(_scalar(con, "SELECT COUNT(*) FROM photo_faces WHERE face_embedding<>''")) if _table_exists(con, "photo_faces") else 0
    return {"db_path": str(db_path), "db_size": db_size, "tables": tables, "images": images, "tags": tags, "faces": faces, "embeddings": embeddings}


def model_comparison() -> list[dict[str, Any]]:
    with closing(get_connection()) as con:
        rows = con.execute(
            """SELECT CASE WHEN model_name='' THEN '(不明)' ELSE model_name END model,
                      COUNT(DISTINCT image_id) images,
                      COUNT(*) tags,
                      AVG(confidence) avg_conf
               FROM photo_ai_tags GROUP BY model_name ORDER BY images DESC"""
        ).fetchall()
        usage = {}
        if _table_exists(con, "photo_ai_usage"):
            for r in con.execute(
                """SELECT CASE WHEN model_name='' THEN '(不明)' ELSE model_name END,
                          SUM(CASE WHEN request_kind='api' THEN 1 ELSE 0 END),
                          COALESCE(SUM(estimated_cost_usd),0)
                   FROM photo_ai_usage GROUP BY model_name"""
            ).fetchall():
                usage[str(r[0])] = (int(r[1] or 0), float(r[2] or 0))
    out=[]
    for r in rows:
        api, cost = usage.get(str(r[0]), (0,0.0))
        out.append({"model":str(r[0]),"images":int(r[1]),"tags":int(r[2]),"avg_conf":float(r[3] or 0),"api":api,"cost":cost})
    return out


def retry_failed_images(limit: int = 100) -> int:
    safe = max(1, min(int(limit), 1000))
    with closing(get_connection()) as con:
        ids = [int(r[0]) for r in con.execute(
            "SELECT id FROM photo_images WHERE analysis_status='failed' ORDER BY id LIMIT ?", (safe,)
        ).fetchall()]
        if not ids:
            return 0
        marks = ",".join("?" for _ in ids)
        con.execute(
            f"UPDATE photo_images SET analysis_status='pending',analysis_error='',updated_at=? WHERE id IN ({marks})",
            (_now(), *ids),
        )
        con.commit()
        return len(ids)


def _fmt_bytes(value: int) -> str:
    n=float(value)
    for unit in ("B","KB","MB","GB","TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


class PrioritySelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        self.owner_id=int(owner_id)
        current=get_priority_mode()
        options=[
            discord.SelectOption(label="古い画像から", value="oldest", default=current=="oldest"),
            discord.SelectOption(label="新しい画像から", value="newest", default=current=="newest"),
            discord.SelectOption(label="人物確認済みを優先", value="reviewed_first", default=current=="reviewed_first"),
            discord.SelectOption(label="新しいブログを優先", value="new_blog_first", default=current=="new_blog_first"),
        ]
        super().__init__(placeholder="AI解析の優先順位", options=options, row=4)

    async def callback(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        await asyncio.to_thread(set_priority_mode, self.values[0], interaction.user.id)
        await interaction.followup.send("✅ AI解析の優先順位を更新しました。自動解析/手動解析の次回取得から反映されます。", ephemeral=True)


class AdminInsightsView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=1200)
        self.owner_id=int(owner_id)
        self.add_item(PrioritySelect(owner_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="AI概要", emoji="🤖", style=discord.ButtonStyle.primary, row=0)
    async def overview(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(ai_overview)
        e=discord.Embed(title="🤖 AIダッシュボード", color=0x5865F2)
        e.description=(f"📷 登録画像 **{d['images']:,}枚**\n🤖 AI解析済み **{d['analyzed']:,}枚**\n"
                       f"⏳ AI未解析 **{d['pending']:,}枚**\n⚠️ AI失敗 **{d['failed']:,}枚**\n"
                       f"🏷️ AIタグ **{d['tags']:,}個**（{d['tagged_images']:,}枚に付与 / 平均{d['avg_tags']:.2f}個）\n"
                       f"👤 顔スキャン済み **{d['faces_scanned']:,}枚**\n✅ 人物確認完了 **{d['reviewed']:,}枚**")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="解析キュー", emoji="⏳", style=discord.ButtonStyle.primary, row=0)
    async def queue(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(queue_stats)
        e=discord.Embed(title="⏳ AI解析キュー", color=0x3498DB)
        e.description=(f"待機 **{d['pending']:,}**\n成功 **{d['completed']:,}**\n確認待ち **{d['review']:,}**\n"
                       f"失敗 **{d['failed']:,}**\nその他 **{d['other']:,}**\n\n現在の優先順位: **{get_priority_mode()}**")
        e.set_footer(text="失敗画像は下の『失敗100件を再試行』からpendingへ戻せます。")
        await interaction.followup.send(embed=e, view=QueueActionView(self.owner_id), ephemeral=True)

    @discord.ui.button(label="AI使用量", emoji="💰", style=discord.ButtonStyle.secondary, row=0)
    async def usage(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(usage_report)
        e=discord.Embed(title="💰 AI使用量レポート", color=0xF1C40F)
        lines=[f"今日 API **{d['today']:,}回**"]
        for label,key in (("7日","week"),("30日","month"),("累計","all")):
            x=d[key]; lines.append(f"{label}: API {x['api']:,} / 再利用 {x['reuse']:,} / {x['tokens']:,} tokens / 推定 ${x['cost']:.6f}")
        e.description="\n".join(lines)
        e.set_footer(text="金額は保存済みusageと設定単価からの推定です。")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="タグ品質", emoji="🏷️", style=discord.ButtonStyle.secondary, row=1)
    async def tags(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(tag_quality_report)
        e=discord.Embed(title="🏷️ タグ品質レポート", color=0xF1C40F)
        if not d.get('available'):
            e.description="タグマスターがまだ初期化されていません。"
        else:
            e.description=(f"代表タグ **{d['total']:,}** / 承認 **{d['approved']:,}** / 未承認 **{d['pending']:,}** / 除外 **{d['blocked']:,}**\n"
                           f"検索対象 **{d['searchable']:,}** / 別表記 **{d['aliases']:,}** / 信頼度0.6未満の割当 **{d['low']:,}**")
            safe_add_field(e, name="使用回数上位", value="\n".join(f"・{n}: {c:,}" for n,c in d['top']) or "なし",inline=False)
            safe_add_field(e, name="カテゴリー別", value="\n".join(f"・{n}: {c:,}（平均{a*100:.1f}%）" for n,c,a in d['categories']) or "なし",inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="顔認識精度", emoji="👤", style=discord.ButtonStyle.secondary, row=1)
    async def faces(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(face_accuracy_report)
        e=discord.Embed(title="👤 顔認識精度レポート", color=0xEB459E)
        if not d.get('available'):
            e.description="AI候補の評価履歴がまだありません。"
        else:
            e.description=f"評価 **{d['total']:,}件** / 採用 **{d['accepted']:,}件** / 修正 **{d['corrected']:,}件** / 実測採用率 **{d['rate']:.1f}%**"
            safe_add_field(e, name="誤判定しやすい組み合わせ", value="\n".join(f"・{a} → {b}: {c}回" for a,b,c in d['confusions']) or "なし",inline=False)
            safe_add_field(e, name="人物別評価", value="\n".join(f"・{n}: 採用{a}/修正{c}/計{t}" for n,a,c,t in d['people']) or "なし",inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="DB健康診断", emoji="🩺", style=discord.ButtonStyle.success, row=1)
    async def dbhealth(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(db_health_report)
        ok=d['integrity']=='ok' and d['foreign_key_errors']==0 and d['orphan_reviews']==0
        e=discord.Embed(title="🩺 DB健康診断", color=0x57F287 if ok else 0xED4245)
        e.description=(f"integrity_check: **{d['integrity']}**\n外部キー異常 **{d['foreign_key_errors']}**\n"
                       f"孤立レビュー **{d['orphan_reviews']}**\n確認キュー未作成候補 **{d['missing_queue']}**\n"
                       f"重複URLグループ **{d['duplicate_urls']}**\nインデックス **{d['indexes']}**")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="システム全体", emoji="📊", style=discord.ButtonStyle.success, row=2)
    async def system(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        d=await asyncio.to_thread(system_report)
        e=discord.Embed(title="📊 システム全体ダッシュボード", color=0x57F287)
        e.description=(f"DB **{_fmt_bytes(d['db_size'])}** / テーブル **{d['tables']}**\n"
                       f"画像 **{d['images']:,}** / AIタグ **{d['tags']:,}**\n"
                       f"検出顔 **{d['faces']:,}** / 特徴量あり **{d['embeddings']:,}**\n"
                       f"AI優先順位 **{get_priority_mode()}**")
        e.set_footer(text="RailwayのCPU/RAM実測値はこのBotから直接取得できないため、DB内部指標を表示しています。")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="モデル比較", emoji="🧪", style=discord.ButtonStyle.secondary, row=2)
    async def models(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        rows=await asyncio.to_thread(model_comparison)
        e=discord.Embed(title="🧪 AIモデル比較（保存済み実績）", color=0x9B59B6)
        e.description="\n".join(
            f"**{r['model']}**: 画像{r['images']:,} / タグ{r['tags']:,} / 平均信頼度{r['avg_conf']*100:.1f}% / API{r['api']:,} / ${r['cost']:.6f}"
            for r in rows[:15]
        ) or "モデル別データはまだありません。"
        e.set_footer(text="新しいAPI呼び出しは行わず、DBに保存済みの実績だけを比較しています。")
        await interaction.followup.send(embed=e, ephemeral=True)

    @discord.ui.button(label="更新", emoji="🔄", style=discord.ButtonStyle.secondary, row=2)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        embed = await asyncio.to_thread(home_embed)
        await interaction.edit_original_response(embed=embed, view=AdminInsightsView(self.owner_id))


class QueueActionView(discord.ui.View):
    def __init__(self, owner_id:int):
        super().__init__(timeout=600); self.owner_id=int(owner_id)
    async def interaction_check(self, interaction:discord.Interaction)->bool:
        if interaction.user.id==self.owner_id:return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。",ephemeral=True);return False
    @discord.ui.button(label="失敗100件を再試行",emoji="🔁",style=discord.ButtonStyle.danger)
    async def retry(self,interaction:discord.Interaction,_:discord.ui.Button)->None:
        await interaction.response.defer(ephemeral=True)
        count=await asyncio.to_thread(retry_failed_images,100)
        await interaction.followup.send(f"✅ {count}件を再試行待ちへ戻しました。APIはまだ呼び出していません。",ephemeral=True)


def home_embed() -> discord.Embed:
    d=ai_overview()
    e=discord.Embed(title="📊 AI・システム統合ダッシュボード", color=0x5865F2)
    e.description=(
        f"登録画像 **{d['images']:,}** / AI解析済み **{d['analyzed']:,}** / 未解析 **{d['pending']:,}**\n"
        f"AIタグ **{d['tags']:,}** / 平均 **{d['avg_tags']:.2f}個/タグ付与画像**\n"
        f"下のボタンから、解析・費用・タグ・顔認識・DBを個別に確認できます。"
    )
    e.set_footer(text="この画面の集計・比較・診断はOpenAI APIを呼びません。")
    return e


async def send_admin_insights(interaction: discord.Interaction) -> None:
    if not interaction.response.is_done():
        await interaction.response.defer(ephemeral=True)
    await asyncio.to_thread(init_insights_schema)
    embed = await asyncio.to_thread(home_embed)
    await interaction.followup.send(embed=embed, view=AdminInsightsView(interaction.user.id), ephemeral=True)
