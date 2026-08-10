"""Optional, on-demand local face recognition for the photo archive.

This module never calls OpenAI.  OpenCV is imported lazily so the normal bot
continues to run without the optional face-recognition dependencies.
"""
from __future__ import annotations

import base64
import json
import tempfile
import zlib
from datetime import datetime, timezone

import requests
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

from bucket_storage import bucket_is_configured, download_to_file
from photo_database import (
    add_face_review,
    confirm_face_person,
    get_connection,
    get_face_candidates,
    get_image_faces,
    get_photo_image,
    save_detected_face,
    save_face_candidate,
)

MODEL_NAME = "opencv-haar-gray32-v1"
EMBEDDING_SIZE = 32
DEFAULT_MATCH_THRESHOLD = 0.72
MIN_FACE_SIZE = 48
MAX_BATCH_SCAN = 1000


class FaceEngineUnavailable(RuntimeError):
    pass


def _load_dependencies():
    try:
        import cv2  # type: ignore
        import numpy as np  # type: ignore
    except ImportError as error:
        raise FaceEngineUnavailable(
            "ローカル顔認識は未導入です。requirements-face.txt の依存関係を追加してください。"
        ) from error
    return cv2, np


def get_face_engine_status() -> dict[str, Any]:
    try:
        cv2, np = _load_dependencies()
        cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
        return {
            "available": Path(cascade_path).is_file(),
            "opencv_version": str(cv2.__version__),
            "numpy_version": str(np.__version__),
            "cascade_path": cascade_path,
            "model_name": MODEL_NAME,
        }
    except FaceEngineUnavailable as error:
        return {"available": False, "error": str(error), "model_name": MODEL_NAME}


@contextmanager
def _local_image_path(image: dict[str, Any]) -> Iterator[str]:
    local_path = str(image.get("local_path") or "").strip()
    if local_path and Path(local_path).is_file():
        yield local_path
        return

    bucket_key = str(image.get("bucket_key") or "").strip()
    suffix = Path(str(image.get("file_name") or "image.jpg")).suffix or ".jpg"

    if bucket_key:
        if not bucket_is_configured():
            raise RuntimeError("Bucket設定が不足しているため画像を取得できません。")
        with tempfile.TemporaryDirectory(prefix="face-scan-") as directory:
            path = str(Path(directory) / f"source{suffix}")
            download_to_file(key=bucket_key, file_path=path)
            yield path
        return

    # Bucket移行前などの古いレコードでは、local_pathとbucket_keyが空でも
    # 元画像URLが残っている場合がある。顔レビュー時だけ一時取得して復旧する。
    image_url = str(image.get("image_url") or "").strip()
    parsed = urlparse(image_url)
    if parsed.scheme in {"http", "https"} and parsed.netloc:
        with tempfile.TemporaryDirectory(prefix="face-scan-url-") as directory:
            path = str(Path(directory) / f"source{suffix}")
            try:
                response = requests.get(
                    image_url,
                    headers={"User-Agent": "Mozilla/5.0"},
                    timeout=(10, 60),
                )
                response.raise_for_status()
                Path(path).write_bytes(response.content)
            except requests.RequestException as error:
                raise FileNotFoundError(
                    f"ローカル画像とBucketキーがなく、元画像URLからの取得にも失敗しました: {error}"
                ) from error
            if not Path(path).is_file() or Path(path).stat().st_size == 0:
                raise FileNotFoundError("元画像URLから取得したファイルが空です。")
            yield path
        return

    raise FileNotFoundError(
        "ローカル画像・Bucketキー・利用可能なHTTP画像URLのいずれも見つかりません。"
    )


def _encode_embedding(vector: Any) -> str:
    raw = vector.astype("float32").tobytes()
    return base64.b64encode(zlib.compress(raw, level=6)).decode("ascii")


def _decode_embedding(value: str, np: Any):
    raw = zlib.decompress(base64.b64decode(value.encode("ascii")))
    vector = np.frombuffer(raw, dtype=np.float32)
    expected = EMBEDDING_SIZE * EMBEDDING_SIZE
    if vector.size != expected:
        raise ValueError("Unsupported face embedding size.")
    return vector


def _make_embedding(gray_face: Any, cv2: Any, np: Any):
    resized = cv2.resize(gray_face, (EMBEDDING_SIZE, EMBEDDING_SIZE), interpolation=cv2.INTER_AREA)
    equalized = cv2.equalizeHist(resized)
    vector = equalized.astype(np.float32).reshape(-1) / 255.0
    vector -= float(vector.mean())
    norm = float(np.linalg.norm(vector))
    if norm > 0:
        vector /= norm
    return vector


def _confirmed_image_people(image_id: int) -> list[dict[str, Any]]:
    with closing(get_connection()) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT photo_people.id AS person_id, photo_people.person_name
            FROM photo_image_people
            JOIN photo_people ON photo_people.person_name = photo_image_people.person_name
            WHERE photo_image_people.image_id = ?
              AND photo_image_people.relation_status = 'confirmed'
            ORDER BY photo_people.person_name
            """,
            (int(image_id),),
        ).fetchall()
        return [dict(row) for row in rows]


def _save_face_scan_status(
    image_id: int,
    status: str,
    *,
    detected_faces: int = 0,
    auto_confirmed_faces: int = 0,
    error_message: str = "",
) -> None:
    """画像単位の顔スキャン履歴を保存する。顔が0件でも完了を記録する。"""
    with closing(get_connection()) as connection:
        connection.execute(
            """
            INSERT INTO photo_face_scans (
                image_id, status, detected_faces, auto_confirmed_faces,
                model_name, error_message, scanned_at, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, datetime('now'), datetime('now'), datetime('now'))
            ON CONFLICT(image_id) DO UPDATE SET
                status = excluded.status,
                detected_faces = excluded.detected_faces,
                auto_confirmed_faces = excluded.auto_confirmed_faces,
                model_name = excluded.model_name,
                error_message = excluded.error_message,
                scanned_at = excluded.scanned_at,
                updated_at = excluded.updated_at
            """,
            (
                int(image_id), str(status), int(detected_faces),
                int(auto_confirmed_faces), MODEL_NAME, str(error_message)[:1000],
            ),
        )
        connection.commit()


def detect_faces_for_image(image_id: int) -> dict[str, Any]:
    """Detect faces and save compact local embeddings. Runs only on command."""
    cv2, np = _load_dependencies()
    image = get_photo_image(int(image_id))
    if not image:
        raise ValueError("画像IDが見つかりません。")

    _save_face_scan_status(int(image_id), "processing")

    cascade_path = str(Path(cv2.data.haarcascades) / "haarcascade_frontalface_default.xml")
    detector = cv2.CascadeClassifier(cascade_path)
    if detector.empty():
        raise RuntimeError("OpenCVの顔検出モデルを読み込めませんでした。")

    with _local_image_path(image) as path:
        source = cv2.imread(path)
    if source is None:
        raise RuntimeError("画像をOpenCVで読み込めませんでした。")

    gray = cv2.cvtColor(source, cv2.COLOR_BGR2GRAY)
    height, width = gray.shape[:2]
    min_side = max(MIN_FACE_SIZE, min(width, height) // 12)
    boxes = detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(min_side, min_side),
    )
    boxes = sorted(boxes, key=lambda box: (int(box[1]), int(box[0])))

    # 再スキャン時に古い検出結果が残らないよう、画像単位で作り直す。
    # photo_face_candidates / photo_face_reviews は外部キーのCASCADEで削除される。
    with closing(get_connection()) as connection:
        connection.execute("DELETE FROM photo_faces WHERE image_id = ?", (int(image_id),))
        connection.commit()

    face_ids: list[int] = []
    for index, (x, y, w, h) in enumerate(boxes, 1):
        crop = gray[int(y):int(y+h), int(x):int(x+w)]
        embedding = _make_embedding(crop, cv2, np)
        face_id = save_detected_face(
            int(image_id),
            index,
            box_x=float(x), box_y=float(y), box_width=float(w), box_height=float(h),
            detection_confidence=1.0,
            model_name=MODEL_NAME,
            face_embedding=_encode_embedding(embedding),
        )
        face_ids.append(face_id)

    # Safe reference seeding: exactly one face and exactly one confirmed person.
    auto_confirmed = 0
    confirmed_people = _confirmed_image_people(int(image_id))
    if len(face_ids) == 1 and len(confirmed_people) == 1:
        confirm_face_person(
            face_ids[0], int(confirmed_people[0]["person_id"]),
            confirmed_by="local-face-single-person-seed",
            confirmation_status="auto_seeded",
        )
        auto_confirmed = 1
    else:
        for face_id in face_ids:
            add_face_review(face_id, "この顔の人物を確認してください。", [])

    _save_face_scan_status(
        int(image_id),
        "completed",
        detected_faces=len(face_ids),
        auto_confirmed_faces=auto_confirmed,
    )

    return {
        "image_id": int(image_id),
        "detected": len(face_ids),
        "face_ids": face_ids,
        "auto_confirmed": auto_confirmed,
        "width": int(width),
        "height": int(height),
    }


def _reference_embeddings(exclude_face_id: int, group_name: str = "") -> list[dict[str, Any]]:
    params: list[Any] = [MODEL_NAME, int(exclude_face_id)]
    group_sql = ""
    if group_name:
        group_sql = " AND photo_people.group_name = ?"
        params.append(group_name)
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT photo_faces.id AS face_id, photo_faces.face_embedding,
                   photo_people.id AS person_id, photo_people.person_name,
                   photo_people.group_name
            FROM photo_faces
            JOIN photo_people ON photo_people.id = photo_faces.confirmed_person_id
            LEFT JOIN photo_face_learning_registry learning
              ON learning.face_id = photo_faces.id
            WHERE photo_faces.model_name = ?
              AND photo_faces.id <> ?
              AND photo_faces.face_embedding <> ''
              AND photo_faces.confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
              AND COALESCE(learning.is_active, 1) = 1
              {group_sql}
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]



def _save_candidate_diagnostic(
    image_id: int,
    face_id: int,
    ranked: list[dict[str, Any]],
    reason: str,
    reference_count: int,
) -> None:
    """正式候補とは別に、しきい値未満も含む上位スコアを診断用に保存する。"""
    now = datetime.now(timezone.utc).isoformat()
    top = ranked[:3]
    with closing(get_connection()) as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS photo_face_candidate_diagnostics (
                face_id INTEGER PRIMARY KEY,
                image_id INTEGER NOT NULL,
                threshold REAL NOT NULL DEFAULT 0,
                reference_count INTEGER NOT NULL DEFAULT 0,
                best_person_id INTEGER,
                best_person_name TEXT NOT NULL DEFAULT '',
                best_confidence REAL NOT NULL DEFAULT 0,
                top_candidates_json TEXT NOT NULL DEFAULT '[]',
                reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            )
            """
        )
        best = top[0] if top else {}
        connection.execute(
            """
            INSERT INTO photo_face_candidate_diagnostics(
                face_id,image_id,threshold,reference_count,best_person_id,best_person_name,
                best_confidence,top_candidates_json,reason,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(face_id) DO UPDATE SET
                image_id=excluded.image_id, threshold=excluded.threshold,
                reference_count=excluded.reference_count, best_person_id=excluded.best_person_id,
                best_person_name=excluded.best_person_name, best_confidence=excluded.best_confidence,
                top_candidates_json=excluded.top_candidates_json, reason=excluded.reason,
                updated_at=excluded.updated_at
            """,
            (
                int(face_id), int(image_id), float(DEFAULT_MATCH_THRESHOLD), int(reference_count),
                int(best.get("person_id")) if best.get("person_id") is not None else None,
                str(best.get("person_name") or ""), float(best.get("confidence") or 0),
                json.dumps(top, ensure_ascii=False), str(reason), now,
            ),
        )
        connection.commit()


def suggest_face_candidates(image_id: int, limit_per_face: int = 5) -> list[dict[str, Any]]:
    """Compare detected faces with confirmed local references using cosine similarity."""
    _, np = _load_dependencies()
    image = get_photo_image(int(image_id))
    if not image:
        raise ValueError("画像IDが見つかりません。")
    faces = get_image_faces(int(image_id))
    if not faces:
        raise ValueError("先に !face_scan 画像ID を実行してください。")

    group_name = str(image.get("group_name") or "")
    result: list[dict[str, Any]] = []
    for face in faces:
        # すでに本確定・自動シード済みの顔は、再び確認待ちへ戻さない。
        if face.get("confirmed_person_id") is not None:
            continue
        encoded = str(face.get("face_embedding") or "")
        if not encoded:
            continue
        target = _decode_embedding(encoded, np)
        references = _reference_embeddings(int(face["id"]), group_name)
        best_by_person_all: dict[int, dict[str, Any]] = {}
        for reference in references:
            try:
                vector = _decode_embedding(str(reference["face_embedding"]), np)
            except Exception:
                continue
            similarity = float(np.dot(target, vector))
            # 正規化済みベクトルのコサイン類似度を0〜1へ丸める。
            confidence = max(0.0, min(similarity, 1.0))
            person_id = int(reference["person_id"])
            current = best_by_person_all.get(person_id)
            if current is None or confidence > float(current["confidence"]):
                best_by_person_all[person_id] = {
                    "person_id": person_id,
                    "person_name": str(reference["person_name"]),
                    "confidence": confidence,
                }

        ranked_all = sorted(
            best_by_person_all.values(),
            key=lambda item: item["confidence"],
            reverse=True,
        )
        candidates = [
            item for item in ranked_all
            if float(item["confidence"]) >= DEFAULT_MATCH_THRESHOLD
        ][:max(1, min(limit_per_face, 10))]

        if not references:
            diagnostic_reason = "参照顔なし"
        elif not ranked_all:
            diagnostic_reason = "有効な参照顔なし"
        elif float(ranked_all[0]["confidence"]) < DEFAULT_MATCH_THRESHOLD:
            diagnostic_reason = "候補しきい値未満"
        elif candidates:
            diagnostic_reason = "候補登録"
        else:
            diagnostic_reason = "候補なし"
        _save_candidate_diagnostic(
            int(image_id), int(face["id"]), ranked_all, diagnostic_reason, len(references)
        )

        # 最新の確定顔を学習元として再計算するため、古い候補をいったん削除する。
        # これにより手動確定・一括確定した顔が、次回の候補生成へ確実に反映される。
        with closing(get_connection()) as connection:
            connection.execute(
                "DELETE FROM photo_face_candidates WHERE face_id = ?",
                (int(face["id"]),),
            )
            connection.commit()

        for rank, candidate in enumerate(candidates, 1):
            save_face_candidate(
                int(face["id"]), int(candidate["person_id"]), float(candidate["confidence"]),
                candidate_rank=rank, model_name=MODEL_NAME,
                raw_value=f"cosine-derived:{candidate['confidence']:.6f}",
            )
        add_face_review(int(face["id"]), "ローカル顔候補を確認してください。", candidates)
        result.append({"face_id": int(face["id"]), "candidates": candidates})
    return result



def diagnose_face_candidates(image_id: int, top_n: int = 3, *, scan_if_missing: bool = True) -> dict[str, Any]:
    """APIを使わず、顔候補が確認待ちに入らない理由を再計算する。

    正式候補の保存条件は変更せず、しきい値未満も診断結果として返す。
    """
    _, np = _load_dependencies()
    image_id = int(image_id)
    image = get_photo_image(image_id)
    if not image:
        raise ValueError("画像IDが見つかりません。")

    faces = get_image_faces(image_id)
    scanned_now = False
    if not faces and scan_if_missing:
        detect_faces_for_image(image_id)
        faces = get_image_faces(image_id)
        scanned_now = True

    group_name = str(image.get("group_name") or "")
    result_faces: list[dict[str, Any]] = []
    summary = {
        "detected_faces": len(faces),
        "with_embedding": 0,
        "confirmed_faces": 0,
        "no_embedding": 0,
        "no_references": 0,
        "below_threshold": 0,
        "registered_candidates": 0,
    }

    for face in faces:
        face_id = int(face["id"])
        confirmed = face.get("confirmed_person_id") is not None
        if confirmed:
            summary["confirmed_faces"] += 1

        encoded = str(face.get("face_embedding") or "")
        if not encoded:
            summary["no_embedding"] += 1
            result_faces.append({
                "face_id": face_id,
                "confirmed": confirmed,
                "reason": "特徴量なし",
                "references": 0,
                "top_candidates": [],
                "registered_count": 0,
            })
            continue

        summary["with_embedding"] += 1
        target = _decode_embedding(encoded, np)
        references = _reference_embeddings(face_id, group_name)
        if not references:
            summary["no_references"] += 1
            result_faces.append({
                "face_id": face_id,
                "confirmed": confirmed,
                "reason": "参照顔なし",
                "references": 0,
                "top_candidates": [],
                "registered_count": len(get_face_candidates(face_id)),
            })
            continue

        best_by_person: dict[int, dict[str, Any]] = {}
        for reference in references:
            try:
                vector = _decode_embedding(str(reference["face_embedding"]), np)
            except Exception:
                continue
            similarity = float(np.dot(target, vector))
            confidence = max(0.0, min(similarity, 1.0))
            person_id = int(reference["person_id"])
            current = best_by_person.get(person_id)
            if current is None or confidence > float(current["confidence"]):
                best_by_person[person_id] = {
                    "person_id": person_id,
                    "person_name": str(reference["person_name"]),
                    "confidence": confidence,
                }

        ranked = sorted(
            best_by_person.values(),
            key=lambda item: float(item["confidence"]),
            reverse=True,
        )[:max(1, min(int(top_n), 10))]
        registered = get_face_candidates(face_id)
        registered_count = len(registered)
        summary["registered_candidates"] += registered_count

        if confirmed:
            reason = "人物確定済み"
        elif not ranked:
            reason = "有効な参照顔なし"
        elif float(ranked[0]["confidence"]) < DEFAULT_MATCH_THRESHOLD:
            reason = "候補しきい値未満"
            summary["below_threshold"] += 1
        elif registered_count:
            reason = "候補登録済み"
        else:
            reason = "しきい値通過・未登録（候補生成の再実行を確認）"

        result_faces.append({
            "face_id": face_id,
            "confirmed": confirmed,
            "reason": reason,
            "references": len(references),
            "top_candidates": ranked,
            "registered_count": registered_count,
        })

    if not faces:
        overall_reason = "顔未検出"
    elif all(item["confirmed"] for item in result_faces):
        overall_reason = "全顔が人物確定済み"
    elif summary["registered_candidates"] > 0:
        overall_reason = "候補登録あり"
    elif summary["below_threshold"] > 0:
        overall_reason = "候補しきい値未満"
    elif summary["no_embedding"] > 0:
        overall_reason = "特徴量なし"
    elif summary["no_references"] > 0:
        overall_reason = "参照顔なし"
    else:
        overall_reason = "候補なし"

    return {
        "image_id": image_id,
        "group_name": group_name,
        "analysis_status": str(image.get("analysis_status") or ""),
        "threshold": float(DEFAULT_MATCH_THRESHOLD),
        "scanned_now": scanned_now,
        "summary": summary,
        "faces": result_faces,
        "overall_reason": overall_reason,
    }

def get_face_summary(image_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for face in get_image_faces(int(image_id)):
        item = dict(face)
        item["candidates"] = get_face_candidates(int(face["id"]))[:5]
        result.append(item)
    return result



def get_unscanned_face_images(limit: int = 20, group_name: str = "") -> list[dict[str, Any]]:
    """顔スキャンが完了していないダウンロード済み画像を古い順に取得する。"""
    limit = max(1, min(int(limit), MAX_BATCH_SCAN))
    params: list[Any] = []
    group_sql = ""
    if group_name.strip():
        group_sql = " AND photo_blogs.group_name = ?"
        params.append(group_name.strip())
    params.append(limit)
    with closing(get_connection()) as connection:
        rows = connection.execute(
            f"""
            SELECT photo_images.id, photo_blogs.group_name, photo_blogs.member_name,
                   photo_blogs.title
            FROM photo_images
            JOIN photo_blogs ON photo_blogs.id = photo_images.blog_id
            LEFT JOIN photo_face_scans ON photo_face_scans.image_id = photo_images.id
            WHERE photo_images.download_status = 'completed'
              AND (photo_images.local_path != '' OR photo_images.bucket_key != '')
              AND (photo_face_scans.image_id IS NULL OR photo_face_scans.status = 'processing')
              {group_sql}
            ORDER BY photo_images.id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


def scan_faces_batch(limit: int = 20, group_name: str = "") -> dict[str, Any]:
    """未スキャン画像を一括処理する。OpenAIは呼ばない。

    失敗済み画像は同じ一括実行内で無限に再試行しないよう対象外にする。
    個別の ``!face_scan 画像ID`` を実行すれば再試行できる。
    """
    targets = get_unscanned_face_images(limit, group_name)
    scanned = detected = auto_confirmed = failed = 0
    errors: list[str] = []
    for target in targets:
        try:
            result = detect_faces_for_image(int(target["id"]))
            scanned += 1
            detected += int(result["detected"])
            auto_confirmed += int(result["auto_confirmed"])
        except Exception as error:
            failed += 1
            try:
                _save_face_scan_status(
                    int(target["id"]),
                    "failed",
                    error_message=f"{type(error).__name__}: {error}",
                )
            except Exception:
                pass
            if len(errors) < 5:
                errors.append(f"画像ID {target['id']}: {type(error).__name__}: {error}")
    return {
        "targets": len(targets), "scanned": scanned, "detected": detected,
        "auto_confirmed": auto_confirmed, "failed": failed, "errors": errors,
    }


def get_face_crop_bytes(face_id: int, padding_ratio: float = 0.18) -> tuple[bytes, str]:
    """レビュー用に顔周辺をJPEGで切り出す。永続保存はしない。"""
    cv2, _ = _load_dependencies()
    with closing(get_connection()) as connection:
        row = connection.execute(
            """
            SELECT photo_faces.*, photo_images.local_path, photo_images.bucket_key,
                   photo_images.file_name
            FROM photo_faces
            JOIN photo_images ON photo_images.id = photo_faces.image_id
            WHERE photo_faces.id = ?
            """, (int(face_id),),
        ).fetchone()
    if not row:
        raise ValueError("顔IDが見つかりません。")
    face = dict(row)
    with _local_image_path(face) as path:
        source = cv2.imread(path)
    if source is None:
        raise RuntimeError("画像をOpenCVで読み込めませんでした。")
    height, width = source.shape[:2]
    x, y = int(face["box_x"]), int(face["box_y"])
    w, h = int(face["box_width"]), int(face["box_height"])
    pad = int(max(w, h) * max(0.0, min(float(padding_ratio), 0.5)))
    x1, y1 = max(0, x-pad), max(0, y-pad)
    x2, y2 = min(width, x+w+pad), min(height, y+h+pad)
    crop = source[y1:y2, x1:x2]
    if crop.size == 0:
        raise RuntimeError("顔の切り出し範囲が不正です。")
    ok, encoded = cv2.imencode('.jpg', crop, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    if not ok:
        raise RuntimeError("顔画像をJPEGへ変換できませんでした。")
    return encoded.tobytes(), f"face_{int(face_id)}.jpg"
