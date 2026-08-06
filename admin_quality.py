"""管理者向けヘルプ・診断・整合性修復・エラー履歴。

外部APIを呼ばず、SQLiteと現在ロード済みのBot情報だけで動作する。
"""
from __future__ import annotations

import ast
import hashlib
import logging
import traceback
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from photo_database import get_connection

LOGGER = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_admin_quality_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_admin_error_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                error_id TEXT NOT NULL UNIQUE,
                area TEXT NOT NULL,
                item_name TEXT NOT NULL DEFAULT '',
                user_id INTEGER NOT NULL DEFAULT 0,
                exception_type TEXT NOT NULL DEFAULT '',
                message TEXT NOT NULL DEFAULT '',
                traceback_text TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photo_admin_error_log_created
              ON photo_admin_error_log(created_at DESC);
            """
        )
        con.commit()


def record_admin_error(
    error: BaseException,
    *,
    area: str,
    item_name: str = "",
    user_id: int = 0,
) -> str:
    """例外をDBへ保存し、画面とログで共通利用できる短いIDを返す。"""
    init_admin_quality_schema()
    seed = f"{_now()}:{area}:{item_name}:{type(error).__name__}:{error}"
    error_id = "ADM-" + hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:10].upper()
    tb = "".join(traceback.format_exception(type(error), error, error.__traceback__))[-12000:]
    try:
        with closing(get_connection()) as con:
            con.execute(
                """INSERT OR IGNORE INTO photo_admin_error_log(
                       error_id,area,item_name,user_id,exception_type,message,traceback_text,created_at
                   ) VALUES(?,?,?,?,?,?,?,?)""",
                (
                    error_id,
                    str(area)[:120],
                    str(item_name)[:200],
                    int(user_id or 0),
                    type(error).__name__[:120],
                    str(error)[:2000],
                    tb,
                    _now(),
                ),
            )
            con.commit()
    except Exception:
        LOGGER.exception("管理者エラー履歴の保存に失敗しました")
    return error_id


HELP_TOPICS: dict[str, tuple[str, str, str]] = {
    "top": (
        "👑 管理者パネル",
        "① 目的に合うカテゴリを選ぶ\n② サブ画面で対象を選ぶ\n③ 実行前の件数・警告を確認する\n④ 完了結果またはエラーIDを確認する",
        "危険な一括操作は、必ず対象件数と上書き件数を確認してください。",
    ),
    "people": (
        "👥 人物確認",
        "① 選択式管理またはブログ単位解析を開く\n② 記事・写真を選ぶ\n③ 候補採用、人物選択、手入力などで確定する\n④ 一覧を更新して完了数を確認する",
        "人物名が登録済みなのに未確認なら『状態整合性』を実行します。",
    ),
    "bulk": (
        "✅ 選択式一括確定",
        "① ブログ内写真一覧を開く\n② 『写真を選んで一括確定』を押す\n③ 対象写真を複数選ぶ\n④ 人物構成を指定し、変更前後を確認して確定する",
        "確定済み写真を含む場合は上書きになります。変更履歴から取り消せます。",
    ),
    "ai": (
        "🧠 AI育成センター",
        "① API送信前プレビューで予定枚数を確認する\n② 1枚・5枚・20枚から必要最小限を選ぶ\n③ AI候補確認で採用・修正・却下する\n④ 学習履歴や仮確定を確認する",
        "自動API解析OFFが標準です。手動解析でも日次・月次上限を超えません。",
    ),
    "errors": (
        "⚠️ エラー管理",
        "① 運用ダッシュボードまたは状態・修復を開く\n② エラー種類を確認する\n③ 対象件数を確認して再試行待ちへ戻す\n④ 再試行後に状態を更新する",
        "不正URL・復旧不能は自動再試行しません。画面のエラーIDは履歴で検索できます。",
    ),
    "hidden": (
        "🚫 除外データ管理",
        "① 除外一覧を開く\n② 理由・URL・画像数を確認する\n③ 投稿者を設定して復元、再解析、または完全削除を選ぶ",
        "完全削除は元に戻せません。通常は除外または復元を使ってください。",
    ),
    "maintenance": (
        "🗃️ DB・保守",
        "① ボタン診断で画面構成とコマンドを確認する\n② 状態整合性で人物登録と確認状態を照合する\n③ エラー履歴で直近の例外を確認する\n④ 必要なら変更取り消しを使う",
        "診断はAPIを呼びません。修復は人物データを消さず、確認状態だけを整えます。",
    ),
}


class HelpSelect(discord.ui.Select):
    def __init__(self, owner_id: int):
        self.owner_id = int(owner_id)
        options = [discord.SelectOption(label=v[0], value=k) for k, v in HELP_TOPICS.items()]
        super().__init__(placeholder="知りたい管理機能を選択", options=options)

    async def callback(self, interaction: discord.Interaction) -> None:
        title, steps, note = HELP_TOPICS[self.values[0]]
        embed = discord.Embed(title=title, description=steps, color=0x3498DB)
        embed.add_field(name="💡 注意点", value=note, inline=False)
        await interaction.response.edit_message(embed=embed, view=AdminHelpView(self.owner_id))


class AdminHelpView(discord.ui.View):
    def __init__(self, owner_id: int):
        super().__init__(timeout=900)
        self.owner_id = int(owner_id)
        self.add_item(HelpSelect(owner_id))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.owner_id:
            return True
        await interaction.response.send_message("この案内は開いた管理者だけが操作できます。", ephemeral=True)
        return False

    @discord.ui.button(label="ボタン診断", emoji="🔍", style=discord.ButtonStyle.primary, row=1)
    async def diagnose(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        report = await run_runtime_diagnostics(interaction.client)
        await interaction.edit_original_response(embed=diagnostic_embed(report), view=self)

    @discord.ui.button(label="状態整合性", emoji="🧩", style=discord.ButtonStyle.success, row=1)
    async def consistency(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        before = await _to_thread(get_review_consistency_stats)
        repaired = await _to_thread(repair_review_consistency)
        after = await _to_thread(get_review_consistency_stats)
        embed = discord.Embed(title="🧩 状態整合性チェック", color=0x57F287)
        embed.add_field(name="修復前", value=_format_consistency(before), inline=False)
        embed.add_field(name="修復結果", value=f"更新 **{repaired['updated']}件** / 作成 **{repaired['inserted']}件**", inline=False)
        embed.add_field(name="修復後", value=_format_consistency(after), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="エラー履歴", emoji="🚨", style=discord.ButtonStyle.secondary, row=1)
    async def errors(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        rows = await _to_thread(get_recent_admin_errors, 15)
        embed = discord.Embed(title="🚨 直近の管理画面エラー", color=0xED4245)
        if not rows:
            embed.description = "記録された管理画面エラーはありません。"
        else:
            embed.description = "\n\n".join(
                f"`{r['error_id']}` **{r['area']}**\n{r['exception_type']}: {r['message'][:180]}\n{r['created_at']}"
                for r in rows
            )[:3900]
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @discord.ui.button(label="状態別テスト", emoji="🧪", style=discord.ButtonStyle.secondary, row=1)
    async def state_tests(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        report = await _to_thread(run_static_regression_tests)
        await interaction.response.send_message(embed=regression_embed(report), ephemeral=True)

    @discord.ui.button(label="検証付きバックアップ", emoji="💾", style=discord.ButtonStyle.success, row=2)
    async def verified_backup(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        await interaction.response.defer(ephemeral=True)
        from maintenance_suite import create_verified_backup
        result = await _to_thread(create_verified_backup)
        embed = discord.Embed(title="💾 検証付きDBバックアップ", color=0x57F287)
        embed.add_field(name="保存先", value=f"`{result['path']}`", inline=False)
        embed.add_field(name="サイズ", value=f"{int(result['size']):,} bytes", inline=True)
        embed.add_field(name="整合性", value="✅ 正常", inline=True)
        embed.add_field(name="テーブル数", value=str(result['verify']['tables']), inline=True)
        await interaction.edit_original_response(embed=embed, view=self)

    @discord.ui.button(label="採用機能一覧", emoji="📋", style=discord.ButtonStyle.secondary, row=2)
    async def feature_list(self, interaction: discord.Interaction, _: discord.ui.Button) -> None:
        from maintenance_suite import feature_checklist
        rows = await _to_thread(feature_checklist)
        icons = {'implemented':'✅','partial':'🟡','planned':'❌'}
        text = "\n".join(f"{icons.get(str(r['status']),'•')} {r['label']}" for r in rows)
        await interaction.response.send_message(embed=discord.Embed(title="📋 採用済み機能チェックリスト", description=text[:4000], color=0x5865F2), ephemeral=True)


async def _to_thread(func, *args):
    import asyncio
    return await asyncio.to_thread(func, *args)


def admin_help_embed() -> discord.Embed:
    return discord.Embed(
        title="⚙️ 管理者パネルの使い方・設定",
        description=(
            "知りたい機能をメニューから選んでください。\n"
            "ここから **ボタン診断・状態整合性修復・エラー履歴・回帰テスト** も実行できます。"
        ),
        color=0xE67E22,
    )


async def send_admin_help(interaction: discord.Interaction) -> None:
    init_admin_quality_schema()
    await interaction.response.send_message(
        embed=admin_help_embed(),
        view=AdminHelpView(interaction.user.id),
        ephemeral=True,
    )


REQUIRED_COMMANDS = (
    "status", "photo_archive_status", "ai_status", "photo_storage",
    "feedback_admin", "operations_dashboard", "photo_archive_run",
    "ai_analyze", "face_review", "review_panel", "photo_archive_repair_zero",
    "photo_archive_stop",
)


async def run_runtime_diagnostics(client: discord.Client) -> dict[str, Any]:
    result: dict[str, Any] = {"commands": [], "missing": [], "schema": [], "regression": {}}
    if isinstance(client, commands.Bot):
        for name in REQUIRED_COMMANDS:
            if client.get_command(name) is None:
                result["missing"].append(name)
            else:
                result["commands"].append(name)
    result["schema"] = await _to_thread(check_required_schema)
    result["regression"] = await _to_thread(run_static_regression_tests)
    return result


def check_required_schema() -> list[str]:
    required = {
        "photo_images": {"id", "blog_id", "image_index"},
        "photo_image_people": {"image_id", "person_name", "relation_status"},
        "photo_review_queue": {"image_id", "status", "review_type"},
        "photo_blogs": {"id", "member_name", "group_name"},
    }
    issues: list[str] = []
    with closing(get_connection()) as con:
        for table, columns in required.items():
            exists = con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
            if not exists:
                issues.append(f"テーブル不足: {table}")
                continue
            actual = {str(r[1]) for r in con.execute(f"PRAGMA table_info({table})").fetchall()}
            for column in sorted(columns - actual):
                issues.append(f"列不足: {table}.{column}")
    return issues


def run_static_regression_tests() -> dict[str, Any]:
    root = Path(__file__).resolve().parent
    tests: list[tuple[str, bool, str]] = []

    review_path = root / "photo_review_view.py"
    db_path = root / "photo_database.py"
    control_path = root / "control_panel.py"
    embed_path = root / "embed_safety.py"

    review_text = review_path.read_text(encoding="utf-8")
    db_text = db_path.read_text(encoding="utf-8")
    control_text = control_path.read_text(encoding="utf-8")

    tests.append(("QuickPeopleSelect専用行", "row=3" in review_text[review_text.find("class QuickPeopleSelect"):review_text.find("class FinalPersonConfirmView")], "Selectとボタンの行競合防止"))
    tests.append(("人物確定キューUPSERT", "ON CONFLICT(image_id) DO UPDATE SET" in db_text[db_text.find("def set_confirmed_image_people"):db_text.find("def get_image_people")], "キュー未作成でもcompleted作成"))
    tests.append(("起動時状態修復", "人物登録済みデータから確認状態を自動修復" in db_text, "過去不整合を非破壊修復"))
    tests.append(("Embed安全処理", embed_path.exists() and "safe_add_field" in embed_path.read_text(encoding="utf-8"), "1024文字・総文字数対策"))
    tests.append(("管理者ヘルプ導線", "使い方・設定" in control_text and "send_admin_help" in control_text, "トップから案内を開ける"))
    tests.append(("25件ページング", "前の25" in review_text and "次の25" in review_text, "Discord選択肢上限対策"))

    syntax_errors: list[str] = []
    for path in root.glob("*.py"):
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except Exception as exc:
            syntax_errors.append(f"{path.name}: {exc}")
    tests.append(("全Python構文", not syntax_errors, "; ".join(syntax_errors[:3]) or "構文エラーなし"))

    return {
        "tests": tests,
        "passed": sum(1 for _, ok, _ in tests if ok),
        "total": len(tests),
    }


def diagnostic_embed(report: dict[str, Any]) -> discord.Embed:
    missing = report.get("missing") or []
    schema = report.get("schema") or []
    regression = report.get("regression") or {}
    ok = not missing and not schema and regression.get("passed") == regression.get("total")
    embed = discord.Embed(title="🔍 管理ボタン診断", color=0x57F287 if ok else 0xFEE75C)
    embed.add_field(name="コマンド", value=f"登録済み **{len(report.get('commands') or [])}/{len(REQUIRED_COMMANDS)}**\n" + ("不足なし" if not missing else "不足: " + ", ".join(missing)), inline=False)
    embed.add_field(name="DB構造", value="問題なし" if not schema else "\n".join(schema[:15]), inline=False)
    embed.add_field(name="回帰テスト", value=f"**{regression.get('passed',0)}/{regression.get('total',0)}** 合格", inline=False)
    return embed


def regression_embed(report: dict[str, Any]) -> discord.Embed:
    embed = discord.Embed(title="🧪 状態別・過去バグ回帰テスト", color=0x57F287 if report["passed"] == report["total"] else 0xFEE75C)
    embed.description = "\n".join(f"{'✅' if ok else '❌'} {name} — {detail}" for name, ok, detail in report["tests"])
    embed.set_footer(text=f"合格 {report['passed']}/{report['total']}")
    return embed


def get_review_consistency_stats() -> dict[str, int]:
    with closing(get_connection()) as con:
        confirmed_without_queue = con.execute(
            """SELECT COUNT(DISTINCT pip.image_id) FROM photo_image_people pip
               WHERE pip.relation_status='confirmed'
                 AND NOT EXISTS(SELECT 1 FROM photo_review_queue q WHERE q.image_id=pip.image_id)"""
        ).fetchone()[0]
        confirmed_not_completed = con.execute(
            """SELECT COUNT(DISTINCT pip.image_id) FROM photo_image_people pip
               JOIN photo_review_queue q ON q.image_id=pip.image_id
               WHERE pip.relation_status='confirmed' AND q.status<>'completed'"""
        ).fetchone()[0]
        completed_without_result = con.execute(
            """SELECT COUNT(*) FROM photo_review_queue q
               WHERE q.status='completed'
                 AND COALESCE(q.selected_value,'')=''
                 AND NOT EXISTS(SELECT 1 FROM photo_image_people pip WHERE pip.image_id=q.image_id AND pip.relation_status='confirmed')"""
        ).fetchone()[0]
    return {
        "confirmed_without_queue": int(confirmed_without_queue or 0),
        "confirmed_not_completed": int(confirmed_not_completed or 0),
        "completed_without_result": int(completed_without_result or 0),
    }


def repair_review_consistency() -> dict[str, int]:
    now = _now()
    with closing(get_connection()) as con:
        cur1 = con.execute(
            """UPDATE photo_review_queue
               SET status='completed',
                   selected_value=COALESCE((SELECT GROUP_CONCAT(p.person_name,'、') FROM photo_image_people p WHERE p.image_id=photo_review_queue.image_id AND p.relation_status='confirmed'), selected_value),
                   updated_at=?, reviewed_at=COALESCE(NULLIF(reviewed_at,''), ?)
               WHERE status<>'completed'
                 AND EXISTS(SELECT 1 FROM photo_image_people p WHERE p.image_id=photo_review_queue.image_id AND p.relation_status='confirmed')""",
            (now, now),
        )
        cur2 = con.execute(
            """INSERT INTO photo_review_queue(
                   image_id,review_type,question,candidates,status,reviewed_by,selected_value,review_note,created_at,updated_at,reviewed_at
               )
               SELECT i.id,'person_identity','この写真に写っている人物を確認してください。','',
                      'completed','system_repair',
                      COALESCE((SELECT GROUP_CONCAT(p.person_name,'、') FROM photo_image_people p WHERE p.image_id=i.id AND p.relation_status='confirmed'),''),
                      '状態整合性パネルによる修復',?,?,?
               FROM photo_images i
               WHERE EXISTS(SELECT 1 FROM photo_image_people p WHERE p.image_id=i.id AND p.relation_status='confirmed')
                 AND NOT EXISTS(SELECT 1 FROM photo_review_queue q WHERE q.image_id=i.id)""",
            (now, now, now),
        )
        con.commit()
        return {"updated": max(0, int(cur1.rowcount or 0)), "inserted": max(0, int(cur2.rowcount or 0))}


def _format_consistency(data: dict[str, int]) -> str:
    return (
        f"人物あり・キューなし **{data['confirmed_without_queue']}件**\n"
        f"人物あり・未完了 **{data['confirmed_not_completed']}件**\n"
        f"完了・結果なし **{data['completed_without_result']}件**"
    )


def get_recent_admin_errors(limit: int = 15) -> list[dict[str, Any]]:
    init_admin_quality_schema()
    with closing(get_connection()) as con:
        rows = con.execute(
            "SELECT error_id,area,item_name,exception_type,message,created_at FROM photo_admin_error_log ORDER BY id DESC LIMIT ?",
            (max(1, min(int(limit), 50)),),
        ).fetchall()
        return [dict(r) for r in rows]
