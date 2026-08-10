from __future__ import annotations

import asyncio
from collections import Counter
from contextlib import closing
from typing import Any

import discord

from embed_safety import safe_add_field
from local_face_recognition import DEFAULT_MATCH_THRESHOLD, diagnose_face_candidates
from photo_database import get_connection


def _recent_analyzed_image_ids(limit: int = 20) -> list[int]:
    limit = max(1, min(int(limit), 100))
    with closing(get_connection()) as con:
        rows = con.execute(
            """
            SELECT image_id, MAX(created_at) AS last_used
            FROM photo_ai_usage
            WHERE image_id IS NOT NULL
              AND request_kind='api'
              AND status IN ('completed','review')
            GROUP BY image_id
            ORDER BY last_used DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    return [int(row[0]) for row in rows]


def diagnose_recent(limit: int = 20) -> dict[str, Any]:
    image_ids = _recent_analyzed_image_ids(limit)
    rows: list[dict[str, Any]] = []
    reasons: Counter[str] = Counter()
    errors: list[dict[str, str]] = []
    for image_id in image_ids:
        try:
            result = diagnose_face_candidates(image_id, top_n=3, scan_if_missing=True)
            rows.append(result)
            reasons[result["overall_reason"]] += 1
        except Exception as exc:
            errors.append({"image_id": str(image_id), "error": f"{type(exc).__name__}: {exc}"})
            reasons["診断エラー"] += 1
    return {
        "requested": int(limit),
        "images": rows,
        "errors": errors,
        "reasons": dict(reasons),
        "threshold": float(DEFAULT_MATCH_THRESHOLD),
    }


def _single_embed(result: dict[str, Any]) -> discord.Embed:
    image_id = int(result["image_id"])
    summary = result["summary"]
    embed = discord.Embed(title=f"🔍 顔候補診断：画像 {image_id}", color=0x3498DB)
    embed.description = (
        f"AI解析状態 **{result.get('analysis_status') or '不明'}**\n"
        f"顔検出 **{summary['detected_faces']}件** / 特徴量あり **{summary['with_embedding']}件**\n"
        f"候補しきい値 **{result['threshold'] * 100:.1f}%**\n"
        f"AI候補確認への登録候補 **{summary['registered_candidates']}件**\n"
        f"判定 **{result['overall_reason']}**"
    )
    if result.get("scanned_now"):
        embed.description += "\n※診断時にローカル顔スキャンを実行しました（OpenAI API不使用）。"

    for index, face in enumerate(result.get("faces", [])[:8], 1):
        candidates = face.get("top_candidates") or []
        candidate_lines = [
            f"{i}. {c['person_name']} **{float(c['confidence']) * 100:.1f}%**"
            for i, c in enumerate(candidates[:3], 1)
        ]
        value = (
            f"理由 **{face['reason']}**\n"
            f"参照顔 **{face['references']}件** / 登録候補 **{face['registered_count']}件**\n"
            + ("\n".join(candidate_lines) if candidate_lines else "候補スコアなし")
        )
        safe_add_field(embed, name=f"顔{index}（face_id={face['face_id']}）", value=value, inline=False)
    if len(result.get("faces", [])) > 8:
        embed.set_footer(text=f"顔は全{len(result['faces'])}件です。表示は先頭8件まで。OpenAI APIは使用していません。")
    else:
        embed.set_footer(text="診断は保存済みDBとローカル顔特徴量だけで実行します。OpenAI APIは使用しません。")
    return embed


def _recent_embed(report: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title="🔍 直近AI解析画像の顔候補診断", color=0x5865F2)
    total = len(report["images"]) + len(report["errors"])
    reasons = report["reasons"]
    lines = [f"・{reason}: **{count}枚**" for reason, count in sorted(reasons.items(), key=lambda x: (-x[1], x[0]))]
    embed.description = (
        f"対象 **{total}枚** / 候補しきい値 **{report['threshold'] * 100:.1f}%**\n"
        "OpenAI APIを使わず、保存済み顔特徴量を再照合しました。\n\n"
        + ("\n".join(lines) if lines else "診断対象がありません。")
    )

    detail_lines: list[str] = []
    for item in report["images"][:20]:
        best_name = "—"
        best_score = 0.0
        for face in item.get("faces", []):
            candidates = face.get("top_candidates") or []
            if candidates and float(candidates[0]["confidence"]) > best_score:
                best_name = str(candidates[0]["person_name"])
                best_score = float(candidates[0]["confidence"])
        best_text = f" / 最高 {best_name} {best_score * 100:.1f}%" if best_score > 0 else ""
        detail_lines.append(f"`{item['image_id']}` {item['overall_reason']}{best_text}")
    if detail_lines:
        safe_add_field(embed, name="画像別", value="\n".join(detail_lines), inline=False)
    if report["errors"]:
        safe_add_field(
            embed,
            name="診断エラー",
            value="\n".join(f"`{e['image_id']}` {e['error']}" for e in report["errors"][:8]),
            inline=False,
        )
    embed.set_footer(text="候補0件でも、最高候補としきい値差を確認できます。")
    return embed


class FaceDiagnosticIdModal(discord.ui.Modal, title="顔候補診断：画像ID指定"):
    image_id = discord.ui.TextInput(label="画像ID", placeholder="例: 85770", max_length=20)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            image_id = int(str(self.image_id.value).strip())
        except ValueError:
            await interaction.response.send_message("⚠️ 画像IDは数字で入力してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            result = await asyncio.to_thread(diagnose_face_candidates, image_id, 3, scan_if_missing=True)
        except Exception as exc:
            await interaction.followup.send(f"⚠️ 診断に失敗しました: {type(exc).__name__}: {exc}", ephemeral=True)
            return
        await interaction.followup.send(embed=_single_embed(result), ephemeral=True)


class FaceCandidateDiagnosticView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="直近20枚を診断", emoji="📊", style=discord.ButtonStyle.primary)
    async def recent(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        report = await asyncio.to_thread(diagnose_recent, 20)
        await interaction.followup.send(embed=_recent_embed(report), ephemeral=True)

    @discord.ui.button(label="画像IDを指定", emoji="🔎", style=discord.ButtonStyle.secondary)
    async def by_id(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FaceDiagnosticIdModal())


async def send_face_candidate_diagnostics(interaction: discord.Interaction) -> None:
    report = await asyncio.to_thread(diagnose_recent, 20)
    embed = _recent_embed(report)
    view = FaceCandidateDiagnosticView(interaction.user.id)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, view=view, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
