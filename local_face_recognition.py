"""Optional, on-demand local face recognition for the photo archive.

This module never calls OpenAI.  OpenCV is imported lazily so the normal bot
continues to run without the optional face-recognition dependencies.
"""
from __future__ import annotations

import base64
import math
import os
import tempfile
import zlib
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Any, Iterator

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
DEFAULT_MATCH_THRESHOLD = 0.86


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
    if not bucket_key:
        raise FileNotFoundError("ローカル画像もBucketキーも見つかりません。")
    if not bucket_is_configured():
        raise RuntimeError("Bucket設定が不足しているため画像を取得できません。")

    suffix = Path(str(image.get("file_name") or "image.jpg")).suffix or ".jpg"
    with tempfile.TemporaryDirectory(prefix="face-scan-") as directory:
        path = str(Path(directory) / f"source{suffix}")
        download_to_file(key=bucket_key, file_path=path)
        yield path


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


def detect_faces_for_image(image_id: int) -> dict[str, Any]:
    """Detect faces and save compact local embeddings. Runs only on command."""
    cv2, np = _load_dependencies()
    image = get_photo_image(int(image_id))
    if not image:
        raise ValueError("画像IDが見つかりません。")

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
    min_side = max(32, min(width, height) // 12)
    boxes = detector.detectMultiScale(
        gray,
        scaleFactor=1.1,
        minNeighbors=5,
        minSize=(min_side, min_side),
    )
    boxes = sorted(boxes, key=lambda box: (int(box[1]), int(box[0])))
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
            WHERE photo_faces.model_name = ?
              AND photo_faces.id <> ?
              AND photo_faces.face_embedding <> ''
              AND photo_faces.confirmation_status IN ('confirmed','manually_confirmed','auto_seeded')
              {group_sql}
            """,
            tuple(params),
        ).fetchall()
        return [dict(row) for row in rows]


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
        encoded = str(face.get("face_embedding") or "")
        if not encoded:
            continue
        target = _decode_embedding(encoded, np)
        best_by_person: dict[int, dict[str, Any]] = {}
        for reference in _reference_embeddings(int(face["id"]), group_name):
            try:
                vector = _decode_embedding(str(reference["face_embedding"]), np)
            except Exception:
                continue
            similarity = float(np.dot(target, vector))
            person_id = int(reference["person_id"])
            current = best_by_person.get(person_id)
            if current is None or similarity > float(current["confidence"]):
                best_by_person[person_id] = {
                    "person_id": person_id,
                    "person_name": str(reference["person_name"]),
                    "confidence": max(0.0, min((similarity + 1.0) / 2.0, 1.0)),
                }
        candidates = sorted(best_by_person.values(), key=lambda item: item["confidence"], reverse=True)[:max(1, min(limit_per_face, 10))]
        for rank, candidate in enumerate(candidates, 1):
            save_face_candidate(
                int(face["id"]), int(candidate["person_id"]), float(candidate["confidence"]),
                candidate_rank=rank, model_name=MODEL_NAME,
                raw_value=f"cosine-derived:{candidate['confidence']:.6f}",
            )
        add_face_review(int(face["id"]), "ローカル顔候補を確認してください。", candidates)
        result.append({"face_id": int(face["id"]), "candidates": candidates})
    return result


def get_face_summary(image_id: int) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for face in get_image_faces(int(image_id)):
        item = dict(face)
        item["candidates"] = get_face_candidates(int(face["id"]))[:5]
        result.append(item)
    return result
