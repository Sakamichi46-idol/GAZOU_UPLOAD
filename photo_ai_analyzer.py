import asyncio
import base64
import json
import mimetypes
import os
import traceback
import io
import tempfile
from contextlib import contextmanager

from PIL import Image, ImageOps

from pathlib import Path
from typing import Any

from openai import OpenAI

from bucket_storage import download_to_file

from ai_cost_control import can_send_image_to_api, finish_api_attempt, record_api_attempt
from ai_center import (
    effective_model,
    effective_prompt,
    phase3_preflight,
    record_cache_event,
)

from photo_database import (
    clear_ai_tags,
    copy_ai_result,
    find_reusable_analysis_by_hash,
    get_connection,
    get_pending_analysis_images,
    get_image_ai_review_gate,
    get_photo_image,
    save_ai_analysis,
    save_ai_tag,
    save_ai_usage,
    update_image_analysis_status,
)


# =========================
# AI解析設定
# =========================

OPENAI_API_KEY = os.getenv(
    "OPENAI_API_KEY",
    "",
).strip()

PHOTO_AI_MODEL = os.getenv(
    "PHOTO_AI_MODEL",
    "gpt-5-nano",
).strip()

PHOTO_AI_DETAIL = os.getenv(
    "PHOTO_AI_DETAIL",
    "low",
).strip().lower()


def get_env_int(
    name: str,
    default: int,
    minimum: int = 1,
) -> int:
    """
    環境変数を安全に整数へ変換する。
    不正な値の場合は既定値を使用する。
    """

    raw_value = os.getenv(
        name,
        str(default),
    )

    try:
        value = int(
            raw_value
        )

    except (
        TypeError,
        ValueError,
    ):
        value = default

    return max(
        value,
        minimum,
    )


def get_env_float(
    name: str,
    default: float,
    minimum: float = 0.1,
) -> float:
    """
    環境変数を安全に小数へ変換する。
    不正な値の場合は既定値を使用する。
    """

    raw_value = os.getenv(
        name,
        str(default),
    )

    try:
        value = float(
            raw_value
        )

    except (
        TypeError,
        ValueError,
    ):
        value = default

    return max(
        value,
        minimum,
    )


PHOTO_AI_BATCH_LIMIT = get_env_int(
    "PHOTO_AI_BATCH_LIMIT",
    3,
)

PHOTO_AI_REQUEST_TIMEOUT = get_env_float(
    "PHOTO_AI_REQUEST_TIMEOUT",
    120.0,
)


PHOTO_AI_MAX_DIMENSION = get_env_int(
    "PHOTO_AI_MAX_DIMENSION",
    512,
    minimum=256,
)

PHOTO_AI_JPEG_QUALITY = get_env_int(
    "PHOTO_AI_JPEG_QUALITY",
    70,
    minimum=40,
)

PHOTO_AI_MAX_FILE_SIZE = get_env_int(
    "PHOTO_AI_MAX_FILE_SIZE",
    20 * 1024 * 1024,
)

PHOTO_AI_REQUEST_INTERVAL = get_env_float(
    "PHOTO_AI_REQUEST_INTERVAL",
    1.0,
    minimum=0.0,
)

PHOTO_AI_MAX_OUTPUT_TOKENS = get_env_int(
    "PHOTO_AI_MAX_OUTPUT_TOKENS",
    500,
    minimum=100,
)

PHOTO_AI_INPUT_PRICE_PER_MILLION = get_env_float(
    "PHOTO_AI_INPUT_PRICE_PER_MILLION",
    0.05,
    minimum=0.0,
)

PHOTO_AI_CACHED_INPUT_PRICE_PER_MILLION = get_env_float(
    "PHOTO_AI_CACHED_INPUT_PRICE_PER_MILLION",
    0.005,
    minimum=0.0,
)

PHOTO_AI_OUTPUT_PRICE_PER_MILLION = get_env_float(
    "PHOTO_AI_OUTPUT_PRICE_PER_MILLION",
    0.40,
    minimum=0.0,
)


# =========================
# 許可値
# =========================

ALLOWED_DETAILS = {
    "low",
    "high",
    "auto",
}

ALLOWED_TAG_CATEGORIES = {
    "person_count",
    "composition",
    "expression",
    "clothing",
    "location",
    "background",
    "pose",
    "object",
    "season",
    "weather",
    "event",
    "other",
}


# =========================
# AI出力形式
# =========================

PHOTO_ANALYSIS_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {
            "type": "string",
            "description": (
                "画像全体の短い日本語説明。"
                "実在人物の名前は含めない。"
            ),
        },
        "person_count": {
            "type": "integer",
            "minimum": 0,
            "description": (
                "画像内に見える人物のおおよその人数。"
            ),
        },
        "clothing": {
            "type": "string",
            "description": (
                "主な服装。分からない場合は空文字。"
            ),
        },
        "expression": {
            "type": "string",
            "description": (
                "主な表情。分からない場合は空文字。"
            ),
        },
        "background": {
            "type": "string",
            "description": (
                "背景や場所。分からない場合は空文字。"
            ),
        },
        "pose": {
            "type": "string",
            "description": (
                "ポーズや構図。分からない場合は空文字。"
            ),
        },
        "objects": {
            "type": "array",
            "description": (
                "画像内に明確に見える主な物体。"
            ),
            "items": {
                "type": "string",
            },
        },
        "overall_confidence": {
            "type": "number",
            "minimum": 0,
            "maximum": 1,
            "description": (
                "解析全体の確信度。"
            ),
        },
        "needs_review": {
            "type": "boolean",
            "description": (
                "画像が不鮮明などで"
                "人間の確認が必要かどうか。"
            ),
        },
        "tags": {
            "type": "array",
            "description": (
                "画像検索に利用する日本語タグ。"
            ),
            "items": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "enum": [
                            "person_count",
                            "composition",
                            "expression",
                            "clothing",
                            "location",
                            "background",
                            "pose",
                            "object",
                            "season",
                            "weather",
                            "event",
                            "other",
                        ],
                    },
                    "tag": {
                        "type": "string",
                    },
                    "confidence": {
                        "type": "number",
                        "minimum": 0,
                        "maximum": 1,
                    },
                },
                "required": [
                    "category",
                    "tag",
                    "confidence",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "summary",
        "person_count",
        "clothing",
        "expression",
        "background",
        "pose",
        "objects",
        "overall_confidence",
        "needs_review",
        "tags",
    ],
    "additionalProperties": False,
}


# =========================
# プロンプト
# =========================

SYSTEM_PROMPT = """
あなたは写真検索データベース用の画像分類AIです。

画像を客観的に分析し、日本語の検索タグを作成してください。

重要なルール:

1. 実在人物の氏名や身元を推測・特定しないでください。
2. 芸能人、有名人、アイドルに見えても名前を出さないでください。
3. 人物は「1人」「2人」「複数人」など人数・構図だけ扱ってください。
4. 年齢、民族、国籍、宗教、健康状態などを推測しないでください。
5. 画像から明確に確認できる内容だけを出力してください。
6. 不鮮明な内容を無理に断定しないでください。
7. タグは検索しやすい短い日本語にしてください。
8. 似た意味のタグを大量に重複させないでください。
9. タグは原則として最大15件程度にしてください。
10. 人物が複数写っている場合でも、人物数を可能な範囲で数えてください。

タグの例:

person_count:
・人物なし
・1人
・2人
・3人
・複数人
・大人数

composition:
・自撮り
・集合写真
・ツーショット
・上半身
・全身
・顔アップ
・縦写真
・横写真

expression:
・笑顔
・無表情
・目を閉じる
・驚いた表情

clothing:
・私服
・制服風
・ドレス
・ライブ衣装
・和服
・浴衣
・帽子
・眼鏡

location:
・屋内
・屋外
・ステージ
・楽屋
・店内
・公園
・海
・街中

season:
・春
・夏
・秋
・冬

event:
・ライブ
・撮影
・誕生日
・旅行
・食事

明確に判定できないタグは追加しないでください。
""".strip()


# =========================
# 共通処理
# =========================

def clamp_confidence(
    value: Any,
) -> float:
    """
    信頼度を0.0から1.0へ収める。
    """

    try:
        number = float(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0.0

    return max(
        0.0,
        min(
            number,
            1.0,
        ),
    )


def normalize_text(
    value: Any,
) -> str:
    """
    値を安全な文字列へ変換する。
    """

    if value is None:
        return ""

    return str(
        value
    ).strip()


def normalize_person_count(
    value: Any,
) -> int:
    """
    人物数を0以上の整数へ変換する。
    """

    try:
        count = int(
            value
        )

    except (
        TypeError,
        ValueError,
    ):
        return 0

    return max(
        count,
        0,
    )


def get_image_detail() -> str:
    """
    APIへ送る画像detailを返す。
    """

    if PHOTO_AI_DETAIL in ALLOWED_DETAILS:
        return PHOTO_AI_DETAIL

    return "low"


def get_image_mime_type(
    image_path: str,
    stored_mime_type: str = "",
) -> str:
    """
    画像のMIMEタイプを取得する。
    """

    stored_mime_type = (
        normalize_text(
            stored_mime_type
        )
        .split(";")[0]
        .strip()
        .lower()
    )

    if stored_mime_type.startswith(
        "image/"
    ):
        return stored_mime_type

    guessed_type, _ = mimetypes.guess_type(
        image_path
    )

    if (
        guessed_type
        and guessed_type.startswith("image/")
    ):
        return guessed_type

    return "image/jpeg"


def image_to_data_url(
    image_path: str,
    mime_type: str,
) -> str:
    """AI送信用に画像を縮小・JPEG圧縮してBase64化する。"""

    try:
        with Image.open(image_path) as source:
            image = ImageOps.exif_transpose(source)
            if getattr(image, "is_animated", False):
                image.seek(0)
            image = image.convert("RGB")
            image.thumbnail(
                (PHOTO_AI_MAX_DIMENSION, PHOTO_AI_MAX_DIMENSION),
                Image.Resampling.LANCZOS,
            )
            buffer = io.BytesIO()
            image.save(
                buffer,
                format="JPEG",
                quality=min(PHOTO_AI_JPEG_QUALITY, 95),
                optimize=True,
            )
            payload = buffer.getvalue()
            mime_type = "image/jpeg"
    except Exception as error:
        print("AI用画像の縮小に失敗したため元画像を使用します:", error)
        with open(image_path, "rb") as image_file:
            payload = image_file.read()

    encoded_image = base64.b64encode(payload).decode("utf-8")
    return f"data:{mime_type};base64,{encoded_image}"


def validate_image_file(
    image_path: str,
) -> Path:
    """
    AI解析前に画像ファイルを確認する。
    """

    if not image_path:
        raise ValueError(
            "画像のlocal_pathが空です。"
        )

    path = Path(
        image_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"画像ファイルがありません: {image_path}"
        )

    if not path.is_file():
        raise ValueError(
            f"画像ファイルではありません: {image_path}"
        )

    file_size = path.stat().st_size

    if file_size <= 0:
        raise ValueError(
            f"画像ファイルが空です: {image_path}"
        )

    if file_size > PHOTO_AI_MAX_FILE_SIZE:
        raise ValueError(
            "AI解析可能サイズを超えています: "
            f"{file_size} bytes"
        )

    return path


def get_openai_client() -> OpenAI:
    """OpenAIクライアントを作成する。"""

    if not OPENAI_API_KEY:
        raise RuntimeError(
            "Railway Variablesに"
            "OPENAI_API_KEYが設定されていません。"
        )

    if not PHOTO_AI_MODEL:
        raise RuntimeError(
            "PHOTO_AI_MODELが空です。"
        )

    return OpenAI(
        api_key=OPENAI_API_KEY,
        timeout=PHOTO_AI_REQUEST_TIMEOUT,
    )


class AIResponseError(RuntimeError):
    """Responses APIが正常な解析本文を返さなかった場合の例外。"""

    def __init__(
        self,
        message: str,
        *,
        usage_data: dict[str, Any] | None = None,
        response_status: str = "",
        response_id: str = "",
    ) -> None:
        super().__init__(message)
        self.usage_data = usage_data or {}
        self.response_status = response_status
        self.response_id = response_id


def get_object_value(
    value: Any,
    name: str,
    default: Any = None,
) -> Any:
    """辞書とSDKオブジェクトの両方から値を取得する。"""

    if isinstance(value, dict):
        return value.get(name, default)

    return getattr(value, name, default)


def build_usage_data(response: Any) -> dict[str, Any]:
    """Responses APIの使用量を安全に辞書へ変換する。"""

    usage = get_object_value(response, "usage")
    input_details = get_object_value(
        usage,
        "input_tokens_details",
    )
    output_details = get_object_value(
        usage,
        "output_tokens_details",
    )

    return {
        "response_id": normalize_text(
            get_object_value(response, "id", "")
        ),
        "input_tokens": int(
            get_object_value(usage, "input_tokens", 0) or 0
        ),
        "output_tokens": int(
            get_object_value(usage, "output_tokens", 0) or 0
        ),
        "total_tokens": int(
            get_object_value(usage, "total_tokens", 0) or 0
        ),
        "cached_input_tokens": int(
            get_object_value(input_details, "cached_tokens", 0) or 0
        ),
        "reasoning_tokens": int(
            get_object_value(output_details, "reasoning_tokens", 0) or 0
        ),
    }


def extract_response_text(response: Any) -> str:
    """output_textが空の場合はresponse.output内も確認する。"""

    direct_text = normalize_text(
        get_object_value(response, "output_text", "")
    )

    if direct_text:
        return direct_text

    text_parts: list[str] = []
    output_items = get_object_value(response, "output", []) or []

    for item in output_items:
        content_items = get_object_value(item, "content", []) or []

        for content in content_items:
            if normalize_text(
                get_object_value(content, "type", "")
            ) != "output_text":
                continue

            text = normalize_text(
                get_object_value(content, "text", "")
            )

            if text:
                text_parts.append(text)

    return "\n".join(text_parts).strip()


def extract_response_refusal(response: Any) -> str:
    """response.output内に拒否理由があれば取得する。"""

    output_items = get_object_value(response, "output", []) or []

    for item in output_items:
        content_items = get_object_value(item, "content", []) or []

        for content in content_items:
            if normalize_text(
                get_object_value(content, "type", "")
            ) != "refusal":
                continue

            refusal = normalize_text(
                get_object_value(content, "refusal", "")
            )

            if refusal:
                return refusal

    return ""


def build_response_diagnostic(response: Any) -> dict[str, Any]:
    """Railwayログへ出す安全な診断情報を作る。"""

    incomplete_details = get_object_value(
        response,
        "incomplete_details",
    )
    response_error = get_object_value(response, "error")
    usage_data = build_usage_data(response)

    output_summary: list[dict[str, Any]] = []
    output_items = get_object_value(response, "output", []) or []

    for item in output_items:
        content_types: list[str] = []
        content_items = get_object_value(item, "content", []) or []

        for content in content_items:
            content_types.append(
                normalize_text(
                    get_object_value(content, "type", "")
                )
            )

        output_summary.append(
            {
                "type": normalize_text(
                    get_object_value(item, "type", "")
                ),
                "status": normalize_text(
                    get_object_value(item, "status", "")
                ),
                "content_types": content_types,
            }
        )

    return {
        "response_id": usage_data["response_id"],
        "status": normalize_text(
            get_object_value(response, "status", "")
        ),
        "incomplete_reason": normalize_text(
            get_object_value(incomplete_details, "reason", "")
        ),
        "error_code": normalize_text(
            get_object_value(response_error, "code", "")
        ),
        "error_message": normalize_text(
            get_object_value(response_error, "message", "")
        ),
        "refusal": extract_response_refusal(response)[:300],
        "output": output_summary,
        "usage": usage_data,
    }


# =========================
# AI通信
# =========================

def request_photo_analysis(
    image_path: str,
    stored_mime_type: str = "",
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    """OpenAIへ画像を送り、構造化された解析結果を取得する。"""

    validate_image_file(image_path)

    mime_type = get_image_mime_type(
        image_path,
        stored_mime_type,
    )

    image_data_url = image_to_data_url(
        image_path,
        mime_type,
    )

    client = get_openai_client()

    active_model = effective_model(PHOTO_AI_MODEL)
    active_prompt, active_prompt_version = effective_prompt(SYSTEM_PROMPT)

    request_options: dict[str, Any] = {
        "model": active_model,
        "store": False,
        "input": [
            {
                "role": "system",
                "content": [
                    {
                        "type": "input_text",
                        "text": active_prompt,
                    },
                ],
            },
            {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": (
                            "この画像を写真検索用に"
                            "分類してください。"
                        ),
                    },
                    {
                        "type": "input_image",
                        "image_url": image_data_url,
                        "detail": get_image_detail(),
                    },
                ],
            },
        ],
        "max_output_tokens": PHOTO_AI_MAX_OUTPUT_TOKENS,
        "text": {
            "verbosity": "low",
            "format": {
                "type": "json_schema",
                "name": "photo_analysis",
                "strict": True,
                "schema": PHOTO_ANALYSIS_SCHEMA,
            },
        },
    }

    # GPT-5系では推論量を抑え、JSON本文へ使える出力枠を確保する。
    if active_model.startswith("gpt-5"):
        request_options["reasoning"] = {
            "effort": "low",
        }

    response = client.responses.create(**request_options)

    usage_data = build_usage_data(response)
    response_status = normalize_text(
        get_object_value(response, "status", "")
    )
    raw_output = extract_response_text(response)

    if response_status != "completed" or not raw_output:
        diagnostic = build_response_diagnostic(response)

        print(
            "OpenAIレスポンス診断:",
            json.dumps(
                diagnostic,
                ensure_ascii=False,
                default=str,
            ),
        )

        incomplete_reason = normalize_text(
            diagnostic.get("incomplete_reason", "")
        )
        refusal = normalize_text(
            diagnostic.get("refusal", "")
        )
        api_error = normalize_text(
            diagnostic.get("error_message", "")
        )

        if incomplete_reason == "max_output_tokens":
            # 出力上限で途切れた場合は、より短い指示と広い出力枠で1回だけ再試行する。
            retry_options = dict(request_options)
            retry_options["max_output_tokens"] = min(max(PHOTO_AI_MAX_OUTPUT_TOKENS * 2, 2048), 8192)
            retry_options["input"] = [
                {"role": "system", "content": [{"type": "input_text", "text": active_prompt}]},
                {"role": "user", "content": [
                    {"type": "input_text", "text": "JSONスキーマに必要な項目だけを最短で返してください。説明文は不要です。"},
                    {"type": "input_image", "image_url": image_data_url, "detail": "low"},
                ]},
            ]
            retry_response = client.responses.create(**retry_options)
            retry_status = normalize_text(get_object_value(retry_response, "status", ""))
            retry_output = extract_response_text(retry_response)
            if retry_status == "completed" and retry_output:
                response = retry_response
                raw_output = retry_output
                usage_data = build_usage_data(retry_response)
                response_status = retry_status
                diagnostic = {}
                incomplete_reason = ""
            else:
                print("AI短縮再試行も未完了:", json.dumps(build_response_diagnostic(retry_response), ensure_ascii=False, default=str))

        if response_status != "completed" or not raw_output:
            if incomplete_reason:
                message = (
                    "AI解析が未完了で終了しました。"
                    f" status={response_status},"
                    f" reason={incomplete_reason},"
                    f" reasoning_tokens="
                    f"{usage_data.get('reasoning_tokens', 0)}"
                )
            elif refusal:
                message = (
                    "AIが画像解析を拒否しました。"
                    f" refusal={refusal[:300]}"
                )
            elif api_error:
                message = (
                    "OpenAI APIが失敗を返しました。"
                    f" error={api_error[:300]}"
                )
            elif not raw_output:
                message = (
                    "AIの解析本文が空でした。"
                    f" status={response_status or 'unknown'},"
                    f" reasoning_tokens="
                    f"{usage_data.get('reasoning_tokens', 0)}"
                )
            else:
                message = (
                    "AI解析レスポンスが完了状態ではありません。"
                    f" status={response_status or 'unknown'}"
                )

            raise AIResponseError(
                message,
                usage_data=usage_data,
                response_status=response_status,
                response_id=usage_data.get("response_id", ""),
            )

    try:
        analysis = json.loads(raw_output)
    except json.JSONDecodeError as error:
        print(
            "AI解析JSON読込失敗:",
            f"response_id={usage_data.get('response_id', '')}",
            f"先頭500文字={raw_output[:500]}",
        )
        raise AIResponseError(
            "AI解析結果をJSONとして読み込めませんでした。",
            usage_data=usage_data,
            response_status=response_status,
            response_id=usage_data.get("response_id", ""),
        ) from error

    if not isinstance(analysis, dict):
        raise AIResponseError(
            "AI解析結果が辞書形式ではありません。",
            usage_data=usage_data,
            response_status=response_status,
            response_id=usage_data.get("response_id", ""),
        )

    return analysis, raw_output, usage_data


def calculate_estimated_cost(usage_data: dict[str, Any]) -> dict[str, float]:
    """Responses APIのusageから推定料金を計算する。"""

    input_tokens = max(int(usage_data.get("input_tokens", 0) or 0), 0)
    cached_tokens = max(int(usage_data.get("cached_input_tokens", 0) or 0), 0)
    uncached_tokens = max(input_tokens - cached_tokens, 0)
    output_tokens = max(int(usage_data.get("output_tokens", 0) or 0), 0)

    input_cost = uncached_tokens / 1_000_000 * PHOTO_AI_INPUT_PRICE_PER_MILLION
    cached_cost = cached_tokens / 1_000_000 * PHOTO_AI_CACHED_INPUT_PRICE_PER_MILLION
    output_cost = output_tokens / 1_000_000 * PHOTO_AI_OUTPUT_PRICE_PER_MILLION

    return {
        "input_cost_usd": input_cost,
        "cached_input_cost_usd": cached_cost,
        "output_cost_usd": output_cost,
        "estimated_cost_usd": input_cost + cached_cost + output_cost,
    }


# =========================
# タグ整理
# =========================

def build_person_count_tags(
    person_count: int,
) -> list[dict[str, Any]]:
    """
    人物数から確実な補助タグを作る。
    """

    tags: list[dict[str, Any]] = []

    if person_count <= 0:

        tags.append(
            {
                "category": "person_count",
                "tag": "人物なし",
                "confidence": 1.0,
            }
        )

    elif person_count == 1:

        tags.append(
            {
                "category": "person_count",
                "tag": "1人",
                "confidence": 1.0,
            }
        )

    elif person_count == 2:

        tags.extend(
            [
                {
                    "category": "person_count",
                    "tag": "2人",
                    "confidence": 1.0,
                },
                {
                    "category": "composition",
                    "tag": "ツーショット",
                    "confidence": 0.95,
                },
                {
                    "category": "composition",
                    "tag": "複数人",
                    "confidence": 1.0,
                },
            ]
        )

    elif person_count == 3:

        tags.extend(
            [
                {
                    "category": "person_count",
                    "tag": "3人",
                    "confidence": 1.0,
                },
                {
                    "category": "composition",
                    "tag": "複数人",
                    "confidence": 1.0,
                },
            ]
        )

    else:

        tags.append(
            {
                "category": "composition",
                "tag": "複数人",
                "confidence": 1.0,
            }
        )

        if person_count >= 6:

            tags.append(
                {
                    "category": "composition",
                    "tag": "大人数",
                    "confidence": 0.95,
                }
            )

    return tags


def normalize_tags(
    analysis: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    AIタグを検証して重複を除去する。
    """

    person_count = normalize_person_count(
        analysis.get(
            "person_count",
            0,
        )
    )

    source_tags_value = analysis.get(
        "tags",
        [],
    )

    if isinstance(
        source_tags_value,
        list,
    ):
        source_tags = list(
            source_tags_value
        )

    else:
        source_tags = []

    source_tags.extend(
        build_person_count_tags(
            person_count
        )
    )

    normalized_tags: list[
        dict[str, Any]
    ] = []

    seen: set[
        tuple[str, str]
    ] = set()

    for source_tag in source_tags:

        if not isinstance(
            source_tag,
            dict,
        ):
            continue

        category = normalize_text(
            source_tag.get(
                "category",
                "other",
            )
        )

        tag = normalize_text(
            source_tag.get(
                "tag",
                "",
            )
        )

        confidence = clamp_confidence(
            source_tag.get(
                "confidence",
                0,
            )
        )

        if not tag:
            continue

        if category not in ALLOWED_TAG_CATEGORIES:
            category = "other"

        key = (
            category,
            tag,
        )

        if key in seen:
            continue

        seen.add(
            key
        )

        normalized_tags.append(
            {
                "category": category,
                "tag": tag,
                "confidence": confidence,
            }
        )

    return normalized_tags


# =========================
# DB保存
# =========================

def save_analysis_result(
    image_id: int,
    analysis: dict[str, Any],
    raw_output: str,
) -> dict[str, Any]:
    """
    AI解析結果とタグをDBへ保存する。
    """

    person_count = normalize_person_count(
        analysis.get(
            "person_count",
            0,
        )
    )

    overall_confidence = clamp_confidence(
        analysis.get(
            "overall_confidence",
            0,
        )
    )

    needs_review = (
        analysis.get(
            "needs_review",
            False,
        )
        is True
    )

    summary = normalize_text(
        analysis.get(
            "summary",
            "",
        )
    )

    clothing = normalize_text(
        analysis.get(
            "clothing",
            "",
        )
    )

    expression = normalize_text(
        analysis.get(
            "expression",
            "",
        )
    )

    background = normalize_text(
        analysis.get(
            "background",
            "",
        )
    )

    pose = normalize_text(
        analysis.get(
            "pose",
            "",
        )
    )

    objects_value = analysis.get(
        "objects",
        [],
    )

    if not isinstance(
        objects_value,
        list,
    ):
        objects_value = []

    objects = [
        normalize_text(item)
        for item in objects_value
        if normalize_text(item)
    ]

    object_text = json.dumps(
        objects,
        ensure_ascii=False,
    )

    tags = normalize_tags(
        analysis
    )

    save_ai_analysis(
        image_id=image_id,
        model_name=PHOTO_AI_MODEL,
        raw_response=raw_output,
        person_name="",
        clothing=clothing,
        expression=expression,
        background=background,
        pose=pose,
        objects=object_text,
        person_count=person_count,
        overall_confidence=overall_confidence,
        needs_review=needs_review,
    )

    # 再解析時に古いタグが残らないよう、
    # 新しいタグを書き込む前に既存AIタグを削除する。
    clear_ai_tags(
        image_id
    )

    for tag_data in tags:

        save_ai_tag(
            image_id=image_id,
            category=tag_data["category"],
            tag=tag_data["tag"],
            confidence=tag_data["confidence"],
            model_name=PHOTO_AI_MODEL,
            raw_value=summary,
        )

    final_status = (
        "review"
        if needs_review
        else "completed"
    )

    update_image_analysis_status(
        image_id,
        final_status,
        "",
    )

    return {
        "image_id": image_id,
        "status": final_status,
        "person_count": person_count,
        "tag_count": len(tags),
        "summary": summary,
        "overall_confidence": (
            overall_confidence
        ),
        "needs_review": needs_review,
    }


@contextmanager
def materialize_analysis_image(image: dict[str, Any]):
    """AI解析用画像をローカルまたはBucketから一時的に用意する。"""

    local_path = normalize_text(image.get("local_path", ""))
    if local_path and Path(local_path).is_file():
        yield local_path
        return

    bucket_key = normalize_text(image.get("bucket_key", ""))
    if not bucket_key:
        if local_path:
            raise FileNotFoundError(f"画像ファイルがありません: {local_path}")
        raise ValueError("画像のlocal_pathとbucket_keyが両方空です。")

    file_name = normalize_text(image.get("file_name", "")) or "analysis-image"
    suffix = Path(file_name).suffix or mimetypes.guess_extension(
        normalize_text(image.get("mime_type", ""))
    ) or ".img"

    with tempfile.TemporaryDirectory(prefix="photo-ai-") as temp_dir:
        temp_path = str(Path(temp_dir) / f"image{suffix}")
        download_to_file(key=bucket_key, file_path=temp_path)
        validate_image_file(temp_path)
        print(
            "AI解析用画像をBucketから一時取得:",
            f"image_id={image.get('id', '')}",
            f"bucket_key={bucket_key}",
        )
        yield temp_path




def _refresh_local_face_candidates_after_analysis(image_id: int) -> dict[str, Any]:
    """
    AI解析成功後にローカル顔候補を更新する。

    - OpenAI APIは呼ばない。
    - 既に顔スキャン済みなら再スキャンせず既存の顔を利用する。
    - 顔認識側の失敗でAI解析本体を失敗扱いにはしない。
    - すでに人物確定済みの顔は候補再生成の対象外。
    """
    from local_face_recognition import detect_faces_for_image, suggest_face_candidates

    summary: dict[str, Any] = {
        "attempted": True,
        "scan_reused": False,
        "detected": 0,
        "auto_confirmed": 0,
        "candidate_faces": 0,
        "candidate_count": 0,
        "error": "",
    }

    try:
        with get_connection() as con:
            scan = con.execute(
                """
                SELECT status, detected_faces, auto_confirmed_faces
                FROM photo_face_scans
                WHERE image_id=?
                """,
                (int(image_id),),
            ).fetchone()

        if scan and str(scan[0] or "") == "completed":
            summary["scan_reused"] = True
            summary["detected"] = int(scan[1] or 0)
            summary["auto_confirmed"] = int(scan[2] or 0)
        else:
            scan_result = detect_faces_for_image(int(image_id))
            summary["detected"] = int(scan_result.get("detected", 0) or 0)
            summary["auto_confirmed"] = int(scan_result.get("auto_confirmed", 0) or 0)

        # 顔がない画像、または全顔が自動確定済みなら候補生成は不要。
        if summary["detected"] <= 0:
            return summary

        candidate_result = suggest_face_candidates(int(image_id))
        summary["candidate_faces"] = sum(1 for item in candidate_result if item.get("candidates"))
        summary["candidate_count"] = sum(len(item.get("candidates") or []) for item in candidate_result)
        return summary

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        print(
            "AI解析後のローカル顔候補更新に失敗:",
            f"image_id={image_id}",
            summary["error"],
        )
        return summary

# =========================
# 画像1枚の解析
# =========================

def analyze_photo_image_sync(
    image_id: int,
    *,
    manual_api: bool = False,
) -> dict[str, Any]:
    """
    画像1枚を同期処理で解析する。
    """

    image = get_photo_image(
        image_id
    )

    if image is None:
        raise ValueError(
            f"画像IDが見つかりません: {image_id}"
        )

    # 有料API・キャッシュ再利用を含むAI解析の前に、
    # 画像が属するブログの人物確認が100%完了していることを必須にする。
    review_gate = get_image_ai_review_gate(image_id)
    if not review_gate.get("is_completed"):
        reason = str(review_gate.get("reason") or "人物確認待ち")
        # analysis_status は pending のまま維持し、人物確認完了後のバッチ対象に残す。
        update_image_analysis_status(image_id, "pending", reason[:1000])
        return {
            "image_id": image_id,
            "status": "waiting_person_review",
            "api_sent": False,
            "waiting_person_review": True,
            "blog_id": int(review_gate.get("blog_id") or 0),
            "review_completed": int(review_gate.get("completed") or 0),
            "review_total": int(review_gate.get("total") or 0),
            "review_pending": int(review_gate.get("pending") or 0),
            "reason": reason,
        }

    image_path = normalize_text(
        image.get(
            "local_path",
            "",
        )
    )

    mime_type = normalize_text(
        image.get(
            "mime_type",
            "",
        )
    )

    update_image_analysis_status(
        image_id,
        "processing",
        "",
    )

    try:

        image_hash = normalize_text(image.get("image_hash", ""))
        reusable = find_reusable_analysis_by_hash(image_id, image_hash)
        if reusable is not None:
            source_image_id = int(reusable["source_image_id"])
            if copy_ai_result(source_image_id, image_id):
                copied = get_photo_image(image_id) or {}
                final_status = normalize_text(copied.get("analysis_status", "completed"))
                save_ai_usage(
                    image_id=image_id,
                    source_image_id=source_image_id,
                    model_name="",
                    request_kind="cache_reuse",
                    status=final_status,
                )
                print(
                    "AI解析結果を同一画像から再利用:",
                    f"image_id={image_id}",
                    f"source_image_id={source_image_id}",
                )
                try:
                    record_cache_event(image_id, "image_hash", True, True, f"source_image_id={source_image_id}")
                except Exception:
                    pass
                face_candidates = _refresh_local_face_candidates_after_analysis(image_id)
                return {
                    "image_id": image_id,
                    "status": final_status,
                    "reused": True,
                    "source_image_id": source_image_id,
                    "api_sent": False,
                    "face_candidates": face_candidates,
                }

        preflight = phase3_preflight(image_id, image_hash)
        # 人物判定がローカルで十分でも、画像内容タグはOpenAIで生成する。
        # ここでは「顔はローカルで解決済み」という事実だけ記録し、
        # 画像全体のAIタグ解析を丸ごとスキップしない。
        if preflight.get("local_face_complete"):
            try:
                record_cache_event(
                    image_id,
                    "local_face_resolved",
                    True,
                    False,
                    "人物判定はローカルで十分。OpenAIは画像タグ生成のため実行。",
                )
            except Exception:
                pass

        api_allowed, block_reason = can_send_image_to_api(manual=manual_api)
        if not api_allowed:
            # 自動巡回では、OFF・上限到達を失敗記録として毎分増やさない。
            # 管理者の手動操作だけ、ブロック理由を監査用に残す。
            if manual_api:
                record_api_attempt(
                    image_id,
                    trigger_kind="manual",
                    allowed=False,
                    result_status="blocked",
                    reason=block_reason,
                )
            update_image_analysis_status(image_id, "pending", block_reason[:1000])
            return {
                "image_id": image_id,
                "status": "blocked",
                "api_blocked": True,
                "api_sent": False,
                "reason": block_reason,
            }

        api_attempt_id = record_api_attempt(
            image_id, trigger_kind="manual" if manual_api else "automatic",
            allowed=True, result_status="started",
        )

        with materialize_analysis_image(image) as analysis_image_path:
            analysis, raw_output, usage_data = (
                request_photo_analysis(
                    image_path=analysis_image_path,
                    stored_mime_type=mime_type,
                )
            )

        result = save_analysis_result(
            image_id=image_id,
            analysis=analysis,
            raw_output=raw_output,
        )

        costs = calculate_estimated_cost(usage_data)
        save_ai_usage(
            image_id=image_id,
            model_name=effective_model(PHOTO_AI_MODEL),
            request_kind="api",
            status=str(result.get("status", "completed")),
            input_tokens=int(usage_data.get("input_tokens", 0) or 0),
            cached_input_tokens=int(usage_data.get("cached_input_tokens", 0) or 0),
            output_tokens=int(usage_data.get("output_tokens", 0) or 0),
            total_tokens=int(usage_data.get("total_tokens", 0) or 0),
            response_id=str(usage_data.get("response_id", "")),
            **costs,
        )
        result["usage"] = usage_data
        result["estimated_cost_usd"] = costs["estimated_cost_usd"]
        result["api_sent"] = True
        result["face_candidates"] = _refresh_local_face_candidates_after_analysis(image_id)
        finish_api_attempt(api_attempt_id, status="completed")

        print(
            "AI画像解析完了:",
            f"image_id={image_id}",
            f"人物数={result['person_count']}",
            f"タグ数={result['tag_count']}",
            f"状態={result['status']}",
        )

        return result

    except Exception as error:

        error_message = (
            f"{type(error).__name__}: {error}"
        )

        if isinstance(error, AIResponseError):
            failure_usage = error.usage_data
            failure_costs = calculate_estimated_cost(failure_usage)

            try:
                save_ai_usage(
                    image_id=image_id,
                    model_name=PHOTO_AI_MODEL,
                    request_kind="api",
                    status="failed",
                    input_tokens=int(
                        failure_usage.get("input_tokens", 0) or 0
                    ),
                    cached_input_tokens=int(
                        failure_usage.get("cached_input_tokens", 0) or 0
                    ),
                    output_tokens=int(
                        failure_usage.get("output_tokens", 0) or 0
                    ),
                    total_tokens=int(
                        failure_usage.get("total_tokens", 0) or 0
                    ),
                    response_id=str(
                        failure_usage.get("response_id", "")
                    ),
                    **failure_costs,
                )
            except Exception as usage_error:
                print(
                    "AI失敗時の使用量保存に失敗:",
                    f"image_id={image_id}",
                    f"{type(usage_error).__name__}: {usage_error}",
                )

        if "api_attempt_id" in locals():
            finish_api_attempt(api_attempt_id, status="failed", reason=error_message)

        update_image_analysis_status(
            image_id,
            "failed",
            error_message[:1000],
        )

        print(
            "AI画像解析失敗:",
            f"image_id={image_id}",
            error_message,
        )

        traceback.print_exc()

        return {
            "image_id": image_id,
            "status": "failed",
            "api_sent": bool("api_attempt_id" in locals()),
            "error": error_message,
        }


async def analyze_photo_image(
    image_id: int,
    *,
    manual_api: bool = False,
) -> dict[str, Any]:
    """
    Discord Botのイベントループを止めずに
    画像1枚を解析する。
    """

    return await asyncio.to_thread(
        analyze_photo_image_sync,
        image_id,
        manual_api=manual_api,
    )


# =========================
# 未解析画像の一括処理
# =========================

async def analyze_pending_images(
    limit: int | None = None,
    *,
    manual_api: bool = False,
) -> dict[str, Any]:
    """
    ダウンロード済みの未解析画像を
    指定件数だけ解析する。
    """

    if limit is None:
        limit = PHOTO_AI_BATCH_LIMIT

    limit = max(
        int(limit),
        1,
    )

    images = await asyncio.to_thread(
        get_pending_analysis_images,
        limit,
    )

    results: list[
        dict[str, Any]
    ] = []

    completed = 0
    review = 0
    failed = 0
    blocked = 0
    waiting_person_review = 0
    cache_reused = 0
    api_sent = 0
    blocked_reasons: dict[str, int] = {}

    for image in images:

        image_id = int(
            image["id"]
        )

        result = await analyze_photo_image(
            image_id, manual_api=manual_api
        )

        results.append(
            result
        )

        status = result.get(
            "status",
            "",
        )

        if result.get("reused"):
            cache_reused += 1

        if result.get("api_sent"):
            api_sent += 1

        if status == "completed":
            completed += 1
        elif status == "review":
            review += 1
        elif status == "blocked":
            blocked += 1
        elif status == "waiting_person_review":
            waiting_person_review += 1
            reason = str(result.get("reason") or "理由不明")
            blocked_reasons[reason] = blocked_reasons.get(reason, 0) + 1
        else:
            failed += 1

        if PHOTO_AI_REQUEST_INTERVAL > 0 and result.get("api_sent"):
            # 実際にAPIへ送った場合だけ待機する。
            await asyncio.sleep(PHOTO_AI_REQUEST_INTERVAL)

    return {
        "requested": limit,
        "found": len(images),
        "completed": completed,
        "review": review,
        "failed": failed,
        "blocked": blocked,
        "waiting_person_review": waiting_person_review,
        "cache_reused": cache_reused,
        "api_sent": api_sent,
        "blocked_reasons": blocked_reasons,
        "results": results,
    }


# =========================
# 設定確認
# =========================

def get_photo_ai_status() -> dict[str, Any]:
    """
    AI解析機能の設定状況を返す。
    APIキーそのものは返さない。
    """

    return {
        "enabled": bool(
            OPENAI_API_KEY
        ),
        "model": PHOTO_AI_MODEL,
        "detail": get_image_detail(),
        "batch_limit": (
            PHOTO_AI_BATCH_LIMIT
        ),
        "request_timeout": (
            PHOTO_AI_REQUEST_TIMEOUT
        ),
        "max_file_size": (
            PHOTO_AI_MAX_FILE_SIZE
        ),
        "request_interval": (
            PHOTO_AI_REQUEST_INTERVAL
        ),
        "max_dimension": PHOTO_AI_MAX_DIMENSION,
        "jpeg_quality": PHOTO_AI_JPEG_QUALITY,
        "max_output_tokens": PHOTO_AI_MAX_OUTPUT_TOKENS,
        "input_price_per_million": PHOTO_AI_INPUT_PRICE_PER_MILLION,
        "cached_input_price_per_million": PHOTO_AI_CACHED_INPUT_PRICE_PER_MILLION,
        "output_price_per_million": PHOTO_AI_OUTPUT_PRICE_PER_MILLION,
    }


# =========================
# 単体実行
# =========================

async def main() -> None:
    """
    このファイルを直接実行した場合のテスト。
    """

    status = get_photo_ai_status()

    print("=" * 50)
    print("写真AI解析設定")
    print(
        "有効:",
        status["enabled"],
    )
    print(
        "モデル:",
        status["model"],
    )
    print(
        "画像detail:",
        status["detail"],
    )
    print(
        "一度の解析件数:",
        status["batch_limit"],
    )
    print(
        "解析間隔:",
        status["request_interval"],
    )
    print("=" * 50)

    if not status["enabled"]:

        print(
            "OPENAI_API_KEYが"
            "設定されていません。"
        )

        return

    result = await analyze_pending_images()

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":

    asyncio.run(
        main()
    )
