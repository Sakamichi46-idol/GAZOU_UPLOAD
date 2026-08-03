from __future__ import annotations

import re
from typing import Iterable

UNKNOWN_OTHER_PREFIX = "その他（名前不明）×"
UNKNOWN_OTHER_LEGACY = {"人物不明", "その他（名前不明）", "名前不明"}
_UNKNOWN_PATTERN = re.compile(r"^その他（名前不明）(?:×|x)?\s*(\d+)?$")


def make_unknown_other_label(count: int) -> str:
    return f"{UNKNOWN_OTHER_PREFIX}{max(1, int(count))}"


def unknown_other_count(value: object) -> int:
    text = str(value or "").strip()
    if not text:
        return 0
    if text in UNKNOWN_OTHER_LEGACY:
        return 1
    match = _UNKNOWN_PATTERN.match(text)
    if not match:
        return 0
    raw = match.group(1)
    return max(1, int(raw)) if raw else 1


def is_unknown_other_label(value: object) -> bool:
    return unknown_other_count(value) > 0


def normalize_people_for_storage(names: Iterable[object]) -> list[str]:
    """名前付き人物を重複排除し、名前不明人数は1つの内部ラベルへ統合する。"""
    result: list[str] = []
    unknown_count = 0
    for value in names:
        text = str(value or "").strip()
        if not text:
            continue
        count = unknown_other_count(text)
        if count:
            unknown_count += count
            continue
        if text not in result:
            result.append(text)
    if unknown_count:
        result.append(make_unknown_other_label(unknown_count))
    return result



def count_people_for_users(names: Iterable[object]) -> int:
    """保存用人物ラベルから、実際に写っている人数を数える。"""
    normalized = normalize_people_for_storage(names)
    total = 0
    for value in normalized:
        unknown_count = unknown_other_count(value)
        total += unknown_count if unknown_count else 1
    return total


def people_items_for_users(names: Iterable[object]) -> list[str]:
    """保存用人物ラベルを一般向けの人物名一覧へ変換する。"""
    normalized = normalize_people_for_storage(names)
    result: list[str] = []
    for value in normalized:
        unknown_count = unknown_other_count(value)
        if unknown_count:
            result.append(f"その他{unknown_count}名")
        else:
            text = str(value or "").strip()
            if text and text not in result:
                result.append(text)
    return result

def format_people_for_users(value: object) -> str:
    """DBの人物文字列を一般利用者向けの自然な表記へ変換する。"""
    text = str(value or "").strip()
    if not text:
        return ""
    raw_items = text.replace("\n", "、").replace(",", "、").replace("，", "、").split("、")
    names: list[str] = []
    unknown_count = 0
    for raw in raw_items:
        item = raw.strip()
        if not item:
            continue
        count = unknown_other_count(item)
        if count:
            unknown_count += count
        elif item not in names:
            names.append(item)
    if unknown_count:
        names.append(f"その他{unknown_count}名")
    return "、".join(names)
