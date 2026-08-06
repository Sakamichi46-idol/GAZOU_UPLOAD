"""写真アーカイブに登録されている全タグのファイル出力。

OpenAI APIや外部通信は使わず、SQLiteの ``photo_ai_tags`` と
``photo_manual_tags`` を読み取り、重複のないタグ一覧をZIPで返す。
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from photo_database import get_connection

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class TagExportRow:
    tag: str
    source: str
    category: str
    image_count: int
    assignment_count: int
    average_confidence: float | None
    maximum_confidence: float | None
    first_registered_at: str
    last_registered_at: str


def _table_exists(connection: Any, table_name: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (table_name,),
    ).fetchone()
    return row is not None


def collect_all_tag_rows() -> list[TagExportRow]:
    """AIタグと手動タグを、省略せずタグ単位で集計して返す。"""
    rows: list[TagExportRow] = []
    with closing(get_connection()) as connection:
        if _table_exists(connection, "photo_ai_tags"):
            ai_rows = connection.execute(
                """
                SELECT
                    TRIM(tag) AS tag,
                    'AI' AS source,
                    COALESCE(NULLIF(TRIM(category), ''), '未分類') AS category,
                    COUNT(DISTINCT image_id) AS image_count,
                    COUNT(*) AS assignment_count,
                    AVG(confidence) AS average_confidence,
                    MAX(confidence) AS maximum_confidence,
                    COALESCE(MIN(created_at), '') AS first_registered_at,
                    COALESCE(MAX(updated_at), MAX(created_at), '') AS last_registered_at
                FROM photo_ai_tags
                WHERE TRIM(COALESCE(tag, '')) <> ''
                GROUP BY TRIM(tag), COALESCE(NULLIF(TRIM(category), ''), '未分類')
                """
            ).fetchall()
            for row in ai_rows:
                rows.append(
                    TagExportRow(
                        tag=str(row["tag"]),
                        source="AI",
                        category=str(row["category"]),
                        image_count=int(row["image_count"] or 0),
                        assignment_count=int(row["assignment_count"] or 0),
                        average_confidence=float(row["average_confidence"]) if row["average_confidence"] is not None else None,
                        maximum_confidence=float(row["maximum_confidence"]) if row["maximum_confidence"] is not None else None,
                        first_registered_at=str(row["first_registered_at"] or ""),
                        last_registered_at=str(row["last_registered_at"] or ""),
                    )
                )

        if _table_exists(connection, "photo_manual_tags"):
            manual_rows = connection.execute(
                """
                SELECT
                    TRIM(tag) AS tag,
                    '手動' AS source,
                    COALESCE(NULLIF(TRIM(category), ''), '未分類') AS category,
                    COUNT(DISTINCT image_id) AS image_count,
                    COUNT(*) AS assignment_count,
                    COALESCE(MIN(created_at), '') AS first_registered_at,
                    COALESCE(MAX(updated_at), MAX(created_at), '') AS last_registered_at
                FROM photo_manual_tags
                WHERE TRIM(COALESCE(tag, '')) <> ''
                GROUP BY TRIM(tag), COALESCE(NULLIF(TRIM(category), ''), '未分類')
                """
            ).fetchall()
            for row in manual_rows:
                rows.append(
                    TagExportRow(
                        tag=str(row["tag"]),
                        source="手動",
                        category=str(row["category"]),
                        image_count=int(row["image_count"] or 0),
                        assignment_count=int(row["assignment_count"] or 0),
                        average_confidence=None,
                        maximum_confidence=None,
                        first_registered_at=str(row["first_registered_at"] or ""),
                        last_registered_at=str(row["last_registered_at"] or ""),
                    )
                )

    rows.sort(key=lambda item: (item.category.casefold(), item.tag.casefold(), item.source))
    return rows


def _format_confidence(value: float | None) -> str:
    if value is None:
        return ""
    # DBが0〜1と0〜100のどちらでも、保存値を勝手に変換せず明示する。
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _build_csv(rows: list[TagExportRow]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(
        [
            "タグ",
            "出所",
            "カテゴリー",
            "使用画像数",
            "登録件数",
            "平均信頼度（DB保存値）",
            "最大信頼度（DB保存値）",
            "初回登録日時",
            "最終更新日時",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.tag,
                row.source,
                row.category,
                row.image_count,
                row.assignment_count,
                _format_confidence(row.average_confidence),
                _format_confidence(row.maximum_confidence),
                row.first_registered_at,
                row.last_registered_at,
            ]
        )
    # Excelでも文字化けしにくいUTF-8 BOM付き。
    return stream.getvalue().encode("utf-8-sig")


def _build_text(rows: list[TagExportRow], generated_at: str) -> bytes:
    unique_names = sorted({row.tag for row in rows}, key=str.casefold)
    ai_count = sum(1 for row in rows if row.source == "AI")
    manual_count = sum(1 for row in rows if row.source == "手動")

    lines = [
        "写真アーカイブ・全タグ一覧",
        f"出力日時（UTC）: {generated_at}",
        f"重複を除いたタグ名: {len(unique_names)}件",
        f"出所・カテゴリー別の集計行: {len(rows)}件（AI {ai_count}件 / 手動 {manual_count}件）",
        "",
        "【タグ名のみ・重複なし】",
    ]
    lines.extend(unique_names)
    lines.extend(["", "【出所・カテゴリー・使用数付き】"])

    current_category: str | None = None
    for row in rows:
        if row.category != current_category:
            current_category = row.category
            lines.extend(["", f"■ {current_category}"])
        confidence = ""
        if row.source == "AI" and row.average_confidence is not None:
            confidence = f" / 平均信頼度={_format_confidence(row.average_confidence)}"
        lines.append(
            f"・{row.tag} [{row.source}] / {row.image_count}枚 / 登録{row.assignment_count}件{confidence}"
        )

    return ("\n".join(lines) + "\n").encode("utf-8-sig")


def _rows_to_csv(headers: list[str], rows: list[list[Any]]) -> bytes:
    stream = io.StringIO(newline="")
    writer = csv.writer(stream)
    writer.writerow(headers)
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8-sig")


def _build_master_exports() -> dict[str, bytes]:
    from tag_master import bootstrap_from_existing, diagnostic_summary, merge_candidates, register_sqlite_functions
    with closing(get_connection()) as connection:
        bootstrap_from_existing(connection)
        register_sqlite_functions(connection)
        masters = connection.execute(
            """SELECT m.id,m.canonical_tag,m.category,m.status,m.searchable,m.minimum_confidence,
                      COUNT(DISTINCT a.alias_key) alias_count,
                      COUNT(DISTINCT c.image_id) image_count
                 FROM tag_master m
                 LEFT JOIN tag_aliases a ON a.canonical_tag_id=m.id
                 LEFT JOIN tag_search_cache c ON c.canonical_tag_id=m.id
                GROUP BY m.id ORDER BY m.category,m.canonical_tag"""
        ).fetchall()
        aliases = connection.execute(
            """SELECT a.alias_tag,m.canonical_tag,m.category,m.status
                 FROM tag_aliases a JOIN tag_master m ON m.id=a.canonical_tag_id
                ORDER BY m.canonical_tag,a.alias_tag"""
        ).fetchall()
        issues = connection.execute(
            """SELECT t.tag,t.category,t.confidence,m.canonical_tag,m.minimum_confidence,
                      CASE WHEN m.status='blocked' THEN '無意味・禁止'
                           WHEN t.confidence < m.minimum_confidence THEN '低信頼'
                           WHEN m.status='pending' THEN '未承認'
                           ELSE '' END issue,
                      COUNT(*) assignments
                 FROM photo_ai_tags t
                 JOIN tag_aliases a ON a.alias_key=tag_normalized_key(t.tag)
                 JOIN tag_master m ON m.id=a.canonical_tag_id
                WHERE m.status IN ('blocked','pending') OR t.confidence < m.minimum_confidence
                GROUP BY t.tag,t.category,t.confidence,m.canonical_tag,m.minimum_confidence,issue
                ORDER BY issue,assignments DESC"""
        ).fetchall()
        merges = merge_candidates(connection, 500)
        summary = diagnostic_summary(connection)

    return {
        "canonical_tags.csv": _rows_to_csv(
            ["ID","代表タグ","カテゴリー","状態","検索対象","最低信頼度","別名数","検索対象画像数"],
            [[*row] for row in masters],
        ),
        "tag_aliases.csv": _rows_to_csv(
            ["別表記","代表タグ","カテゴリー","状態"], [[*row] for row in aliases]
        ),
        "tag_quality_issues.csv": _rows_to_csv(
            ["原文タグ","原カテゴリー","信頼度","代表タグ","最低信頼度","問題","登録件数"],
            [[*row] for row in issues],
        ),
        "merge_candidates.csv": _rows_to_csv(
            ["統合元ID","統合元","統合先ID","統合先","カテゴリー","類似度"],
            [[x["left_id"],x["left"],x["right_id"],x["right"],x["category"],f'{x["similarity"]:.4f}'] for x in merges],
        ),
        "tag_master_summary.json": __import__("json").dumps(summary, ensure_ascii=False, indent=2).encode("utf-8"),
    }


def build_all_tags_export_zip() -> tuple[bytes, str, dict[str, int]]:
    """原文・代表タグ・品質問題・統合候補をZIPとして返す。"""
    rows = collect_all_tag_rows()
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    date_part = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("raw_tags.txt", _build_text(rows, generated_at))
        archive.writestr("raw_tags.csv", _build_csv(rows))
        for name, data in _build_master_exports().items():
            archive.writestr(name, data)
        archive.writestr(
            "README.txt",
            (
                "raw_tags.*: DBに保存された原文タグの集計\n"
                "canonical_tags.csv: 代表タグ・承認状態・検索対象件数\n"
                "tag_aliases.csv: 同義語・表記揺れと代表タグの対応\n"
                "tag_quality_issues.csv: 無意味・未承認・低信頼タグ候補\n"
                "merge_candidates.csv: 非破壊統合の候補\n"
                "tag_master_summary.json: タグマスター全体の集計\n"
            ).encode("utf-8-sig"),
        )

    unique_names = {row.tag for row in rows}
    stats = {
        "unique_tags": len(unique_names),
        "summary_rows": len(rows),
        "ai_rows": sum(1 for row in rows if row.source == "AI"),
        "manual_rows": sum(1 for row in rows if row.source == "手動"),
    }
    return output.getvalue(), f"photo_archive_all_tags_{date_part}.zip", stats

