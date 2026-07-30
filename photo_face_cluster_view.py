from __future__ import annotations

import asyncio
import io
from typing import Any

import discord
from discord.ext import commands
from PIL import Image, ImageDraw, ImageOps

from local_face_recognition import get_face_crop_bytes
from photo_database import complete_face_cluster, get_person_by_name
from photo_face_clustering import FaceClusteringUnavailable, cluster_pending_faces


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _reviewer(user: discord.abc.User) -> str:
    name = _text(getattr(user, "display_name", "")) or _text(getattr(user, "name", ""))
    return f"{name} ({user.id})"


def _build_contact_sheet_sync(items: list[dict[str, Any]]) -> bytes:
    samples = items[:9]
    tile_size = 220
    columns = 3
    rows = (len(samples) + columns - 1) // columns
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
        label = f"顔ID {int(item['face_id'])}"
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
) -> discord.Embed:
    items = cluster["items"]

    embed = discord.Embed(
        title=f"🧩 類似顔クラスタ {index + 1}/{total}",
        description=(
            "ローカル特徴量だけで、同一人物の可能性が高い顔をまとめています。\n"
            "画像を確認し、全員が同じ人物の場合だけ一括確定してください。"
        ),
        color=discord.Color.teal(),
    )

    embed.add_field(
        name="クラスタ件数",
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

    face_ids = ", ".join(
        str(int(item["face_id"]))
        for item in items[:20]
    )

    if len(items) > 20:
        face_ids += f" ほか{len(items)-20}件"

    embed.add_field(
        name="顔ID",
        value=face_ids or "なし",
        inline=False,
    )

    embed.set_image(url="attachment://face_cluster.jpg")
    embed.set_footer(
        text="OpenAI APIは使用しません。確定操作は取り消しできません。"
    )

    return embed


class ClusterPersonModal(
    discord.ui.Modal,
    title="クラスタの人物を確定",
):
    person_name = discord.ui.TextInput(
        label="人物名",
        placeholder="人物マスターに登録済みの正確な名前",
        min_length=1,
        max_length=100,
    )

    def __init__(self, parent: "FaceClusterReviewView") -> None:
        super().__init__()
        self.parent_view = parent

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:

        name = _text(self.person_name.value)

        person = await asyncio.to_thread(
            get_person_by_name,
            name,
        )

        if not person:
            await interaction.response.send_message(
                f"⚠️ 人物マスターに **{discord.utils.escape_markdown(name)}** は見つかりません。",
                ephemeral=True,
            )
            return

        await self.parent_view.confirm_current(
            interaction,
            int(person["id"]),
            _text(person["person_name"]),
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

        self._update_buttons()

    @property
    def current(self) -> dict[str, Any]:
        return self.clusters[self.index]

    def _update_buttons(self) -> None:
        self.previous_button.disabled = self.index <= 0
        self.next_button.disabled = self.index >= len(self.clusters) - 1

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

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

    async def _render(
        self,
        interaction: discord.Interaction,
    ) -> None:

        self._update_buttons()

        data = await asyncio.to_thread(
            _build_contact_sheet_sync,
            self.current["items"],
        )

        file = discord.File(
            io.BytesIO(data),
            filename="face_cluster.jpg",
        )

        embed = build_cluster_embed(
            self.current,
            index=self.index,
            total=len(self.clusters),
            threshold=self.threshold,
        )

        await interaction.response.edit_message(
            embed=embed,
            attachments=[file],
            view=self,
        )

    async def confirm_current(
        self,
        interaction: discord.Interaction,
        person_id: int,
        person_name: str,
    ) -> None:

        face_ids = [
            int(item["face_id"])
            for item in self.current["items"]
        ]

        completed = await asyncio.to_thread(
            complete_face_cluster,
            face_ids,
            person_id,
            _reviewer(interaction.user),
            f"顔クラスタ一括確定 threshold={self.threshold:.4f}",
        )

        self.clusters.pop(self.index)

        if not self.clusters:
            self.finished = True
            self.stop()

            embed = discord.Embed(
                title="✅ すべての顔クラスタ確認が完了しました",
                color=discord.Color.green(),
            )

            embed.add_field(
                name="最後に確定した人物",
                value=person_name,
            )

            embed.add_field(
                name="確定件数",
                value=f"{completed:,}件",
            )

            await interaction.response.edit_message(
                embed=embed,
                attachments=[],
                view=None,
            )
            return

        self.index = min(
            self.index,
            len(self.clusters) - 1,
        )

        self._update_buttons()

        data = await asyncio.to_thread(
            _build_contact_sheet_sync,
            self.current["items"],
        )

        file = discord.File(
            io.BytesIO(data),
            filename="face_cluster.jpg",
        )

        embed = build_cluster_embed(
            self.current,
            index=self.index,
            total=len(self.clusters),
            threshold=self.threshold,
        )

        embed.description = (
            f"✅ **{discord.utils.escape_markdown(person_name)}** として "
            f"{completed}件確定しました。\n\n"
            + (embed.description or "")
        )

        await interaction.response.edit_message(
            embed=embed,
            attachments=[file],
            view=self,
        )

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
        await interaction.response.send_modal(
            ClusterPersonModal(self)
        )

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
        await self._render(interaction)

    @discord.ui.button(
        label="次へ",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def next_button(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.index = min(
            len(self.clusters) - 1,
            self.index + 1,
        )
        await self._render(interaction)

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

    data = await asyncio.to_thread(
        _build_contact_sheet_sync,
        clusters[0]["items"],
    )

    view = FaceClusterReviewView(
        clusters,
        owner_id=ctx.author.id,
        threshold=float(result["threshold"]),
    )

    file = discord.File(
        io.BytesIO(data),
        filename="face_cluster.jpg",
    )

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

    message = await ctx.send(
        embed=embed,
        view=view,
        file=file,
    )

    view.message = message

    return len(clusters)
