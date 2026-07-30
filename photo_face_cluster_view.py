from __future__ import annotations

import asyncio
import io
import re
from typing import Any

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageOps

from local_face_recognition import get_face_crop_bytes
from photo_database import complete_face_cluster, get_person_by_name
from photo_face_clustering import FaceClusteringUnavailable, cluster_pending_faces


CONTACT_SHEET_LIMIT = 9
MIN_CONFIRM_ITEMS = 2


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _reviewer(user: discord.abc.User) -> str:
    name = _text(getattr(user, "display_name", "")) or _text(
        getattr(user, "name", "")
    )
    return f"{name} ({user.id})"


def _parse_face_ids(value: str) -> list[int]:
    """Extract unique positive face IDs from comma/space/newline separated text."""
    ids: list[int] = []
    seen: set[int] = set()
    for token in re.findall(r"\d+", value):
        face_id = int(token)
        if face_id > 0 and face_id not in seen:
            ids.append(face_id)
            seen.add(face_id)
    return ids


def _build_contact_sheet_sync(items: list[dict[str, Any]]) -> bytes:
    samples = items[:CONTACT_SHEET_LIMIT]
    tile_size = 220
    columns = 3
    rows = max(1, (len(samples) + columns - 1) // columns)
    sheet = Image.new("RGB", (columns * tile_size, rows * tile_size), "white")
    draw = ImageDraw.Draw(sheet)

    for index, item in enumerate(samples):
        data, _ = get_face_crop_bytes(int(item["face_id"]))
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            image.thumbnail((tile_size - 16, tile_size - 38))
            x = (index % columns) * tile_size + (tile_size - image.width) // 2
            y = (index // columns) * tile_size + 8
            sheet.paste(image, (x, y))

        # Use ASCII so the default Pillow font never produces mojibake.
        label = f"ID {int(item['face_id'])}"
        draw.text(
            (
                (index % columns) * tile_size + 8,
                (index // columns + 1) * tile_size - 25,
            ),
            label,
            fill="black",
        )

    output = io.BytesIO()
    sheet.save(output, format="JPEG", quality=88, optimize=True)
    return output.getvalue()


def build_cluster_embed(
    cluster: dict[str, Any],
    *,
    index: int,
    total: int,
    threshold: float,
    notice: str = "",
) -> discord.Embed:
    items = cluster["items"]
    excluded_ids = [int(value) for value in cluster.get("excluded_face_ids", [])]

    description_parts: list[str] = []
    if notice:
        description_parts.append(notice)
    description_parts.append(
        "ローカル特徴量だけで、同一人物の可能性が高い顔をまとめています。\n"
        "別人が混ざっている場合は、その顔IDを除外してから確定してください。"
    )

    embed = discord.Embed(
        title=f"🧩 類似顔クラスタ {index + 1}/{total}",
        description="\n\n".join(description_parts),
        color=discord.Color.teal(),
    )

    embed.add_field(
        name="現在の確定対象",
        value=f"{len(items):,}件",
        inline=True,
    )
    embed.add_field(
        name="判定しきい値",
        value=f"{threshold * 100:.1f}%",
        inline=True,
    )
    embed.add_field(
        name="クラスタ内類似度",
        value=(
            f"最低 {float(cluster['minimum_similarity']) * 100:.1f}% / "
            f"平均 {float(cluster['average_similarity']) * 100:.1f}% / "
            f"最高 {float(cluster['maximum_similarity']) * 100:.1f}%"
        ),
        inline=False,
    )

    face_ids = ", ".join(str(int(item["face_id"])) for item in items[:20])
    if len(items) > 20:
        face_ids += f" ほか{len(items) - 20}件"
    embed.add_field(name="確定対象の顔ID", value=face_ids or "なし", inline=False)

    if excluded_ids:
        excluded_text = ", ".join(str(value) for value in excluded_ids[:20])
        if len(excluded_ids) > 20:
            excluded_text += f" ほか{len(excluded_ids) - 20}件"
        embed.add_field(
            name="この画面で除外中",
            value=excluded_text,
            inline=False,
        )

    if len(items) > CONTACT_SHEET_LIMIT:
        embed.add_field(
            name="画像表示",
            value=f"先頭{CONTACT_SHEET_LIMIT}件を表示しています。顔ID欄には全件を表示します。",
            inline=False,
        )

    embed.set_image(url="attachment://face_cluster.jpg")
    embed.set_footer(
        text="OpenAI APIは使用しません。確定前に最終確認が表示されます。"
    )
    return embed


class ExcludeFacesModal(discord.ui.Modal, title="クラスタから顔を除外"):
    face_ids = discord.ui.TextInput(
        label="除外する顔ID",
        placeholder="例: 225, 293",
        min_length=1,
        max_length=400,
        style=discord.TextStyle.paragraph,
    )

    def __init__(self, parent: "FaceClusterReviewView") -> None:
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        requested_ids = _parse_face_ids(_text(self.face_ids.value))
        if not requested_ids:
            await interaction.response.send_message(
                "⚠️ 有効な顔IDを入力してください。",
                ephemeral=True,
            )
            return

        current_ids = {
            int(item["face_id"]) for item in self.parent_view.current["items"]
        }
        valid_ids = [face_id for face_id in requested_ids if face_id in current_ids]
        invalid_ids = [face_id for face_id in requested_ids if face_id not in current_ids]

        if not valid_ids:
            await interaction.response.send_message(
                "⚠️ 入力された顔IDは、現在のクラスタに含まれていません。",
                ephemeral=True,
            )
            return

        remaining_count = len(current_ids) - len(valid_ids)
        if remaining_count < MIN_CONFIRM_ITEMS:
            await interaction.response.send_message(
                f"⚠️ 除外後は{MIN_CONFIRM_ITEMS}件以上残す必要があります。",
                ephemeral=True,
            )
            return

        self.parent_view.exclude_from_current(valid_ids)
        notice = f"🗑️ 顔ID {', '.join(map(str, valid_ids))} を一時的に除外しました。"
        if invalid_ids:
            notice += f"\n⚠️ 対象外ID: {', '.join(map(str, invalid_ids))}"

        await interaction.response.send_message(notice, ephemeral=True)
        await self.parent_view.edit_main_message(notice=notice)


class ClusterPersonModal(discord.ui.Modal, title="クラスタの人物を指定"):
    person_name = discord.ui.TextInput(
        label="人物名",
        placeholder="人物マスターに登録済みの正確な名前",
        min_length=1,
        max_length=100,
    )

    def __init__(self, parent: "FaceClusterReviewView") -> None:
        super().__init__()
        self.parent_view = parent

    async def on_submit(self, interaction: discord.Interaction) -> None:
        name = _text(self.person_name.value)
        person = await asyncio.to_thread(get_person_by_name, name)

        if not person:
            await interaction.response.send_message(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(name)}** は見つかりません。",
                ephemeral=True,
            )
            return

        face_ids = self.parent_view.current_face_ids()
        if len(face_ids) < MIN_CONFIRM_ITEMS:
            await interaction.response.send_message(
                "⚠️ 確定対象が不足しています。クラスタを再確認してください。",
                ephemeral=True,
            )
            return

        confirm_view = FinalClusterConfirmView(
            self.parent_view,
            person_id=int(person["id"]),
            person_name=_text(person["person_name"]),
            face_ids=face_ids,
            owner_id=interaction.user.id,
        )
        await interaction.response.send_message(
            "⚠️ **最終確認**\n"
            f"人物: **{discord.utils.escape_markdown(confirm_view.person_name)}**\n"
            f"確定する顔ID: `{', '.join(map(str, face_ids))}`\n"
            f"件数: **{len(face_ids)}件**\n\n"
            "内容が正しい場合だけ「確定する」を押してください。",
            view=confirm_view,
            ephemeral=True,
        )


class FinalClusterConfirmView(discord.ui.View):
    def __init__(
        self,
        parent: "FaceClusterReviewView",
        *,
        person_id: int,
        person_name: str,
        face_ids: list[int],
        owner_id: int,
    ) -> None:
        super().__init__(timeout=180)
        self.parent_view = parent
        self.person_id = int(person_id)
        self.person_name = person_name
        self.face_ids = list(face_ids)
        self.owner_id = int(owner_id)
        self.used = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "この最終確認は操作を開始した本人だけが使えます。",
                ephemeral=True,
            )
            return False
        if self.used:
            await interaction.response.send_message(
                "この最終確認はすでに終了しています。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(
        label="確定する",
        emoji="✅",
        style=discord.ButtonStyle.success,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.used = True
        for item in self.children:
            item.disabled = True

        # Refuse stale confirmation when the reviewer changed the cluster meanwhile.
        if self.parent_view.current_face_ids() != self.face_ids:
            await interaction.response.edit_message(
                content="⚠️ クラスタの内容が変更されています。もう一度人物名を入力してください。",
                view=None,
            )
            self.stop()
            return

        await interaction.response.edit_message(
            content="⏳ 一括確定しています…",
            view=self,
        )
        try:
            completed = await self.parent_view.commit_current(
                user=interaction.user,
                person_id=self.person_id,
                person_name=self.person_name,
            )
        except Exception as error:
            self.used = False
            for item in self.children:
                item.disabled = False
            await interaction.edit_original_response(
                content=(
                    "❌ 一括確定に失敗しました。データは確定されていない可能性があります。\n"
                    f"`{type(error).__name__}: {error}`"
                ),
                view=self,
            )
            return

        await interaction.edit_original_response(
            content=(
                f"✅ **{discord.utils.escape_markdown(self.person_name)}** として "
                f"**{completed}件**確定しました。"
            ),
            view=None,
        )
        self.stop()

    @discord.ui.button(
        label="キャンセル",
        emoji="✖️",
        style=discord.ButtonStyle.secondary,
    )
    async def cancel_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.used = True
        self.stop()
        await interaction.response.edit_message(
            content="確定をキャンセルしました。クラスタのデータは変更していません。",
            view=None,
        )


class FaceClusterReviewView(discord.ui.View):
    def __init__(
        self,
        clusters: list[dict[str, Any]],
        *,
        owner_id: int,
        threshold: float,
    ) -> None:
        super().__init__(timeout=900)
        self.clusters = clusters
        self.owner_id = int(owner_id)
        self.threshold = float(threshold)
        self.index = 0
        self.message: discord.Message | None = None
        self.finished = False

        for cluster in self.clusters:
            cluster.setdefault("original_items", list(cluster["items"]))
            cluster.setdefault("excluded_face_ids", [])
        self._update_buttons()

    @property
    def current(self) -> dict[str, Any]:
        return self.clusters[self.index]

    def current_face_ids(self) -> list[int]:
        return [int(item["face_id"]) for item in self.current["items"]]

    def exclude_from_current(self, face_ids: list[int]) -> None:
        excluded = {int(value) for value in face_ids}
        self.current["items"] = [
            item
            for item in self.current["items"]
            if int(item["face_id"]) not in excluded
        ]
        saved = {int(value) for value in self.current.get("excluded_face_ids", [])}
        self.current["excluded_face_ids"] = sorted(saved | excluded)
        self.current["size"] = len(self.current["items"])
        self._update_buttons()

    def restore_current(self) -> bool:
        excluded = self.current.get("excluded_face_ids", [])
        if not excluded:
            return False
        self.current["items"] = list(self.current["original_items"])
        self.current["excluded_face_ids"] = []
        self.current["size"] = len(self.current["items"])
        self._update_buttons()
        return True

    def _update_buttons(self) -> None:
        if not self.clusters:
            return
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.clusters) - 1
        self.restore_button.disabled = not bool(
            self.current.get("excluded_face_ids", [])
        )
        self.confirm_button.disabled = len(self.current["items"]) < MIN_CONFIRM_ITEMS

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "このクラスタ確認は開始した本人だけが操作できます。",
                ephemeral=True,
            )
            return False
        if self.finished:
            await interaction.response.send_message(
                "このクラスタ確認は終了しています。",
                ephemeral=True,
            )
            return False
        return True

    async def _make_render_parts(
        self,
        *,
        notice: str = "",
    ) -> tuple[discord.Embed, discord.File]:
        self._update_buttons()
        data = await asyncio.to_thread(
            _build_contact_sheet_sync,
            self.current["items"],
        )
        file = discord.File(io.BytesIO(data), filename="face_cluster.jpg")
        embed = build_cluster_embed(
            self.current,
            index=self.index,
            total=len(self.clusters),
            threshold=self.threshold,
            notice=notice,
        )
        return embed, file

    async def _render_interaction(
        self,
        interaction: discord.Interaction,
        *,
        notice: str = "",
    ) -> None:
        embed, file = await self._make_render_parts(notice=notice)
        await interaction.response.edit_message(
            embed=embed,
            attachments=[file],
            view=self,
        )

    async def edit_main_message(self, *, notice: str = "") -> None:
        if self.message is None:
            return
        embed, file = await self._make_render_parts(notice=notice)
        await self.message.edit(embed=embed, attachments=[file], view=self)

    async def commit_current(
        self,
        *,
        user: discord.abc.User,
        person_id: int,
        person_name: str,
    ) -> int:
        face_ids = self.current_face_ids()
        excluded_ids = [
            int(value) for value in self.current.get("excluded_face_ids", [])
        ]
        note = f"顔クラスタ一括確定 threshold={self.threshold:.4f}"
        if excluded_ids:
            note += f" excluded={','.join(map(str, excluded_ids))}"

        completed = await asyncio.to_thread(
            complete_face_cluster,
            face_ids,
            person_id,
            _reviewer(user),
            note,
        )
        if completed <= 0:
            raise RuntimeError("確定可能な確認待ち顔がありませんでした")

        self.clusters.pop(self.index)
        if not self.clusters:
            self.finished = True
            self.stop()
            if self.message is not None:
                embed = discord.Embed(
                    title="✅ すべての顔クラスタ確認が完了しました",
                    color=discord.Color.green(),
                )
                embed.add_field(name="最後に確定した人物", value=person_name)
                embed.add_field(name="確定件数", value=f"{completed:,}件")
                await self.message.edit(embed=embed, attachments=[], view=None)
            return completed

        self.index = min(self.index, len(self.clusters) - 1)
        await self.edit_main_message(
            notice=(
                f"✅ **{discord.utils.escape_markdown(person_name)}** として "
                f"{completed}件確定しました。"
            )
        )
        return completed

    @discord.ui.button(
        label="人物名を入力して一括確定",
        emoji="✅",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def confirm_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(ClusterPersonModal(self))

    @discord.ui.button(
        label="顔IDを除外",
        emoji="🗑️",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def exclude_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.send_modal(ExcludeFacesModal(self))

    @discord.ui.button(
        label="除外を元に戻す",
        emoji="↩️",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def restore_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        restored = self.restore_current()
        notice = "↩️ このクラスタの除外をすべて元に戻しました。" if restored else "変更はありません。"
        await self._render_interaction(interaction, notice=notice)

    @discord.ui.button(
        label="前へ",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def previous_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._render_interaction(interaction)

    @discord.ui.button(
        label="次へ（保留）",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.index = min(len(self.clusters) - 1, self.index + 1)
        await self._render_interaction(interaction, notice="⏸️ 前のクラスタは変更せず保留しました。")

    @discord.ui.button(
        label="終了",
        emoji="✖️",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def finish_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            embed=discord.Embed(
                title="✖️ 顔クラスタ確認を終了しました",
                description="未確定クラスタのデータは変更していません。",
                color=discord.Color.light_grey(),
            ),
            attachments=[],
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


async def send_face_cluster_review(
    ctx: commands.Context,
    limit: int = 200,
    similarity_percent: float = 90.0,
) -> int:
    safe_limit = max(20, min(int(limit), 500))
    safe_percent = max(70.0, min(float(similarity_percent), 99.9))

    await ctx.send(
        f"🧩 顔クラスタを計算しています… 最大 **{safe_limit}件** / "
        f"類似度 **{safe_percent:.1f}%以上**\n"
        "OpenAI APIは使用しません。"
    )

    try:
        result = await asyncio.to_thread(
            cluster_pending_faces,
            safe_limit,
            safe_percent / 100.0,
        )
    except FaceClusteringUnavailable as error:
        await ctx.send(f"⚠️ {error}")
        return 0
    except Exception as error:
        await ctx.send(
            f"❌ 顔クラスタリングに失敗しました: "
            f"`{type(error).__name__}: {error}`"
        )
        return 0

    clusters = result["clusters"]
    if not clusters:
        await ctx.send(
            "✅ 指定条件で2件以上にまとまる顔クラスタはありませんでした。\n"
            f"有効特徴量: **{result['valid_count']:,}件** / "
            f"単独: **{result['singleton_count']:,}件**"
        )
        return 0

    for cluster in clusters:
        cluster["original_items"] = list(cluster["items"])
        cluster["excluded_face_ids"] = []

    data = await asyncio.to_thread(_build_contact_sheet_sync, clusters[0]["items"])
    view = FaceClusterReviewView(
        clusters,
        owner_id=ctx.author.id,
        threshold=float(result["threshold"]),
    )
    file = discord.File(io.BytesIO(data), filename="face_cluster.jpg")
    embed = build_cluster_embed(
        clusters[0],
        index=0,
        total=len(clusters),
        threshold=float(result["threshold"]),
    )
    embed.add_field(
        name="計算結果",
        value=(
            f"入力 {result['input_count']:,}件 / "
            f"有効 {result['valid_count']:,}件 / "
            f"クラスタ {len(clusters):,}組 / "
            f"単独 {result['singleton_count']:,}件"
        ),
        inline=False,
    )

    message = await ctx.send(embed=embed, view=view, file=file)
    view.message = message
    return len(clusters)
