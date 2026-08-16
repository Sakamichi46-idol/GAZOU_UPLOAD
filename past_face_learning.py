"""過去に管理者が確定した顔をローカル顔認証の参照データとして監査・登録する。

OpenAI APIは使用しない。既存の ``photo_faces.face_embedding`` を再利用し、
重複・品質不足・人物マスター不整合を診断して、反映履歴を保存する。
"""
from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

import discord

from embed_safety import safe_add_field
from photo_database import get_connection


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_past_face_learning_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_face_learning_registry (
                face_id INTEGER PRIMARY KEY,
                person_id INTEGER NOT NULL,
                embedding_hash TEXT NOT NULL DEFAULT '',
                quality_grade TEXT NOT NULL DEFAULT 'B',
                quality_score REAL NOT NULL DEFAULT 0,
                source TEXT NOT NULL DEFAULT 'past_confirmed',
                is_active INTEGER NOT NULL DEFAULT 1,
                registered_by INTEGER NOT NULL DEFAULT 0,
                registered_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY(face_id) REFERENCES photo_faces(id) ON DELETE CASCADE,
                FOREIGN KEY(person_id) REFERENCES photo_people(id) ON DELETE CASCADE
            );
            CREATE INDEX IF NOT EXISTS idx_face_learning_person
              ON photo_face_learning_registry(person_id, is_active);
            CREATE INDEX IF NOT EXISTS idx_face_learning_hash
              ON photo_face_learning_registry(embedding_hash, person_id);

            CREATE TABLE IF NOT EXISTS photo_face_learning_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                admin_user_id INTEGER NOT NULL DEFAULT 0,
                scanned_count INTEGER NOT NULL DEFAULT 0,
                eligible_count INTEGER NOT NULL DEFAULT 0,
                registered_count INTEGER NOT NULL DEFAULT 0,
                duplicate_count INTEGER NOT NULL DEFAULT 0,
                no_embedding_count INTEGER NOT NULL DEFAULT 0,
                invalid_person_count INTEGER NOT NULL DEFAULT 0,
                low_quality_count INTEGER NOT NULL DEFAULT 0,
                result_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
            """
        )
        con.commit()


def _hash_embedding(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8", errors="ignore")).hexdigest()


def _quality(row: dict[str, Any]) -> tuple[str, float, str]:
    """保存済みメタデータだけで保守的な品質判定を行う。"""
    confidence = max(0.0, min(float(row.get("detection_confidence") or 0), 1.0))
    width = max(0.0, float(row.get("box_width") or 0))
    height = max(0.0, float(row.get("box_height") or 0))
    min_side = min(width, height)

    # box値は実装世代によってpxまたは比率の可能性があるため、0は不明扱い。
    size_score = 0.65 if min_side <= 0 else (1.0 if min_side >= 96 else 0.8 if min_side >= 64 else 0.55 if min_side >= 40 else 0.25)
    detection_score = confidence if confidence > 0 else 0.65
    score = round((size_score * 0.55) + (detection_score * 0.45), 4)
    if score >= 0.82:
        return "A", score, "高品質"
    if score >= 0.62:
        return "B", score, "利用可能"
    return "C", score, "品質不足"


def analyze_past_confirmed_faces() -> dict[str, Any]:
    init_past_face_learning_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT f.id AS face_id, f.confirmed_person_id AS person_id,
                   f.face_embedding, f.detection_confidence,
                   f.box_width, f.box_height, f.confirmation_status,
                   p.person_name, p.group_name
            FROM photo_faces f
            LEFT JOIN photo_people p ON p.id=f.confirmed_person_id
            WHERE f.confirmed_person_id IS NOT NULL
              AND f.confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
            ORDER BY f.id
            """
        ).fetchall()
        existing = {
            int(r[0]): dict(r)
            for r in con.execute(
                "SELECT face_id,person_id,embedding_hash,quality_grade,is_active FROM photo_face_learning_registry"
            ).fetchall()
        }

    seen: set[tuple[int, str]] = set()
    summary: dict[str, Any] = {
        "scanned": len(rows), "eligible": 0, "already_registered": 0,
        "duplicate": 0, "no_embedding": 0, "invalid_person": 0,
        "low_quality": 0, "quality_a": 0, "quality_b": 0,
        "items": [], "excluded_items": [],
    }
    for raw in rows:
        row = dict(raw)
        face_id = int(row["face_id"])
        person_id = int(row.get("person_id") or 0)
        embedding = str(row.get("face_embedding") or "").strip()
        if not person_id or not str(row.get("person_name") or "").strip():
            summary["invalid_person"] += 1
            continue
        if not embedding:
            summary["no_embedding"] += 1
            continue
        digest = _hash_embedding(embedding)
        key = (person_id, digest)
        if key in seen:
            summary["duplicate"] += 1
            summary["excluded_items"].append({
                "face_id": face_id, "person_id": person_id, "embedding_hash": digest,
                "quality_grade": "DUPLICATE", "quality_score": 0.0,
            })
            continue
        seen.add(key)
        grade, score, _ = _quality(row)
        if grade == "C":
            summary["low_quality"] += 1
            summary["excluded_items"].append({
                "face_id": face_id, "person_id": person_id, "embedding_hash": digest,
                "quality_grade": grade, "quality_score": score,
            })
            continue
        if face_id in existing and int(existing[face_id].get("is_active") or 0) == 1:
            summary["already_registered"] += 1
            continue
        summary["eligible"] += 1
        summary[f"quality_{grade.lower()}"] += 1
        summary["items"].append({
            "face_id": face_id, "person_id": person_id,
            "person_name": str(row.get("person_name") or ""),
            "embedding_hash": digest, "quality_grade": grade,
            "quality_score": score,
        })
    return summary


def apply_past_confirmed_faces(admin_user_id: int) -> dict[str, Any]:
    result = analyze_past_confirmed_faces()
    now = _now()
    registered = 0
    by_person: dict[str, int] = {}
    with closing(get_connection()) as con:
        try:
            con.execute("BEGIN IMMEDIATE")
            for item in result["items"]:
                con.execute(
                    """
                    INSERT INTO photo_face_learning_registry(
                        face_id,person_id,embedding_hash,quality_grade,quality_score,
                        source,is_active,registered_by,registered_at,updated_at
                    ) VALUES(?,?,?,?,?,'past_confirmed',1,?,?,?)
                    ON CONFLICT(face_id) DO UPDATE SET
                        person_id=excluded.person_id,
                        embedding_hash=excluded.embedding_hash,
                        quality_grade=excluded.quality_grade,
                        quality_score=excluded.quality_score,
                        source=excluded.source,is_active=1,
                        registered_by=excluded.registered_by,updated_at=excluded.updated_at
                    """,
                    (item["face_id"], item["person_id"], item["embedding_hash"],
                     item["quality_grade"], item["quality_score"], int(admin_user_id), now, now),
                )
                registered += 1
                name = item["person_name"]
                by_person[name] = by_person.get(name, 0) + 1
            for item in result.get("excluded_items", []):
                con.execute(
                    """
                    INSERT INTO photo_face_learning_registry(
                        face_id,person_id,embedding_hash,quality_grade,quality_score,
                        source,is_active,registered_by,registered_at,updated_at
                    ) VALUES(?,?,?,?,?,'past_confirmed_quality_check',0,?,?,?)
                    ON CONFLICT(face_id) DO UPDATE SET
                        person_id=excluded.person_id,embedding_hash=excluded.embedding_hash,
                        quality_grade=excluded.quality_grade,quality_score=excluded.quality_score,
                        source=excluded.source,is_active=0,registered_by=excluded.registered_by,
                        updated_at=excluded.updated_at
                    """,
                    (item["face_id"], item["person_id"], item["embedding_hash"],
                     item["quality_grade"], item["quality_score"], int(admin_user_id), now, now),
                )
            log_payload = {k: v for k, v in result.items() if k not in ("items", "excluded_items")}
            log_payload["by_person"] = by_person
            con.execute(
                """INSERT INTO photo_face_learning_runs(
                    admin_user_id,scanned_count,eligible_count,registered_count,
                    duplicate_count,no_embedding_count,invalid_person_count,
                    low_quality_count,result_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (int(admin_user_id), result["scanned"], result["eligible"], registered,
                 result["duplicate"], result["no_embedding"], result["invalid_person"],
                 result["low_quality"], json.dumps(log_payload, ensure_ascii=False), now),
            )
            con.commit()
        except Exception:
            con.rollback()
            raise
    result["registered"] = registered
    result["by_person"] = by_person
    return result


def learning_stats() -> dict[str, Any]:
    init_past_face_learning_schema()
    with closing(get_connection()) as con:
        total = int(con.execute("SELECT COUNT(*) FROM photo_face_learning_registry WHERE is_active=1").fetchone()[0] or 0)
        people = int(con.execute("SELECT COUNT(DISTINCT person_id) FROM photo_face_learning_registry WHERE is_active=1").fetchone()[0] or 0)
        grades = {str(r[0]): int(r[1]) for r in con.execute(
            "SELECT quality_grade,COUNT(*) FROM photo_face_learning_registry WHERE is_active=1 GROUP BY quality_grade"
        ).fetchall()}
        top = [dict(r) for r in con.execute(
            """SELECT p.person_name,p.group_name,COUNT(*) AS face_count,
                      SUM(CASE WHEN r.quality_grade='A' THEN 1 ELSE 0 END) AS quality_a,
                      SUM(CASE WHEN r.quality_grade='B' THEN 1 ELSE 0 END) AS quality_b,
                      MAX(r.updated_at) AS last_updated
               FROM photo_face_learning_registry r
               JOIN photo_people p ON p.id=r.person_id
               WHERE r.is_active=1
               GROUP BY r.person_id ORDER BY face_count DESC,p.person_name LIMIT 25"""
        ).fetchall()]
        runs = [dict(r) for r in con.execute(
            "SELECT * FROM photo_face_learning_runs ORDER BY id DESC LIMIT 10"
        ).fetchall()]
    return {"total": total, "people": people, "grades": grades, "top": top, "runs": runs}


def analysis_embed(result: dict[str, Any]) -> discord.Embed:
    e = discord.Embed(title="🧠 過去の確定顔：反映前診断", color=0xFEE75C)
    safe_add_field(e, name="対象", value=(
        f"確定済み顔 **{result['scanned']:,}件**\n"
        f"新規反映可能 **{result['eligible']:,}件**\n"
        f"反映済み **{result['already_registered']:,}件**"
    ), inline=True)
    safe_add_field(e, name="品質", value=(
        f"品質A **{result['quality_a']:,}件**\n品質B **{result['quality_b']:,}件**\n"
        f"品質不足 **{result['low_quality']:,}件**"
    ), inline=True)
    safe_add_field(e, name="除外", value=(
        f"特徴量なし **{result['no_embedding']:,}件**\n"
        f"重複 **{result['duplicate']:,}件**\n"
        f"人物マスター不整合 **{result['invalid_person']:,}件**"
    ), inline=True)
    e.set_footer(text="この診断だけではDBを変更せず、OpenAI APIも呼びません。")
    return e


def stats_embed() -> discord.Embed:
    stats = learning_stats()
    e = discord.Embed(title="📈 ローカル顔学習状況", color=0x57F287)
    safe_add_field(e, name="全体", value=(
        f"学習済み人物 **{stats['people']:,}人**\n"
        f"有効な参照顔 **{stats['total']:,}件**\n"
        f"品質A **{stats['grades'].get('A',0):,}件** / 品質B **{stats['grades'].get('B',0):,}件**"
    ), inline=False)
    top_text = "\n".join(
        f"・{r['person_name']}：{int(r['face_count']):,}件（A {int(r['quality_a'] or 0):,} / B {int(r['quality_b'] or 0):,}）"
        for r in stats["top"]
    ) or "まだ反映されていません。"
    safe_add_field(e, name="人物別（上位25人）", value=top_text, inline=False)
    return e


class ApplyPastLearningView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この確認画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="反映する", emoji="✅", style=discord.ButtonStyle.success)
    async def apply(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await __import__("asyncio").to_thread(apply_past_confirmed_faces, interaction.user.id)
        e = discord.Embed(title="✅ 過去の確定顔を反映しました", color=0x57F287)
        safe_add_field(e, name="結果", value=(
            f"新規反映 **{result['registered']:,}件**\n"
            f"反映済みのため省略 **{result['already_registered']:,}件**\n"
            f"重複除外 **{result['duplicate']:,}件**\n"
            f"品質・特徴量等で除外 **{result['low_quality'] + result['no_embedding'] + result['invalid_person']:,}件**"
        ), inline=False)
        await interaction.followup.send(embed=e, ephemeral=True)
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キャンセルしました。", embed=None, view=None)
        self.stop()


class PastFaceLearningView(discord.ui.View):
    def __init__(self, owner_id: int) -> None:
        super().__init__(timeout=600)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="反映前診断", emoji="🔍", style=discord.ButtonStyle.primary)
    async def diagnose(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        result = await __import__("asyncio").to_thread(analyze_past_confirmed_faces)
        await interaction.followup.send(embed=analysis_embed(result), view=ApplyPastLearningView(interaction.user.id), ephemeral=True)

    @discord.ui.button(label="学習状況", emoji="📈", style=discord.ButtonStyle.secondary)
    async def status(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        await interaction.followup.send(embed=await __import__("asyncio").to_thread(stats_embed), ephemeral=True)

    @discord.ui.button(label="反映履歴", emoji="📜", style=discord.ButtonStyle.secondary)
    async def history(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        stats = await __import__("asyncio").to_thread(learning_stats)
        lines = []
        for row in stats["runs"]:
            lines.append(
                f"**#{row['id']}** {row['created_at']}\n反映 {int(row['registered_count']):,} / 対象 {int(row['scanned_count']):,} / 除外 {int(row['duplicate_count']) + int(row['no_embedding_count']) + int(row['invalid_person_count']) + int(row['low_quality_count']):,}"
            )
        e = discord.Embed(title="📜 過去顔データ反映履歴", description="\n\n".join(lines) or "履歴はありません。", color=0x5865F2)
        await interaction.followup.send(embed=e, ephemeral=True)


def past_learning_home_embed() -> discord.Embed:
    e = discord.Embed(
        title="🧠 過去の確定顔をAIへ反映",
        description=(
            "過去に管理者が本確定した顔特徴量を、ローカル顔認証の参照データとして監査・登録します。\n"
            "OpenAI APIは使用しません。まず「反映前診断」で件数を確認してください。"
        ),
        color=0x5865F2,
    )
    safe_add_field(e, name="安全ルール", value="仮確定・人物不明・特徴量なし・低品質・重複は自動除外します。元の写真や人物確定は変更しません。", inline=False)
    return e
