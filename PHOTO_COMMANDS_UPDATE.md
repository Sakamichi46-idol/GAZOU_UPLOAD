# photo_commands.py 更新内容

## 修正ファイル

- `photo_commands.py`
  - 現在の `photo_review_view.py` に存在しない旧関数の import を削除。
  - `send_next_person_review`、`send_person_review`、`send_person_review_batch` に対応。
  - `!photo_person_show` を追加。
  - `!review_next`、`!review_list`、`!review_stats`、`!review_skip` を追加・整理。
  - `!photo_edit` を現在のレビューDBとUIに対応。
  - `!review_done` を人物確定処理とレビュー完了処理に対応。
  - 既存の検索、タグ、お気に入り、再解析、再ダウンロード、統計、DBリセット機能を維持。

## 確認済み

- プロジェクト内の全Pythonファイルを `compileall` で構文確認済み。
- `photo_commands.py` 単体を `py_compile` で構文確認済み。

## Railway

環境変数の追加・削除はありません。
