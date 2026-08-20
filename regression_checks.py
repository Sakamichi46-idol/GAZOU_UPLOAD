"""Offline regression checks for previously observed bugs."""
from __future__ import annotations
import ast
from pathlib import Path
ROOT=Path(__file__).resolve().parent

MOJIBAKE_MARKERS=("ã","ã","ð","â","ï¸","æ¤","ç®")

def check_face_candidate_diagnostics() -> list[str]:
    errors: list[str] = []
    local_text = Path(__file__).with_name("local_face_recognition.py").read_text(encoding="utf-8")
    diag_text = Path(__file__).with_name("face_candidate_diagnostics.py").read_text(encoding="utf-8")
    ai_text = Path(__file__).with_name("ai_center.py").read_text(encoding="utf-8")
    if "def diagnose_face_candidates(" not in local_text:
        errors.append("diagnose_face_candidates がありません")
    if "INTEGRATED_CANDIDATE_THRESHOLD" not in diag_text or "diagnose_recent" not in diag_text:
        errors.append("顔候補診断モジュールが不完全です")
    if 'label="顔候補診断"' not in ai_text:
        errors.append("AI管理に顔候補診断ボタンがありません")
    if "OpenAI APIを使わず" not in diag_text and "OpenAI APIは使用しません" not in diag_text:
        errors.append("API不使用の診断表示がありません")
    face_callback = ai_text.split("async def face_diagnostics", 1)[1].split("@discord.ui.button", 1)[0]
    defer_pos = face_callback.find(".defer(")
    import_pos = face_callback.find("from face_candidate_diagnostics import")
    if defer_pos < 0 or import_pos < 0 or defer_pos > import_pos:
        errors.append("AI管理の顔候補診断callbackがimport前にdeferしていません")
    if "from local_face_recognition import diagnose_face_candidates" in diag_text.split("def _load_face_diagnostics", 1)[0]:
        errors.append("顔候補診断がlocal_face_recognitionをトップレベルimportしています")
    if "def _load_face_diagnostics" not in diag_text:
        errors.append("顔認識の遅延importヘルパーがありません")
    if "await interaction.edit_original_response" not in diag_text:
        errors.append("defer後の診断メニュー表示がedit_original_responseを使っていません")
    if "def regenerate_single(" not in diag_text or "この画像の候補を再生成" not in diag_text:
        errors.append("しきい値通過・未登録画像の候補再生成機能がありません")
    if "suggest_face_candidates" not in diag_text:
        errors.append("診断画面から正式候補を再生成できません")
    return errors


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

    # 2026-08-11:
    # discord.py の View は内部で `_refresh(components)` を使用する。
    # 自作 View が `_refresh(self)` を定義すると、Discord のメッセージ更新時に
    # TypeError: ... _refresh() takes 1 positional argument but 2 were given
    # で Bot 全体がクラッシュするため、予約内部名との衝突を防ぐ。
    combined_search = (ROOT/'combined_photo_search.py').read_text(encoding='utf-8')
    combined_tree = ast.parse(combined_search)
    combined_refresh_override = False
    combined_sync_method = False
    for node in combined_tree.body:
        if isinstance(node, ast.ClassDef) and node.name == 'CombinedSearchView':
            method_names = {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            combined_refresh_override = '_refresh' in method_names
            combined_sync_method = '_sync_navigation_buttons' in method_names
            break
    checks.append((
        'combined_search_no_discord_refresh_collision',
        not combined_refresh_override and combined_sync_method
        and 'self._sync_navigation_buttons()' in combined_search,
        'CombinedSearchViewはdiscord.py内部の_refreshを上書きしない',
    ))

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

    phase3=(ROOT/'ai_center.py').read_text(encoding='utf-8') if (ROOT/'ai_center.py').exists() else ''
    analyzer=(ROOT/'photo_ai_analyzer.py').read_text(encoding='utf-8')
    checks.append(('phase3_panel_entry','photo:admin:phase3_ai' in panel,'管理者トップからPhase 3 AI管理へ接続'))
    checks.append(('phase3_profiles',all(x in phase3 for x in ('節約モード','標準モード','高精度モード')),'AI設定プロファイル'))
    checks.append(('phase3_two_stage','phase3_preflight' in analyzer and 'local_face_complete' in analyzer and '画像内容タグはOpenAI' in analyzer,'人物ローカル判定＋OpenAIタグ生成の二段階判定'))
    checks.append(('phase3_hash_cache','record_cache_event(image_id, "image_hash"' in analyzer,'画像ハッシュキャッシュ診断'))
    scoring=(ROOT/'face_candidate_scoring.py').read_text(encoding='utf-8') if (ROOT/'face_candidate_scoring.py').exists() else ''
    local_face=(ROOT/'local_face_recognition.py').read_text(encoding='utf-8') if (ROOT/'local_face_recognition.py').exists() else ''
    face_center=(ROOT/'face_candidate_center.py').read_text(encoding='utf-8') if (ROOT/'face_candidate_center.py').exists() else ''
    face_review=(ROOT/'photo_face_review_view.py').read_text(encoding='utf-8') if (ROOT/'photo_face_review_view.py').exists() else ''
    tag_master_text=(ROOT/'tag_master.py').read_text(encoding='utf-8') if (ROOT/'tag_master.py').exists() else ''
    checks.append(('integrated_face_scoring', all(x in scoring for x in ('face_similarity','person_quality','reference_count','acceptance_rate','integrated_score')), '顔類似度・人物品質・参照数・採用率の統合スコア'))
    checks.append(('candidate_reason_display', 'score_reason' in face_review and '統合' in face_center, '顔候補の判定理由を確認画面へ表示'))
    checks.append(('safe_confirmed_learning', all(x in scoring for x in ('重複特徴量','同一ブログの参照上限','人物ごとの参照上限','品質不足')), '確定結果の誤学習防止'))
    checks.append(('learning_hook_single_bulk', 'register_confirmed_face_learning' in db and 'bulk_manual_review' in db, '単体・一括確定を安全学習へ接続'))
    checks.append(('reference_diversity', 'per_person' in local_face and 'per_blog' in local_face, '人物・ブログ単位で参照顔の偏りを抑制'))
    checks.append(('tag_quality_search_policy', 'phase3_tag_quality' in tag_master_text and 'quality_score, 0.75' in tag_master_text, 'タグ品質をAIタグ検索キャッシュへ反映'))
    checks.append(('phase3_prompt_model','effective_model' in analyzer and 'effective_prompt' in analyzer,'モデル・プロンプト動的管理'))

    # photo_ai_analyzer から ai_center へ依存する主要関数は、
    # 関数ローカルではなくモジュールスコープで解決できることを確認する。
    analyzer_tree = ast.parse(analyzer)
    ai_center_imports = set()
    for node in analyzer_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'ai_center':
            ai_center_imports.update(alias.asname or alias.name for alias in node.names)
    required_ai_center_names = {
        'effective_model', 'effective_prompt', 'phase3_preflight', 'record_cache_event'
    }
    checks.append((
        'phase3_analyzer_ai_center_import_scope',
        required_ai_center_names.issubset(ai_center_imports),
        'photo_ai_analyzerのPhase3依存関数をモジュールスコープでimport',
    ))
    ai_center_tree = ast.parse(phase3)
    ai_center_defs = {
        node.name for node in ai_center_tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    checks.append((
        'phase3_ai_center_exports',
        required_ai_center_names.issubset(ai_center_defs),
        'ai_center側に必要なPhase3関数が実在',
    ))
    checks.append(('phase3_schedule','start_phase3_schedule_worker' in (ROOT/'archive_main.py').read_text(encoding='utf-8'),'解析予約ワーカー'))
    checks.append(('phase3_quality',all(x in phase3 for x in ('phase3_tag_quality','phase3_person_quality','phase3_photo_quality')),'タグ・人物・写真品質'))
    checks.append(('phase3_recommendations','phase3_recommendations' in phase3 and 'おすすめ写真' in phase3,'おすすめ写真'))
    checks.append(('phase3_tag_search','TagSearchModal' in phase3 and 'similar_tags' in phase3,'タグ候補・類似タグ検索'))
    checks.append(('phase3_utf8',not any(marker in phase3 for marker in MOJIBAKE_MARKERS),'Phase3日本語文字化けなし'))
    checks.append((
        'ai_success_local_face_candidate_hook',
        '_refresh_local_face_candidates_after_analysis(image_id)' in analyzer
        and 'result["face_candidates"] = _refresh_local_face_candidates_after_analysis(image_id)' in analyzer,
        'AI解析成功後にローカル顔スキャン・候補生成へ接続',
    ))
    local_face=(ROOT/'local_face_recognition.py').read_text(encoding='utf-8')
    checks.append((
        'local_face_confirmed_skip',
        'if face.get("confirmed_person_id") is not None:' in local_face,
        '確定済み顔を候補再生成でpendingへ戻さない',
    ))
    checks.append((
        'face_candidate_failure_isolated',
        'AI解析後のローカル顔候補更新に失敗:' in analyzer and 'summary["error"]' in analyzer,
        'ローカル顔候補生成失敗をAI解析本体から分離',
    ))
    analyzer_db_imports = set()
    for node in analyzer_tree.body:
        if isinstance(node, ast.ImportFrom) and node.module == 'photo_database':
            analyzer_db_imports.update(alias.asname or alias.name for alias in node.names)
    checks.append((
        'ai_post_analysis_face_refresh_has_db_connection_import',
        'get_connection' in analyzer_db_imports
        and 'with get_connection() as con:' in analyzer,
        'AI解析後のローカル顔候補更新でget_connectionを正しくimport',
    ))

    diag_errors = check_face_candidate_diagnostics()
    checks.append((
        'face_candidate_diagnostics',
        not diag_errors,
        '顔候補診断: ' + ('OK' if not diag_errors else ' / '.join(diag_errors)),
    ))

    advanced_admin=(ROOT/'advanced_admin_features.py').read_text(encoding='utf-8') if (ROOT/'advanced_admin_features.py').exists() else ''
    checks.append((
        'person_set_selection_mode',
        'class PersonSetCreateModeView' in advanced_admin
        and 'label="メンバーから選択"' in advanced_admin
        and 'label="名前を直接入力"' in advanced_admin,
        '人物セット保存で選択式と直接入力式を選べる',
    ))
    checks.append((
        'person_set_named_builder',
        'class PersonSetNameModal' in advanced_admin
        and 'class PersonSetBuilderState' in advanced_admin
        and 'label="セット名"' in advanced_admin,
        '選択式人物セットにセット名を付けられる',
    ))
    checks.append((
        'person_set_hierarchical_select',
        all(name in advanced_admin for name in (
            'class PersonSetGroupSelect',
            'class PersonSetGenerationSelect',
            'class PersonSetMemberSelect',
            'label="別の期生から追加"',
            'label="別グループから追加"',
        )),
        'グループ・期生・人物を選択UIで複数追加できる',
    ))
    checks.append((
        'person_set_named_save',
        'def save_person_set(' in advanced_admin
        and 'ON CONFLICT(set_name) DO UPDATE SET' in advanced_admin
        and 'label="この人物セットを保存"' in advanced_admin,
        '名前付き人物セットを保存・更新できる',
    ))

    review_text=(ROOT/'photo_review_view.py').read_text(encoding='utf-8') if (ROOT/'photo_review_view.py').exists() else ''
    checks.append((
        'person_set_partial_edit_before_confirm',
        'base_person_set_name: str = ""' in review_text
        and '必要な人物だけ外したり、別の人物を追加してから確定できます。' in review_text
        and 'view=SelectedPeopleView(state)' in review_text,
        '人物セットを即確定せず部分修正画面へ読み込める',
    ))
    checks.append((
        'person_set_edit_keeps_set_name',
        '**人物セット:**' in review_text
        and 'base_person_set_name=normalize_text(item["name"])' in review_text,
        '人物セット編集中に元セット名を表示できる',
    ))
    checks.append((
        'person_set_edit_can_add_remove',
        'class SelectedPeopleView' in review_text
        and 'class RemoveSelect' in review_text
        and 'label="人物を追加"' in review_text
        and 'label="この内容で確定"' in review_text,
        '人物セットから人物を外す・追加する・確定する操作を維持',
    ))
    checks.append((
        'person_set_edit_audit_note',
        '人物セット「{self.state.base_person_set_name}」を部分修正して確定' in review_text,
        '人物セット部分修正の確定を履歴noteに残す',
    ))

    members_text=(ROOT/'sakamichi_members.py').read_text(encoding='utf-8') if (ROOT/'sakamichi_members.py').exists() else ''
    admin_text=(ROOT/'admin_workflow.py').read_text(encoding='utf-8') if (ROOT/'admin_workflow.py').exists() else ''
    checks.append((
        'author_name_sort_uses_surname_kana',
        'def member_surname_kana_sort_key' in members_text
        and 'member_surname_kana_sort_key' in admin_text
        and 'メンバー名順（五十音）' in admin_text,
        '投稿者のメンバー名順は名字の読み仮名による五十音順',
    ))
    checks.append((
        'author_name_sort_handles_uemura_kamimura',
        "'上村莉菜': 'うえむら'" in members_text
        and "'上村ひなの': 'かみむら'" in members_text,
        '同じ漢字で読みが違う名字はフルネーム例外で扱う',
    ))
    checks.append((
        'author_name_sort_has_safe_fallback',
        'return (text, text)' in members_text,
        '読み未登録の新メンバーでもソート処理を継続できる',
    ))

    photo_db_text=(ROOT/'photo_database.py').read_text(encoding='utf-8') if (ROOT/'photo_database.py').exists() else ''
    checks.append((
        'person_review_missing_queue_auto_repair',
        'def ensure_pending_person_review_queue' in photo_db_text
        and 'if normalized_status == "pending":' in photo_db_text
        and 'ensure_pending_person_review_queue(group_name, blog_id)' in photo_db_text,
        '人物確認pending取得前にキュー未作成の未確定画像を自動補完する',
    ))
    checks.append((
        'person_review_auto_repair_excludes_confirmed',
        "pip.relation_status = 'confirmed'" in photo_db_text
        and "existing_review.review_type = 'person_identity'" in photo_db_text,
        '人物確定済み・既存レビュー行を重複してpending化しない',
    ))
    checks.append((
        'person_review_auto_repair_excludes_terminal',
        "NOT IN ('invalid_url', 'permanent_failed')" in photo_db_text
        and 'reviewable_image_count = max(0, image_count - terminal)' in photo_db_text,
        '復旧不能画像は人物確認キューと残り件数から除外する',
    ))

    photo_db_text=(ROOT/'photo_database.py').read_text(encoding='utf-8') if (ROOT/'photo_database.py').exists() else ''
    checks.append((
        'admin_blog_month_filter_supports_japanese_date',
        'f"____年{month_value}月%"' in photo_db_text
        and 'f"____年{month_value:02d}月%"' in photo_db_text,
        'ブログ月フィルターは2019年3月/2019年03月形式にも対応する',
    ))

    # Discord interaction timeout guard: audited UI callbacks that perform potentially
    # slow thread/DB work must acknowledge the interaction before that work begins.
    interaction_guard_files = [
        'admin_quality.py','admin_insights.py','admin_workflow.py','admin_operations.py',
        'ai_center.py','photo_review_view.py','tag_master_admin.py','user_experience.py',
        'face_candidate_center.py','photo_search.py','past_face_learning.py','photo_face_review_view.py',
    ]
    interaction_guard_violations=[]
    for guard_name in interaction_guard_files:
        guard_path=ROOT/guard_name
        if not guard_path.exists():
            continue
        guard_text=guard_path.read_text(encoding='utf-8')
        try:
            guard_tree=ast.parse(guard_text)
        except Exception:
            continue
        for node in ast.walk(guard_tree):
            if not isinstance(node,ast.AsyncFunctionDef):
                continue
            is_ui=(node.name=='callback' or any(isinstance(d,ast.Call) and 'discord.ui.button' in ast.unparse(d.func) for d in node.decorator_list))
            if not is_ui:
                continue
            src=ast.get_source_segment(guard_text,node) or ''
            heavy_positions=[p for token in ('asyncio.to_thread','_to_thread(') if (p:=src.find(token))>=0]
            if not heavy_positions:
                continue
            first_heavy=min(heavy_positions)
            ack_positions=[p for token in ('.defer(','.send_message(','.edit_message(','.send_modal(') if (p:=src.find(token))>=0]
            first_ack=min(ack_positions) if ack_positions else 10**9
            if first_heavy < first_ack:
                interaction_guard_violations.append(f'{guard_name}:{node.name}')
    checks.append((
        'discord_heavy_callbacks_ack_before_work',
        not interaction_guard_violations,
        '重いDiscord UIコールバックは処理前にACKする: '+(', '.join(interaction_guard_violations[:10]) if interaction_guard_violations else 'OK'),
    ))

    admin_workflow_text=(ROOT/'admin_workflow.py').read_text(encoding='utf-8') if (ROOT/'admin_workflow.py').exists() else ''
    photo_db_text=(ROOT/'photo_database.py').read_text(encoding='utf-8') if (ROOT/'photo_database.py').exists() else ''
    checks.append((
        'completed_no_people_is_displayed_as_nobody',
        'if status == "completed":\n                    desc = "人物なし"' in admin_workflow_text,
        '人物なし確定済み写真を未確認と表示しない',
    ))
    checks.append((
        'blog_workflow_stats_available',
        'def get_blog_workflow_stats_for_admin' in photo_db_text
        and 'label="処理件数"' in admin_workflow_text
        and "'blogs_with_skipped_photos'" in admin_workflow_text,
        'ブログ処理件数・スキップ件数を管理画面で確認できる',
    ))
    checks.append((
        'blog_workflow_stats_separates_hidden_and_photo_skip',
        "hidden_reason = 'MANUAL_HIDE'" in photo_db_text
        and "'skipped_photos'" in admin_workflow_text
        and "'hidden_blogs'" in admin_workflow_text,
        '写真スキップとブログ除外を別集計する',
    ))

    ux_text=(ROOT/'user_experience.py').read_text(encoding='utf-8') if (ROOT/'user_experience.py').exists() else ''
    review_text=(ROOT/'photo_review_view.py').read_text(encoding='utf-8') if (ROOT/'photo_review_view.py').exists() else ''
    advanced_text=(ROOT/'advanced_admin_features.py').read_text(encoding='utf-8') if (ROOT/'advanced_admin_features.py').exists() else ''
    explorer_text=(ROOT/'photo_tag_explorer.py').read_text(encoding='utf-8') if (ROOT/'photo_tag_explorer.py').exists() else ''
    checks.append((
        'explore_defer_uses_followup',
        'if interaction.response.is_done()' in ux_text and 'interaction.followup.send' in ux_text,
        'ランダム・今日の1枚はdefer後に二重responseしない',
    ))
    checks.append((
        'explore_uses_bucket_presigned_url',
        'from photo_search import get_display_image_url' in ux_text and 'return get_display_image_url(row)' in ux_text,
        'ランダム・今日の1枚でBucket署名URLを利用できる',
    ))
    checks.append((
        'person_set_apply_pagination',
        'PERSON_SET_APPLY_PAGE_SIZE = 25' in review_text and 'label="次へ"' in review_text and 'count_person_sets' in advanced_text,
        '保存済み人物セット26件目以降へページ送りできる',
    ))
    checks.append((
        'member_result_period_sort',
        'label="並び順・期間"' in explorer_text and 'label="最新順"' in explorer_text and 'label="古い順"' in explorer_text and 'label="期間を指定"' in explorer_text,
        '人物検索結果を最新順・古い順・期間で絞り込める',
    ))

    user_experience_text=(ROOT/'user_experience.py').read_text(encoding='utf-8') if (ROOT/'user_experience.py').exists() else ''
    checks.append((
        'user_experience_no_photo_search_top_level_import',
        'from photo_search import get_display_image_url' not in user_experience_text.split('def _display_url', 1)[0]
        and 'def _display_url' in user_experience_text
        and 'from photo_search import get_display_image_url' in user_experience_text.split('def _display_url', 1)[1],
        'photo_search↔user_experience の起動時循環importを防止する',
    ))

    control_panel_text=(ROOT/'control_panel.py').read_text(encoding='utf-8') if (ROOT/'control_panel.py').exists() else ''
    combined_search_text=(ROOT/'combined_photo_search.py').read_text(encoding='utf-8') if (ROOT/'combined_photo_search.py').exists() else ''
    review_view_text=(ROOT/'photo_review_view.py').read_text(encoding='utf-8') if (ROOT/'photo_review_view.py').exists() else ''
    checks.append((
        'user_blog_person_search_is_selection_based',
        'class BlogPersonGroupView' in control_panel_text
        and 'class BlogPersonGenerationView' in control_panel_text
        and 'class BlogPersonMemberView' in control_panel_text
        and 'グループを選んでください。' in control_panel_text,
        'ユーザーパネルのブログ人物検索をグループ→期→メンバー選択式にする',
    ))
    checks.append((
        'user_blog_person_search_has_period_sort',
        'label="最新順"' in control_panel_text
        and 'label="古い順"' in control_panel_text
        and 'label="年・月を指定"' in control_panel_text
        and 'label="期間を指定"' in control_panel_text
        and 'def send_blog_person_search' in combined_search_text,
        '選択したメンバーを最新順・古い順・年月・期間で絞り込める',
    ))
    checks.append((
        'person_set_apply_has_pagination',
        'PERSON_SET_APPLY_PAGE_SIZE = 25' in review_view_text
        and 'label="次へ"' in review_view_text
        and 'label="前へ"' in review_view_text
        and 'count_person_sets' in review_view_text,
        '保存済み人物セット26件目以降へ前へ/次へで移動できる',
    ))

    tag_explorer_text=(ROOT/'photo_tag_explorer.py').read_text(encoding='utf-8') if (ROOT/'photo_tag_explorer.py').exists() else ''
    checks.append((
        'tag_explorer_component_rows_fit_discord_limit',
        'label="人物名入力", emoji="🔤", style=discord.ButtonStyle.secondary, row=3' in tag_explorer_text
        and 'label="タグ名入力", emoji="⌨️", style=discord.ButtonStyle.secondary, row=3' in tag_explorer_text
        and 'label="条件を個別解除", emoji="➖", style=discord.ButtonStyle.secondary, row=3' in tag_explorer_text
        and 'emoji="🔍", style=discord.ButtonStyle.success, row=4' in tag_explorer_text
        and 'label="全解除", emoji="🧹", style=discord.ButtonStyle.danger, row=4' in tag_explorer_text
        and 'label="終了", emoji="✖️", style=discord.ButtonStyle.secondary, row=4' in tag_explorer_text,
        'タグ検索トップの各rowをDiscordの最大5幅以内に収める',
    ))

    control_panel_text=(ROOT/'control_panel.py').read_text(encoding='utf-8') if (ROOT/'control_panel.py').exists() else ''
    combined_search_text=(ROOT/'combined_photo_search.py').read_text(encoding='utf-8') if (ROOT/'combined_photo_search.py').exists() else ''
    checks.append((
        'blog_person_search_supports_poster_or_subject',
        'class BlogPersonMatchModeView' in control_panel_text
        and 'label="投稿者で探す"' in control_panel_text
        and 'label="写っている人物で探す"' in control_panel_text
        and 'match_mode=state.match_mode' in control_panel_text
        and 'match_mode: str = "poster"' in combined_search_text,
        '人物検索で投稿者/写っている人物を選択できる',
    ))
    checks.append((
        'blog_person_search_groups_by_blog',
        'def _blog_person_grouped_results' in combined_search_text
        and 'class GroupedBlogResultView' in combined_search_text
        and '"images": []' in combined_search_text
        and 'blog_id not in grouped' in combined_search_text,
        '人物検索結果を同一ブログ単位でまとめる',
    ))
    grouped_part = combined_search_text.split('def _blog_person_grouped_results',1)[1].split('def _blog_group_embed',1)[0] if 'def _blog_person_grouped_results' in combined_search_text else ''
    checks.append((
        'blog_person_search_no_twenty_image_limit',
        'LIMIT ?' not in grouped_part
        and 'BLOG_GROUP_PAGE_SIZE = 5' in combined_search_text
        and 'BLOG_IMAGE_BATCH_SIZE = 10' in combined_search_text,
        '人物検索の20枚制限を廃止しブログ単位ページングにする',
    ))

    photo_db_text=(ROOT/'photo_database.py').read_text(encoding='utf-8')
    analyzer_text=(ROOT/'photo_ai_analyzer.py').read_text(encoding='utf-8')
    cost_text=(ROOT/'ai_cost_control.py').read_text(encoding='utf-8')
    checks.append((
        'ai_requires_completed_person_review_blog',
        'def get_blog_person_review_gate' in photo_db_text
        and "gate_review.review_type = 'person_identity'" in photo_db_text
        and "gate_review.status = 'completed'" in photo_db_text
        and 'get_image_ai_review_gate(image_id)' in analyzer_text
        and '"waiting_person_review"' in analyzer_text,
        'AI解析は人物確認100%完了ブログだけ許可する',
    ))
    checks.append((
        'ai_pending_queue_is_review_gated',
        "gate_review.status = 'completed'" in photo_db_text.split('def get_pending_analysis_images',1)[1],
        '一括AI解析キューも人物確認完了ブログだけ取得する',
    ))
    checks.append((
        'ai_preview_uses_same_review_gate',
        'get_pending_analysis_images(safe_limit)' in cost_text
        and "waiting_person_review" in cost_text,
        'AI解析前プレビューも実処理と同じ人物確認ゲートを使う',
    ))

    failed=[c for c in checks if not c[1]]
    return {'ok':not failed,'total':len(checks),'failed':failed,'checks':checks}
if __name__=='__main__':
    import json; print(json.dumps(run(),ensure_ascii=False,indent=2))
