from __future__ import annotations

import asyncio
import os
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import discord
from discord.ext import commands
from embed_safety import safe_add_field

from person_labels import (
    count_people_for_users,
    format_people_for_users,
    make_unknown_other_label,
    normalize_people_for_storage,
    people_items_for_users,
    unknown_other_count,
)
from photo_database import (
    get_connection,
    get_pending_person_reviews,
    get_frequent_confirmed_people,
    get_author_cooccurrence_people,
    get_skipped_person_reviews,
    save_person,
    set_confirmed_blog_people,
    set_confirmed_image_people,
    utc_now_text,
)
from sakamichi_members import SAKAMICHI_MEMBERS, iter_members, member_group_generation_sort_key
from advanced_admin_features import (
    HOLD_REASON_LABELS, create_people_snapshot, save_hold_reason, save_provisional_people, load_person_sets, count_person_sets,
)
from operation_locks import resource_lock

REVIEW_EMBED_COLOR = discord.Color.blue()
SUCCESS_EMBED_COLOR = discord.Color.green()
SKIP_EMBED_COLOR = discord.Color.orange()
MAX_CANDIDATE_DISPLAY = 10
SELECT_PAGE_SIZE = 25
SUCCESS_NOTICE_SECONDS = 4.0

_SAKAMICHI_MEMBER_NAMES = {name for _group, _generation, name in iter_members()}


def normalize_text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def truncate_text(value: Any, max_length: int) -> str:
    text = normalize_text(value)
    return text if len(text) <= max_length else text[: max_length - 1] + "…"


def split_person_names(value: Any) -> list[str]:
    text = normalize_text(value).replace("\n", "、").replace(",", "、").replace("，", "、")
    result: list[str] = []
    for item in text.split("、"):
        name = item.strip()
        if name and name not in result:
            result.append(name)
    return result


def get_reviewer_name(user: discord.abc.User) -> str:
    name = normalize_text(getattr(user, "display_name", "")) or normalize_text(getattr(user, "name", ""))
    return f"{name} ({user.id})".strip()


def build_people_confirmation_text(image_id: int, names: list[str]) -> str:
    """人物確定後に、人数と確定名を漏れなく表示する。"""
    normalized = normalize_people_for_storage(names)
    if not normalized:
        return f"✅ 写真ID **{image_id}** を人物なしで確定しました。"

    people_count = count_people_for_users(normalized)
    display_items = people_items_for_users(normalized)
    escaped_items = [discord.utils.escape_markdown(item) for item in display_items]
    people_lines = "\n".join(f"・{item}" for item in escaped_items)
    return (
        f"✅ 写真ID **{image_id}** に **{people_count}人**を設定しました。\n\n"
        f"👤 **設定した人物**\n{people_lines}"
    )


def get_quick_people_for_review(review: dict[str, Any]) -> list[dict[str, Any]]:
    """投稿者を先頭に、投稿者との共写回数順で人物候補を返す。

    キャッシュは使わず、人物確認画面を作るたびSQLiteから再集計する。
    そのため確認済み写真が増えると次に開く画面の順位も自動で変化する。
    共写回数が同じ場合はプロジェクト共通の人物順で安定させる。
    """
    author = normalize_text(review.get("member_name"))
    if not author:
        return []

    people = get_author_cooccurrence_people(author)
    by_name: dict[str, dict[str, Any]] = {}
    for item in people:
        name = normalize_text(item.get("person_name"))
        if not name:
            continue
        copied = dict(item)
        copied["cooccurrence_count"] = int(copied.get("cooccurrence_count") or 0)
        copied["is_author"] = name == author
        by_name[name] = copied

    # 投稿者が人物マスターにまだ無い場合でも、必ず1番目に表示する。
    if author not in by_name:
        by_name[author] = {
            "person_name": author,
            "group_name": normalize_text(review.get("group_name")),
            "generation_name": "",
            "cooccurrence_count": 0,
            "is_author": True,
        }

    result = list(by_name.values())
    result.sort(
        key=lambda item: (
            0 if normalize_text(item.get("person_name")) == author else 1,
            -int(item.get("cooccurrence_count") or 0),
            member_group_generation_sort_key(normalize_text(item.get("person_name"))),
        )
    )
    return result


def build_candidate_names(review: dict[str, Any]) -> list[str]:
    """投稿者を最優先にし、その後へAI候補を重複なしで並べる。"""
    result: list[str] = []
    for key in ("member_name", "candidate_people", "ai_person_name", "candidates"):
        for name in split_person_names(review.get(key, "")):
            if name and name not in result:
                result.append(name)
    return result


def seed_member_master() -> None:
    for group_name, generation_name, person_name in iter_members():
        save_person(person_name, group_name, generation_name, True)


def mark_person_review_skipped(image_id: int, reviewed_by: str = "", note: str = "") -> None:
    now = utc_now_text()
    with closing(get_connection()) as connection:
        connection.execute(
            """UPDATE photo_review_queue
               SET status='skipped', reviewed_by=?, selected_value='', review_note=?,
                   reviewed_at=?, updated_at=? WHERE image_id=?""",
            (reviewed_by, note, now, now, image_id),
        )
        connection.commit()


def build_review_embed(review: dict[str, Any], quick_people: list[dict[str, Any]] | None = None) -> discord.Embed:
    candidates = build_candidate_names(review)
    confirmed = split_person_names(review.get("confirmed_people", ""))
    is_skipped = normalize_text(review.get("review_status")) == "skipped"
    if confirmed:
        title = "✏️ 登録人物の確認・編集"
        description = (
            "この写真には人物が登録済みです。現在の内容を確認し、"
            "「登録人物を編集」または「名前・不明人数を再入力」から修正できます。"
        )
    elif is_skipped:
        title = "⏭️ スキップ済み写真の再確認"
        description = "以前スキップした写真です。今回、人物情報を確定してください。"
    else:
        title = "🖼️ 写真に写る人物を確認"
        description = "写真に写っている人物を確認してください。複数人の選択にも対応しています。"
    embed = discord.Embed(
        title=title,
        description=description,
        color=SUCCESS_EMBED_COLOR if confirmed else (SKIP_EMBED_COLOR if is_skipped else REVIEW_EMBED_COLOR),
    )
    safe_add_field(embed, name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    image_index = int(review.get("image_index") or 0)
    total_blog_images = int(review.get("total_blog_images") or 0)
    image_position = f"{image_index} / {total_blog_images}" if total_blog_images else str(image_index or "不明")
    safe_add_field(embed, name="ブログ内の画像", value=image_position, inline=True)
    safe_add_field(embed, name="グループ", value=normalize_text(review.get("group_name")) or "不明", inline=True)
    safe_add_field(embed, name="ブログ投稿者（最優先候補）", value=normalize_text(review.get("member_name")) or "不明", inline=True)
    safe_add_field(embed, name="タイトル", value=truncate_text(review.get("title"), 1000) or "タイトルなし", inline=False)
    if is_skipped and normalize_text(review.get("review_note")):
        safe_add_field(embed, 
            name="前回のスキップメモ",
            value=truncate_text(review.get("review_note"), 1000),
            inline=False,
        )
    if review.get("published_at"):
        safe_add_field(embed, name="投稿日", value=truncate_text(review.get("published_at"), 1000), inline=False)
    candidate_text = "\n".join(f"{i}. {name}" for i, name in enumerate(candidates[:MAX_CANDIDATE_DISPLAY], 1))
    safe_add_field(embed, name="🤖 人物候補", value=candidate_text or "候補はありません。", inline=False)
    if quick_people:
        quick_text = " / ".join(
            (f"{item.get('person_name', '')}（投稿者）" if item.get('is_author') else f"{item.get('person_name', '')}（共写{int(item.get('cooccurrence_count') or 0)}回）")
            for item in quick_people[:8]
            if normalize_text(item.get("person_name"))
        )
        if quick_text:
            safe_add_field(embed, name="⚡ よく使う人物（投稿者＋共写回数順）", value=truncate_text(quick_text, 1000), inline=False)
    safe_add_field(embed, name="現在の確定人物", value=format_people_for_users("、".join(confirmed)) or "未確定", inline=False)
    if review.get("blog_url"):
        safe_add_field(embed, name="ブログ", value=f"[元のブログを開く]({review['blog_url']})", inline=False)
    embed.set_footer(
        text=(
            "人物の新規設定・再設定・スキップ再確認のすべてで、"
            "名前不明人数を追加・減少できます。"
        )
    )
    return embed


def build_completed_embed(review: dict[str, Any], names: list[str], reviewer: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="✅ 写真の人物確定完了", description="写真に写っている人物を確定しました。", color=SUCCESS_EMBED_COLOR)
    safe_add_field(embed, name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    safe_add_field(embed, name="確定人物", value=format_people_for_users("、".join(names)) if names else "人物なし", inline=False)
    safe_add_field(embed, name="確認者", value=discord.utils.escape_markdown(normalize_text(getattr(reviewer, "display_name", reviewer.name))), inline=False)
    return embed


def build_skipped_embed(review: dict[str, Any], reviewer: discord.abc.User) -> discord.Embed:
    embed = discord.Embed(title="⏭️ 写真人物確認をスキップしました", color=SKIP_EMBED_COLOR)
    safe_add_field(embed, name="画像ID", value=str(review.get("image_id", 0)), inline=True)
    safe_add_field(embed, name="操作した人", value=normalize_text(getattr(reviewer, "display_name", reviewer.name)), inline=False)
    return embed


async def _delete_message_safely(
    message: discord.Message | None,
    interaction: discord.Interaction | None = None,
) -> None:
    """エフェメラルを含むDiscordメッセージを可能な経路で確実に削除する。

    古いInteractionから作られたWebhookMessageでは ``message.delete()`` が
    失敗することがあるため、現在のInteractionトークンから
    ``followup.delete_message(message.id)`` も試す。
    """
    if message is None:
        return

    try:
        await message.delete()
        return
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass

    if interaction is None:
        return

    message_id = int(getattr(message, "id", 0) or 0)
    if not message_id:
        return
    try:
        await interaction.followup.delete_message(message_id)
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass


async def _delete_unique_messages(
    *messages: discord.Message | None,
    interaction: discord.Interaction | None = None,
) -> None:
    """同じDiscordメッセージを二重削除せず、安全にまとめて削除する。"""
    seen: set[int] = set()
    for message in messages:
        if message is None:
            continue
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id and message_id in seen:
            continue
        if message_id:
            seen.add(message_id)
        await _delete_message_safely(message, interaction)


async def _delete_message_later(
    message: discord.Message | None,
    delay: float = SUCCESS_NOTICE_SECONDS,
) -> None:
    await asyncio.sleep(max(0.0, delay))
    await _delete_message_safely(message)


async def _delete_original_response_later(
    interaction: discord.Interaction,
    delay: float = SUCCESS_NOTICE_SECONDS,
) -> None:
    await asyncio.sleep(max(0.0, delay))
    try:
        await interaction.delete_original_response()
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass


async def _finish_selection_message(
    interaction: discord.Interaction,
    source_message: discord.Message | None,
    selection_message: discord.Message | None,
    text: str,
) -> None:
    """階層式人物選択の確定後、関係するエフェメラルをすべて終了する。

    ``delete_original_response()`` だけでは followup で作った人物選択画面が
    残るケースがあるため、元カード・保存済み選択メッセージ・現在の
    ``interaction.message`` を明示的に削除する。
    """
    if not interaction.response.is_done():
        await interaction.response.defer()

    # 現在押したコンポーネントが載っているメッセージは、
    # Interaction自身の削除APIが最も確実。最初にこれを試す。
    try:
        await interaction.delete_original_response()
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass

    await _delete_unique_messages(
        source_message,
        selection_message,
        interaction.message,
        interaction=interaction,
    )

    try:
        notice = await interaction.followup.send(text, ephemeral=True, wait=True)
        asyncio.create_task(_delete_message_later(notice))
    except discord.HTTPException:
        pass


async def _finish_review_message(
    interaction: discord.Interaction,
    source_message: discord.Message | None,
    text: str,
) -> None:
    """人物確認の成功後、元カードと現在操作中のUIを確実に消す。

    コンポーネントinteractionがfollowupエフェメラル上で発生した場合、
    ``delete_original_response()`` はその表示中メッセージを必ずしも指さない。
    そのため ``interaction.message`` を明示的に削除し、元の人物確認カードも
    別途削除する。Modal経路ではoriginal response削除をフォールバックに使う。
    """
    if not interaction.response.is_done():
        await interaction.response.defer()

    try:
        await interaction.delete_original_response()
    except (discord.HTTPException, discord.NotFound, discord.Forbidden):
        pass

    await _delete_unique_messages(
        source_message,
        interaction.message,
        interaction=interaction,
    )

    try:
        notice = await interaction.followup.send(
            text,
            ephemeral=True,
            wait=True,
        )
        asyncio.create_task(_delete_message_later(notice))
    except discord.HTTPException:
        pass


class ReviewSession:
    def __init__(
        self,
        destination: commands.Context | discord.Interaction | discord.abc.Messageable,
        *,
        owner_id: int,
        batch_size: int = 5,
        queue_status: str = "pending",
        group_name: str = "",
        continuous: bool = False,
        require_final_confirmation: bool = False,
        fixed_blog_id: int | None = None,
    ):
        self.destination = destination
        self.owner_id = owner_id
        self.batch_size = max(1, min(int(batch_size), 10))
        self.queue_status = "skipped" if queue_status == "skipped" else "pending"
        self.group_name = normalize_text(group_name)
        self.continuous = bool(continuous)
        self.require_final_confirmation = bool(require_final_confirmation)
        self.current_blog_id: int | None = int(fixed_blog_id) if fixed_blog_id is not None else None
        self.fixed_blog_id: int | None = int(fixed_blog_id) if fixed_blog_id is not None else None
        self.active_image_ids: set[int] = set()
        self.completed_image_ids: set[int] = set()
        self.message_by_image_id: dict[int, discord.Message] = {}
        self.lock = asyncio.Lock()
        self.stopped = False

    @property
    def queue_label(self) -> str:
        status_label = "スキップ済み" if self.queue_status == "skipped" else "人物確認待ち"
        if self.group_name:
            return f"{self.group_name}の{status_label}"
        return status_label

    def get_reviews(self, limit: int, blog_id: int | None = None) -> list[dict[str, Any]]:
        if self.queue_status == "skipped":
            return get_skipped_person_reviews(limit, self.group_name, blog_id)
        return get_pending_person_reviews(limit, self.group_name, blog_id)

    async def send_message(self, *args: Any, **kwargs: Any) -> discord.Message | None:
        destination = self.destination
        if isinstance(destination, discord.Interaction):
            kwargs.setdefault("ephemeral", True)
            if destination.response.is_done():
                return await destination.followup.send(*args, wait=True, **kwargs)
            await destination.response.send_message(*args, **kwargs)
            return await destination.original_response()
        return await destination.send(*args, **kwargs)

    async def start_batch(self) -> int:
        if self.stopped:
            return 0

        # まず現在のブログを続け、残りがなければ次のブログへ移る。
        reviews: list[dict[str, Any]] = []
        if self.current_blog_id is not None:
            reviews = await asyncio.to_thread(
                self.get_reviews,
                self.batch_size,
                self.current_blog_id,
            )

        if not reviews and self.fixed_blog_id is not None:
            await self.send_message("✅ このブログには人物確認待ちの写真がありません。")
            self.stopped = True
            return 0

        if not reviews:
            first = await asyncio.to_thread(self.get_reviews, 1, None)
            if not first:
                await self.send_message(f"✅ {self.queue_label}の写真はありません。")
                self.stopped = True
                return 0
            self.current_blog_id = int(first[0]["blog_id"])
            reviews = await asyncio.to_thread(
                self.get_reviews,
                self.batch_size,
                self.current_blog_id,
            )

        self.active_image_ids = {int(review["image_id"]) for review in reviews}
        self.completed_image_ids.clear()
        self.message_by_image_id.clear()
        for review in reviews:
            message = await send_person_review(self.destination, review, session=self)
            if message is not None:
                self.message_by_image_id[int(review["image_id"])] = message
        return len(reviews)

    async def mark_done(
        self,
        image_id: int,
        destination: discord.Interaction | None = None,
    ) -> None:
        # 次の写真は直近の操作Interactionから送信する。
        # 古いInteractionトークンの有効期限に依存せず、長時間の確認作業を続けられる。
        if destination is not None:
            self.destination = destination
        async with self.lock:
            if self.stopped or image_id not in self.active_image_ids:
                return
            self.completed_image_ids.add(image_id)
            if self.completed_image_ids != self.active_image_ids:
                return
            finished_count = len(self.active_image_ids)
            self.active_image_ids.clear()
            self.completed_image_ids.clear()
            self.message_by_image_id.clear()

        same_blog_remaining = []
        if self.current_blog_id is not None:
            same_blog_remaining = await asyncio.to_thread(
                self.get_reviews,
                1,
                self.current_blog_id,
            )
        if not same_blog_remaining and self.fixed_blog_id is None:
            self.current_blog_id = None

        if self.continuous:
            await self.start_batch()
            return

        remaining = await asyncio.to_thread(self.get_reviews, 1)
        if not remaining:
            await self.send_message(
                f"🎉 {finished_count}件のレビューが完了し、{self.queue_label}は0件になりました。"
            )
            self.stopped = True
            return

        await self.send_message(
            f"✅ {finished_count}件のレビューが完了しました。\n続けて確認しますか？",
            view=ReviewContinueView(self),
        )


    async def complete_current_blog(
        self,
        interaction: discord.Interaction,
        names: list[str],
    ) -> int:
        if self.current_blog_id is None:
            await interaction.response.send_message("対象ブログを特定できませんでした。", ephemeral=True)
            return 0

        reviewer = get_reviewer_name(interaction.user)
        await interaction.response.defer(ephemeral=True)
        try:
            count = await asyncio.to_thread(
                _commit_blog_people_with_snapshots,
                self.current_blog_id,
                names,
                interaction.user.id,
                reviewer,
                self.queue_status,
            )
        except Exception as exc:
            try:
                await interaction.followup.send(
                    f"⚠️ ブログ一括確定に失敗しました: {exc}", ephemeral=True
                )
            except discord.HTTPException:
                pass
            return 0

        # 確定済みレビューは残さず消す。大量確定後に古いエフェメラルが
        # 画面へ残り続けるのを防ぐ。
        for message in list(self.message_by_image_id.values()):
            await _delete_message_safely(message)

        self.active_image_ids.clear()
        self.completed_image_ids.clear()
        self.message_by_image_id.clear()
        self.current_blog_id = None

        try:
            await interaction.delete_original_response()
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            pass
        try:
            notice = await interaction.followup.send(
                f"✅ このブログの **{count}件** を **{format_people_for_users('、'.join(names))}** で一括確定しました。",
                ephemeral=True,
                wait=True,
            )
            asyncio.create_task(_delete_message_later(notice))
        except discord.HTTPException:
            pass

        if self.continuous:
            await self.start_batch()
        else:
            remaining = await asyncio.to_thread(self.get_reviews, 1, None)
            if remaining:
                await self.send_message("続けて確認しますか？", view=ReviewContinueView(self))
            else:
                await self.send_message(f"🎉 {self.queue_label}は0件になりました。")
                self.stopped = True
        return count


class ReviewContinueView(discord.ui.View):
    def __init__(self, session: ReviewSession):
        super().__init__(timeout=None)
        self.session = session

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "このレビュー操作はコマンドを実行した本人だけが使えます。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="次のセット", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_batch(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="🔄 次のレビューを読み込んでいます…", view=None)
        await self.session.start_batch()
        self.stop()

    @discord.ui.button(label="連続レビュー", emoji="🔁", style=discord.ButtonStyle.success)
    async def continuous_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.session.continuous = True
        await interaction.response.edit_message(
            content="🔁 連続レビューを開始しました。各セットが終わると自動で次へ進みます。",
            view=None,
        )
        await self.session.start_batch()
        self.stop()

    @discord.ui.button(label="終了", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def stop_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.session.stopped = True
        await interaction.response.edit_message(content="⏹️ レビューを終了しました。", view=None)
        self.stop()


class PersonInputModal(discord.ui.Modal, title="人物名・名前不明人数を入力"):
    person_names = discord.ui.TextInput(
        label="名前が分かる人物",
        placeholder="複数人は「、」で区切ります。名前不明だけなら空欄でOKです。",
        style=discord.TextStyle.paragraph,
        required=False,
        max_length=500,
    )
    unknown_count = discord.ui.TextInput(
        label="名前不明のその他人物の人数",
        placeholder="例: 2（いなければ0または空欄）",
        required=False,
        max_length=3,
    )
    note = discord.ui.TextInput(label="メモ", required=False, max_length=500)

    def __init__(self, parent: "PersonReviewView"):
        super().__init__(timeout=300)
        self.parent_view = parent
        # 再編集時は現在の登録内容を初期値として表示し、差分だけ直せるようにする。
        current_raw = split_person_names(parent.review.get("confirmed_people", ""))
        current_unknown = sum(unknown_other_count(name) for name in current_raw)
        current_names = [name for name in current_raw if not unknown_other_count(name)]
        if current_names:
            self.person_names.default = "、".join(current_names)[:500]
        if current_unknown:
            self.unknown_count.default = str(current_unknown)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = normalize_people_for_storage(split_person_names(self.person_names.value))
        raw_unknown = normalize_text(self.unknown_count.value)
        try:
            unknown_count = int(raw_unknown or "0")
        except ValueError:
            await interaction.response.send_message(
                "名前不明人数は0以上の整数で入力してください。",
                ephemeral=True,
            )
            return
        if unknown_count < 0 or unknown_count > 999:
            await interaction.response.send_message(
                "名前不明人数は0〜999人で入力してください。",
                ephemeral=True,
            )
            return
        if unknown_count:
            names.append(make_unknown_other_label(unknown_count))
            names = normalize_people_for_storage(names)
        if not names:
            await interaction.response.send_message(
                "人物名または名前不明人数を入力してください。人物が写っていない場合は「人物なし」を使ってください。",
                ephemeral=True,
            )
            return
        for name in names:
            if name not in _SAKAMICHI_MEMBER_NAMES and not unknown_other_count(name):
                await asyncio.to_thread(save_person, name, "その他", "その他", False)
        await self.parent_view.complete_review(
            interaction,
            names,
            note=normalize_text(self.note.value) or "人物名・名前不明人数を手入力",
        )


@dataclass
class SelectionState:
    review: dict[str, Any]
    owner_id: int
    source_message: discord.Message
    selection_message: discord.Message | None = None
    session: ReviewSession | None = None
    selected_names: list[str] = field(default_factory=list)
    group_name: str = ""
    generation_name: str = ""
    member_page: int = 0
    remove_page: int = 0
    unknown_other_people: int = 0
    base_person_set_name: str = ""
    commit_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    committed: bool = False

    def add_names(self, names: list[str]) -> None:
        for name in names:
            if name not in self.selected_names:
                self.selected_names.append(name)


class OwnedView(discord.ui.View):
    def __init__(self, state: SelectionState, timeout: float | None = None):
        super().__init__(timeout=timeout)
        self.state = state

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message("この選択画面はコマンドを実行した本人だけが操作できます。", ephemeral=True)
            return False
        return True


def selection_text(state: SelectionState) -> str:
    display_names = list(state.selected_names)
    if state.unknown_other_people:
        display_names.append(f"その他（名前不明）{state.unknown_other_people}人")
    selected = "、".join(display_names) if display_names else "まだ選択されていません。"
    selected = truncate_text(selected, 1700)
    path = " → ".join(v for v in (state.group_name, state.generation_name) if v) or "グループを選んでください。"
    total = len(state.selected_names) + state.unknown_other_people
    base_set = (
        f"**人物セット:** {discord.utils.escape_markdown(state.base_person_set_name)}\n"
        if state.base_person_set_name
        else ""
    )
    return (
        f"{base_set}"
        f"**選択場所:** {path}\n"
        f"**選択中（合計{total}人）:** {selected}"
    )


class OtherPersonInputModal(discord.ui.Modal, title="その他の人物を追加"):
    person_names = discord.ui.TextInput(
        label="人物名",
        placeholder="複数人は「、」で区切ってください。",
        style=discord.TextStyle.paragraph,
        max_length=500,
    )

    def __init__(self, state: SelectionState):
        super().__init__(timeout=300)
        self.state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        names = [n for n in split_person_names(self.person_names.value) if n]
        if not names:
            await interaction.response.send_message("人物名を入力してください。", ephemeral=True)
            return
        self.state.add_names(names)
        for name in names:
            if name not in _SAKAMICHI_MEMBER_NAMES:
                await asyncio.to_thread(save_person, name, "その他", "その他", False)
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))


class GroupSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        super().__init__(placeholder="グループを選択", options=[discord.SelectOption(label=g, value=g) for g in SAKAMICHI_MEMBERS])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.group_name = self.values[0]
        self.state.generation_name = ""
        self.state.member_page = 0
        view = GenerationView(self.state)
        await interaction.response.edit_message(content=selection_text(self.state), view=view)


class GroupView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        self.add_item(GroupSelect(state))

    @discord.ui.button(label="候補を追加", emoji="🤖", style=discord.ButtonStyle.secondary, row=1)
    async def add_candidates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.add_names(build_candidate_names(self.state.review))
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))

    @discord.ui.button(label="その他の人物を追加", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
    async def other_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(OtherPersonInputModal(self.state))

    @discord.ui.button(label="名前不明を1人追加", emoji="❓", style=discord.ButtonStyle.secondary, row=2)
    async def add_unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.unknown_other_people += 1
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="名前不明を1人減らす", emoji="➖", style=discord.ButtonStyle.secondary, row=2)
    async def remove_unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.unknown_other_people = max(0, self.state.unknown_other_people - 1)
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))


class GenerationSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        generations = SAKAMICHI_MEMBERS[state.group_name]
        super().__init__(placeholder="期生を選択", options=[discord.SelectOption(label=g, value=g) for g in generations])
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.generation_name = self.values[0]
        self.state.member_page = 0
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))


class GenerationView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        self.add_item(GenerationSelect(state))

    @discord.ui.button(label="グループへ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=1)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class MemberSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        all_names = SAKAMICHI_MEMBERS[state.group_name][state.generation_name]
        page_count = max(1, (len(all_names) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        state.member_page = max(0, min(state.member_page, page_count - 1))
        start = state.member_page * SELECT_PAGE_SIZE
        names = all_names[start:start + SELECT_PAGE_SIZE]
        options = [
            discord.SelectOption(label=n, value=n, default=n in state.selected_names)
            for n in names
        ]
        super().__init__(
            placeholder=f"写っているメンバーを選択（{state.member_page + 1}/{page_count}ページ）",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.add_names(list(self.values))
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))


class MemberView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        all_names = SAKAMICHI_MEMBERS[state.group_name][state.generation_name]
        page_count = max(1, (len(all_names) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        state.member_page = max(0, min(state.member_page, page_count - 1))
        self.add_item(MemberSelect(state))
        self.previous_page.disabled = state.member_page <= 0
        self.next_page.disabled = state.member_page >= page_count - 1

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.member_page = max(0, self.state.member_page - 1)
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.member_page += 1
        await interaction.response.edit_message(content=selection_text(self.state), view=MemberView(self.state))

    @discord.ui.button(label="別の期生から追加", emoji="➕", style=discord.ButtonStyle.secondary, row=2)
    async def generation(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.generation_name = ""
        self.state.member_page = 0
        await interaction.response.edit_message(content=selection_text(self.state), view=GenerationView(self.state))

    @discord.ui.button(label="別グループから追加", emoji="🌳", style=discord.ButtonStyle.secondary, row=2)
    async def group(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        self.state.member_page = 0
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="選択中を確認", emoji="📋", style=discord.ButtonStyle.primary, row=2)
    async def selected(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.remove_page = 0
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


class RemoveSelect(discord.ui.Select):
    def __init__(self, state: SelectionState):
        page_count = max(1, (len(state.selected_names) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        state.remove_page = max(0, min(state.remove_page, page_count - 1))
        start = state.remove_page * SELECT_PAGE_SIZE
        names = state.selected_names[start:start + SELECT_PAGE_SIZE]
        options = [discord.SelectOption(label=n, value=n) for n in names]
        super().__init__(
            placeholder=f"解除する人物を選択（{state.remove_page + 1}/{page_count}ページ）",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
        )
        self.state = state

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.selected_names = [n for n in self.state.selected_names if n not in self.values]
        page_count = max(1, (len(self.state.selected_names) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        self.state.remove_page = min(self.state.remove_page, page_count - 1)
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))


async def _commit_selection_state(
    interaction: discord.Interaction,
    state: SelectionState,
    names: list[str],
    *,
    note: str,
) -> bool:
    """階層式人物選択の全確定経路を直列化して完了する。

    同じエフェメラルでボタンを連打しても1回だけDB更新し、
    成功後は人物選択画面と元の人物確認カードを削除する。
    """
    image_id = int(state.review["image_id"])
    async with state.commit_lock:
        if state.committed:
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "✅ この写真はすでに確定済みです。", ephemeral=True
                )
            return False

        if not interaction.response.is_done():
            await interaction.response.defer()

        normalized = normalize_people_for_storage(names)
        try:
            await asyncio.to_thread(
                _commit_selected_people,
                image_id,
                normalized,
                interaction.user.id,
                get_reviewer_name(interaction.user),
                note,
            )
        except Exception as exc:
            if interaction.response.is_done():
                try:
                    await interaction.followup.send(
                        f"⚠️ 人物確定に失敗しました: {exc}", ephemeral=True
                    )
                except discord.HTTPException:
                    pass
            return False

        state.committed = True
        await _finish_selection_message(
            interaction,
            state.source_message,
            state.selection_message,
            build_people_confirmation_text(image_id, normalized),
        )
        if state.session:
            await state.session.mark_done(image_id, interaction)
        return True


class SelectedPeopleView(OwnedView):
    def __init__(self, state: SelectionState):
        super().__init__(state)
        page_count = max(1, (len(state.selected_names) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)
        state.remove_page = max(0, min(state.remove_page, page_count - 1))
        if state.selected_names:
            self.add_item(RemoveSelect(state))
        self.previous_page.disabled = not state.selected_names or state.remove_page <= 0
        self.next_page.disabled = not state.selected_names or state.remove_page >= page_count - 1
        self.confirm.disabled = not bool(state.selected_names or state.unknown_other_people)

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.remove_page = max(0, self.state.remove_page - 1)
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.remove_page += 1
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))

    @discord.ui.button(label="人物を追加", emoji="➕", style=discord.ButtonStyle.primary, row=1)
    async def add(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.group_name = ""
        self.state.generation_name = ""
        await interaction.response.edit_message(content=selection_text(self.state), view=GroupView(self.state))

    @discord.ui.button(label="この内容で確定", emoji="✅", style=discord.ButtonStyle.success, row=1)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        names = list(self.state.selected_names)
        if self.state.unknown_other_people:
            names.append(make_unknown_other_label(self.state.unknown_other_people))
        names = normalize_people_for_storage(names)
        if not names:
            await interaction.response.send_message(
                "確定する人物が選択されていません。人物が写っていない場合は「人物なし」を使ってください。",
                ephemeral=True,
            )
            return
        note = "階層式レビュー画面から複数人を確定"
        if self.state.base_person_set_name:
            note = f"人物セット「{self.state.base_person_set_name}」を部分修正して確定"

        await _commit_selection_state(
            interaction,
            self.state,
            names,
            note=note,
        )

    @discord.ui.button(label="名前不明を1人追加", emoji="❓", style=discord.ButtonStyle.secondary, row=2)
    async def add_unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.unknown_other_people += 1
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))

    @discord.ui.button(label="名前不明を1人減らす", emoji="➖", style=discord.ButtonStyle.secondary, row=2)
    async def remove_unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.unknown_other_people = max(0, self.state.unknown_other_people - 1)
        await interaction.response.edit_message(content=selection_text(self.state), view=SelectedPeopleView(self.state))

    @discord.ui.button(label="人物なし", emoji="🚫", style=discord.ButtonStyle.danger, row=3)
    async def nobody(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _commit_selection_state(
            interaction,
            self.state,
            [],
            note="人物なし",
        )

    @discord.ui.button(label="その他（名前不明）1人", emoji="❓", style=discord.ButtonStyle.secondary, row=3)
    async def unknown(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await _commit_selection_state(
            interaction,
            self.state,
            [make_unknown_other_label(1)],
            note="その他の人物（名前不明）1人",
        )


def _commit_selected_people(
    image_id: int,
    names: list[str],
    admin_user_id: int,
    reviewer: str,
    note: str,
) -> None:
    """階層式人物選択からの確定処理を1か所に集約する。

    スナップショット作成と人物確定の呼び出し順を全経路で揃え、
    「通常確定だけUndoできるが人物なしは戻せない」といった差を防ぐ。
    """
    with resource_lock("image_people_confirm", int(image_id), int(admin_user_id), ttl_seconds=120):
        create_people_snapshot(int(image_id), int(admin_user_id), "person_review_confirm")
        set_confirmed_image_people(
            int(image_id),
            list(names),
            confirmed_by=reviewer,
            note=note,
        )


def _commit_blog_people_with_snapshots(
    blog_id: int,
    names: list[str],
    admin_user_id: int,
    reviewer: str,
    queue_status: str,
) -> int:
    """ブログ単位確定も排他・Undo可能な同じ規則で処理する。"""
    status = "skipped" if queue_status == "skipped" else "pending"
    with resource_lock("blog_people_confirm", int(blog_id), int(admin_user_id), ttl_seconds=180):
        with closing(get_connection()) as con:
            rows = con.execute(
                """
                SELECT q.image_id
                  FROM photo_review_queue q
                  JOIN photo_images i ON i.id=q.image_id
                 WHERE i.blog_id=?
                   AND q.review_type='person_identity'
                   AND q.status=?
                 ORDER BY i.image_index, i.id
                """,
                (int(blog_id), status),
            ).fetchall()
        image_ids = [int(row[0]) for row in rows]
        for image_id in image_ids:
            create_people_snapshot(image_id, int(admin_user_id), "blog_person_review_confirm")
        return set_confirmed_blog_people(
            int(blog_id),
            list(names),
            confirmed_by=reviewer,
            note="Discordレビュー画面からブログ単位で一括確定",
            statuses=(status,),
        )


class BlogBulkConfirmView(discord.ui.View):
    def __init__(self, session: ReviewSession, author_name: str):
        super().__init__(timeout=None)
        self.session = session
        self.author_name = author_name

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.session.owner_id:
            await interaction.response.send_message("この操作はレビュー開始者だけが使えます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="一括確定する", emoji="✅", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.session.complete_current_blog(interaction, [self.author_name])
        self.stop()

    @discord.ui.button(label="キャンセル", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="一括確定をキャンセルしました。", view=None)
        self.stop()


@dataclass
class QuickPeopleState:
    parent_view: "PersonReviewView"
    people: list[dict[str, Any]]
    owner_id: int
    selected_names: list[str] = field(default_factory=list)
    page: int = 0

    @property
    def total_pages(self) -> int:
        return max(1, (len(self.people) + SELECT_PAGE_SIZE - 1) // SELECT_PAGE_SIZE)

    def page_people(self) -> list[dict[str, Any]]:
        start = self.page * SELECT_PAGE_SIZE
        return self.people[start:start + SELECT_PAGE_SIZE]

    def ordered_names(self) -> list[str]:
        return [normalize_text(item.get("person_name")) for item in self.people if normalize_text(item.get("person_name"))]

    def replace_page_selection(self, values: list[str]) -> None:
        page_names = {normalize_text(item.get("person_name")) for item in self.page_people()}
        selected = {name for name in self.selected_names if name not in page_names}
        selected.update(normalize_text(value) for value in values if normalize_text(value))
        self.selected_names = [name for name in self.ordered_names() if name in selected]


class QuickPeoplePageSelect(discord.ui.Select):
    """1ページ最大25人。ページをまたいだ複数選択を保持する。"""

    def __init__(self, state: QuickPeopleState):
        self.state = state
        selected = set(state.selected_names)
        options: list[discord.SelectOption] = []
        for item in state.page_people():
            name = normalize_text(item.get("person_name"))
            if not name:
                continue
            is_author = bool(item.get("is_author"))
            count = int(item.get("cooccurrence_count") or 0)
            description = "ブログ投稿者（最優先）" if is_author else f"投稿者との共写 {count}回"
            options.append(
                discord.SelectOption(
                    label=truncate_text(name, 100),
                    value=name,
                    description=truncate_text(description, 100),
                    default=name in selected,
                )
            )

        super().__init__(
            placeholder="よく使う人物から選択（複数可）",
            min_values=0,
            # 10人制限は設けない。Discordの1Select上限25人まで同時選択でき、
            # 25人を超える候補はページをまたいで選択内容を保持する。
            max_values=max(1, len(options)),
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.state.replace_page_selection(list(self.values))
        view = QuickPeoplePagedView(self.state)
        await interaction.response.edit_message(content=view.content(), view=view)


class QuickPeoplePagedView(discord.ui.View):
    """投稿者＋共写頻度ランキングから人数制限なしで人物を選ぶUI。"""

    def __init__(self, state: QuickPeopleState):
        super().__init__(timeout=600)
        self.state = state
        if state.page_people():
            self.add_item(QuickPeoplePageSelect(state))
        self.previous.disabled = state.page <= 0
        self.next.disabled = state.page >= state.total_pages - 1
        self.confirm.disabled = not bool(state.selected_names)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.state.owner_id:
            return True
        await interaction.response.send_message(
            "この選択画面はレビュー開始者だけが操作できます。",
            ephemeral=True,
        )
        return False

    def content(self) -> str:
        selected = format_people_for_users("、".join(self.state.selected_names)) or "まだ選択されていません。"
        start = self.state.page * SELECT_PAGE_SIZE + 1 if self.state.people else 0
        end = min(len(self.state.people), start + SELECT_PAGE_SIZE - 1) if self.state.people else 0
        return (
            "⚡ **よく使う人物から選択（複数可）**\n"
            "1番上はブログ投稿者、2番目以降は投稿者との共写回数が多い順です。\n"
            "ページを移動しても選択内容は保持されます。\n\n"
            f"表示 **{start}〜{end}/{len(self.state.people)}人** "
            f"（**{self.state.page + 1}/{self.state.total_pages}ページ**）\n"
            f"**選択中 {len(self.state.selected_names)}人:** {truncate_text(selected, 1400)}"
        )

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.page = max(0, self.state.page - 1)
        view = QuickPeoplePagedView(self.state)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.page = min(self.state.total_pages - 1, self.state.page + 1)
        view = QuickPeoplePagedView(self.state)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="このページを解除", emoji="🧹", style=discord.ButtonStyle.secondary, row=1)
    async def clear_page(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.replace_page_selection([])
        view = QuickPeoplePagedView(self.state)
        await interaction.response.edit_message(content=view.content(), view=view)

    @discord.ui.button(label="選択した人物で確定へ", emoji="✅", style=discord.ButtonStyle.success, row=2)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        names = list(self.state.selected_names)
        if not names:
            await interaction.response.send_message("人物を1人以上選択してください。", ephemeral=True)
            return
        parent = self.state.parent_view
        note = "投稿者＋共写回数順メニューから確定"
        if parent.session and parent.session.require_final_confirmation:
            label = format_people_for_users("、".join(normalize_people_for_storage(names)))
            await interaction.response.edit_message(
                content=(
                    f"🔎 **最終確認**\n写真ID **{parent.image_id}** を "
                    f"**{discord.utils.escape_markdown(label)}** で確定しますか？"
                ),
                view=FinalPersonConfirmView(parent, names, note),
            )
            self.stop()
            return
        await parent.commit_review(interaction, names, note=note)
        self.stop()

    @discord.ui.button(label="閉じる", emoji="✖️", style=discord.ButtonStyle.secondary, row=2)
    async def close(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(content="人物選択を閉じました。", view=None)
        self.stop()


class FinalPersonConfirmView(discord.ui.View):
    """人物候補をDBへ保存する直前の最終確認。"""

    def __init__(self, parent: "PersonReviewView", names: list[str], note: str):
        super().__init__(timeout=None)
        self.parent = parent
        self.names = list(names)
        self.note = note

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.parent.session and interaction.user.id != self.parent.session.owner_id:
            await interaction.response.send_message("この最終確認はレビュー開始者だけが操作できます。", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="この内容で確定", emoji="✅", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.parent.commit_review(interaction, self.names, note=self.note)
        self.stop()

    @discord.ui.button(label="選び直す", emoji="↩️", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        # 最終確認は補助エフェメラルなので、選び直すときは消して元カードへ戻す。
        await interaction.response.defer(ephemeral=True)
        try:
            await interaction.delete_original_response()
        except (discord.HTTPException, discord.NotFound, discord.Forbidden):
            try:
                await interaction.edit_original_response(content=None, embed=None, view=None)
            except (discord.HTTPException, discord.NotFound, discord.Forbidden):
                pass
        self.stop()




class HoldReasonView(discord.ui.View):
    def __init__(self, review_view: "PersonReviewView") -> None:
        super().__init__(timeout=300)
        self.review_view = review_view
        for code, label in HOLD_REASON_LABELS.items():
            button = discord.ui.Button(label=label, style=discord.ButtonStyle.secondary, custom_id=f"hold:{code}")
            async def callback(interaction: discord.Interaction, c=code, l=label):
                await interaction.response.defer(ephemeral=True)
                await asyncio.to_thread(save_hold_reason, self.review_view.image_id, c, "", interaction.user.id)
                await _delete_message_safely(self.review_view.message or interaction.message)
                await interaction.edit_original_response(content=f"⏸️ 写真ID **{self.review_view.image_id}** を「{l}」で保留しました。", view=None)
                asyncio.create_task(_delete_original_response_later(interaction))
                if self.review_view.session:
                    await self.review_view.session.mark_done(self.review_view.image_id, interaction)
            button.callback = callback
            self.add_item(button)

class PersonSetApplySelect(discord.ui.Select):
    def __init__(self, review_view: "PersonReviewView", sets: list[dict[str, Any]]):
        self.review_view=review_view
        options=[discord.SelectOption(label=x["name"][:100],description="、".join(x["people"])[:100],value=str(x["id"])) for x in sets]
        super().__init__(placeholder="人物セットを選択",options=options)
        self.sets={str(x["id"]):x for x in sets}
    async def callback(self, interaction: discord.Interaction):
        item = self.sets[self.values[0]]

        # 人物セットは「そのまま確定」ではなく、確認・部分修正できる初期値として読み込む。
        # セット内の一部だけ外す、別人物を追加する、といった修正をしてから確定できる。
        source_message = self.review_view.message or interaction.message
        state = SelectionState(
            review=self.review_view.review,
            owner_id=interaction.user.id,
            source_message=source_message,
            selection_message=interaction.message,
            session=self.review_view.session,
            selected_names=normalize_people_for_storage(list(item["people"])),
            base_person_set_name=normalize_text(item["name"]),
        )

        await interaction.response.edit_message(
            content=(
                f"📚 人物セット **{discord.utils.escape_markdown(item['name'])}** を読み込みました。\n"
                "必要な人物だけ外したり、別の人物を追加してから確定できます。\n\n"
                + selection_text(state)
            ),
            view=SelectedPeopleView(state),
        )

PERSON_SET_APPLY_PAGE_SIZE = 25


class PersonSetApplyView(discord.ui.View):
    def __init__(
        self,
        review_view: "PersonReviewView",
        sets: list[dict[str, Any]],
        *,
        page: int = 0,
        total: int = 0,
    ):
        super().__init__(timeout=300)
        self.review_view = review_view
        self.page = max(0, int(page))
        self.total = max(0, int(total))
        if sets:
            self.add_item(PersonSetApplySelect(review_view, sets))
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * PERSON_SET_APPLY_PAGE_SIZE >= self.total

    @classmethod
    async def create(
        cls,
        review_view: "PersonReviewView",
        page: int = 0,
    ) -> "PersonSetApplyView":
        total = await asyncio.to_thread(count_person_sets)
        max_page = max(0, (total - 1) // PERSON_SET_APPLY_PAGE_SIZE) if total else 0
        safe_page = max(0, min(int(page), max_page))
        sets = await asyncio.to_thread(
            load_person_sets,
            PERSON_SET_APPLY_PAGE_SIZE,
            safe_page * PERSON_SET_APPLY_PAGE_SIZE,
        )
        return cls(review_view, sets, page=safe_page, total=total)

    def content(self) -> str:
        if self.total <= 0:
            return "人物セットがまだ登録されていません。"
        start = self.page * PERSON_SET_APPLY_PAGE_SIZE + 1
        end = min(self.total, start + PERSON_SET_APPLY_PAGE_SIZE - 1)
        pages = max(1, (self.total + PERSON_SET_APPLY_PAGE_SIZE - 1) // PERSON_SET_APPLY_PAGE_SIZE)
        return (
            "使用する人物セットを選んでください。\n"
            f"表示 **{start}〜{end}/{self.total}件**（**{self.page + 1}/{pages}ページ**）"
        )

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        view = await PersonSetApplyView.create(self.review_view, self.page - 1)
        await interaction.edit_original_response(content=view.content(), view=view)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        view = await PersonSetApplyView.create(self.review_view, self.page + 1)
        await interaction.edit_original_response(content=view.content(), view=view)

class PersonReviewView(discord.ui.View):
    def __init__(self, review: dict[str, Any], session: ReviewSession | None = None):
        super().__init__(timeout=None)
        self.review = review
        self.session = session
        self.image_id = int(review["image_id"])
        self.message: discord.Message | None = None
        self._commit_lock = asyncio.Lock()
        self._committed = False
        self.source_message_id = 0
        self.candidates = build_candidate_names(review)
        # 投稿者＋共写回数ランキングは画面生成時に毎回DBから取得する。
        # 確定データが増えるたび、次に開く人物確認で順位が自然に更新される。
        self.quick_people = get_quick_people_for_review(review)
        self.accept_candidate.disabled = not bool(self.candidates)
        has_confirmed_people = bool(split_person_names(review.get("confirmed_people", "")))
        is_skipped_review = normalize_text(review.get("review_status")) == "skipped"
        if has_confirmed_people:
            self.select_person.label = "登録人物を編集"
            self.select_person.emoji = "✏️"
            self.manual_input.label = "名前・不明人数を直接編集"
        elif is_skipped_review:
            self.select_person.label = "人物を再設定"
            self.manual_input.label = "名前・不明人数を直接編集"
        else:
            self.manual_input.label = "名前・不明人数を入力"
        self.quick_people_button.disabled = not bool(self.quick_people)

        # スキップ済み一覧では再スキップすると同じ写真が再取得され続けるため、
        # スキップボタンを無効化して必ず確定・終了のどちらかを選べるようにする。
        if self.session and self.session.queue_status == "skipped":
            self.skip_review.disabled = True
            self.skip_review.label = "再スキップ不可"

        if self.session and self.session.continuous:
            stop_button = discord.ui.Button(
                label="連続停止",
                emoji="⏹️",
                style=discord.ButtonStyle.secondary,
                row=1,
            )

            async def stop_continuous(interaction: discord.Interaction) -> None:
                self.session.continuous = False
                await interaction.response.send_message(
                    "⏹️ 連続レビューを停止しました。現在表示中のセット終了後に続行確認を表示します。",
                    ephemeral=True,
                )

            stop_button.callback = stop_continuous
            self.add_item(stop_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if self.session and self.session.owner_id and interaction.user.id != self.session.owner_id:
            await interaction.response.send_message(
                "このレビュー操作はコマンドを実行した本人だけが使えます。",
                ephemeral=True,
            )
            return False
        return True

    async def complete_review(
        self,
        interaction: discord.Interaction,
        names: list[str],
        *,
        note: str,
    ) -> None:
        if self.session and self.session.require_final_confirmation:
            normalized_names = normalize_people_for_storage(names)
            label = "人物なし" if not normalized_names else format_people_for_users("、".join(normalized_names))
            await interaction.response.send_message(
                f"🔎 **最終確認**\n写真ID **{self.image_id}** を **{discord.utils.escape_markdown(label)}** で確定しますか？",
                view=FinalPersonConfirmView(self, names, note),
                ephemeral=True,
            )
            return
        await self.commit_review(interaction, names, note=note)

    async def commit_review(
        self,
        interaction: discord.Interaction,
        names: list[str],
        *,
        note: str,
    ) -> None:
        async with self._commit_lock:
            if self._committed:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "✅ この写真はすでに確定済みです。", ephemeral=True
                    )
                return

            if not interaction.response.is_done():
                await interaction.response.defer()

            names = normalize_people_for_storage(names)
            try:
                await asyncio.to_thread(
                    _commit_selected_people,
                    self.image_id,
                    names,
                    interaction.user.id,
                    get_reviewer_name(interaction.user),
                    note,
                )
            except Exception as exc:
                try:
                    await interaction.followup.send(
                        f"⚠️ 人物確定に失敗しました: {exc}", ephemeral=True
                    )
                except discord.HTTPException:
                    pass
                return

            self._committed = True
            confirmation_text = build_people_confirmation_text(self.image_id, names)
            await _finish_review_message(
                interaction,
                self.message or interaction.message,
                confirmation_text,
            )
            if self.session:
                await self.session.mark_done(self.image_id, interaction)

    @discord.ui.button(label="候補をすべて採用", emoji="✅", style=discord.ButtonStyle.success, row=0)
    async def accept_candidate(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            self.candidates,
            note="表示候補をすべて採用",
        )

    @discord.ui.button(label="人物を選ぶ", emoji="👥", style=discord.ButtonStyle.primary, row=0)
    async def select_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        """人物選択は新しいfollowupを作らず、元のレビューカード上で進める。

        以前は人物選択だけ別のエフェメラルfollowupへ遷移していたため、
        確定後に元の「写真の人物確認」カードだけが残るケースがあった。
        同じメッセージを編集して選択UIへ切り替えることで、確定時に
        現在のInteractionから1枚を確実に削除できるようにする。
        """
        await interaction.response.defer()
        await asyncio.to_thread(seed_member_master)
        initial_raw = split_person_names(self.review.get("confirmed_people", ""))
        initial_unknown = sum(unknown_other_count(name) for name in initial_raw)
        initial = [name for name in initial_raw if not unknown_other_count(name)]
        source_message = self.message or interaction.message
        state = SelectionState(
            review=self.review,
            owner_id=interaction.user.id,
            source_message=source_message,
            selection_message=source_message,
            session=self.session,
            selected_names=initial,
            unknown_other_people=initial_unknown,
        )
        await interaction.edit_original_response(
            content=selection_text(state),
            view=GroupView(state),
        )

    @discord.ui.button(label="手入力", emoji="✏️", style=discord.ButtonStyle.secondary, row=0)
    async def manual_input(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PersonInputModal(self))

    @discord.ui.button(label="スキップ", emoji="⏭️", style=discord.ButtonStyle.secondary, row=0)
    async def skip_review(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer()
        await asyncio.to_thread(
            mark_person_review_skipped,
            self.image_id,
            get_reviewer_name(interaction.user),
            "Discordレビュー画面でスキップ",
        )
        await _finish_review_message(
            interaction,
            self.message or interaction.message,
            f"⏭️ 写真ID **{self.image_id}** をスキップ一覧へ移しました。",
        )
        if self.session:
            await self.session.mark_done(self.image_id, interaction)

    @discord.ui.button(label="このブログを投稿者で一括確定", emoji="📚", style=discord.ButtonStyle.success, row=1)
    async def confirm_blog_author(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        author = normalize_text(self.review.get("member_name"))
        if not author:
            await interaction.response.send_message("ブログ投稿者が取得できないため一括確定できません。", ephemeral=True)
            return
        if not self.session:
            await interaction.response.send_message(
                "一括確定は `!review_list` 系コマンドから開いたレビューで利用してください。",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"このブログの確認待ち画像を **{author}** で一括確定しますか？",
            view=BlogBulkConfirmView(self.session, author),
            ephemeral=True,
        )

    @discord.ui.button(label="人物なし", emoji="🚫", style=discord.ButtonStyle.danger, row=1)
    async def no_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            [],
            note="人物は写っていない",
        )

    @discord.ui.button(label="人物不明", emoji="❓", style=discord.ButtonStyle.secondary, row=1)
    async def unknown_person(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self.complete_review(
            interaction,
            [make_unknown_other_label(1)],
            note="その他の人物が1人写っているが名前不明",
        )

    @discord.ui.button(
        label="よく使う人物から選択（複数可）",
        emoji="⚡",
        style=discord.ButtonStyle.primary,
        row=2,
    )
    async def quick_people_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        if not self.quick_people:
            await interaction.response.send_message(
                "選択できる人物がありません。",
                ephemeral=True,
            )
            return
        state = QuickPeopleState(
            parent_view=self,
            people=[dict(item) for item in self.quick_people],
            owner_id=interaction.user.id,
        )
        view = QuickPeoplePagedView(state)
        await interaction.response.send_message(
            view.content(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(label="理由を選んで保留", emoji="⏸️", style=discord.ButtonStyle.secondary, row=2)
    async def hold_with_reason(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_message("保留理由を選んでください。", view=HoldReasonView(self), ephemeral=True)

    @discord.ui.button(label="候補を仮確定", emoji="🧪", style=discord.ButtonStyle.secondary, row=2)
    async def provisional_candidates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        if not self.candidates:
            await interaction.followup.send("仮確定できる候補がありません。", ephemeral=True); return
        await asyncio.to_thread(save_provisional_people, self.image_id, self.candidates, "review_candidates", 0.0)
        await interaction.followup.send(
            f"🧪 写真ID **{self.image_id}** を仮確定しました。\n人物: {'、'.join(self.candidates)}\n本確定はAI育成センターから行えます。",
            ephemeral=True,
        )

    @discord.ui.button(label="人物セット", emoji="📚", style=discord.ButtonStyle.secondary, row=2)
    async def use_person_set(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True, thinking=True)
        view = await PersonSetApplyView.create(self, 0)
        if view.total <= 0:
            await interaction.followup.send(
                "人物セットがまだ登録されていません。",
                ephemeral=True,
            )
            return
        await interaction.followup.send(
            view.content(),
            view=view,
            ephemeral=True,
        )


async def send_person_review(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    review: dict[str, Any],
    *,
    session: ReviewSession | None = None,
) -> discord.Message | None:
    view = PersonReviewView(review, session=session)
    embed = build_review_embed(review, view.quick_people)
    local_path = normalize_text(review.get("local_path"))
    image_url = normalize_text(review.get("image_url"))
    file: discord.File | None = None
    if local_path and os.path.isfile(local_path):
        filename = os.path.basename(local_path) or f"photo_{review['image_id']}.jpg"
        file = discord.File(local_path, filename=filename)
        embed.set_image(url=f"attachment://{filename}")
    elif image_url:
        embed.set_image(url=image_url)
    kwargs: dict[str, Any] = {"embed": embed, "view": view}
    if file:
        kwargs["file"] = file
    if isinstance(destination, discord.Interaction):
        kwargs.setdefault("ephemeral", True)
        if destination.response.is_done():
            message = await destination.followup.send(**kwargs, wait=True)
        else:
            await destination.response.send_message(**kwargs)
            message = await destination.original_response()
    else:
        message = await destination.send(**kwargs)
    view.message = message
    view.source_message_id = int(getattr(message, "id", 0) or 0)
    return message


async def send_next_person_review(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    *,
    group_name: str = "",
) -> dict[str, Any] | None:
    reviews = await asyncio.to_thread(get_pending_person_reviews, 1, group_name)
    if not reviews:
        message = "✅ 人物確認待ちの写真はありません。"
        if isinstance(destination, discord.Interaction):
            await (destination.followup.send(message, ephemeral=True) if destination.response.is_done() else destination.response.send_message(message, ephemeral=True))
        else:
            await destination.send(message)
        return None
    await send_person_review(destination, reviews[0])
    return reviews[0]


async def send_person_review_batch(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    limit: int = 5,
    *,
    queue_status: str = "pending",
    group_name: str = "",
    continuous: bool = False,
    require_final_confirmation: bool = False,
) -> int:
    owner = getattr(destination, "author", None) or getattr(destination, "user", None)
    owner_id = int(getattr(owner, "id", 0) or 0)
    session = ReviewSession(
        destination,
        owner_id=owner_id,
        batch_size=max(1, min(int(limit), 10)),
        queue_status=queue_status,
        group_name=group_name,
        continuous=continuous,
        require_final_confirmation=require_final_confirmation,
    )
    return await session.start_batch()


async def send_skipped_person_review_batch(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    limit: int = 5,
    *,
    group_name: str = "",
) -> int:
    """過去にスキップした人物レビューを再表示する。"""
    return await send_person_review_batch(
        destination,
        limit=limit,
        queue_status="skipped",
        group_name=group_name,
    )

async def send_blog_person_review_batch(
    destination: commands.Context | discord.Interaction | discord.abc.Messageable,
    blog_id: int,
    limit: int = 5,
    *,
    continuous: bool = True,
    require_final_confirmation: bool = True,
) -> int:
    """指定ブログだけを対象に人物確認レビューを開始する。"""
    owner = getattr(destination, "author", None) or getattr(destination, "user", None)
    owner_id = int(getattr(owner, "id", 0) or 0)
    session = ReviewSession(
        destination,
        owner_id=owner_id,
        batch_size=max(1, min(int(limit), 10)),
        queue_status="pending",
        continuous=continuous,
        require_final_confirmation=require_final_confirmation,
        fixed_blog_id=int(blog_id),
    )
    count = await session.start_batch()
    # 指定記事に対象がないとき、通常のstart_batchは次の記事へ移るため、
    # 呼び出し後に別記事へ切り替わっていないかを防ぐ。
    return count
