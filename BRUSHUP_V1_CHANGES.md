# ブログアーカイブBot 運用安定化版 v1

## 追加
- `!status` 統合ステータスコマンド
- 通知アーカイブの実行状況・前回結果・処理時間の記録
- ブログ一覧取得の最大3回再試行（初回、5秒後、10秒後）
- 記事画像URL取得の最大3回再試行
- `[INFO]` `[WARNING]` `[ERROR]` `[SUCCESS]` 形式の主要ログ

## 改善
- 定期巡回と `!archive_run` の二重実行を既存ロックで統一
- 記事ごとの成功・失敗件数を集計
- 失敗時にも前回エラーを `!status` で確認可能

## 維持
- DB保存先 `/data/archive.db`
- 写真DB `/data/photo_archive.db`
- 写真保存先 `/data/photo_images`
- Railway Variables
- 既存コマンド、通知、Cloudflare Bucket、AI解析機能
