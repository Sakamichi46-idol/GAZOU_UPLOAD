"""Local clustering for pending face reviews.

This module never calls OpenAI. It groups pending face embeddings by cosine
similarity so a reviewer can label several likely-identical faces at once.
"""
from __future__ import annotations

import base64
import zlib
from typing import Any

from photo_database import get_pending_face_embeddings

EMBEDDING_SIZE = 32
MAX_CLUSTER_INPUT = 500


class FaceClusteringUnavailable(RuntimeError):
    """Raised when NumPy or valid face embeddings are unavailable."""


def _load_numpy():
    try:
        import numpy as np  # type: ignore
    except ImportError as error:
        raise FaceClusteringUnavailable(
            "顔クラスタリングにはNumPyが必要です。requirements.txtを確認してください。"
        ) from error
    return np


def _decode_embedding(value: str, np: Any):
    raw = zlib.decompress(base64.b64decode(value.encode("ascii")))
    vector = np.frombuffer(raw, dtype=np.float32)
    expected = EMBEDDING_SIZE * EMBEDDING_SIZE
    if vector.size != expected:
        raise ValueError(f"Unsupported face embedding size: {vector.size}")
    norm = float(np.linalg.norm(vector))
    if norm <= 0:
        raise ValueError("Zero-length face embedding")
    return vector / norm


def cluster_pending_faces(
    limit: int = 200,
    similarity_threshold: float = 0.90,
    *,
    minimum_cluster_size: int = 2,
) -> dict[str, Any]:
    """Group pending face reviews using connected components.

    Similarity is cosine similarity. Only clusters with at least
    ``minimum_cluster_size`` members are returned; remaining faces are counted
    as singletons. The operation is read-only.
    """
    np = _load_numpy()
    safe_limit = max(2, min(int(limit), MAX_CLUSTER_INPUT))
    threshold = max(0.70, min(float(similarity_threshold), 0.9999))
    min_size = max(2, int(minimum_cluster_size))

    rows = get_pending_face_embeddings(safe_limit)
    valid_rows: list[dict[str, Any]] = []
    vectors: list[Any] = []
    invalid = 0
    for row in rows:
        value = str(row.get("face_embedding") or "").strip()
        if not value:
            invalid += 1
            continue
        try:
            vectors.append(_decode_embedding(value, np))
            valid_rows.append(row)
        except Exception:
            invalid += 1

    if len(valid_rows) < 2:
        return {
            "clusters": [],
            "input_count": len(rows),
            "valid_count": len(valid_rows),
            "invalid_count": invalid,
            "singleton_count": len(valid_rows),
            "threshold": threshold,
        }

    matrix = np.vstack(vectors)
    similarities = matrix @ matrix.T

    parent = list(range(len(valid_rows)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        root_left = find(left)
        root_right = find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for left in range(len(valid_rows)):
        matches = np.where(similarities[left, left + 1 :] >= threshold)[0]
        for offset in matches.tolist():
            union(left, left + 1 + int(offset))

    grouped: dict[int, list[int]] = {}
    for index in range(len(valid_rows)):
        grouped.setdefault(find(index), []).append(index)

    clusters: list[dict[str, Any]] = []
    singleton_count = 0
    for indices in grouped.values():
        if len(indices) < min_size:
            singleton_count += len(indices)
            continue
        submatrix = similarities[np.ix_(indices, indices)]
        pair_values = submatrix[np.triu_indices(len(indices), k=1)]
        clusters.append(
            {
                "items": [valid_rows[index] for index in indices],
                "size": len(indices),
                "minimum_similarity": float(pair_values.min()) if pair_values.size else 1.0,
                "average_similarity": float(pair_values.mean()) if pair_values.size else 1.0,
                "maximum_similarity": float(pair_values.max()) if pair_values.size else 1.0,
            }
        )

    clusters.sort(
        key=lambda cluster: (
            -int(cluster["size"]),
            -float(cluster["average_similarity"]),
            int(cluster["items"][0]["face_id"]),
        )
    )
    return {
        "clusters": clusters,
        "input_count": len(rows),
        "valid_count": len(valid_rows),
        "invalid_count": invalid,
        "singleton_count": singleton_count,
        "threshold": threshold,
    }
