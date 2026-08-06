from __future__ import annotations

import asyncio
import math
from contextlib import closing
from dataclasses import dataclass, field
from typing import Any

import discord

from community_features import FeedbackModal, record_usage_event
from photo_database import add_photo_favorite, get_connection
from photo_search import (
    build_photo_attachment_files,
    close_discord_files,
    get_display_image_url,
)
from person_labels import format_people_for_users, is_unknown_other_label
from sakamichi_members import SAKAMICHI_MEMBERS
from photo_search_tags import (
    SEARCH_CATEGORY_DEFS,
    build_curated_index,
    match_canonical_tag,
)


PAGE_SIZE = 9
OPTIONS_PER_PAGE = 25

CATEGORY_DEFS = SEARCH_CATEGORY_DEFS

RAW_CATEGORY_KEYS = {
    "person", "hair", "clothing", "expression", "location", "composition",
    "pose", "event", "season", "object", "animal", "food", "accessory", "shooting", "other",
}

CATEGORY_ALIASES = {
    "background": "location",
    "weather": "season",
    "person_count": "shooting",
    "composition": "shooting",
    "object": "accessory",
    "manual": "other",
    "": "other",
}


def _normalized_category(category: str) -> str:
    clean = str(category or "").strip().lower()
    clean = CATEGORY_ALIASES.get(clean, clean)
    return clean if clean in CATEGORY_DEFS else "other"


def _short(value: Any, limit: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[: max(0, limit - 1)] + "…"


def _all_image_ids() -> set[int]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id
            FROM photo_images
            WHERE download_status = 'completed'
              AND (local_path != '' OR bucket_key != '')
            """
        ).fetchall()

    return {int(row[0]) for row in rows}


def _load_tag_index() -> dict[str, dict[str, set[int]]]:
    """人物と承認済み代表タグの検索索引を作る。

    タグマスターのキャッシュが空の場合だけ非破壊で再構築する。
    手動タグはAIタグより優先され、低信頼・却下・blockedタグは除外される。
    """
    index: dict[str, dict[str, set[int]]] = {category: {} for category in CATEGORY_DEFS}

    with closing(get_connection()) as connection:
        people = connection.execute(
            """
            SELECT image_id, person_name
            FROM photo_image_people
            WHERE relation_status = 'confirmed'
              AND TRIM(person_name) != ''
            """
        ).fetchall()

        from tag_master import bootstrap_from_existing, rebuild_cache
        bootstrap_from_existing(connection)
        cache_count = int(connection.execute("SELECT COUNT(*) FROM tag_search_cache").fetchone()[0])
        if cache_count <= 0:
            rebuild_cache(connection)

        tag_rows = connection.execute(
            """
            SELECT c.image_id, m.canonical_tag, m.category
            FROM tag_search_cache c
            JOIN tag_master m ON m.id = c.canonical_tag_id
            WHERE m.status = 'approved' AND m.searchable = 1
            """
        ).fetchall()

    for image_id, person_name in people:
        tag = str(person_name).strip()
        if not tag or is_unknown_other_label(tag):
            continue
        index["person"].setdefault(tag, set()).add(int(image_id))

    for image_id, canonical_tag, category in tag_rows:
        normalized_category = _normalized_category(str(category or ""))
        index.setdefault(normalized_category, {}).setdefault(str(canonical_tag), set()).add(int(image_id))

    return index


def _matching_ids(
    all_ids: set[int],
    index: dict[str, dict[str, set[int]]],
    selections: dict[str, set[str]],
    *,
    exclude_category: str | None = None,
    person_match_mode: str = "or",
) -> set[int]:
    result = set(all_ids)

    for category, selected_tags in selections.items():
        if category == exclude_category or not selected_tags:
            continue

        if category == "person" and person_match_mode == "and":
            category_ids = set(all_ids)
            for tag in selected_tags:
                category_ids.intersection_update(
                    index.get(category, {}).get(tag, set())
                )
        else:
            category_ids: set[int] = set()
            for tag in selected_tags:
                category_ids.update(
                    index.get(category, {}).get(tag, set())
                )

        result.intersection_update(category_ids)

        if not result:
            break

    return result


def _option_counts(
    all_ids: set[int],
    index: dict[str, dict[str, set[int]]],
    selections: dict[str, set[str]],
    category: str,
    *,
    person_match_mode: str = "or",
) -> list[tuple[str, int]]:
    base = _matching_ids(
        all_ids,
        index,
        selections,
        exclude_category=category,
        person_match_mode=person_match_mode,
    )

    selected = selections.get(category, set())
    values: list[tuple[str, int]] = []

    for tag, image_ids in index.get(category, {}).items():
        count = len(base.intersection(image_ids))

        if count > 0 or tag in selected:
            values.append((tag, count))

    values.sort(
        key=lambda item: (
            item[0] not in selected,
            -item[1],
            item[0],
        )
    )

    return values


def _load_results(image_ids: set[int]) -> list[dict[str, Any]]:
    if not image_ids:
        return []

    placeholders = ",".join("?" for _ in image_ids)
    params = tuple(sorted(image_ids))

    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT
                photo_images.id,
                photo_images.image_url,
                photo_images.image_index,
                photo_images.local_path,
                photo_blogs.blog_url,
                photo_blogs.group_name,
                photo_blogs.member_name,
                photo_blogs.title,
                photo_blogs.published_at,

                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'confirmed'
                ), '') AS confirmed_people,

                COALESCE((
                    SELECT GROUP_CONCAT(person_name, '、')
                    FROM photo_image_people pip
                    WHERE pip.image_id = photo_images.id
                      AND pip.relation_status = 'candidate'
                ), '') AS candidate_people,

                COALESCE(photo_images.analysis_status, 'pending') AS analysis_status,
                COALESCE(photo_images.bucket_key, '') AS bucket_key,

                COALESCE(photo_ai_analysis.clothing, '') AS clothing,
                COALESCE(photo_ai_analysis.expression, '') AS expression,
                COALESCE(photo_ai_analysis.background, '') AS background,
                COALESCE(photo_ai_analysis.pose, '') AS pose,
                COALESCE(photo_ai_analysis.objects, '') AS objects,

                COALESCE((
                    SELECT GROUP_CONCAT(tag, '、')
                    FROM (
                        SELECT tag
                        FROM photo_ai_tags t
                        WHERE t.image_id = photo_images.id
                        ORDER BY confidence DESC, id ASC
                        LIMIT 15
                    )
                ), '') AS ai_tags,

                COALESCE((
                    SELECT GROUP_CONCAT(tag, '、')
                    FROM (
                        SELECT tag
                        FROM photo_manual_tags m
                        WHERE m.image_id = photo_images.id
                        ORDER BY id ASC
                        LIMIT 15
                    )
                ), '') AS manual_tags

            FROM photo_images

            JOIN photo_blogs
                ON photo_blogs.id = photo_images.blog_id

            LEFT JOIN photo_ai_analysis
                ON photo_ai_analysis.image_id = photo_images.id

            WHERE photo_images.id IN ({placeholders})

            ORDER BY
                photo_blogs.published_at DESC,
                photo_images.image_index ASC,
                photo_images.id DESC
            """,
            params,
        ).fetchall()

    return [dict(row) for row in rows]


@dataclass
class ExplorerState:
    owner_id: int
    person_match_mode: str = "or"

    selections: dict[str, set[str]] = field(
        default_factory=lambda: {
            key: set() for key in CATEGORY_DEFS
        }
    )

    all_ids: set[int] = field(default_factory=set)

    index: dict[str, dict[str, set[int]]] = field(
        default_factory=dict
    )

    @classmethod
    async def create(cls, owner_id: int) -> "ExplorerState":
        all_ids, index = await asyncio.gather(
            asyncio.to_thread(_all_image_ids),
            asyncio.to_thread(_load_tag_index),
        )

        return cls(
            owner_id=owner_id,
            all_ids=all_ids,
            index=index,
        )

    def result_ids(self) -> set[int]:
        return _matching_ids(
            self.all_ids,
            self.index,
            self.selections,
            person_match_mode=self.person_match_mode,
        )

    def selected_lines(self) -> list[str]:
        lines: list[str] = []

        for category, tags in self.selections.items():
            if not tags:
                continue

            emoji, label = CATEGORY_DEFS[category]

            suffix = ""
            if category == "person" and len(tags) >= 2:
                suffix = "（全員）" if self.person_match_mode == "and" else "（いずれか）"

            lines.append(
                f"{emoji} **{label}:** {'・'.join(sorted(tags))}{suffix}"
            )

        return lines

    def clear(self) -> None:
        for tags in self.selections.values():
            tags.clear()


def _zero_result_advice(state: ExplorerState) -> list[str]:
    advice: list[tuple[int, str]] = []
    for category, tags in state.selections.items():
        if not tags:
            continue
        copied = {key: set(values) for key, values in state.selections.items()}
        copied[category].clear()
        count = len(_matching_ids(
            state.all_ids,
            state.index,
            copied,
            person_match_mode=state.person_match_mode,
        ))
        if count:
            emoji, label = CATEGORY_DEFS[category]
            advice.append((count, f"{emoji} {label}を解除 → {count:,}枚"))
    advice.sort(reverse=True)
    return [text for _count, text in advice[:3]]


def _person_matches(state: ExplorerState, query: str) -> list[str]:
    clean = str(query or "").strip().lower()
    if not clean:
        return []
    return [
        name for name in state.index.get("person", {})
        if clean in name.lower()
    ][:20]


class OwnedView(discord.ui.View):
    def __init__(
        self,
        state: ExplorerState,
        *,
        timeout: float = 600,
    ):
        super().__init__(timeout=timeout)
        self.state = state

    async def interaction_check(
        self,
        interaction: discord.Interaction,
    ) -> bool:
        if interaction.user.id != self.state.owner_id:
            await interaction.response.send_message(
                "⚠️ この検索画面は、コマンドを実行した本人だけが操作できます。",
                ephemeral=True,
            )
            return False

        return True

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True


def build_explorer_embed(
    state: ExplorerState,
) -> discord.Embed:
    result_count = len(state.result_ids())
    selected_lines = state.selected_lines()

    embed = discord.Embed(
        title="🔍 写真検索",
        description=(
            "整理済みの検索用タグから条件を絞り込みます。\n"
            "「タグ名入力」では同義語も候補になります。\n"
            "人物は **全員写っている（AND）／いずれか（OR）** を切り替えられます。"
        ),
        color=0x2B90D9,
    )

    embed.add_field(
        name="📊 検索状況",
        value=(
            f"登録画像：**{len(state.all_ids):,}枚**\n"
            f"候補画像：**{result_count:,}枚**"
        ),
        inline=False,
    )

    embed.add_field(
        name="現在の条件",
        value=(
            "\n".join(selected_lines)
            if selected_lines
            else "条件はまだ選択されていません。"
        ),
        inline=False,
    )

    if result_count == 0 and selected_lines:
        advice = _zero_result_advice(state)
        embed.add_field(
            name="⚠️ 該当なし",
            value=(
                "次の条件を解除すると候補が戻ります。\n" + "\n".join(advice)
                if advice else
                "条件の組み合わせに一致する画像がありません。条件を減らしてください。"
            ),
            inline=False,
        )
    else:
        recommendations: list[tuple[str, str, int]] = []
        current_ids = state.result_ids()
        for category, tag_map in state.index.items():
            selected = state.selections.get(category, set())
            for tag, ids in tag_map.items():
                if tag in selected:
                    continue
                count = len(current_ids.intersection(ids))
                if count:
                    recommendations.append((category, tag, count))
        recommendations.sort(key=lambda item: (-item[2], item[1]))
        recommendation_lines = []
        for category, tag, count in recommendations[:5]:
            emoji, _ = CATEGORY_DEFS[category]
            recommendation_lines.append(f"{emoji} {tag} **({count:,})**")
        embed.add_field(
            name="✨ おすすめ",
            value="\n".join(recommendation_lines) if recommendation_lines else "追加できるおすすめ条件はありません。",
            inline=False,
        )

    embed.set_footer(text="候補件数は条件を変更するたびに更新されます。操作期限は10分です。")
    return embed


class PersonNameModal(discord.ui.Modal, title="人物名で検索"):
    person_name = discord.ui.TextInput(
        label="人物名",
        placeholder="例：岩本蓮加（部分一致可）",
        max_length=50,
    )

    def __init__(self, state: ExplorerState):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        matches = _person_matches(self.state, str(self.person_name))
        if not matches:
            await interaction.response.send_message(
                "⚠️ 一致する確認済み人物が見つかりませんでした。",
                ephemeral=True,
            )
            return
        if len(matches) == 1:
            self.state.selections["person"].add(matches[0])
            view = ExplorerView(self.state)
            await interaction.response.edit_message(
                embed=build_explorer_embed(self.state),
                view=view,
            )
            return
        view = PersonMatchView(self.state, matches)
        await interaction.response.send_message(
            "候補から人物を選んでください。",
            view=view,
            ephemeral=True,
        )


class PersonMatchSelect(discord.ui.Select):
    def __init__(self, parent: "PersonMatchView"):
        self.parent_view = parent
        page_matches = parent.page_matches()
        super().__init__(
            placeholder=f"人物を選択（{parent.page + 1}/{parent.page_count}ページ）",
            min_values=1,
            max_values=max(1, len(page_matches)),
            options=[discord.SelectOption(label=_short(name, 100), value=name) for name in page_matches],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        self.parent_view.state.selections["person"].update(self.values)
        await interaction.response.send_message(
            "✅ 人物条件に追加しました。元の検索画面へ戻ってください。",
            ephemeral=True,
        )
        self.parent_view.stop()


class PersonMatchView(OwnedView):
    PAGE_SIZE = 25

    def __init__(self, state: ExplorerState, matches: list[str], page: int = 0):
        super().__init__(state, timeout=180)
        self.matches = list(matches)
        self.page_count = max(1, (len(self.matches) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.add_item(PersonMatchSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def page_matches(self) -> list[str]:
        start = self.page * self.PAGE_SIZE
        return self.matches[start:start + self.PAGE_SIZE]

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"候補から人物を選んでください。表示 {max(1, (self.page - 1) * self.PAGE_SIZE + 1)}〜{min(self.page * self.PAGE_SIZE, len(self.matches))}人目 / 全{len(self.matches)}人",
            view=PersonMatchView(self.state, self.matches, self.page - 1),
        )

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        next_page = self.page + 1
        start = next_page * self.PAGE_SIZE + 1
        end = min((next_page + 1) * self.PAGE_SIZE, len(self.matches))
        await interaction.response.edit_message(
            content=f"候補から人物を選んでください。表示 {start}〜{end}人目 / 全{len(self.matches)}人",
            view=PersonMatchView(self.state, self.matches, next_page),
        )


class SearchTagMatchSelect(discord.ui.Select):
    def __init__(self, parent: "SearchTagMatchView"):
        self.parent_view = parent
        options = []
        for index, (category, label) in enumerate(parent.page_matches()):
            emoji, category_label = CATEGORY_DEFS[category]
            options.append(discord.SelectOption(
                label=_short(label, 85),
                description=f"{category_label}に追加"[:100],
                emoji=emoji,
                value=str(index),
            ))
        super().__init__(
            placeholder="追加する検索タグを選択（複数可）",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        for value in self.values:
            category, label = self.parent_view.page_matches()[int(value)]
            if label in self.parent_view.state.index.get(category, {}):
                self.parent_view.state.selections[category].add(label)
        await interaction.response.send_message(
            "✅ 検索条件へ追加しました。元の検索画面へ戻ってください。",
            ephemeral=True,
        )
        self.parent_view.stop()


class SearchTagMatchView(OwnedView):
    PAGE_SIZE = 25

    def __init__(self, state: ExplorerState, matches: list[tuple[str, str]], page: int = 0):
        super().__init__(state, timeout=180)
        self.matches = list(matches)
        self.page_count = max(1, (len(self.matches) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.add_item(SearchTagMatchSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    def page_matches(self) -> list[tuple[str, str]]:
        start = self.page * self.PAGE_SIZE
        return self.matches[start:start + self.PAGE_SIZE]

    @discord.ui.button(label="前の25件", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"候補からタグを選んでください。（{self.page}/{self.page_count}ページ）",
            view=SearchTagMatchView(self.state, self.matches, self.page - 1),
        )

    @discord.ui.button(label="次の25件", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            content=f"候補からタグを選んでください。（{self.page + 2}/{self.page_count}ページ）",
            view=SearchTagMatchView(self.state, self.matches, self.page + 1),
        )


class SearchTagModal(discord.ui.Modal, title="検索タグを入力"):
    query = discord.ui.TextInput(
        label="タグ名（部分一致可）",
        placeholder="例：海、ロング、顔アップ、ケーキ",
        max_length=50,
    )

    def __init__(self, state: ExplorerState):
        super().__init__()
        self.state = state

    async def on_submit(self, interaction: discord.Interaction) -> None:
        matches = [
            (category, label)
            for category, label in match_canonical_tag(str(self.query))
            if label in self.state.index.get(category, {})
        ]
        if not matches:
            await interaction.response.send_message(
                "⚠️ 一致する検索用タグ、または該当画像が見つかりませんでした。",
                ephemeral=True,
            )
            return
        if len(matches) == 1:
            category, label = matches[0]
            self.state.selections[category].add(label)
            view = ExplorerView(self.state)
            await interaction.response.edit_message(
                embed=build_explorer_embed(self.state),
                view=view,
            )
            return
        await interaction.response.send_message(
            "候補から検索タグを選んでください。",
            view=SearchTagMatchView(self.state, matches),
            ephemeral=True,
        )


class ConditionRemoveSelect(discord.ui.Select):
    def __init__(self, parent: "ConditionRemoveView"):
        self.parent_view = parent
        options = []
        all_keys: list[tuple[str, str]] = []
        for category, tags in parent.state.selections.items():
            for tag in sorted(tags):
                all_keys.append((category, tag))
        start = parent.page * parent.PAGE_SIZE
        self.keys = all_keys[start:start + parent.PAGE_SIZE]
        for index, (category, tag) in enumerate(self.keys):
            emoji, label = CATEGORY_DEFS[category]
            options.append(discord.SelectOption(
                label=_short(tag, 85),
                description=f"{label}から解除"[:100],
                emoji=emoji,
                value=str(index),
            ))
        super().__init__(
            placeholder="解除する条件を選択",
            min_values=1,
            max_values=len(options),
            options=options,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        for value in self.values:
            category, tag = self.keys[int(value)]
            self.parent_view.state.selections[category].discard(tag)
        view = ExplorerView(self.parent_view.state)
        await interaction.response.edit_message(
            embed=build_explorer_embed(self.parent_view.state),
            view=view,
        )


class ConditionRemoveView(OwnedView):
    PAGE_SIZE = 25

    def __init__(self, state: ExplorerState, page: int = 0):
        super().__init__(state)
        self.total = sum(len(tags) for tags in state.selections.values())
        self.page_count = max(1, (self.total + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(int(page), self.page_count - 1))
        self.add_item(ConditionRemoveSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.page_count - 1

    @discord.ui.button(label="前の25件", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=build_explorer_embed(self.state),
            view=ConditionRemoveView(self.state, self.page - 1),
        )

    @discord.ui.button(label="次の25件", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.edit_message(
            embed=build_explorer_embed(self.state),
            view=ConditionRemoveView(self.state, self.page + 1),
        )

    @discord.ui.button(label="戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=2)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ExplorerView(self.state)
        await interaction.response.edit_message(embed=build_explorer_embed(self.state), view=view)




def _master_member_names() -> set[str]:
    return {
        name
        for generations in SAKAMICHI_MEMBERS.values()
        for names in generations.values()
        for name in names
    }


def _available_generation_people(state: ExplorerState, group_name: str, generation_name: str) -> list[str]:
    available = state.index.get("person", {})
    return [
        name for name in SAKAMICHI_MEMBERS.get(group_name, {}).get(generation_name, [])
        if name in available
    ]


def _other_people(state: ExplorerState) -> list[str]:
    master = _master_member_names()
    return sorted(
        name for name in state.index.get("person", {})
        if name not in master and not is_unknown_other_label(name)
    )


class PersonGroupSelect(discord.ui.Select):
    def __init__(self, parent: "PersonGroupView"):
        self.parent_view = parent
        options: list[discord.SelectOption] = []
        available = parent.state.index.get("person", {})
        for group_name, generations in SAKAMICHI_MEMBERS.items():
            count = sum(
                1 for names in generations.values() for name in names
                if name in available
            )
            if count:
                options.append(discord.SelectOption(
                    label=group_name,
                    value=f"group:{group_name}",
                    description=f"検索可能な人物 {count}人",
                    emoji="🌳",
                ))
        other_count = len(_other_people(parent.state))
        if other_count:
            options.append(discord.SelectOption(
                label="その他の人物",
                value="other",
                description=f"坂道メンバー以外 {other_count}人",
                emoji="👤",
            ))
        super().__init__(
            placeholder="グループを選択",
            min_values=1,
            max_values=1,
            options=options or [discord.SelectOption(label="選択可能な人物がありません", value="none")],
            disabled=not bool(options),
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        if value == "other":
            view = PersonMemberView(self.parent_view.state, "その他の人物", "", page=0, other=True)
        else:
            view = PersonGenerationView(self.parent_view.state, value.split(":", 1)[1])
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PersonGroupView(OwnedView):
    def __init__(self, state: ExplorerState):
        super().__init__(state)
        self.add_item(PersonGroupSelect(self))

    def build_embed(self) -> discord.Embed:
        selected = sorted(self.state.selections["person"])
        embed = discord.Embed(
            title="👤 人物を選択",
            description="坂道メンバーは **グループ → 期 → 人物** の順で選択します。\n坂道メンバー以外は「その他の人物」から選べます。",
            color=0x5865F2,
        )
        embed.add_field(name="選択中", value="・".join(selected) if selected else "未選択", inline=False)
        embed.add_field(name="現在の候補画像", value=f"**{len(self.state.result_ids()):,}枚**", inline=False)
        return embed

    @discord.ui.button(label="人物名を入力", emoji="🔤", style=discord.ButtonStyle.secondary, row=1)
    async def input_name(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.send_modal(PersonNameModal(self.state))

    @discord.ui.button(label="人物条件を解除", emoji="🧹", style=discord.ButtonStyle.danger, row=1)
    async def clear_people(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        self.state.selections["person"].clear()
        view = PersonGroupView(self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="検索画面へ戻る", emoji="↩️", style=discord.ButtonStyle.success, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ExplorerView(self.state)
        await interaction.response.edit_message(embed=build_explorer_embed(self.state), view=view)


class PersonGenerationSelect(discord.ui.Select):
    def __init__(self, parent: "PersonGenerationView"):
        self.parent_view = parent
        options = []
        for generation_name in SAKAMICHI_MEMBERS.get(parent.group_name, {}):
            count = len(_available_generation_people(parent.state, parent.group_name, generation_name))
            if count:
                options.append(discord.SelectOption(
                    label=generation_name,
                    value=generation_name,
                    description=f"検索可能な人物 {count}人",
                    emoji="🎓",
                ))
        super().__init__(placeholder="期・区分を選択", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        view = PersonMemberView(self.parent_view.state, self.parent_view.group_name, self.values[0], page=0)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PersonGenerationView(OwnedView):
    def __init__(self, state: ExplorerState, group_name: str):
        super().__init__(state)
        self.group_name = group_name
        self.add_item(PersonGenerationSelect(self))

    def build_embed(self) -> discord.Embed:
        return discord.Embed(
            title=f"👤 {self.group_name}",
            description="期・卒業区分を選択してください。画像に登録されている人物だけが候補に表示されます。",
            color=0x5865F2,
        )

    @discord.ui.button(label="グループへ戻る", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PersonGroupView(self.state)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PersonMemberSelect(discord.ui.Select):
    def __init__(self, parent: "PersonMemberView", visible_names: list[str]):
        self.parent_view = parent
        selected = parent.state.selections["person"]
        super().__init__(
            placeholder="人物を選択（選択済みは✅）",
            min_values=1,
            max_values=len(visible_names),
            options=[
                discord.SelectOption(
                    label=_short(name, 100),
                    value=name,
                    emoji="✅" if name in selected else None,
                ) for name in visible_names
            ],
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        selected = self.parent_view.state.selections["person"]
        for name in self.values:
            if name in selected:
                selected.remove(name)
            else:
                selected.add(name)
        view = PersonMemberView(
            self.parent_view.state,
            self.parent_view.group_name,
            self.parent_view.generation_name,
            page=self.parent_view.page,
            other=self.parent_view.other,
        )
        await interaction.response.edit_message(embed=view.build_embed(), view=view)


class PersonMemberView(OwnedView):
    def __init__(self, state: ExplorerState, group_name: str, generation_name: str, *, page: int = 0, other: bool = False):
        super().__init__(state)
        self.group_name = group_name
        self.generation_name = generation_name
        self.other = other
        self.all_names = _other_people(state) if other else _available_generation_people(state, group_name, generation_name)
        self.max_page = max(0, math.ceil(len(self.all_names) / OPTIONS_PER_PAGE) - 1)
        self.page = max(0, min(page, self.max_page))
        start = self.page * OPTIONS_PER_PAGE
        visible = self.all_names[start:start + OPTIONS_PER_PAGE]
        if visible:
            self.add_item(PersonMemberSelect(self, visible))
        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= self.max_page

    def build_embed(self) -> discord.Embed:
        title = "その他の人物" if self.other else f"{self.group_name} → {self.generation_name}"
        selected = sorted(self.state.selections["person"])
        return discord.Embed(
            title=f"👤 {title}",
            description=(
                "人物を選ぶと条件へ追加され、もう一度選ぶと解除されます。\n"
                f"ページ **{self.page + 1}/{self.max_page + 1}**・候補 **{len(self.all_names)}人**\n"
                f"選択中：{'・'.join(selected) if selected else '未選択'}"
            ),
            color=0x5865F2,
        )

    @discord.ui.button(label="前の25人", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PersonMemberView(self.state, self.group_name, self.generation_name, page=self.page - 1, other=self.other)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="次の25人", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = PersonMemberView(self.state, self.group_name, self.generation_name, page=self.page + 1, other=self.other)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="別の分類を選ぶ", emoji="↩️", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        if self.other:
            view = PersonGroupView(self.state)
        else:
            view = PersonGenerationView(self.state, self.group_name)
        await interaction.response.edit_message(embed=view.build_embed(), view=view)

    @discord.ui.button(label="検索画面へ戻る", emoji="🔍", style=discord.ButtonStyle.success, row=1)
    async def explorer(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ExplorerView(self.state)
        await interaction.response.edit_message(embed=build_explorer_embed(self.state), view=view)


class ExplorerView(OwnedView):
    def __init__(self, state: ExplorerState):
        super().__init__(state)
        self._build_items()

    def _build_items(self) -> None:
        for index_no, (category, (emoji, label)) in enumerate(CATEGORY_DEFS.items()):
            selected_count = len(self.state.selections.get(category, set()))
            tag_count = len(self.state.index.get(category, {}))
            text = f"{label} ({selected_count if selected_count else tag_count})"
            button = discord.ui.Button(
                label=text[:80], emoji=emoji,
                style=discord.ButtonStyle.primary if selected_count else discord.ButtonStyle.secondary,
                custom_id=f"tag_category:{category}", row=index_no // 5,
            )
            async def callback(interaction: discord.Interaction, key: str = category) -> None:
                if key == "person":
                    view = PersonGroupView(self.state)
                else:
                    view = CategoryView(self.state, key, page=0)
                await interaction.response.edit_message(embed=view.build_embed(), view=view)
            button.callback = callback
            self.add_item(button)

        name_button = discord.ui.Button(label="人物名入力", emoji="🔤", style=discord.ButtonStyle.secondary, row=2)
        async def name_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(PersonNameModal(self.state))
        name_button.callback = name_callback
        self.add_item(name_button)

        tag_input_button = discord.ui.Button(label="タグ名入力", emoji="⌨️", style=discord.ButtonStyle.secondary, row=2)
        async def tag_input_callback(interaction: discord.Interaction) -> None:
            await interaction.response.send_modal(SearchTagModal(self.state))
        tag_input_button.callback = tag_input_callback
        self.add_item(tag_input_button)

        mode_label = "人物：全員" if self.state.person_match_mode == "and" else "人物：いずれか"
        mode_button = discord.ui.Button(label=mode_label, emoji="👥", style=discord.ButtonStyle.primary, row=2)
        async def mode_callback(interaction: discord.Interaction) -> None:
            self.state.person_match_mode = "or" if self.state.person_match_mode == "and" else "and"
            view = ExplorerView(self.state)
            await interaction.response.edit_message(embed=build_explorer_embed(self.state), view=view)
        mode_button.callback = mode_callback
        self.add_item(mode_button)

        has_conditions = any(self.state.selections.values())
        remove_button = discord.ui.Button(label="条件を個別解除", emoji="➖", style=discord.ButtonStyle.secondary, row=2, disabled=not has_conditions)
        async def remove_callback(interaction: discord.Interaction) -> None:
            view = ConditionRemoveView(self.state)
            await interaction.response.edit_message(
                embed=discord.Embed(title="➖ 条件を個別解除", description="解除する条件を選択してください。", color=0x5865F2),
                view=view,
            )
        remove_button.callback = remove_callback
        self.add_item(remove_button)

        count = len(self.state.result_ids())
        search_button = discord.ui.Button(label=f"{count:,}枚を検索"[:80], emoji="🔍", style=discord.ButtonStyle.success, row=3, disabled=count == 0)
        async def search_callback(interaction: discord.Interaction) -> None:
            await interaction.response.defer()
            results = await asyncio.to_thread(_load_results, self.state.result_ids())
            view = ResultsView(self.state, results, page=0)
            files = await view.build_files()
            if not files:
                await interaction.edit_original_response(
                    content="⚠️ 検索結果の画像を取得できませんでした。",
                    embeds=[],
                    attachments=[],
                    view=None,
                )
                return
            try:
                await interaction.edit_original_response(
                    content=view.control_content(),
                    embeds=[],
                    attachments=files,
                    view=view,
                )
            finally:
                close_discord_files(files)
        search_button.callback = search_callback
        self.add_item(search_button)

        reset_button = discord.ui.Button(label="全解除", emoji="🧹", style=discord.ButtonStyle.danger, row=3, disabled=not has_conditions)
        async def reset_callback(interaction: discord.Interaction) -> None:
            self.state.clear()
            view = ExplorerView(self.state)
            await interaction.response.edit_message(embed=build_explorer_embed(self.state), view=view)
        reset_button.callback = reset_callback
        self.add_item(reset_button)

        close_button = discord.ui.Button(label="終了", emoji="✖️", style=discord.ButtonStyle.secondary, row=3)
        async def close_callback(interaction: discord.Interaction) -> None:
            for item in self.children:
                item.disabled = True
            await interaction.response.edit_message(view=self)
            self.stop()
        close_button.callback = close_callback
        self.add_item(close_button)


class TagToggleSelect(discord.ui.Select):
    def __init__(
        self,
        parent_view: "CategoryView",
        options: list[discord.SelectOption],
    ):
        self.parent_view = parent_view

        super().__init__(
            placeholder="追加・解除するタグを選択（複数可）",
            min_values=1,
            max_values=max(1, len(options)),
            options=options,
            row=0,
        )

    async def callback(
        self,
        interaction: discord.Interaction,
    ) -> None:
        selected = self.parent_view.state.selections[
            self.parent_view.category
        ]

        for value in self.values:
            try:
                tag = self.parent_view.visible_tags[
                    int(value)
                ]
            except (ValueError, IndexError):
                continue

            if tag in selected:
                selected.remove(tag)
            else:
                selected.add(tag)

        new_view = CategoryView(
            self.parent_view.state,
            self.parent_view.category,
            page=self.parent_view.page,
        )

        await interaction.response.edit_message(
            embed=new_view.build_embed(),
            view=new_view,
        )


class CategoryView(OwnedView):
    def __init__(
        self,
        state: ExplorerState,
        category: str,
        page: int = 0,
    ):
        super().__init__(state)

        self.category = category
        self.page = max(0, page)

        self.options_data = _option_counts(
            state.all_ids,
            state.index,
            state.selections,
            category,
            person_match_mode=state.person_match_mode,
        )

        max_page = max(
            0,
            math.ceil(
                len(self.options_data) / OPTIONS_PER_PAGE
            ) - 1,
        )

        self.page = min(
            self.page,
            max_page,
        )

        start = self.page * OPTIONS_PER_PAGE

        visible = self.options_data[
            start : start + OPTIONS_PER_PAGE
        ]

        self.visible_tags = [
            tag for tag, _count in visible
        ]

        selected = state.selections[category]

        if visible:
            options = [
                discord.SelectOption(
                    label=_short(tag, 85),
                    value=str(index),
                    description=f"該当 {count:,}件"[:100],
                    emoji="✅" if tag in selected else None,
                )
                for index, (tag, count) in enumerate(visible)
            ]

            self.add_item(
                TagToggleSelect(
                    self,
                    options,
                )
            )

        self.previous.disabled = self.page <= 0
        self.next.disabled = self.page >= max_page
        self.clear_category.disabled = not bool(selected)

    def build_embed(self) -> discord.Embed:
        emoji, label = CATEGORY_DEFS[self.category]

        selected = sorted(
            self.state.selections[self.category]
        )

        total_pages = max(
            1,
            math.ceil(
                len(self.options_data) / OPTIONS_PER_PAGE
            ),
        )

        embed = discord.Embed(
            title=f"{emoji} {label}",
            description=(
                "タグを選ぶと追加、もう一度選ぶと解除されます。\n"
                "複数をまとめて選択できます。"
            ),
            color=0x5865F2,
        )

        embed.add_field(
            name="このカテゴリーの選択",
            value=(
                "・".join(selected)
                if selected
                else "未選択"
            ),
            inline=False,
        )

        embed.add_field(
            name="現在の候補画像",
            value=f"**{len(self.state.result_ids()):,}枚**",
            inline=True,
        )

        embed.add_field(
            name="ページ",
            value=f"**{self.page + 1}/{total_pages}**",
            inline=True,
        )

        if not self.options_data:
            embed.add_field(
                name="タグ",
                value=(
                    "このカテゴリーには"
                    "利用できるタグがありません。"
                ),
                inline=False,
            )

        embed.set_footer(
            text=(
                "各タグの件数は、ほかのカテゴリーで"
                "選んだ条件を反映しています。"
            )
        )

        return embed

    @discord.ui.button(
        label="前へ",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = CategoryView(
            self.state,
            self.category,
            self.page - 1,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="次へ",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = CategoryView(
            self.state,
            self.category,
            self.page + 1,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="この分類を解除",
        emoji="🧹",
        style=discord.ButtonStyle.danger,
        row=1,
    )
    async def clear_category(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        self.state.selections[
            self.category
        ].clear()

        view = CategoryView(
            self.state,
            self.category,
            0,
        )

        await interaction.response.edit_message(
            embed=view.build_embed(),
            view=view,
        )

    @discord.ui.button(
        label="Explorerへ戻る",
        emoji="↩️",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            embed=build_explorer_embed(self.state),
            view=view,
        )


class TagResultSelect(discord.ui.Select):
    """現在ページの1〜9枚目から詳細表示する写真を選ぶ。"""

    def __init__(self, parent_view: "ResultsView") -> None:
        self.parent_view = parent_view
        options = [
            discord.SelectOption(
                label=f"{offset + 1}枚目の詳細",
                description=f"タグ検索結果の{parent_view.page * PAGE_SIZE + offset + 1}件目",
                value=str(offset),
                emoji="📷",
            )
            for offset, _result in enumerate(parent_view.current_results())
        ]
        super().__init__(
            placeholder="詳細を見る写真を選択",
            min_values=1,
            max_values=1,
            options=options,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction) -> None:
        try:
            item_offset = int(self.values[0])
        except (ValueError, IndexError):
            await interaction.response.send_message(
                "⚠️ 選択した写真を取得できませんでした。",
                ephemeral=True,
            )
            return

        index = self.parent_view.page * PAGE_SIZE + item_offset
        if index >= len(self.parent_view.results):
            await interaction.response.send_message(
                "⚠️ 選択した写真を取得できませんでした。",
                ephemeral=True,
            )
            return

        view = DetailView(
            self.parent_view.state,
            self.parent_view.results,
            index=index,
            return_page=self.parent_view.page,
        )
        await interaction.response.edit_message(
            content=None,
            embeds=[view.build_embed()],
            attachments=[],
            view=view,
        )


class ResultsView(OwnedView):
    """タグ検索結果を1ページ9枚ずつ、1つのメッセージにまとめて表示する。"""

    def __init__(
        self,
        state: ExplorerState,
        results: list[dict[str, Any]],
        page: int = 0,
    ):
        super().__init__(state)
        self.results = results

        max_page = max(0, math.ceil(len(results) / PAGE_SIZE) - 1)
        self.page = max(0, min(page, max_page))

        self.add_item(TagResultSelect(self))
        self.previous.disabled = self.page <= 0
        self.next.disabled = (self.page + 1) * PAGE_SIZE >= len(self.results)

    def current_results(self) -> list[dict[str, Any]]:
        start = self.page * PAGE_SIZE
        return self.results[start : start + PAGE_SIZE]

    async def build_files(self) -> list[discord.File]:
        """現在ページの最大9枚を、Discordの同時添付用ファイルに変換する。"""
        return await build_photo_attachment_files(self.current_results())

    def control_content(self) -> str:
        start = self.page * PAGE_SIZE + 1
        end = min(len(self.results), start + PAGE_SIZE - 1)
        return (
            "🔍 **タグ検索結果**\n"
            f"取得件数: **{len(self.results)}件**\n"
            f"現在表示: **{start}〜{end}件目**（最大9枚を1セットで表示）"
        )

    async def _change_page(self, interaction: discord.Interaction, page: int) -> None:
        # 最大9枚の取得に時間がかかってもInteractionを失効させない。
        await interaction.response.defer()
        view = ResultsView(self.state, self.results, page)
        files = await view.build_files()
        if not files:
            await interaction.followup.send(
                "⚠️ 次のページの画像を取得できませんでした。",
                ephemeral=True,
            )
            return
        try:
            await interaction.edit_original_response(
                content=view.control_content(),
                embeds=[],
                attachments=files,
                view=view,
            )
        finally:
            close_discord_files(files)

    @discord.ui.button(label="前の9枚", emoji="◀️", style=discord.ButtonStyle.secondary, row=1)
    async def previous(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._change_page(interaction, self.page - 1)

    @discord.ui.button(label="次の9枚", emoji="▶️", style=discord.ButtonStyle.secondary, row=1)
    async def next(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await self._change_page(interaction, self.page + 1)

    @discord.ui.button(label="条件変更", emoji="🔧", style=discord.ButtonStyle.success, row=1)
    async def explorer(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        view = ExplorerView(self.state)
        await interaction.response.edit_message(
            content=None,
            embeds=[build_explorer_embed(self.state)],
            attachments=[],
            view=view,
        )


class DetailView(OwnedView):
    def __init__(
        self,
        state: ExplorerState,
        results: list[dict[str, Any]],
        *,
        index: int,
        return_page: int,
    ):
        super().__init__(state)

        self.results = results

        self.index = max(
            0,
            min(
                index,
                len(results) - 1,
            ),
        )

        self.return_page = return_page

        self.previous.disabled = self.index <= 0

        self.next.disabled = (
            self.index >= len(results) - 1
        )

    def build_embed(self) -> discord.Embed:
        result = self.results[self.index]

        embed = discord.Embed(
            title="📷 写真の詳細",
            url=result.get("blog_url") or None,
            color=0xF1C40F,
        )

        embed.add_field(
            name="📝 ブログタイトル",
            value=_short(result.get("title") or "無題", 1024),
            inline=False,
        )

        embed.add_field(
            name="🖼️ 画像ID",
            value=str(result.get("id")),
            inline=True,
        )

        embed.add_field(
            name="🏷️ グループ",
            value=result.get("group_name") or "不明",
            inline=True,
        )

        embed.add_field(
            name="✍️ 投稿者",
            value=result.get("member_name") or "不明",
            inline=True,
        )

        confirmed = str(result.get("confirmed_people") or "").strip()
        candidates = str(result.get("candidate_people") or "").strip()
        display_people = format_people_for_users(confirmed or candidates)
        embed.add_field(
            name="👤 写っている人物",
            value=display_people or "未確定",
            inline=False,
        )
        embed.add_field(
            name="🔐 人物確認状態",
            value="✅ 人による確認済み" if confirmed else "⚠️ AI判定・確認待ち",
            inline=False,
        )

        details: list[str] = []

        for label, key in (
            ("服装", "clothing"),
            ("表情", "expression"),
            ("場所・背景", "background"),
            ("ポーズ", "pose"),
            ("小物", "objects"),
        ):
            value = str(
                result.get(key) or ""
            ).strip()

            if value:
                details.append(
                    f"**{label}:** {value}"
                )

        if details:
            embed.add_field(
                name="🔎 AI解析",
                value=_short(
                    "\n".join(details),
                    1024,
                ),
                inline=False,
            )

        tag_lines: list[str] = []

        if result.get("ai_tags"):
            tag_lines.append(
                f"**AI:** {result['ai_tags']}"
            )

        if result.get("manual_tags"):
            tag_lines.append(
                f"**手動:** {result['manual_tags']}"
            )

        if tag_lines:
            embed.add_field(
                name="🏷️ タグ",
                value=_short(
                    "\n".join(tag_lines),
                    1024,
                ),
                inline=False,
            )

        embed.add_field(
            name="📅 投稿日時",
            value=result.get("published_at") or "不明",
            inline=False,
        )

        image_url = get_display_image_url(result)

        if image_url:
            embed.set_image(url=image_url)

        embed.set_footer(
            text=(
                f"検索結果 "
                f"{self.index + 1}/{len(self.results)}"
                f"・記事内 "
                f"{result.get('image_index', 0)}枚目"
            )
        )

        return embed

    @discord.ui.button(
        label="前の画像",
        emoji="◀️",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def previous(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = DetailView(
            self.state,
            self.results,
            index=self.index - 1,
            return_page=self.return_page,
        )

        await interaction.response.edit_message(
            embeds=[view.build_embed()],
            view=view,
        )

    @discord.ui.button(
        label="次の画像",
        emoji="▶️",
        style=discord.ButtonStyle.secondary,
        row=0,
    )
    async def next(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = DetailView(
            self.state,
            self.results,
            index=self.index + 1,
            return_page=self.return_page,
        )

        await interaction.response.edit_message(
            embeds=[view.build_embed()],
            view=view,
        )

    @discord.ui.button(
        label="お気に入り登録",
        emoji="⭐",
        style=discord.ButtonStyle.success,
        row=1,
    )
    async def favorite(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        result = self.results[self.index]

        try:
            image_id = int(result.get("id", 0))
        except (TypeError, ValueError):
            image_id = 0

        if image_id <= 0:
            await interaction.response.send_message(
                "⚠️ この写真の画像IDを取得できませんでした。",
                ephemeral=True,
            )
            return

        try:
            added = await asyncio.to_thread(
                add_photo_favorite,
                image_id,
                interaction.user.id,
            )
            if added:
                await asyncio.to_thread(
                    record_usage_event,
                    interaction.user.id,
                    "favorite",
                    image_id=image_id,
                )
        except Exception as error:
            print("タグ検索画面のお気に入り登録エラー:", error)
            await interaction.response.send_message(
                "⚠️ お気に入り登録中にエラーが発生しました。",
                ephemeral=True,
            )
            return

        if added:
            message = f"⭐ 画像ID **{image_id}** をお気に入りに登録しました。"
        else:
            message = f"⭐ 画像ID **{image_id}** はすでにお気に入り登録済みです。"

        await interaction.response.send_message(
            message,
            ephemeral=True,
        )

    @discord.ui.button(
        label="この写真を報告",
        emoji="⚠️",
        style=discord.ButtonStyle.secondary,
        row=1,
    )
    async def report(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        image_id = int(self.results[self.index].get("id") or 0)
        await interaction.response.send_modal(
            FeedbackModal(category="人物名・写真情報の間違い", image_id=image_id)
        )

    @discord.ui.button(
        label="一覧へ戻る",
        emoji="🗂️",
        style=discord.ButtonStyle.primary,
        row=0,
    )
    async def list_back(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        await interaction.response.defer()
        view = ResultsView(
            self.state,
            self.results,
            self.index // PAGE_SIZE,
        )

        files = await view.build_files()
        if not files:
            await interaction.followup.send(
                "⚠️ 一覧の画像を取得できませんでした。",
                ephemeral=True,
            )
            return
        try:
            await interaction.edit_original_response(
                content=view.control_content(),
                embeds=[],
                attachments=files,
                view=view,
            )
        finally:
            close_discord_files(files)

    @discord.ui.button(
        label="条件変更",
        emoji="🔧",
        style=discord.ButtonStyle.success,
        row=0,
    )
    async def explorer(
        self,
        interaction: discord.Interaction,
        _: discord.ui.Button,
    ) -> None:
        view = ExplorerView(self.state)

        await interaction.response.edit_message(
            content=None,
            embeds=[build_explorer_embed(self.state)],
            attachments=[],
            view=view,
        )


def get_tag_category_summary() -> dict[str, list[tuple[str, int]]]:
    """DBに存在するタグを、検索画面と同じカテゴリー規則で集計する。"""
    index = _load_tag_index()
    summary: dict[str, list[tuple[str, int]]] = {}
    for category in CATEGORY_DEFS:
        values = [
            (tag, len(image_ids))
            for tag, image_ids in index.get(category, {}).items()
        ]
        values.sort(key=lambda item: (-item[1], item[0]))
        summary[category] = values
    return summary


def get_uncategorized_tag_summary() -> list[tuple[str, str, int]]:
    """空欄・manual・未知カテゴリーのタグと使用画像数を返す。"""
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT raw_category, tag, COUNT(DISTINCT image_id) AS image_count
            FROM (
                SELECT image_id, TRIM(category) AS raw_category, TRIM(tag) AS tag
                FROM photo_ai_tags
                WHERE TRIM(tag) != ''
                UNION ALL
                SELECT image_id, TRIM(category) AS raw_category, TRIM(tag) AS tag
                FROM photo_manual_tags
                WHERE TRIM(tag) != ''
            )
            WHERE LOWER(raw_category) = 'manual'
               OR raw_category = ''
               OR (
                    LOWER(raw_category) NOT IN ({known})
                    AND LOWER(raw_category) NOT IN ({aliases})
               )
            GROUP BY raw_category, tag
            ORDER BY image_count DESC, tag
            """.format(
                known=','.join('?' for _ in RAW_CATEGORY_KEYS),
                aliases=','.join('?' for _ in CATEGORY_ALIASES if _),
            ),
            tuple(RAW_CATEGORY_KEYS) + tuple(key for key in CATEGORY_ALIASES if key),
        ).fetchall()

    return [
        (str(row[0] or ''), str(row[1] or ''), int(row[2] or 0))
        for row in rows
    ]


async def send_photo_tag_explorer(ctx) -> None:
    message = await ctx.send(
        "🔄 タグ情報を読み込んでいます…"
    )

    try:
        state = await ExplorerState.create(
            ctx.author.id
        )

        view = ExplorerView(state)

        await message.edit(
            content=None,
            embed=build_explorer_embed(state),
            view=view,
        )

    except Exception as error:
        await message.edit(
            content=(
                "⚠️ タグ検索画面の作成に失敗しました。\n"
                f"`{type(error).__name__}: {error}`"
            )
        )
