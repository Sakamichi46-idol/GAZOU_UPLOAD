from __future__ import annotations

import asyncio
import io
import logging
from typing import Any

from PIL import Image

import discord
from discord.ext import commands
from embed_safety import safe_add_field
from photo_search import get_display_image_url

from local_face_recognition import (
    FaceEngineUnavailable,
    get_face_crop_bytes,
    suggest_face_candidates,
)
from photo_image_repair import repair_photo_image
from sakamichi_members import SAKAMICHI_MEMBERS
from photo_database import (
    complete_face_review,
    complete_face_review_no_face,
    get_face_review_by_face_id,
    get_face_candidates,
    get_pending_face_reviews,
    get_person_by_name,
    reopen_face_review,
    skip_face_review,
)

LOGGER = logging.getLogger(__name__)


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _short(value: Any, limit: int = 1000) -> str:
    text = _text(value)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _reviewer(user: discord.abc.User) -> str:
    name = _text(getattr(user, "display_name", "")) or _text(getattr(user, "name", ""))
    return f"{name} ({user.id})"


async def _load_candidates(review: dict[str, Any]) -> list[dict[str, Any]]:
    """候補が未生成なら、その画像の顔候補をローカル計算してから再取得する。"""
    face_id = int(review["face_id"])
    candidates = await asyncio.to_thread(get_face_candidates, face_id)
    if candidates:
        return candidates[:5]

    try:
        await asyncio.to_thread(suggest_face_candidates, int(review["image_id"]), 5)
    except (FaceEngineUnavailable, ValueError):
        pass
    except Exception:
        # レビュー画面自体は候補生成に失敗しても表示できるようにする。
        pass
    return (await asyncio.to_thread(get_face_candidates, face_id))[:5]


def _prepare_face_review_image(data: bytes, min_side: int = 720) -> bytes:
    """Discord確認用だけ顔切り抜きを拡大する。認識用データ自体は変更しない。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image = image.convert("RGB")
            width, height = image.size
            if width <= 0 or height <= 0:
                return data
            scale = max(1.0, float(min_side) / float(max(width, height)))
            if scale > 1.0:
                image = image.resize(
                    (max(1, int(width * scale)), max(1, int(height * scale))),
                    Image.Resampling.LANCZOS,
                )
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=94, optimize=True)
            return output.getvalue()
    except Exception:
        return data


async def _load_review_image(review: dict[str, Any]) -> tuple[bytes, str]:
    """顔切り抜きを取得し、必要なら元画像を1度だけ修復して再試行する。"""
    try:
        data, _ = await asyncio.to_thread(get_face_crop_bytes, int(review["face_id"]))
        return _prepare_face_review_image(data), "face_review.jpg"
    except FileNotFoundError as first_error:
        repair_result = await repair_photo_image(int(review["image_id"]))
        if repair_result.get("success"):
            data, _ = await asyncio.to_thread(get_face_crop_bytes, int(review["face_id"]))
            return _prepare_face_review_image(data), "face_review.jpg"
        raise FileNotFoundError(
            f"{first_error} / repair: {repair_result.get('error') or '修復失敗'}"
        )


async def _next_pending_review(exclude_face_id: int = 0) -> dict[str, Any] | None:
    rows = await asyncio.to_thread(get_pending_face_reviews, 50)
    for row in rows:
        if int(row.get("face_id") or 0) != int(exclude_face_id or 0):
            return row
    return None


def build_face_review_embed(
    review: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="👤 顔の人物確認",
        description=("顔を1件ずつ確認します。候補・投稿者・メンバー選択から確定してください。"
                     "顔ではない切り抜きは **🚫 顔なし** を選べます。候補はローカル処理による参考値です。"),
        color=discord.Color.blue(),
    )
    safe_add_field(embed, name="顔ID", value=str(review.get("face_id", "不明")), inline=True)
    safe_add_field(embed, name="画像ID", value=str(review.get("image_id", "不明")), inline=True)
    safe_add_field(embed, name="顔番号", value=str(review.get("face_index", "不明")), inline=True)
    safe_add_field(embed, name="グループ", value=_text(review.get("group_name")) or "不明", inline=True)
    safe_add_field(embed, name="ブログ投稿者", value=_text(review.get("member_name")) or "不明", inline=True)
    safe_add_field(embed, name="投稿日", value=_short(review.get("published_at"), 100) or "不明", inline=True)
    safe_add_field(embed, name="タイトル", value=_short(review.get("title")) or "タイトルなし", inline=False)

    if candidates:
        lines = []
        for index, item in enumerate(candidates, 1):
            integrated = float(item.get("confidence") or 0)
            raw = float(item.get("face_similarity") or 0)
            band = _text(item.get("confidence_band"))
            reason = _text(item.get("score_reason"))
            line = f"{index}. **{item.get('person_name', '不明')}** — 統合 {integrated * 100:.1f}%"
            if raw:
                line += f" / 顔 {raw * 100:.1f}%"
            if band:
                line += f" / {band}"
            lines.append(line)
            if reason:
                lines.append(f"   └ {reason}")
        candidate_text = "\n".join(lines)
    else:
        candidate_text = "一致閾値を超えた候補はありません。メンバー選択または投稿者ボタンを使ってください。"
    safe_add_field(embed, name="ローカル顔候補", value=candidate_text, inline=False)

    if review.get("blog_url"):
        safe_add_field(embed, name="ブログ", value=f"[元のブログを開く]({review['blog_url']})", inline=False)
    embed.set_image(url="attachment://face_review.jpg")
    embed.set_footer(text="1件ずつ表示します。確定・顔なし・保留の後は自動で次へ進みます。OpenAI APIは使用しません。")
    return embed


def build_completed_embed(
    review: dict[str, Any],
    person_name: str,
    user: discord.abc.User,
) -> discord.Embed:
    embed = discord.Embed(title="✅ 顔レビュー完了", color=discord.Color.green())
    safe_add_field(embed, name="顔ID", value=str(review.get("face_id", "不明")), inline=True)
    safe_add_field(embed, name="画像ID", value=str(review.get("image_id", "不明")), inline=True)
    safe_add_field(embed, name="確定人物", value=person_name, inline=False)
    safe_add_field(embed, name="確認者", value=_text(getattr(user, "display_name", user.name)), inline=False)
    return embed


class FacePersonModal(discord.ui.Modal, title="顔の人物を指定"):
    person_name = discord.ui.TextInput(
        label="人物名",
        placeholder="人物マスターに登録済みの正確な名前",
        min_length=1,
        max_length=100,
    )

    def __init__(self, parent: "FaceReviewView") -> None:
        super().__init__()
        self.parent_view = parent
        current_name = _text(getattr(parent, "current_person_name", ""))
        if current_name and current_name != "不明":
            self.person_name.default = current_name[:100]

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        name = _text(self.person_name.value)
        person = await asyncio.to_thread(get_person_by_name, name)
        if not person:
            await interaction.followup.send(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(name)}** は見つかりません。名前を確認してください。",
                ephemeral=True,
            )
            return
        await self.parent_view.complete(interaction, int(person["id"]), _text(person["person_name"]), "人物名入力")


class CandidateSelect(discord.ui.Select):
    def __init__(self, parent: "FaceReviewView", candidates: list[dict[str, Any]]) -> None:
        options: list[discord.SelectOption] = []
        for item in candidates[:5]:
            confidence = float(item.get("confidence") or 0)
            options.append(
                discord.SelectOption(
                    label=_short(item.get("person_name"), 100) or "不明",
                    value=str(int(item["person_id"])),
                    description=(f"統合 {confidence*100:.1f}% / 顔 {float(item.get('face_similarity') or 0)*100:.1f}%")[:100],
                )
            )
        super().__init__(
            placeholder="候補から人物を選択",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )
        self.parent_view = parent
        self.person_by_id = {int(item["person_id"]): item for item in candidates[:5]}

    async def callback(self, interaction: discord.Interaction) -> None:
        person_id = int(self.values[0])
        item = self.person_by_id[person_id]
        await self.parent_view.complete(
            interaction,
            person_id,
            _text(item.get("person_name")) or "不明",
            f"候補選択 類似度={float(item.get('confidence') or 0):.6f}",
        )


class FaceMemberSelectionState:
    """顔レビュー用の階層式メンバー選択状態。"""

    def __init__(self, parent_view: "FaceReviewView") -> None:
        self.parent_view = parent_view
        self.owner_id = parent_view.owner_id
        self.group_name = ""
        self.generation_name = ""
        self.member_page = 0


def _face_selection_text(state: FaceMemberSelectionState) -> str:
    lines = ["👤 **顔に写っているメンバーを選択してください。**"]
    if state.group_name:
        lines.append(f"グループ: **{discord.utils.escape_markdown(state.group_name)}**")
    if state.generation_name:
        lines.append(f"期・区分: **{discord.utils.escape_markdown(state.generation_name)}**")
    return "\n".join(lines)


class FaceMemberOwnedView(discord.ui.View):
    def __init__(self, state: FaceMemberSelectionState) -> None:
        super().__init__(timeout=None)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message(
                "このメンバー選択は顔レビューを開始した本人だけが操作できます。",
                ephemeral=True,
            )
            return False
        if self.state.parent_view.finished:
            await interaction.response.send_message(
                "この顔レビューはすでに完了しています。",
                ephemeral=True,
            )
            return False
        return True


class FaceGroupSelect(discord.ui.Select):
    def __init__(self, state: FaceMemberSelectionState) -> None:
        options = [discord.SelectOption(label=name, value=name) for name in SAKAMICHI_MEMBERS]
        super().__init__(
            placeholder="グループを選択",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.group_name = self.values[0]
        self.state.generation_name = ""
        self.state.member_page = 0
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceGenerationView(self.state),
        )


class FaceGroupView(FaceMemberOwnedView):
    def __init__(self, state: FaceMemberSelectionState) -> None:
        super().__init__(state)
        self.add_item(FaceGroupSelect(state))


class FaceGenerationSelect(discord.ui.Select):
    def __init__(self, state: FaceMemberSelectionState) -> None:
        generations = SAKAMICHI_MEMBERS[state.group_name]
        options = [discord.SelectOption(label=name, value=name) for name in generations]
        super().__init__(
            placeholder="期・区分を選択",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.generation_name = self.values[0]
        self.state.member_page = 0
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceMemberView(self.state),
        )


class FaceGenerationView(FaceMemberOwnedView):
    def __init__(self, state: FaceMemberSelectionState) -> None:
        super().__init__(state)
        self.add_item(FaceGenerationSelect(state))

    @discord.ui.button(label="グループへ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceGroupView(self.state),
        )


class FaceMemberSelect(discord.ui.Select):
    PAGE_SIZE = 25

    def __init__(self, state: FaceMemberSelectionState) -> None:
        all_names = list(SAKAMICHI_MEMBERS[state.group_name][state.generation_name])
        page_count = max(1, (len(all_names) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        state.member_page = max(0, min(state.member_page, page_count - 1))
        start = state.member_page * self.PAGE_SIZE
        names = all_names[start:start + self.PAGE_SIZE]
        options = [discord.SelectOption(label=name, value=name) for name in names]
        super().__init__(
            placeholder=f"メンバーを選択（{state.member_page + 1}/{page_count}ページ）",
            min_values=1,
            max_values=1,
            options=options,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        await self.state.parent_view.complete_from_member_select(interaction, self.values[0])


class FaceMemberView(FaceMemberOwnedView):
    PAGE_SIZE = 25

    def __init__(self, state: FaceMemberSelectionState) -> None:
        super().__init__(state)
        self.add_item(FaceMemberSelect(state))
        total = len(SAKAMICHI_MEMBERS[state.group_name][state.generation_name])
        page_count = max(1, (total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.previous.disabled = state.member_page <= 0
        self.next.disabled = state.member_page >= page_count - 1

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.member_page = max(0, self.state.member_page - 1)
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceMemberView(self.state),
        )

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.member_page += 1
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceMemberView(self.state),
        )

    @discord.ui.button(label="別の期・区分へ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def generation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.generation_name = ""
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceGenerationView(self.state),
        )

    @discord.ui.button(label="別グループへ戻る", emoji="🌳", style=discord.ButtonStyle.secondary, row=2)
    async def group(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(
            content=_face_selection_text(self.state),
            view=FaceGroupView(self.state),
        )


class ConfirmedFaceEditView(discord.ui.View):
    """確定済みの顔人物を、同じ確認メッセージから再編集するView。"""

    def __init__(
        self,
        review: dict[str, Any],
        *,
        owner_id: int,
        person_id: int,
        person_name: str,
    ) -> None:
        super().__init__(timeout=None)
        self.review = review
        self.owner_id = int(owner_id)
        self.current_person_id = int(person_id)
        self.current_person_name = _text(person_name) or "不明"
        self.message: discord.Message | None = None
        self.finished = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この人物編集は顔レビューを行った本人だけが操作できます。",
                ephemeral=True,
            )
            return False
        return True

    async def complete(
        self,
        interaction: discord.Interaction,
        person_id: int,
        person_name: str,
        note: str,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        await asyncio.to_thread(
            complete_face_review,
            int(self.review["face_id"]),
            int(person_id),
            _reviewer(interaction.user),
            f"再編集: {note}",
        )
        self.current_person_id = int(person_id)
        self.current_person_name = _text(person_name) or "不明"
        embed = build_completed_embed(self.review, self.current_person_name, interaction.user)
        embed.description = "登録済みの人物を更新しました。必要なら、下のボタンから再編集できます。"

        if self.message is not None:
            try:
                await self.message.edit(embed=embed, view=self)
            except discord.HTTPException:
                pass

        escaped = discord.utils.escape_markdown(self.current_person_name)
        if interaction.message is not None and self.message is not None and interaction.message.id == self.message.id:
            await interaction.edit_original_response(embed=embed, view=self)
        else:
            await interaction.followup.send(
                f"✅ 顔ID **{self.review.get('face_id', '不明')}** の登録人物を **{escaped}** に更新しました。",
                ephemeral=True,
            )

    async def complete_from_member_select(
        self,
        interaction: discord.Interaction,
        person_name: str,
    ) -> None:
        person = await asyncio.to_thread(get_person_by_name, person_name)
        if not person:
            await interaction.response.send_message(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(person_name)}** が見つかりません。",
                ephemeral=True,
            )
            return
        await self.complete(
            interaction,
            int(person["id"]),
            _text(person["person_name"]),
            "階層式メンバー選択メニュー",
        )

    @discord.ui.button(label="登録人物を編集", emoji="✏️", style=discord.ButtonStyle.primary, row=0)
    async def edit_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = FaceMemberSelectionState(self)
        await interaction.response.send_message(
            _face_selection_text(state)
            + f"\n\n👤 **現在の登録人物**\n・{discord.utils.escape_markdown(self.current_person_name)}"
            + "\n\n新しい人物を選ぶと、現在の登録内容を置き換えます。",
            view=FaceGroupView(state),
            ephemeral=True,
        )

    @discord.ui.button(label="名前を入力して編集", emoji="⌨️", style=discord.ButtonStyle.secondary, row=0)
    async def edit_by_name(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FacePersonModal(self))


class ConfirmedFaceCorrectionModal(discord.ui.Modal, title="確定済みの顔を修正"):
    face_id_input = discord.ui.TextInput(
        label="顔ID",
        placeholder="例: 10541",
        max_length=20,
    )
    person_name_input = discord.ui.TextInput(
        label="新しい人物名（顔でない場合は『顔なし』）",
        placeholder="例: 山下葉留花 / 顔なし",
        max_length=100,
    )

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            face_id = int(_text(self.face_id_input.value))
        except ValueError:
            await interaction.followup.send("⚠️ 顔IDは数字で入力してください。", ephemeral=True)
            return

        review = await asyncio.to_thread(get_face_review_by_face_id, face_id)
        if not review:
            await interaction.followup.send(f"⚠️ 顔ID **{face_id}** は見つかりません。", ephemeral=True)
            return

        name = _text(self.person_name_input.value)
        if name in {"顔なし", "なし", "顔ではない", "not_a_face"}:
            await asyncio.to_thread(
                complete_face_review_no_face,
                face_id,
                _reviewer(interaction.user),
                "確定済み修正: 顔なし",
            )
            await interaction.followup.send(
                f"✅ 顔ID **{face_id}** を **顔なし** に修正しました。",
                ephemeral=True,
            )
            return

        person = await asyncio.to_thread(get_person_by_name, name)
        if not person:
            await interaction.followup.send(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(name)}** は見つかりません。",
                ephemeral=True,
            )
            return

        await asyncio.to_thread(
            complete_face_review,
            face_id,
            int(person["id"]),
            _reviewer(interaction.user),
            "確定済み修正モーダル",
        )
        await interaction.followup.send(
            f"✅ 顔ID **{face_id}** を **{discord.utils.escape_markdown(_text(person['person_name']))}** に修正しました。",
            ephemeral=True,
        )



class FaceReviewFinalConfirmView(discord.ui.View):
    """顔レビューのDB確定直前に、もう一度内容を確認するView。"""

    def __init__(self, parent_view: "FaceReviewView", *, person_id: int | None,
                 person_name: str, note: str, no_face: bool = False) -> None:
        super().__init__(timeout=600)
        self.parent_view = parent_view
        self.owner_id = parent_view.owner_id
        self.person_id = int(person_id) if person_id is not None else None
        self.person_name = _text(person_name) or "不明"
        self.note = _text(note)
        self.no_face = bool(no_face)
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この最終確認は顔確認を開始した本人だけが操作できます。", ephemeral=True)
            return False
        if self.done:
            await interaction.response.send_message("この最終確認は処理済みです。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="この内容で確定", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        self.done = True
        self.stop()
        if self.no_face:
            await self.parent_view._commit_no_face(interaction)
        else:
            if self.person_id is None:
                await interaction.followup.send("⚠️ 確定する人物IDがありません。", ephemeral=True)
                return
            await self.parent_view._commit_person(
                interaction, self.person_id, self.person_name, self.note)
        try:
            await interaction.edit_original_response(
                content="✅ 確定しました。顔確認画面を次の1件へ更新しました。",
                embed=None, view=None)
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            pass

    @discord.ui.button(label="選び直す", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.done = True
        self.stop()
        await interaction.response.edit_message(
            content="↩️ 確定しませんでした。元の顔確認画面から選び直してください。",
            embed=None, view=None)

class FaceReviewView(discord.ui.View):
    def __init__(
        self,
        review: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        owner_id: int,
        previous_face_id: int | None = None,
        previous_label: str = "",
    ) -> None:
        super().__init__(timeout=None)
        self.review = review
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self.finished = False
        self.candidates = list(candidates or [])
        self.previous_face_id = int(previous_face_id) if previous_face_id else None
        self.previous_label = _text(previous_label)

        if candidates:
            self.add_item(CandidateSelect(self, candidates))

        member_name = _text(review.get("member_name"))
        self.author_button.disabled = not bool(member_name)
        if member_name:
            self.author_button.label = f"投稿者: {_short(member_name, 60)}"

        self.undo_button.disabled = self.previous_face_id is None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "このレビューは開始した本人だけが操作できます。",
                ephemeral=True,
            )
            return False
        if self.finished:
            await interaction.response.send_message(
                "この顔レビューはすでに処理済みです。",
                ephemeral=True,
            )
            return False
        return True

    async def _defer(self, interaction: discord.Interaction) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False, thinking=False)

    async def _advance(
        self, interaction: discord.Interaction, *, completed_face_id: int,
        completed_label: str,
    ) -> None:
        """元の顔確認メッセージそのものを次の1件へ差し替える。"""
        target_message = self.message
        next_review = await _next_pending_review(exclude_face_id=completed_face_id)
        if next_review is None:
            embed = discord.Embed(
                title="✅ 顔確認の確認待ちはありません",
                description=(f"直前: 顔ID **{completed_face_id}** → "
                             f"**{discord.utils.escape_markdown(completed_label)}**\n"
                             "必要なら管理メニューから確定済み修正を行えます。"),
                color=discord.Color.green())
            if target_message is not None:
                await target_message.edit(embed=embed, view=None, attachments=[])
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return
        try:
            candidates = await _load_candidates(next_review)
            data, filename = await _load_review_image(next_review)
        except Exception as error:
            LOGGER.exception("次の顔レビュー読み込みに失敗しました face_id=%s", next_review.get("face_id"))
            embed = discord.Embed(title="⚠️ 次の顔を表示できませんでした",
                description=f"`{type(error).__name__}: {_short(error, 1200)}`",
                color=discord.Color.orange())
            if target_message is not None:
                await target_message.edit(embed=embed, view=None, attachments=[])
            else:
                await interaction.followup.send(embed=embed, ephemeral=True)
            return
        next_view = FaceReviewView(next_review, candidates, owner_id=self.owner_id,
            previous_face_id=completed_face_id, previous_label=completed_label)
        file = discord.File(io.BytesIO(data), filename=filename)
        embed = build_face_review_embed(next_review, candidates)
        embed.description = (f"✅ 直前: 顔ID **{completed_face_id}** → "
            f"**{discord.utils.escape_markdown(completed_label)}**\n\n" + (embed.description or ""))
        if target_message is not None:
            next_view.message = target_message
            await target_message.edit(embed=embed, view=next_view, attachments=[file])
        else:
            message = await interaction.followup.send(embed=embed, view=next_view, file=file, wait=True)
            next_view.message = message

    async def _send_final_confirmation(
        self, interaction: discord.Interaction, *, person_id: int | None,
        person_name: str, note: str, no_face: bool = False,
    ) -> None:
        label = "🚫 顔なし" if no_face else f"👤 {discord.utils.escape_markdown(person_name)}"
        embed = discord.Embed(
            title="🔎 最終確認",
            description=(f"顔ID **{self.review['face_id']}** を **{label}** として確定しますか？\n\n"
                         "まだDBには保存していません。画像をもう一度確認して、問題なければ確定してください。"),
            color=discord.Color.gold())
        view = FaceReviewFinalConfirmView(
            self, person_id=person_id, person_name=person_name, note=note, no_face=no_face)
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, view=view, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def complete(self, interaction: discord.Interaction, person_id: int,
                       person_name: str, note: str) -> None:
        """人物選択時点では保存せず、最終確認を表示する。"""
        await self._send_final_confirmation(
            interaction, person_id=int(person_id), person_name=person_name, note=note)

    async def _commit_person(self, interaction: discord.Interaction, person_id: int,
                             person_name: str, note: str) -> None:
        await asyncio.to_thread(complete_face_review, int(self.review["face_id"]),
            int(person_id), _reviewer(interaction.user), note)
        try:
            from admin_operations import record_ai_decision, write_audit
            top = self.candidates[0] if self.candidates else {}
            suggested_name = _text(top.get("person_name"))
            confidence = float(top.get("confidence") or 0)
            if not self.candidates: decision = "no_candidate"
            elif int(top.get("person_id") or 0) == int(person_id): decision = "accepted"
            else: decision = "corrected"
            await asyncio.to_thread(record_ai_decision, interaction.user.id,
                int(self.review.get("image_id") or 0), int(self.review["face_id"]), decision,
                suggested_person=suggested_name, confirmed_person=person_name, confidence=confidence)
            await asyncio.to_thread(write_audit, interaction.user.id, "face_person_confirm",
                target_type="face", target_id=int(self.review["face_id"]),
                detail=f"{suggested_name or '候補なし'} -> {person_name} ({decision})")
        except Exception:
            LOGGER.exception("AI判断・監査ログの保存に失敗しました")
        self.finished = True
        self.stop()
        await self._advance(interaction, completed_face_id=int(self.review["face_id"]),
                            completed_label=person_name)

    async def _commit_no_face(self, interaction: discord.Interaction) -> None:
        await asyncio.to_thread(complete_face_review_no_face, int(self.review["face_id"]),
            _reviewer(interaction.user), "Discord顔レビュー: 顔なし（最終確認済み）")
        self.finished = True
        self.stop()
        await self._advance(interaction, completed_face_id=int(self.review["face_id"]),
                            completed_label="顔なし")

    async def complete_from_member_select(
        self, interaction: discord.Interaction, person_name: str,
    ) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=True, thinking=True)
        person = await asyncio.to_thread(get_person_by_name, person_name)
        if not person:
            await interaction.followup.send(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(person_name)}** が見つかりません。",
                ephemeral=True)
            return
        person_name_text = _text(person["person_name"])
        embed = discord.Embed(
            title="🔎 最終確認",
            description=(f"顔ID **{self.review['face_id']}** を "
                         f"**👤 {discord.utils.escape_markdown(person_name_text)}** として確定しますか？\n\n"
                         "まだDBには保存していません。"),
            color=discord.Color.gold())
        view = FaceReviewFinalConfirmView(self, person_id=int(person["id"]),
            person_name=person_name_text, note="階層式メンバー選択メニュー（最終確認済み）")
        await interaction.edit_original_response(content=None, embed=embed, view=view)

    @discord.ui.button(label="投稿者で確定", emoji="📝", style=discord.ButtonStyle.primary, row=1)
    async def author_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False, thinking=False)
        member_name = _text(self.review.get("member_name"))
        person = await asyncio.to_thread(get_person_by_name, member_name)
        if not person:
            await interaction.followup.send(
                f"⚠️ 投稿者 **{discord.utils.escape_markdown(member_name)}** が人物マスターに見つかりません。",
                ephemeral=True,
            )
            return
        await self.complete(
            interaction,
            int(person["id"]),
            _text(person["person_name"]),
            "ブログ投稿者ボタン",
        )

    @discord.ui.button(label="別人物を選択", emoji="👥", style=discord.ButtonStyle.secondary, row=1)
    async def other_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        state = FaceMemberSelectionState(self)
        await interaction.response.send_message(
            _face_selection_text(state),
            view=FaceGroupView(state),
            ephemeral=True,
        )

    @discord.ui.button(label="名前を入力", emoji="⌨️", style=discord.ButtonStyle.secondary, row=1)
    async def name_input_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FacePersonModal(self))

    @discord.ui.button(label="顔なし", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def no_face_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._send_final_confirmation(
            interaction, person_id=None, person_name="顔なし",
            note="Discord顔レビュー: 顔なし", no_face=True)

    @discord.ui.button(label="今回は保留", emoji="⏭️", style=discord.ButtonStyle.secondary, row=2)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if not interaction.response.is_done():
            await interaction.response.defer(ephemeral=False, thinking=False)
        await asyncio.to_thread(
            skip_face_review,
            int(self.review["face_id"]),
            _reviewer(interaction.user),
            "Discord顔レビューで保留",
        )
        self.finished = True
        self.stop()
        await self._advance(
            interaction,
            completed_face_id=int(self.review["face_id"]),
            completed_label="保留",
        )

    @discord.ui.button(label="元画像を見る", emoji="🖼️", style=discord.ButtonStyle.secondary, row=2)
    async def original_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        image_url = await asyncio.to_thread(get_display_image_url, self.review)
        embed = discord.Embed(
            title=f"🖼️ 元画像 / 画像ID {self.review.get('image_id', '不明')}",
            color=discord.Color.blurple(),
        )
        if image_url:
            embed.set_image(url=image_url)
        if self.review.get("blog_url"):
            safe_add_field(
                embed,
                name="ブログ",
                value=f"[元のブログを開く]({self.review['blog_url']})",
                inline=False,
            )
        if not image_url:
            embed.description = "画像URLを作成できませんでした。元ブログから確認してください。"
        await interaction.followup.send(embed=embed, ephemeral=True)

    @discord.ui.button(label="直前を取り消す", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def undo_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.previous_face_id is None:
            await interaction.response.send_message("取り消せる直前操作はありません。", ephemeral=True)
            return
        await self._defer(interaction)
        face_id = int(self.previous_face_id)
        await asyncio.to_thread(
            reopen_face_review,
            face_id,
            _reviewer(interaction.user),
            f"直前操作を取り消し: {self.previous_label}",
        )
        review = await asyncio.to_thread(get_face_review_by_face_id, face_id)
        if not review:
            await interaction.followup.send("⚠️ 取り消した顔を再表示できませんでした。", ephemeral=True)
            return
        candidates = await _load_candidates(review)
        data, filename = await _load_review_image(review)
        view = FaceReviewView(review, candidates, owner_id=self.owner_id)
        view.message = interaction.message
        file = discord.File(io.BytesIO(data), filename=filename)
        embed = build_face_review_embed(review, candidates)
        embed.description = "↩️ 直前の確定を取り消しました。もう一度この顔を確認してください。\n\n" + (embed.description or "")
        await interaction.edit_original_response(embed=embed, view=view, attachments=[file])

    @discord.ui.button(label="確定済みを修正", emoji="🛠️", style=discord.ButtonStyle.secondary, row=2)
    async def correction_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(ConfirmedFaceCorrectionModal())

    async def on_timeout(self) -> None:
        # timeout=None のため通常は呼ばれない。将来timeoutを変更しても操作不能化しない。
        return


async def send_face_review_batch(ctx: commands.Context, limit: int = 1) -> int:
    """顔確認は常に1件だけ表示し、判定後に同じメッセージで次へ進む。"""
    reviews = await asyncio.to_thread(get_pending_face_reviews, 50)
    if not reviews:
        await ctx.send("✅ 顔の確認待ちはありません。")
        return 0

    for review in reviews:
        try:
            candidates = await _load_candidates(review)
            data, filename = await _load_review_image(review)
        except FileNotFoundError as error:
            await asyncio.to_thread(
                skip_face_review,
                int(review["face_id"]),
                "system: unavailable source image",
                f"確認画像を取得できないため保留: {type(error).__name__}: {error}",
            )
            continue
        except Exception as error:
            LOGGER.exception("顔レビュー画像の作成に失敗 face_id=%s", review.get("face_id"))
            await ctx.send(
                f"❌ 顔ID **{review['face_id']}**（画像ID **{review['image_id']}**）の確認画像を作成できませんでした: "
                f"`{type(error).__name__}: {_short(error, 900)}`"
            )
            continue

        view = FaceReviewView(review, candidates, owner_id=ctx.author.id)
        file = discord.File(io.BytesIO(data), filename=filename)
        message = await ctx.send(
            embed=build_face_review_embed(review, candidates),
            view=view,
            file=file,
        )
        view.message = message
        return 1

    await ctx.send("⚠️ 表示可能な顔確認項目がありませんでした。取得不能な項目は保留へ移しました。")
    return 0


def build_fast_review_embed(
    items: list[dict[str, Any]],
    min_confidence: float,
) -> discord.Embed:
    """高信頼度の1位候補を一括確認するためのプレビュー。"""
    embed = discord.Embed(
        title="⚡ 高信頼度の顔候補を一括確認",
        description=(
            "ローカル顔認識の1位候補だけを表示しています。\n"
            "画像を個別確認せず一括確定する機能なので、内容を確認してから実行してください。"
        ),
        color=discord.Color.gold(),
    )
    safe_add_field(embed, name="対象件数", value=f"{len(items):,}件", inline=True)
    safe_add_field(embed, name="最低信頼度", value=f"{min_confidence * 100:.1f}%", inline=True)

    grouped: dict[str, list[float]] = {}
    for item in items:
        name = _text(item.get("person_name")) or "不明"
        grouped.setdefault(name, []).append(float(item.get("confidence") or 0))

    lines: list[str] = []
    for name, values in sorted(grouped.items(), key=lambda pair: (-len(pair[1]), pair[0])):
        lines.append(
            f"・**{discord.utils.escape_markdown(name)}**: {len(values)}件 "
            f"({min(values) * 100:.1f}〜{max(values) * 100:.1f}%)"
        )
    safe_add_field(embed, 
        name="候補別内訳",
        value="\n".join(lines[:20]) or "候補なし",
        inline=False,
    )
    if len(lines) > 20:
        embed.set_footer(text=f"ほか {len(lines) - 20}人物。OpenAI APIは使用しません。")
    else:
        embed.set_footer(text="OpenAI APIは使用しません。")
    return embed


class FastFaceReviewView(discord.ui.View):
    def __init__(
        self,
        items: list[dict[str, Any]],
        *,
        owner_id: int,
        min_confidence: float,
    ) -> None:
        super().__init__(timeout=300)
        self.items = items
        self.owner_id = int(owner_id)
        self.min_confidence = float(min_confidence)
        self.finished = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("この一括確認は開始した本人だけが操作できます。", ephemeral=True)
            return False
        if self.finished:
            await interaction.response.send_message("この一括確認はすでに終了しています。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="表示中をすべて確定", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from photo_database import complete_face_reviews_bulk

        await interaction.response.defer()
        completed = await asyncio.to_thread(
            complete_face_reviews_bulk,
            self.items,
            _reviewer(interaction.user),
            f"高信頼度一括確定 threshold={self.min_confidence:.4f}",
        )
        self.finished = True
        self.stop()
        embed = discord.Embed(title="✅ 高信頼度の顔候補を一括確定しました", color=discord.Color.green())
        safe_add_field(embed, name="確定件数", value=f"{completed:,}件", inline=True)
        safe_add_field(embed, name="最低信頼度", value=f"{self.min_confidence * 100:.1f}%", inline=True)
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="キャンセル", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.finished = True
        self.stop()
        embed = discord.Embed(title="✖️ 一括確定をキャンセルしました", color=discord.Color.light_grey())
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_fast_face_review(
    ctx: commands.Context,
    limit: int = 20,
    min_confidence_percent: float = 95.0,
) -> int:
    """高信頼度の1位候補をまとめてプレビューし、ボタンで一括確定する。"""
    from photo_database import get_high_confidence_pending_face_reviews

    limit = max(1, min(int(limit), 100))
    percent = max(90.0, min(float(min_confidence_percent), 100.0))
    threshold = percent / 100.0
    items = await asyncio.to_thread(
        get_high_confidence_pending_face_reviews,
        limit,
        threshold,
    )
    if not items:
        await ctx.send(f"✅ 信頼度 **{percent:.1f}%以上** の顔確認待ちはありません。")
        return 0

    view = FastFaceReviewView(
        items,
        owner_id=ctx.author.id,
        min_confidence=threshold,
    )
    message = await ctx.send(embed=build_fast_review_embed(items, threshold), view=view)
    view.message = message
    return len(items)


def build_person_group_review_embed(
    items: list[dict[str, Any]],
    person_name: str,
    min_confidence: float,
) -> discord.Embed:
    """同じ人物が1位候補の顔をまとめて確認するプレビュー。"""
    confidences = [float(item.get("confidence") or 0) for item in items]
    embed = discord.Embed(
        title="👥 同じ人物候補をまとめて確認",
        description=(
            f"1位候補が **{discord.utils.escape_markdown(person_name)}** の顔だけを集めています。\n"
            "一括確定すると、表示中の顔すべてがこの人物の学習用参照に追加されます。"
        ),
        color=discord.Color.purple(),
    )
    safe_add_field(embed, name="人物", value=person_name, inline=True)
    safe_add_field(embed, name="対象件数", value=f"{len(items):,}件", inline=True)
    safe_add_field(embed, name="最低信頼度", value=f"{min_confidence * 100:.1f}%", inline=True)
    if confidences:
        safe_add_field(embed, 
            name="候補信頼度",
            value=(
                f"最低 {min(confidences) * 100:.1f}% / "
                f"平均 {sum(confidences) / len(confidences) * 100:.1f}% / "
                f"最高 {max(confidences) * 100:.1f}%"
            ),
            inline=False,
        )

    samples = []
    for item in items[:10]:
        samples.append(
            f"・顔ID **{int(item['face_id'])}** / "
            f"画像ID **{int(item['image_id'])}** / "
            f"{float(item.get('confidence') or 0) * 100:.1f}%"
        )
    safe_add_field(embed, 
        name="先頭10件",
        value="\n".join(samples) or "対象なし",
        inline=False,
    )
    embed.set_footer(text="安全のため自動確定は行いません。OpenAI APIも使用しません。")
    return embed


class PersonGroupFaceReviewView(discord.ui.View):
    def __init__(
        self,
        items: list[dict[str, Any]],
        person_name: str,
        *,
        owner_id: int,
        min_confidence: float,
    ) -> None:
        super().__init__(timeout=300)
        self.items = items
        self.person_name = person_name
        self.owner_id = int(owner_id)
        self.min_confidence = float(min_confidence)
        self.finished = False
        self.message: discord.Message | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この一括確認は開始した本人だけが操作できます。",
                ephemeral=True,
            )
            return False
        if self.finished:
            await interaction.response.send_message(
                "この一括確認はすでに終了しています。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="この人物で一括確定", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm_all(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from photo_database import complete_face_reviews_bulk

        await interaction.response.defer()
        completed = await asyncio.to_thread(
            complete_face_reviews_bulk,
            self.items,
            _reviewer(interaction.user),
            (
                f"人物別一括確定 person={self.person_name} "
                f"threshold={self.min_confidence:.4f}"
            ),
        )
        self.finished = True
        self.stop()
        embed = discord.Embed(
            title="✅ 同じ人物候補を一括確定しました",
            color=discord.Color.green(),
        )
        safe_add_field(embed, name="人物", value=self.person_name, inline=True)
        safe_add_field(embed, name="確定件数", value=f"{completed:,}件", inline=True)
        safe_add_field(embed, 
            name="最低信頼度",
            value=f"{self.min_confidence * 100:.1f}%",
            inline=True,
        )
        await interaction.edit_original_response(embed=embed, view=None)

    @discord.ui.button(label="キャンセル", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✖️ 人物別の一括確定をキャンセルしました",
                color=discord.Color.light_grey(),
            ),
            view=None,
        )

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_person_group_face_review(
    ctx: commands.Context,
    person_name: str,
    limit: int = 50,
    min_confidence_percent: float = 90.0,
) -> int:
    """指定人物が1位候補の確認待ち顔をまとめて確定する。"""
    from photo_database import get_person_pending_face_reviews, get_person_by_name

    person_name = _text(person_name)
    if not person_name:
        await ctx.send(
            "使い方: `!face_review_person 人物名 [件数] [最低信頼度]`\n"
            "例: `!face_review_person 井上和 50 95`"
        )
        return 0

    person = await asyncio.to_thread(get_person_by_name, person_name)
    if not person:
        await ctx.send(
            f"⚠️ 人物マスターに **{discord.utils.escape_markdown(person_name)}** は見つかりません。"
        )
        return 0

    limit = max(1, min(int(limit), 100))
    percent = max(80.0, min(float(min_confidence_percent), 100.0))
    threshold = percent / 100.0
    items = await asyncio.to_thread(
        get_person_pending_face_reviews,
        person_name,
        limit,
        threshold,
    )
    if not items:
        await ctx.send(
            f"✅ **{discord.utils.escape_markdown(person_name)}** が1位候補で、"
            f"信頼度 **{percent:.1f}%以上** の確認待ちはありません。"
        )
        return 0

    view = PersonGroupFaceReviewView(
        items,
        person_name,
        owner_id=ctx.author.id,
        min_confidence=threshold,
    )
    message = await ctx.send(
        embed=build_person_group_review_embed(items, person_name, threshold),
        view=view,
    )
    view.message = message
    return len(items)
