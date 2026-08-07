from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import discord

from photo_database import get_connection
from tag_master import (
    approve_tag,
    block_tag,
    bootstrap_from_existing,
    diagnostic_summary,
    merge_candidates,
    merge_tags,
    rebuild_cache,
)

PAGE_SIZE = 20


def _summary_embed() -> discord.Embed:
    with closing(get_connection()) as con:
        bootstrap_from_existing(con)
        stats = diagnostic_summary(con)

    e = discord.Embed(
        title="🏷️ タグマスター管理",
        color=0xF1C40F,
    )

    e.description = (
        "原文タグを削除せず、代表タグ・同義語・承認状態・検索対象を管理します。"
    )

    e.add_field(
        name="代表タグ",
        value=f"{stats['master']:,}件",
        inline=True,
    )
    e.add_field(
        name="承認済み",
        value=f"{stats['approved']:,}件",
        inline=True,
    )
    e.add_field(
        name="未承認",
        value=f"{stats['pending']:,}件",
        inline=True,
    )
    e.add_field(
        name="除外",
        value=f"{stats['blocked']:,}件",
        inline=True,
    )
    e.add_field(
        name="別表記",
        value=f"{stats['aliases']:,}件",
        inline=True,
    )
    e.add_field(
        name="低信頼割当",
        value=f"{stats['low_confidence']:,}件",
        inline=True,
    )
    e.add_field(
        name="検索キャッシュ",
        value=f"{stats['cache']:,}件",
        inline=True,
    )

    e.set_footer(
        text="手動タグ > 承認済みAIタグ > 未承認AIタグ の優先順位です。"
    )

    return e


class MergeModal(discord.ui.Modal, title="代表タグを統合"):
    source_id = discord.ui.TextInput(
        label="統合元ID",
        placeholder="例: 125",
    )

    target_id = discord.ui.TextInput(
        label="統合先ID",
        placeholder="例: 30",
    )

    async def on_submit(
        self,
        interaction: discord.Interaction,
    ) -> None:

        try:
            source = int(
                str(self.source_id.value).strip()
            )

            target = int(
                str(self.target_id.value).strip()
            )

            await asyncio.to_thread(
                _merge,
                source,
                target,
                str(interaction.user.id),
            )

        except Exception as exc:
            await interaction.response.send_message(
                f"⚠️ 統合できませんでした: {exc}",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "✅ 元データを削除せず、"
            "別名の向き先を代表タグへ統合しました。",
            ephemeral=True,
        )


def _merge(
    source: int,
    target: int,
    actor: str,
) -> None:

    with closing(get_connection()) as con:
        merge_tags(
            con,
            source,
            target,
            actor=actor,
        )

        rebuild_cache(con)
        con.commit()


class PendingTagSelect(discord.ui.Select):
    def __init__(
        self,
        owner_id: int,
        rows: list[Any],
        action: str,
    ):
        self.owner_id = int(owner_id)
        self.action = action

        options = [
            discord.SelectOption(
                label=str(r[1])[:100],
                value=str(r[0]),
                description=(
                    f"{r[2]} / 使用{r[3]}件"
                )[:100],
            )
            for r in rows[:25]
        ]

        super().__init__(
            placeholder="対象タグを選択",
            min_values=1,
            max_values=min(
                25,
                len(options),
            ),
            options=options,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:

        ids = [
            int(v)
            for v in self.values
        ]

        actor = str(
            interaction.user.id
        )

        def run() -> None:
            with closing(
                get_connection()
            ) as con:

                for mid in ids:

                    if self.action == "approve":
                        approve_tag(
                            con,
                            mid,
                            actor=actor,
                        )
                    else:
                        block_tag(
                            con,
                            mid,
                            actor=actor,
                        )

                rebuild_cache(con)
                con.commit()

        await asyncio.to_thread(run)

        action_name = (
            "承認"
            if self.action == "approve"
            else "検索対象外"
        )

        await interaction.response.send_message(
            f"✅ {len(ids)}件を"
            f"{action_name}にしました。",
            ephemeral=True,
        )


class PendingTagView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
        page: int = 0,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(owner_id)
        self.page = max(
            0,
            page,
        )

        with closing(
            get_connection()
        ) as con:

            total = int(
                con.execute(
                    """
                    SELECT COUNT(*)
                    FROM tag_master
                    WHERE status='pending'
                    """
                ).fetchone()[0]
            )

            rows = con.execute(
                """
                SELECT
                    m.id,
                    m.canonical_tag,
                    m.category,
                    (
                        SELECT COUNT(*)
                        FROM tag_aliases a
                        WHERE
                            a.canonical_tag_id=m.id
                    ) AS aliases
                FROM tag_master m
                WHERE m.status='pending'
                ORDER BY
                    aliases DESC,
                    m.id
                LIMIT ?
                OFFSET ?
                """,
                (
                    PAGE_SIZE,
                    self.page * PAGE_SIZE,
                ),
            ).fetchall()

        self.total = total
        self.rows = rows

        if rows:
            self.add_item(
                PendingTagSelect(
                    owner_id,
                    rows,
                    "approve",
                )
            )

            self.add_item(
                PendingTagSelect(
                    owner_id,
                    rows,
                    "block",
                )
            )

        self.previous.disabled = (
            self.page <= 0
        )

        self.next.disabled = (
            (self.page + 1)
            * PAGE_SIZE
            >= total
        )

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await interaction.response.send_message(
            "この画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )

        return False

    def embed(self) -> discord.Embed:
        start = (
            self.page * PAGE_SIZE + 1
            if self.rows
            else 0
        )

        lines = [
            (
                f"`{r[0]}` "
                f"**{r[1]}** "
                f"— {r[2]} / "
                f"別表記{r[3]}件"
            )
            for r in self.rows
        ]

        e = discord.Embed(
            title="🆕 未承認タグ",
            description=(
                "\n".join(lines)
                or
                "未承認タグはありません。"
            ),
            color=0xFEE75C,
        )

        end = (
            start
            + len(self.rows)
            - 1
            if self.rows
            else 0
        )

        e.set_footer(
            text=(
                f"{start}〜{end}"
                f" / {self.total}件"
            )
        )

        return e

    @discord.ui.button(
        label="前へ",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        view = PendingTagView(
            self.owner_id,
            self.page - 1,
        )

        await interaction.response.edit_message(
            embed=view.embed(),
            view=view,
        )

    @discord.ui.button(
        label="次へ",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=2,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        view = PendingTagView(
            self.owner_id,
            self.page + 1,
        )

        await interaction.response.edit_message(
            embed=view.embed(),
            view=view,
        )


class TagMasterView(discord.ui.View):
    def __init__(
        self,
        owner_id: int,
    ):
        super().__init__(
            timeout=900
        )

        self.owner_id = int(owner_id)

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:

        if (
            interaction.user.id
            == self.owner_id
        ):
            return True

        await interaction.response.send_message(
            "この画面は開いた管理者だけが操作できます。",
            ephemeral=True,
        )

        return False

    @discord.ui.button(
        label="未承認タグ",
        emoji="🆕",
        style=discord.ButtonStyle.primary,
    )
    async def pending(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        view = PendingTagView(
            self.owner_id
        )

        await interaction.response.send_message(
            embed=view.embed(),
            view=view,
            ephemeral=True,
        )

    @discord.ui.button(
        label="統合候補",
        emoji="🔗",
        style=discord.ButtonStyle.secondary,
    )
    async def candidates(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        def load():
            with closing(
                get_connection()
            ) as con:
                return merge_candidates(
                    con,
                    25,
                )

        rows = await asyncio.to_thread(
            load
        )

        lines = [
            (
                f"`{x['left_id']}` "
                f"{x['left']} "
                f"↔ "
                f"`{x['right_id']}` "
                f"{x['right']}"
                f"（{x['similarity'] * 100:.1f}%）"
            )
            for x in rows
        ]

        await interaction.response.send_message(
            embed=discord.Embed(
                title="🔗 統合候補",
                description=(
                    "\n".join(lines)
                    or
                    "候補なし"
                ),
                color=0x5865F2,
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="ID指定で統合",
        emoji="🧩",
        style=discord.ButtonStyle.success,
    )
    async def merge(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        await interaction.response.send_modal(
            MergeModal()
        )

    @discord.ui.button(
        label="検索索引を再構築",
        emoji="🔄",
        style=discord.ButtonStyle.secondary,
    )
    async def rebuild(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        def run():
            with closing(
                get_connection()
            ) as con:

                bootstrap_from_existing(
                    con
                )

                result = rebuild_cache(
                    con
                )

                con.commit()

                return result

        result = await asyncio.to_thread(
            run
        )

        await interaction.response.send_message(
            (
                "✅ 検索索引を再構築しました。\n"
                f"代表タグ {result['tags']}件\n"
                f"対応 {result['assignments']}件"
            ),
            ephemeral=True,
        )

    @discord.ui.button(
        label="更新",
        emoji="♻️",
        style=discord.ButtonStyle.secondary,
    )
    async def refresh(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:

        await interaction.response.edit_message(
            embed=_summary_embed(),
            view=self,
        )


async def send_tag_master_panel(
    ctx: Any,
) -> None:

    await ctx.send(
        embed=_summary_embed(),
        view=TagMasterView(
            ctx.author.id
        ),
    )
