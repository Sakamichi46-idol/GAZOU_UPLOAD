# 写真アーカイブ Bucket移行版 — Phase 1

この版は、約16,000枚を安全に保存できる構成へ移るための**第1段階**です。
既存のSQLite DBとVolume画像を消さず、Railway Storage Bucketへ段階的に複製できます。

## Phase 1でできること

- 新規画像をBucketへ保存
- `PHOTO_STORAGE_MODE=dual` ではVolumeにも同時保存（安全移行用）
- 検索・タグ画面はBucketの署名付きURLを優先して表示
- 既存Volume画像を500枚ずつBucketへ移行
- Bucket保存済みかをSQLiteに記録
- 移行中に失敗しても既存画像は残る

## RailwayでBucketを作る

1. RailwayのProject Canvasで `+ New` → `Bucket`
2. Bucketを作成
3. BotサービスのVariablesでBucketのVariable Referencesを追加

必要なRailway標準変数:

```text
BUCKET
ACCESS_KEY_ID
SECRET_ACCESS_KEY
REGION
ENDPOINT
```

追加設定:

```text
PHOTO_STORAGE_MODE=dual
PHOTO_BUCKET_URL_TTL=604800
```

`dual` は「BucketとVolumeの両方へ保存」です。移行完了まではこの設定を使ってください。

## 既存画像の移行

Railway Shellまたは一時Jobで次を実行します。

```bash
python migrate_photos_to_bucket.py --limit 500
```

何度か実行し、次の表示になるまで繰り返します。

```text
completed=0 failed=0 local_deleted=0
```

最初は `--delete-local` を付けないでください。

## Phase 2へ切り替える前の確認

- Bucketへの移行失敗がない
- Discordの写真検索で画像が表示される
- 新規画像の `bucket_status` が `completed`
- DBバックアップを取得済み

Phase 2ではAI解析をBucket画像へ完全対応させ、確認後にVolumeの画像を削除し、`PHOTO_STORAGE_MODE=bucket` へ切り替えます。
