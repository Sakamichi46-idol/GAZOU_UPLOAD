from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import discord

from photo_database import get_connection
from photo_search import get_display_image_url, shorten_text


FAVORITE_VIEW_TIMEOUT_SECONDS = 300
MAX_FAVORITE_ITEMS = 100


def get_user_favorites(
    discord_user_id: str | int,
    limit: int = MAX_FAVORITE_ITEMS,
) -> list[dict[str, Any]]:
    """指定ユーザーのお気に入り写真を新しい順に取得する。"""
    safe_limit = max(1, min(int(limit), MAX_FAVORITE_ITEMS))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT
                photo_favorites.id AS favorite_id,
                photo_favorites.created_at AS favorite_created_at,
                photo_images.id,
                photo_images.image_url,
                photo_images.bucket_key,
                photo_images.image_index,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at
            FROM photo_favorites
            INNER JOIN photo_images
                ON photo_images.id = photo_favorites.image_id
            INNER JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id
            WHERE photo_favorites.discord_user_id = ?
            ORDER BY photo_favorites.id DESC
            LIMIT ?
            """,
            (str(discord_user_id), safe_limit),
        ).fetchall()

    return [dict(row) for row in rows]


def remove_user_favorite(
    image_id: int,
    discord_user_id: str | int,
) -> bool:
    """指定ユーザーのお気に入りから画像を1件削除する。"""
    with closing(get_connection()) as connection:
        cursor = connection.execute(
            """
            DELETE FROM photo_favorites
            WHERE image_id = ?
              AND discord_user_id = ?
            """,
            (int(image_id), str(discord_user_id)),
        )
        connection.commit()
        return cursor.rowcount > 0


def build_favorite_embed(
    favorite: dict[str, Any],
    *,
    index: int,
    total: int,
) -> discord.Embed:
    title = favorite.get("title") or "無題"
    blog_url = str(favorite.get("blog_url") or "").strip()

    embed = discord.Embed(
        title=shorten_text(title, 256),
        url=blog_url or None,
        description="⭐ お気に入りに登録している写真です。",
        color=0xF1C40F,
    )
    embed.add_field(
        name="🏷️ グループ",
        value=shorten_text(favorite.get("group_name") or "不明", 1024),
        inline=True,
    )
    embed.add_field(
        name="👤 ブログ投稿者",
        value=shorten_text(favorite.get("member_name") or "不明", 1024),
        inline=True,
    )
    embed.add_field(
        name="📅 投稿日時",
        value=shorten_text(favorite.get("published_at") or "不明", 1024),
        inline=False,
    )

    image_url = get_display_image_url(favorite)
    if image_url:
        embed.set_image(url=image_url)
    else:
        embed.add_field(
            name="⚠️ 表示できません",
            value="この写真には現在利用できる表示用URLがありません。",
            inline=False,
        )

    embed.set_footer(
        text=(
            f"お気に入り {index}/{total}"
            f" • 画像ID {int(favorite.get('id') or 0)}"
            f" • 記事内 {int(favorite.get('image_index') or 0)}枚目"
        )
    )
    return embed


class FavoriteGalleryView(discord.ui.View):
    """お気に入り写真を1枚ずつ表示し、個別削除できる画面。"""

    def __init__(
        self,
        *,
        owner_id: int,
        favorites: list[dict[str, Any]],
    ) -> None:
        super().__init__(timeout=FAVORITE_VIEW_TIMEOUT_SECONDS)
        self.owner_id = int(owner_id)
        self.favorites = favorites
        self.index = 0
        self.message: discord.Message | None = None
        self._sync_buttons()

    def _sync_buttons(self) -> None:
        has_items = bool(self.favorites)
        self.previous_button.disabled = not has_items or self.index <= 0
        self.next_button.disabled = not has_items or self.index >= len(self.favorites) - 1
        self.delete_button.disabled = not has_items

    def current_embed(self) -> discord.Embed:
        if not self.favorites:
            return discord.Embed(
                title="⭐ お気に入り",
                description="お気に入りはまだありません。\n写真検索・人物検索・タグ検索の結果から登録できます。",
                color=0x95A5A6,
            )

        return build_favorite_embed(
            self.favorites[self.index],
            index=self.index + 1,
            total=len(self.favorites),
        )

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True

        await interaction.response.send_message(
            "このお気に入り一覧を操作できるのは、表示した本人だけです。",
            ephemeral=True,
        )
        return False

    @discord.ui.button(label="前へ", emoji="◀️", style=discord.ButtonStyle.secondary)
    async def previous_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.index = max(0, self.index - 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="次へ", emoji="▶️", style=discord.ButtonStyle.primary)
    async def next_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        self.index = min(len(self.favorites) - 1, self.index + 1)
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.current_embed(), view=self)

    @discord.ui.button(label="この写真を削除", emoji="🗑️", style=discord.ButtonStyle.danger)
    async def delete_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        if not self.favorites:
            await interaction.response.send_message(
                "⚠️ 削除できるお気に入りがありません。",
                ephemeral=True,
            )
            return

        current = self.favorites[self.index]
        image_id = int(current.get("id") or 0)

        try:
            deleted = await asyncio.to_thread(
                remove_user_favorite,
                image_id,
                interaction.user.id,
            )
        except Exception as error:
            print("お気に入り個別削除エラー:", error)
            await interaction.response.send_message(
                "⚠️ お気に入りの削除中にエラーが発生しました。",
                ephemeral=True,
            )
            return

        if not deleted:
            await interaction.response.send_message(
                "⚠️ この写真はすでにお気に入りから削除されています。",
                ephemeral=True,
            )
            return

        self.favorites.pop(self.index)
        if self.index >= len(self.favorites):
            self.index = max(0, len(self.favorites) - 1)
        self._sync_buttons()

        await interaction.response.edit_message(embed=self.current_embed(), view=self)
        await interaction.followup.send(
            f"🗑️ 画像ID **{image_id}** をお気に入りから削除しました。",
            ephemeral=True,
        )

    @discord.ui.button(label="閉じる", emoji="⏹️", style=discord.ButtonStyle.secondary)
    async def close_button(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ) -> None:
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)
        self.stop()

    async def on_timeout(self) -> None:
        for child in self.children:
            child.disabled = True
        if self.message:
            try:
                await self.message.edit(view=self)
            except (discord.HTTPException, discord.NotFound):
                pass


async def send_favorite_gallery(
    ctx,
    *,
    limit: int = MAX_FAVORITE_ITEMS,
) -> None:
    """お気に入り写真一覧を画像付きの操作画面として送信する。"""
    try:
        favorites = await asyncio.to_thread(
            get_user_favorites,
            ctx.author.id,
            limit,
        )
    except Exception as error:
        print("お気に入り一覧取得エラー:", error)
        await ctx.send("⚠️ お気に入り一覧の取得中にエラーが発生しました。")
        return

    if not favorites:
        await ctx.send(
            "⭐ お気に入りはまだありません。\n"
            "写真検索・人物検索・タグ検索の結果から登録できます。"
        )
        return

    view = FavoriteGalleryView(owner_id=ctx.author.id, favorites=favorites)
    message = await ctx.send(embed=view.current_embed(), view=view)
    view.message = message
