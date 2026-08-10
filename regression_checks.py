"""Offline regression checks for previously observed bugs."""
from __future__ import annotations
import ast
from pathlib import Path
ROOT=Path(__file__).resolve().parent

MOJIBAKE_MARKERS=("ã","ã","ð","â","ï¸","æ¤","ç®")


def run()->dict:
    checks=[]
    py_files=list(ROOT.glob('*.py'))
    for p in py_files:
        try:
            text=p.read_text(encoding='utf-8')
            ast.parse(text)
            checks.append((p.name,True,''))
        except Exception as e:
            checks.append((p.name,False,str(e)))
    text=(ROOT/'photo_review_view.py').read_text(encoding='utf-8')
    checks.append(('review_select_row', 'row=3' in text or 'row = 3' in text, '人物選択Select専用行'))

    # 2026-08-08: 階層式人物選択で「この内容で確定」を押すと、
    # SelectedPeopleView に存在しない self.image_id を参照して AttributeError になった回帰。
    selected_block = text.split('class SelectedPeopleView', 1)[1].split('class BlogBulkConfirmView', 1)[0]
    checks.append((
        'selected_people_confirm_image_id',
        'create_people_snapshot, self.image_id' not in selected_block
        and 'image_id = int(state.review["image_id"])' in text
        and '_commit_selection_state' in selected_block,
        '階層式人物確定はSelectionState.reviewのimage_idを使用',
    ))
    checks.append((
        'selected_people_confirm_helper',
        '_commit_selected_people' in selected_block and 'create_people_snapshot(int(image_id)' in text,
        '通常確定・人物なし・名前不明の確定処理を共通化',
    ))
    checks.append((
        'selected_people_confirm_enabled',
        'self.confirm.disabled = not bool(state.selected_names or state.unknown_other_people)' in selected_block,
        '人物または名前不明人数があれば確定ボタンを有効化',
    ))
    db=(ROOT/'photo_database.py').read_text(encoding='utf-8')
    checks.append(('review_upsert','ON CONFLICT' in db and 'photo_review_queue' in db,'確認キューUPSERT'))
    checks.append((
        'person_confirm_ephemeral_cleanup',
        '_delete_unique_messages' in text
        and 'interaction.delete_original_response()' in text
        and 'interaction.followup.delete_message(message_id)' in text,
        '人物確定後は現在InteractionとメッセージIDの両経路でエフェメラルを削除',
    ))
    checks.append((
        'person_confirm_single_message_flow',
        'selection_message=source_message' in text
        and 'await interaction.edit_original_response(' in text
        and 'selection_message = await interaction.followup.send' not in text,
        '人物選択は別followupを作らず元の人物確認カードを同一メッセージで編集',
    ))
    checks.append((
        'person_confirm_double_tap_guard',
        'commit_lock: asyncio.Lock' in text and 'self._commit_lock = asyncio.Lock()' in text and 'state.committed' in text and 'self._committed' in text,
        '階層式・通常レビューの二重確定防止',
    ))
    checks.append((
        'person_confirm_resource_lock',
        'resource_lock("image_people_confirm"' in text and 'resource_lock("blog_people_confirm"' in text,
        '画像単位・ブログ単位のDB排他ロック',
    ))
    checks.append((
        'person_confirm_snapshot_all_paths',
        '_commit_selection_state' in text and '_commit_selected_people' in text and 'create_people_snapshot' in text,
        '通常・人物なし・名前不明・人物セットを共通スナップショット経路へ統一',
    ))
    checks.append((
        'bulk_confirm_ephemeral_cleanup',
        '_commit_blog_people_with_snapshots' in text and 'for message in list(self.message_by_image_id.values())' in text,
        'ブログ一括確定後もレビュー用エフェメラルを削除',
    ))
    checks.append(('embed_safety',(ROOT/'embed_safety.py').exists(),'Embed安全化'))
    lock_text=(ROOT/'operation_locks.py').read_text(encoding='utf-8') if (ROOT/'operation_locks.py').exists() else ''
    checks.append(('operation_lock_non_reentrant', 'すでに処理中です' in lock_text and 'INSERT OR REPLACE' not in lock_text, '同一管理者の二重操作も拒否'))
    checks.append(('operation_lock_bootstrap', 'CREATE TABLE IF NOT EXISTS photo_operation_locks' in lock_text, '旧DBでもロック表を自己補完'))

    # 過去に実際に発生した文字化けを再発させない。
    bad=[]
    for p in py_files:
        if p.name == 'regression_checks.py':
            continue
        t=p.read_text(encoding='utf-8',errors='replace')
        if any(marker in t for marker in MOJIBAKE_MARKERS):
            bad.append(p.name)
    checks.append(('utf8_mojibake', not bad, '文字化け候補: '+','.join(bad)))

    insights=(ROOT/'admin_insights.py').read_text(encoding='utf-8') if (ROOT/'admin_insights.py').exists() else ''
    for label in ('AIダッシュボード','AI解析キュー','AI使用量レポート','タグ品質レポート','顔認識精度レポート','DB健康診断','システム全体ダッシュボード','AIモデル比較'):
        checks.append((f'insights:{label}', label in insights, label))
    panel=(ROOT/'control_panel.py').read_text(encoding='utf-8')
    checks.append(('insights_panel_entry','photo:admin:ai_insights' in panel,'管理者トップからAIダッシュボードへ接続'))
    checks.append(('priority_queue_order','photo_ai_priority_settings' in db and "reviewed_first" in db,'解析優先順位を未解析キューへ反映'))

    failed=[c for c in checks if not c[1]]
    return {'ok':not failed,'total':len(checks),'failed':failed,'checks':checks}
if __name__=='__main__':
    import json; print(json.dumps(run(),ensure_ascii=False,indent=2))
