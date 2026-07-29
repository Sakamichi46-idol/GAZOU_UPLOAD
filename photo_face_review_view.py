from __future__ import annotations

import asyncio
import io
from typing import Any

import discord
from discord.ext import commands

from local_face_recognition import (
    FaceEngineUnavailable,
    get_face_crop_bytes,
    suggest_face_candidates,
)
from photo_database import (
    complete_face_review,
    get_face_candidates,
    get_pending_face_reviews,
    get_person_by_name,
    skip_face_review,
)


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


def build_face_review_embed(
    review: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> discord.Embed:
    embed = discord.Embed(
        title="👤 顔の人物確認",
        description="候補を選ぶか、別の人物名を入力して確定してください。候補はローカル処理による参考値です。",
        color=discord.Color.blue(),
    )
    embed.add_field(name="顔ID", value=str(review.get("face_id", "不明")), inline=True)
    embed.add_field(name="画像ID", value=str(review.get("image_id", "不明")), inline=True)
    embed.add_field(name="顔番号", value=str(review.get("face_index", "不明")), inline=True)
    embed.add_field(name="グループ", value=_text(review.get("group_name")) or "不明", inline=True)
    embed.add_field(name="ブログ投稿者", value=_text(review.get("member_name")) or "不明", inline=True)
    embed.add_field(name="投稿日", value=_short(review.get("published_at"), 100) or "不明", inline=True)
    embed.add_field(name="タイトル", value=_short(review.get("title")) or "タイトルなし", inline=False)

    if candidates:
        lines = [
            f"{index}. **{item.get('person_name', '不明')}** — {float(item.get('confidence') or 0) * 100:.1f}%"
            for index, item in enumerate(candidates, 1)
        ]
        candidate_text = "\n".join(lines)
    else:
        candidate_text = "一致閾値を超えた候補はありません。別人物入力または投稿者ボタンを使ってください。"
    embed.add_field(name="ローカル顔候補", value=candidate_text, inline=False)

    if review.get("blog_url"):
        embed.add_field(name="ブログ", value=f"[元のブログを開く]({review['blog_url']})", inline=False)
    embed.set_image(url="attachment://face_review.jpg")
    embed.set_footer(text="OpenAI APIは使用しません。最終確定は必ず画像を見て行ってください。")
    return embed


def build_completed_embed(
    review: dict[str, Any],
    person_name: str,
    user: discord.abc.User,
) -> discord.Embed:
    embed = discord.Embed(title="✅ 顔レビュー完了", color=discord.Color.green())
    embed.add_field(name="顔ID", value=str(review.get("face_id", "不明")), inline=True)
    embed.add_field(name="画像ID", value=str(review.get("image_id", "不明")), inline=True)
    embed.add_field(name="確定人物", value=person_name, inline=False)
    embed.add_field(name="確認者", value=_text(getattr(user, "display_name", user.name)), inline=False)
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

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = _text(self.person_name.value)
        person = await asyncio.to_thread(get_person_by_name, name)
        if not person:
            await interaction.response.send_message(
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
                    description=f"ローカル類似度 {confidence * 100:.1f}%",
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


class FaceReviewView(discord.ui.View):
    def __init__(
        self,
        review: dict[str, Any],
        candidates: list[dict[str, Any]],
        *,
        owner_id: int,
    ) -> None:
        super().__init__(timeout=600)
        self.review = review
        self.owner_id = int(owner_id)
        self.message: discord.Message | None = None
        self.finished = False
        if candidates:
            self.add_item(CandidateSelect(self, candidates))

        member_name = _text(review.get("member_name"))
        self.author_button.disabled = not bool(member_name)
        if member_name:
            self.author_button.label = f"投稿者: {_short(member_name, 60)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("このレビューは開始した本人だけが操作できます。", ephemeral=True)
            return False
        if self.finished:
            await interaction.response.send_message("この顔レビューはすでに完了しています。", ephemeral=True)
            return False
        return True

    async def complete(
        self,
        interaction: discord.Interaction,
        person_id: int,
        person_name: str,
        note: str,
    ) -> None:
        await asyncio.to_thread(
            complete_face_review,
            int(self.review["face_id"]),
            int(person_id),
            _reviewer(interaction.user),
            note,
        )
        self.finished = True
        self.stop()
        embed = build_completed_embed(self.review, person_name, interaction.user)
        await interaction.response.edit_message(embed=embed, view=None)

    @discord.ui.button(label="投稿者で確定", emoji="📝", style=discord.ButtonStyle.primary, row=1)
    async def author_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        member_name = _text(self.review.get("member_name"))
        person = await asyncio.to_thread(get_person_by_name, member_name)
        if not person:
            await interaction.response.send_message(
                f"⚠️ 投稿者 **{discord.utils.escape_markdown(member_name)}** が人物マスターに見つかりません。",
                ephemeral=True,
            )
            return
        await self.complete(interaction, int(person["id"]), _text(person["person_name"]), "ブログ投稿者ボタン")

    @discord.ui.button(label="別人物を入力", emoji="⌨️", style=discord.ButtonStyle.secondary, row=1)
    async def other_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(FacePersonModal(self))

    @discord.ui.button(label="今回は保留", emoji="⏭️", style=discord.ButtonStyle.secondary, row=1)
    async def skip_button(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await asyncio.to_thread(
            skip_face_review,
            int(self.review["face_id"]),
            _reviewer(interaction.user),
            "Discord顔レビューで保留",
        )
        self.finished = True
        self.stop()
        embed = discord.Embed(title="⏭️ 顔レビューを保留しました", color=discord.Color.orange())
        embed.add_field(name="顔ID", value=str(self.review.get("face_id", "不明")), inline=True)
        embed.add_field(name="画像ID", value=str(self.review.get("image_id", "不明")), inline=True)
        await interaction.response.edit_message(embed=embed, view=None)

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True
        if self.message is not None:
            try:
                await self.message.edit(view=self)
            except discord.HTTPException:
                pass


async def send_face_review_batch(ctx: commands.Context, limit: int = 1) -> int:
    """確認待ちの顔を、切り出し画像と操作UI付きで送る。"""
    limit = max(1, min(int(limit), 5))
    reviews = await asyncio.to_thread(get_pending_face_reviews, limit)
    if not reviews:
        await ctx.send("✅ 顔の確認待ちはありません。")
        return 0

    sent = 0
    for review in reviews:
        candidates = await _load_candidates(review)
        try:
            data, _ = await asyncio.to_thread(get_face_crop_bytes, int(review["face_id"]))
        except Exception as error:
            await ctx.send(
                f"❌ 顔ID **{review['face_id']}** の確認画像を作成できませんでした: "
                f"`{type(error).__name__}: {error}`"
            )
            continue

        view = FaceReviewView(review, candidates, owner_id=ctx.author.id)
        file = discord.File(io.BytesIO(data), filename="face_review.jpg")
        message = await ctx.send(
            embed=build_face_review_embed(review, candidates),
            view=view,
            file=file,
        )
        view.message = message
        sent += 1
    return sent
