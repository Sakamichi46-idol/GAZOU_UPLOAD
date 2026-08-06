"""管理者向けAI顔候補確認センター。

OpenAI APIは使用せず、ローカル顔特徴量候補を信頼度順・人物指定で確認する。
管理者が本確定した顔は ``photo_faces.confirmation_status='manually_confirmed'``
となり、次回のローカル候補計算で参照顔として再利用される。
"""
from __future__ import annotations

import asyncio
import io
import math
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import discord

from embed_safety import safe_add_field
from local_face_recognition import get_face_crop_bytes
from photo_database import (
    complete_face_reviews_bulk,
    get_connection,
    get_face_candidates,
    get_person_by_name,
)
from photo_face_review_view import FaceReviewView, build_face_review_embed

PAGE_SIZE = 25


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _reviewer(user: discord.abc.User) -> str:
    name = _text(getattr(user, "display_name", "")) or _text(getattr(user, "name", ""))
    return f"{name} ({user.id})"


def _order_sql(sort_mode: str) -> str:
    return {
        "confidence_desc": "top.confidence DESC, r.id ASC",
        "confidence_asc": "top.confidence ASC, r.id ASC",
        "newest": "r.id DESC",
        "oldest": "r.id ASC",
    }.get(sort_mode, "top.confidence DESC, r.id ASC")


def query_candidate_reviews(
    *,
    page: int = 0,
    sort_mode: str = "confidence_desc",
    person_name: str = "",
    min_confidence: float = 0.0,
) -> tuple[int, list[dict[str, Any]]]:
    """1位候補を持つ確認待ち顔をページ取得する。"""
    page = max(0, int(page))
    min_confidence = max(0.0, min(float(min_confidence), 1.0))
    person_name = _text(person_name)
    where = ["r.status='pending'", "top.candidate_rank=1", "top.confidence>=?"]
    params: list[Any] = [min_confidence]
    if person_name:
        where.append("p.person_name=?")
        params.append(person_name)
    where_sql = " AND ".join(where)
    with closing(get_connection()) as con:
        total = int(con.execute(
            f"""
            SELECT COUNT(*)
            FROM photo_face_reviews r
            JOIN photo_faces f ON f.id=r.face_id
            JOIN photo_face_candidates top ON top.face_id=f.id
            JOIN photo_people p ON p.id=top.person_id
            WHERE {where_sql}
            """,
            tuple(params),
        ).fetchone()[0] or 0)
        rows = con.execute(
            f"""
            SELECT r.id AS review_id,r.face_id,f.image_id,f.face_index,
                   top.person_id,top.confidence,p.person_name,
                   p.group_name AS person_group_name,
                   b.group_name,b.member_name,b.title,b.published_at,b.blog_url
            FROM photo_face_reviews r
            JOIN photo_faces f ON f.id=r.face_id
            JOIN photo_face_candidates top ON top.face_id=f.id
            JOIN photo_people p ON p.id=top.person_id
            JOIN photo_images i ON i.id=f.image_id
            JOIN photo_blogs b ON b.id=i.blog_id
            WHERE {where_sql}
            ORDER BY {_order_sql(sort_mode)}
            LIMIT ? OFFSET ?
            """,
            (*params, PAGE_SIZE, page * PAGE_SIZE),
        ).fetchall()
    return total, [dict(row) for row in rows]


def get_face_review_by_id(face_id: int) -> dict[str, Any] | None:
    with closing(get_connection()) as con:
        row = con.execute(
            """
            SELECT r.*,f.image_id,f.face_index,f.box_x,f.box_y,f.box_width,f.box_height,
                   i.local_path,i.bucket_key,i.file_name,i.image_url,i.download_status,i.download_error,
                   b.blog_url,b.group_name,b.member_name,b.title,b.published_at
            FROM photo_face_reviews r
            JOIN photo_faces f ON f.id=r.face_id
            JOIN photo_images i ON i.id=f.image_id
            JOIN photo_blogs b ON b.id=i.blog_id
            WHERE r.face_id=?
            """,
            (int(face_id),),
        ).fetchone()
    return dict(row) if row else None


@dataclass
class CandidateState:
    owner_id: int
    page: int = 0
    sort_mode: str = "confidence_desc"
    person_name: str = ""
    min_confidence: float = 0.0
    selected_face_ids: set[int] = field(default_factory=set)

    def load(self) -> tuple[int, list[dict[str, Any]]]:
        return query_candidate_reviews(
            page=self.page,
            sort_mode=self.sort_mode,
            person_name=self.person_name,
            min_confidence=self.min_confidence,
        )


def center_embed(state: CandidateState) -> discord.Embed:
    total, rows = state.load()
    pages = max(1, math.ceil(total / PAGE_SIZE))
    state.page = min(state.page, pages - 1)
    title = "🤖 AI顔候補を確認"
    e = discord.Embed(
        title=title,
        description=(
            "管理者の本確定結果は、次回のローカル顔候補計算で参照顔として利用されます。\n"
            "仮確定・保留は学習参照へ追加しません。OpenAI APIは使用しません。"
        ),
        color=0x5865F2,
    )
    labels = {
        "confidence_desc": "信頼度が高い順",
        "confidence_asc": "信頼度が低い順",
        "newest": "新しい順",
        "oldest": "古い順",
    }
    safe_add_field(e, name="表示条件", value=(
        f"並び順：**{labels.get(state.sort_mode, state.sort_mode)}**\n"
        f"人物：**{state.person_name or '全員'}**\n"
        f"最低信頼度：**{state.min_confidence * 100:.1f}%**"
    ), inline=True)
    safe_add_field(e, name="件数", value=(
        f"対象 **{total:,}件**\nページ **{state.page + 1}/{pages}**\n"
        f"選択中 **{len(state.selected_face_ids):,}件**"
    ), inline=True)
    lines = []
    for item in rows[:12]:
        lines.append(
            f"・顔ID **{item['face_id']}** — **{item['person_name']}** "
            f"{float(item.get('confidence') or 0) * 100:.1f}%"
        )
    safe_add_field(e, name="このページの先頭候補", value="\n".join(lines) or "対象なし", inline=False)
    e.set_footer(text="複数選択後に『選択を一括採用』を押すと、変更前確認を経て確定します。")
    return e


class CandidateFaceSelect(discord.ui.Select):
    def __init__(self, state: CandidateState, rows: list[dict[str, Any]]) -> None:
        self.state = state
        self.rows = rows
        options = []
        for item in rows:
            face_id = int(item["face_id"])
            confidence = float(item.get("confidence") or 0)
            options.append(discord.SelectOption(
                label=f"顔{face_id}: {_text(item.get('person_name'))[:70]}",
                value=str(face_id),
                description=f"{confidence * 100:.1f}% / 画像ID {item.get('image_id')}",
                default=face_id in state.selected_face_ids,
            ))
        super().__init__(
            placeholder="個別表示または一括採用する顔を選択",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        page_ids = {int(row["face_id"]) for row in self.rows}
        self.state.selected_face_ids.difference_update(page_ids)
        self.state.selected_face_ids.update(int(value) for value in self.values)
        view = CandidateCenterView(self.state)
        await interaction.response.edit_message(embed=center_embed(self.state), view=view)


class SortSelect(discord.ui.Select):
    def __init__(self, state: CandidateState) -> None:
        self.state = state
        options = [
            discord.SelectOption(label="信頼度が高い順", value="confidence_desc", default=state.sort_mode == "confidence_desc"),
            discord.SelectOption(label="信頼度が低い順", value="confidence_asc", default=state.sort_mode == "confidence_asc"),
            discord.SelectOption(label="新しい順", value="newest", default=state.sort_mode == "newest"),
            discord.SelectOption(label="古い順", value="oldest", default=state.sort_mode == "oldest"),
        ]
        super().__init__(placeholder="並び順を変更", options=options, row=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.sort_mode = self.values[0]
        self.state.page = 0
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))


class PersonFilterModal(discord.ui.Modal):
    def __init__(self, state: CandidateState) -> None:
        super().__init__(title="候補人物で絞り込み", timeout=300)
        self.state = state
        self.name = discord.ui.TextInput(
            label="人物名（空欄で解除）",
            placeholder="例：金村美玖",
            required=False,
            max_length=100,
            default=state.person_name,
        )
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = _text(self.name.value)
        if name:
            person = await asyncio.to_thread(get_person_by_name, name)
            if not person:
                await interaction.response.send_message(f"⚠️ 人物マスターに **{name}** は見つかりません。", ephemeral=True)
                return
            name = _text(person.get("person_name"))
        self.state.person_name = name
        self.state.page = 0
        self.state.selected_face_ids.clear()
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))


class ConfidenceFilterModal(discord.ui.Modal):
    def __init__(self, state: CandidateState) -> None:
        super().__init__(title="最低信頼度", timeout=300)
        self.state = state
        self.value_input = discord.ui.TextInput(
            label="最低信頼度（0〜100）",
            placeholder="例：95",
            default=f"{state.min_confidence * 100:.1f}",
            max_length=6,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            percent = float(str(self.value_input.value).strip())
        except ValueError:
            await interaction.response.send_message("0〜100の数値を入力してください。", ephemeral=True)
            return
        self.state.min_confidence = max(0.0, min(percent, 100.0)) / 100.0
        self.state.page = 0
        self.state.selected_face_ids.clear()
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))


class BulkConfirmView(discord.ui.View):
    def __init__(self, owner_id: int, items: list[dict[str, Any]]) -> None:
        super().__init__(timeout=300)
        self.owner_id = int(owner_id)
        self.items = items

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この確認は開始した管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="一括採用を確定", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        completed = await asyncio.to_thread(
            complete_face_reviews_bulk,
            self.items,
            _reviewer(interaction.user),
            "AI顔候補センター一括採用",
        )
        try:
            from admin_operations import record_ai_decision, write_audit
            for item in self.items:
                await asyncio.to_thread(
                    record_ai_decision,
                    interaction.user.id,
                    int(item.get("image_id") or 0),
                    int(item["face_id"]),
                    "accepted",
                    suggested_person=_text(item.get("person_name")),
                    confirmed_person=_text(item.get("person_name")),
                    confidence=float(item.get("confidence") or 0),
                )
            await asyncio.to_thread(
                write_audit,
                interaction.user.id,
                "face_candidates_bulk_accept",
                target_type="face",
                target_id=0,
                detail=f"count={completed}; face_ids={','.join(str(x['face_id']) for x in self.items)[:1500]}",
            )
        except Exception:
            pass
        await interaction.followup.send(
            f"✅ **{completed}件**を候補どおり一括確定しました。\n"
            "確定顔特徴量は、次回のローカル候補計算で参照されます。",
            ephemeral=True,
        )
        self.stop()

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="キャンセルしました。", embed=None, view=None)
        self.stop()


class CandidateCenterView(discord.ui.View):
    def __init__(self, state: CandidateState) -> None:
        super().__init__(timeout=900)
        self.state = state
        total, rows = state.load()
        self.total = total
        self.rows = rows
        if rows:
            self.add_item(CandidateFaceSelect(state, rows))
        self.add_item(SortSelect(state))
        pages = max(1, math.ceil(total / PAGE_SIZE))
        self.previous.disabled = state.page <= 0
        self.next.disabled = state.page >= pages - 1
        self.open_one.disabled = len(state.selected_face_ids) != 1
        self.bulk.disabled = not bool(state.selected_face_ids)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.state.owner_id:
            return True
        await interaction.response.send_message("この画面は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.page = max(0, self.state.page - 1)
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary, row=2)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.page += 1
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))

    @discord.ui.button(label="人物指定", emoji="👤", style=discord.ButtonStyle.primary, row=2)
    async def person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PersonFilterModal(self.state))

    @discord.ui.button(label="信頼度指定", emoji="📈", style=discord.ButtonStyle.primary, row=2)
    async def confidence(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfidenceFilterModal(self.state))

    @discord.ui.button(label="選択を解除", emoji="🧹", style=discord.ButtonStyle.secondary, row=2)
    async def clear(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.selected_face_ids.clear()
        await interaction.response.edit_message(embed=center_embed(self.state), view=CandidateCenterView(self.state))

    @discord.ui.button(label="選択した1件を開く", emoji="🖼️", style=discord.ButtonStyle.primary, row=3)
    async def open_one(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        face_id = next(iter(self.state.selected_face_ids))
        review = await asyncio.to_thread(get_face_review_by_id, face_id)
        if not review:
            await interaction.response.send_message("対象の顔レビューが見つかりません。", ephemeral=True)
            return
        try:
            data, _ = await asyncio.to_thread(get_face_crop_bytes, face_id)
        except Exception as error:
            await interaction.response.send_message(f"顔画像を作成できませんでした: `{type(error).__name__}: {error}`", ephemeral=True)
            return
        candidates = await asyncio.to_thread(get_face_candidates, face_id)
        view = FaceReviewView(review, candidates[:5], owner_id=interaction.user.id)
        await interaction.response.send_message(
            embed=build_face_review_embed(review, candidates[:5]),
            file=discord.File(io.BytesIO(data), filename="face_review.jpg"),
            view=view,
            ephemeral=True,
        )
        try:
            view.message = await interaction.original_response()
        except discord.HTTPException:
            pass

    @discord.ui.button(label="選択を一括採用", emoji="✅", style=discord.ButtonStyle.success, row=3)
    async def bulk(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        selected = set(self.state.selected_face_ids)
        items: list[dict[str, Any]] = []
        # ページをまたいだ選択にも対応するため、顔IDごとに1位候補を再取得する。
        with closing(get_connection()) as con:
            for face_id in sorted(selected):
                row = con.execute(
                    """
                    SELECT f.id AS face_id,f.image_id,c.person_id,c.confidence,p.person_name
                    FROM photo_faces f
                    JOIN photo_face_reviews r ON r.face_id=f.id AND r.status='pending'
                    JOIN photo_face_candidates c ON c.face_id=f.id AND c.candidate_rank=1
                    JOIN photo_people p ON p.id=c.person_id
                    WHERE f.id=?
                    """,
                    (face_id,),
                ).fetchone()
                if row:
                    items.append(dict(row))
        if not items:
            await interaction.response.send_message("一括採用できる確認待ち候補がありません。", ephemeral=True)
            return
        names: dict[str, int] = {}
        for item in items:
            names[_text(item.get("person_name"))] = names.get(_text(item.get("person_name")), 0) + 1
        detail = "\n".join(f"・{name}: {count}件" for name, count in names.items())
        e = discord.Embed(title="⚠️ AI顔候補の一括採用確認", color=0xFEE75C)
        safe_add_field(e, name="対象", value=f"{len(items)}件", inline=True)
        safe_add_field(e, name="候補別", value=detail or "なし", inline=False)
        safe_add_field(e, name="注意", value="画像を見ずに確定すると誤学習につながります。表示条件と候補を確認してください。", inline=False)
        await interaction.response.send_message(embed=e, view=BulkConfirmView(interaction.user.id, items), ephemeral=True)

    @discord.ui.button(label="使い方", emoji="❓", style=discord.ButtonStyle.secondary, row=3)
    async def help(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        e = discord.Embed(title="❓ AI顔候補確認の使い方", color=0x3498DB)
        e.description = (
            "① 並び順・人物・最低信頼度を指定\n"
            "② 一覧から顔を1件または複数選択\n"
            "③ 1件は画像を開いて採用・修正\n"
            "④ 複数は内容確認後に一括採用\n\n"
            "管理者が本確定した顔だけが、次回候補生成の参照顔になります。"
        )
        safe_add_field(e, name="おすすめ", value="最初は信頼度が高い順で1件ずつ確認し、精度が安定した人物だけ一括確認してください。", inline=False)
        safe_add_field(e, name="費用", value="この機能はローカル顔特徴量とSQLiteだけを使い、OpenAI APIを呼びません。", inline=False)
        await interaction.response.send_message(embed=e, ephemeral=True)


async def send_face_candidate_center(interaction: discord.Interaction) -> None:
    state = CandidateState(owner_id=interaction.user.id)
    await interaction.response.send_message(embed=center_embed(state), view=CandidateCenterView(state), ephemeral=True)
