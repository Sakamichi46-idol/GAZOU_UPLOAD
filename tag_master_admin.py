from __future__ import annotations

import asyncio
from contextlib import closing
from typing import Any

import discord

from photo_database import get_connection
from tag_master import (
    approve_tag, block_tag, bootstrap_from_existing, diagnostic_summary,
    merge_candidates, merge_tags, rebuild_cache,
)

PAGE_SIZE = 20


def _summary_embed() -> discord.Embed:
    with closing(get_connection()) as con:
        bootstrap_from_existing(con)
        stats = diagnostic_summary(con)
    e = discord.Embed(title="ð·ï¸ ã¿ã°ãã¹ã¿ã¼ç®¡ç", color=0xF1C40F)
    e.description = "åæã¿ã°ãåé¤ãããä»£è¡¨ã¿ã°ã»åç¾©èªã»æ¿èªç¶æã»æ¤ç´¢å¯¾è±¡ãç®¡çãã¾ãã"
    e.add_field(name="ä»£è¡¨ã¿ã°", value=f"{stats['master']:,}ä»¶", inline=True)
    e.add_field(name="æ¿èªæ¸ã¿", value=f"{stats['approved']:,}ä»¶", inline=True)
    e.add_field(name="æªæ¿èª", value=f"{stats['pending']:,}ä»¶", inline=True)
    e.add_field(name="é¤å¤", value=f"{stats['blocked']:,}ä»¶", inline=True)
    e.add_field(name="å¥è¡¨è¨", value=f"{stats['aliases']:,}ä»¶", inline=True)
    e.add_field(name="ä½ä¿¡é ¼å²å½", value=f"{stats['low_confidence']:,}ä»¶", inline=True)
    e.add_field(name="æ¤ç´¢ã­ã£ãã·ã¥", value=f"{stats['cache']:,}ä»¶", inline=True)
    e.set_footer(text="æåã¿ã° > æ¿èªæ¸ã¿AIã¿ã° > æªæ¿èªAIã¿ã° ã®åªåé ä½ã§ãã")
    return e


class MergeModal(discord.ui.Modal, title="ä»£è¡¨ã¿ã°ãçµ±å"):
    source_id = discord.ui.TextInput(label="çµ±ååID", placeholder="ä¾: 125")
    target_id = discord.ui.TextInput(label="çµ±ååID", placeholder="ä¾: 30")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            source = int(str(self.source_id.value).strip())
            target = int(str(self.target_id.value).strip())
            await asyncio.to_thread(_merge, source, target, str(interaction.user.id))
        except Exception as exc:
            await interaction.response.send_message(f"â ï¸ çµ±åã§ãã¾ããã§ãã: {exc}", ephemeral=True)
            return
        await interaction.response.send_message("â åãã¼ã¿ãåé¤ãããå¥åã®åãåãä»£è¡¨ã¿ã°ã¸çµ±åãã¾ããã", ephemeral=True)


def _merge(source: int, target: int, actor: str) -> None:
    with closing(get_connection()) as con:
        merge_tags(con, source, target, actor=actor)
        rebuild_cache(con)
        con.commit()


class PendingTagSelect(discord.ui.Select):
    def __init__(self, owner_id: int, rows: list[Any], action: str):
        self.owner_id = int(owner_id)
        self.action = action
        options = [discord.SelectOption(label=str(r[1])[:100], value=str(r[0]), description=f"{r[2]} / ä½¿ç¨{r[3]}ä»¶"[:100]) for r in rows[:25]]
        super().__init__(placeholder="å¯¾è±¡ã¿ã°ãé¸æ", min_values=1, max_values=min(25, len(options)), options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        ids = [int(v) for v in self.values]
        actor = str(interaction.user.id)
        def run() -> None:
            with closing(get_connection()) as con:
                for mid in ids:
                    if self.action == "approve": approve_tag(con, mid, actor=actor)
                    else: block_tag(con, mid, actor=actor)
                rebuild_cache(con)
                con.commit()
        await asyncio.to_thread(run)
        await interaction.response.send_message(f"â {len(ids)}ä»¶ã{'æ¿èª' if self.action=='approve' else 'æ¤ç´¢å¯¾è±¡å¤'}ã«ãã¾ããã", ephemeral=True)


class PendingTagView(discord.ui.View):
    def __init__(self, owner_id: int, page: int = 0):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.page = max(0, page)
        with closing(get_connection()) as con:
            total = int(con.execute("SELECT COUNT(*) FROM tag_master WHERE status='pending'").fetchone()[0])
            rows = con.execute(
                """SELECT m.id,m.canonical_tag,m.category,
                          (SELECT COUNT(*) FROM tag_aliases a WHERE a.canonical_tag_id=m.id) aliases
                     FROM tag_master m WHERE m.status='pending'
                    ORDER BY aliases DESC,m.id LIMIT ? OFFSET ?""",
                (PAGE_SIZE, self.page * PAGE_SIZE),
            ).fetchall()
        self.total = total
        self.rows = rows
        if rows:
            self.add_item(PendingTagSelect(owner_id, rows, "approve"))
            self.add_item(PendingTagSelect(owner_id, rows, "block"))
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * PAGE_SIZE >= total

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id: return True
        await interaction.response.send_message("ãã®ç»é¢ã¯éããç®¡çèã ããæä½ã§ãã¾ãã", ephemeral=True); return False

    def embed(self) -> discord.Embed:
        start = self.page * PAGE_SIZE + 1 if self.rows else 0
        lines = [f"`{r[0]}` **{r[1]}** â {r[2]} / å¥è¡¨è¨{r[3]}ä»¶" for r in self.rows]
        e = discord.Embed(title="ð æªæ¿èªã¿ã°", description="\n".join(lines) or "æªæ¿èªã¿ã°ã¯ããã¾ããã", color=0xFEE75C)
        e.set_footer(text=f"{start}ã{start+len(self.rows)-1 if self.rows else 0} / {self.total}ä»¶")
        return e

    @discord.ui.button(label="åã¸", emoji="âï¸", style=discord.ButtonStyle.secondary, row=2)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PendingTagView(self.owner_id, self.page - 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)

    @discord.ui.button(label="æ¬¡ã¸", emoji="â¶ï¸", style=discord.ButtonStyle.secondary, row=2)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PendingTagView(self.owner_id, self.page + 1)
        await interaction.response.edit_message(embed=view.embed(), view=view)


class TagMasterView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900); self.owner_id = int(owner_id)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id: return True
        await interaction.response.send_message("ãã®ç»é¢ã¯éããç®¡çèã ããæä½ã§ãã¾ãã", ephemeral=True); return False

    @discord.ui.button(label="æªæ¿èªã¿ã°", emoji="ð", style=discord.ButtonStyle.primary)
    async def pending(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PendingTagView(self.owner_id)
        await interaction.response.send_message(embed=view.embed(), view=view, ephemeral=True)

    @discord.ui.button(label="çµ±ååè£", emoji="ð", style=discord.ButtonStyle.secondary)
    async def candidates(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        def load():
            with closing(get_connection()) as con: return merge_candidates(con, 25)
        rows = await asyncio.to_thread(load)
        lines = [f"`{x['left_id']}` {x['left']} â `{x['right_id']}` {x['right']}ï¼{x['similarity']*100:.1f}%ï¼" for x in rows]
        await interaction.response.send_message(embed=discord.Embed(title="ð çµ±ååè£", description="\n".join(lines) or "åè£ãªã", color=0x5865F2), ephemeral=True)

    @discord.ui.button(label="IDæå®ã§çµ±å", emoji="ð§©", style=discord.ButtonStyle.success)
    async def merge(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(MergeModal())

    @discord.ui.button(label="æ¤ç´¢ç´¢å¼ãåæ§ç¯", emoji="ð", style=discord.ButtonStyle.secondary)
    async def rebuild(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        def run():
            with closing(get_connection()) as con:
                bootstrap_from_existing(con); result = rebuild_cache(con); con.commit(); return result
        result = await asyncio.to_thread(run)
        await interaction.response.send_message(f"â æ¤ç´¢ç´¢å¼ãåæ§ç¯ãã¾ãããä»£è¡¨ã¿ã°{result['tags']}ä»¶ / å¯¾å¿{result['assignments']}ä»¶", ephemeral=True)

    @discord.ui.button(label="æ´æ°", emoji="â»ï¸", style=discord.ButtonStyle.secondary)
    async def refresh(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(embed=_summary_embed(), view=self)


async def send_tag_master_panel(ctx: Any) -> None:
    await ctx.send(embed=_summary_embed(), view=TagMasterView(ctx.author.id))
