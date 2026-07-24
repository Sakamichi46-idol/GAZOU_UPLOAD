"""Safely migrate existing Volume images to Railway Storage Bucket.

Run in Railway shell/job:
    python migrate_photos_to_bucket.py --limit 500
Repeat until remaining=0. Local files are retained unless --delete-local is used.
"""
from __future__ import annotations

import argparse
import mimetypes
import os
from contextlib import closing

from bucket_storage import bucket_is_configured, object_exists, upload_file
from photo_database import get_connection, init_photo_db, utc_now_text


def migrate(limit: int, delete_local: bool) -> tuple[int, int, int]:
    init_photo_db()
    if not bucket_is_configured():
        raise RuntimeError("Bucket credentials are not configured.")
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT id, local_path, file_name, mime_type, image_hash,
                   group_name, member_name, published_at, blog_id, image_index
            FROM photo_images
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            WHERE download_status='completed'
              AND local_path!=''
              AND (bucket_key='' OR bucket_status!='completed')
            ORDER BY photo_images.id
            LIMIT ?
            """, (max(1, limit),)
        ).fetchall()

    ok = failed = deleted = 0
    for row in rows:
        path = str(row["local_path"] or "")
        if not os.path.isfile(path):
            failed += 1
            continue
        ext = os.path.splitext(str(row["file_name"] or path))[1].lower() or ".jpg"
        key = f"photos/{int(row['blog_id']):09d}/{int(row['image_index']):03d}-{str(row['image_hash'] or '')[:16]}{ext}"
        mime = str(row["mime_type"] or mimetypes.guess_type(path)[0] or "application/octet-stream")
        try:
            if not object_exists(key):
                upload_file(key=key, file_path=path, content_type=mime)
            with closing(get_connection()) as connection:
                connection.execute(
                    """UPDATE photo_images SET bucket_key=?, bucket_status='completed',
                       bucket_error='', storage_backend='bucket', updated_at=? WHERE id=?""",
                    (key, utc_now_text(), int(row["id"])),
                )
                connection.commit()
            ok += 1
            if delete_local:
                os.remove(path)
                with closing(get_connection()) as connection:
                    connection.execute("UPDATE photo_images SET local_path='' WHERE id=?", (int(row["id"]),))
                    connection.commit()
                deleted += 1
        except Exception as error:
            failed += 1
            with closing(get_connection()) as connection:
                connection.execute(
                    "UPDATE photo_images SET bucket_status='failed', bucket_error=?, updated_at=? WHERE id=?",
                    (str(error)[:1000], utc_now_text(), int(row["id"])),
                )
                connection.commit()
            print(f"FAILED image_id={row['id']}: {error}")
    return ok, failed, deleted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--delete-local", action="store_true")
    args = parser.parse_args()
    ok, failed, deleted = migrate(args.limit, args.delete_local)
    print(f"completed={ok} failed={failed} local_deleted={deleted}")


if __name__ == "__main__":
    main()
