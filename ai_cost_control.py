"""Low-cost AI usage controls for the photo archive bot.

The default policy is intentionally conservative for hobby operation:
- automatic paid image API calls are disabled;
- explicit administrator requests are allowed only within daily/monthly limits;
- cache reuse and local processing never consume the API allowance.
"""
from __future__ import annotations

import os
from contextlib import closing
from datetime import datetime, timezone
from typing import Any

from photo_database import get_connection

DEFAULT_AUTO_API = str(os.getenv("PHOTO_AI_AUTO_API_ENABLED", "0")).lower() in {"1", "true", "yes", "on"}
DEFAULT_DAILY_LIMIT = max(int(os.getenv("PHOTO_AI_DAILY_IMAGE_LIMIT", "20") or 20), 0)
DEFAULT_MONTHLY_LIMIT = max(int(os.getenv("PHOTO_AI_MONTHLY_IMAGE_LIMIT", "300") or 300), 0)
DEFAULT_FAILURE_STOP = max(int(os.getenv("PHOTO_AI_CONSECUTIVE_FAILURE_STOP", "3") or 3), 1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def init_ai_cost_control_schema() -> None:
    with closing(get_connection()) as con:
        con.executescript(
            """
            CREATE TABLE IF NOT EXISTS photo_ai_cost_settings (
                id INTEGER PRIMARY KEY CHECK(id=1),
                auto_api_enabled INTEGER NOT NULL DEFAULT 0,
                daily_image_limit INTEGER NOT NULL DEFAULT 20,
                monthly_image_limit INTEGER NOT NULL DEFAULT 300,
                consecutive_failure_stop INTEGER NOT NULL DEFAULT 3,
                is_paused INTEGER NOT NULL DEFAULT 0,
                pause_reason TEXT NOT NULL DEFAULT '',
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS photo_ai_api_attempts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                image_id INTEGER NOT NULL DEFAULT 0,
                trigger_kind TEXT NOT NULL DEFAULT 'automatic',
                allowed INTEGER NOT NULL DEFAULT 0,
                result_status TEXT NOT NULL DEFAULT '',
                reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_photo_ai_api_attempts_time
              ON photo_ai_api_attempts(created_at DESC);
            CREATE INDEX IF NOT EXISTS idx_photo_ai_api_attempts_image
              ON photo_ai_api_attempts(image_id, created_at DESC);
            """
        )
        con.execute(
            """INSERT OR IGNORE INTO photo_ai_cost_settings(
                   id,auto_api_enabled,daily_image_limit,monthly_image_limit,
                   consecutive_failure_stop,is_paused,pause_reason,updated_at
               ) VALUES(1,?,?,?,?,0,'',?)""",
            (
                1 if DEFAULT_AUTO_API else 0,
                DEFAULT_DAILY_LIMIT,
                DEFAULT_MONTHLY_LIMIT,
                DEFAULT_FAILURE_STOP,
                _now(),
            ),
        )
        con.commit()


def get_ai_cost_settings() -> dict[str, Any]:
    init_ai_cost_control_schema()
    with closing(get_connection()) as con:
        row = con.execute("SELECT * FROM photo_ai_cost_settings WHERE id=1").fetchone()
        return dict(row) if row else {}


def update_ai_cost_settings(**values: Any) -> dict[str, Any]:
    init_ai_cost_control_schema()
    allowed = {
        "auto_api_enabled", "daily_image_limit", "monthly_image_limit",
        "consecutive_failure_stop", "is_paused", "pause_reason",
    }
    pairs = [(key, values[key]) for key in values if key in allowed]
    if not pairs:
        return get_ai_cost_settings()
    assignments = ",".join(f"{key}=?" for key, _ in pairs)
    params = [value for _, value in pairs] + [_now()]
    with closing(get_connection()) as con:
        con.execute(
            f"UPDATE photo_ai_cost_settings SET {assignments},updated_at=? WHERE id=1",
            tuple(params),
        )
        con.commit()
    return get_ai_cost_settings()


def _period_counts(con) -> tuple[int, int]:
    now = datetime.now(timezone.utc)
    day = now.strftime("%Y-%m-%d")
    month = now.strftime("%Y-%m")
    daily = int(con.execute(
        """SELECT COUNT(*) FROM photo_ai_api_attempts
           WHERE allowed=1 AND result_status IN ('started','completed','failed')
             AND substr(created_at,1,10)=?""", (day,)
    ).fetchone()[0] or 0)
    monthly = int(con.execute(
        """SELECT COUNT(*) FROM photo_ai_api_attempts
           WHERE allowed=1 AND result_status IN ('started','completed','failed')
             AND substr(created_at,1,7)=?""", (month,)
    ).fetchone()[0] or 0)
    return daily, monthly


def get_ai_cost_status() -> dict[str, Any]:
    settings = get_ai_cost_settings()
    with closing(get_connection()) as con:
        daily, monthly = _period_counts(con)
        cache_reuse = int(con.execute(
            "SELECT COUNT(*) FROM photo_ai_usage WHERE request_kind='cache_reuse'"
        ).fetchone()[0] or 0) if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_ai_usage'"
        ).fetchone() else 0
        api_calls = int(con.execute(
            "SELECT COUNT(*) FROM photo_ai_usage WHERE request_kind='api'"
        ).fetchone()[0] or 0) if con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='photo_ai_usage'"
        ).fetchone() else 0
    return {
        **settings,
        "daily_used": daily,
        "monthly_used": monthly,
        "daily_remaining": max(int(settings.get("daily_image_limit", 0)) - daily, 0),
        "monthly_remaining": max(int(settings.get("monthly_image_limit", 0)) - monthly, 0),
        "cache_reuse_total": cache_reuse,
        "api_calls_total": api_calls,
    }


def _recent_consecutive_failures(con) -> int:
    rows = con.execute(
        """SELECT result_status FROM photo_ai_api_attempts
           WHERE allowed=1 AND result_status IN ('completed','failed')
           ORDER BY id DESC LIMIT 20"""
    ).fetchall()
    count = 0
    for row in rows:
        if str(row[0]) != "failed":
            break
        count += 1
    return count


def can_send_image_to_api(*, manual: bool) -> tuple[bool, str]:
    settings = get_ai_cost_settings()
    with closing(get_connection()) as con:
        daily, monthly = _period_counts(con)
        failures = _recent_consecutive_failures(con)
    if int(settings.get("is_paused", 0)):
        return False, str(settings.get("pause_reason") or "API利用が一時停止されています。")
    if not manual and not int(settings.get("auto_api_enabled", 0)):
        return False, "節約モードにより自動API解析は停止中です。"
    daily_limit = int(settings.get("daily_image_limit", 0) or 0)
    monthly_limit = int(settings.get("monthly_image_limit", 0) or 0)
    if daily_limit >= 0 and daily >= daily_limit:
        return False, f"1日のAPI画像上限（{daily_limit}枚）に達しました。"
    if monthly_limit >= 0 and monthly >= monthly_limit:
        return False, f"1か月のAPI画像上限（{monthly_limit}枚）に達しました。"
    stop_after = int(settings.get("consecutive_failure_stop", 3) or 3)
    if failures >= stop_after:
        update_ai_cost_settings(is_paused=1, pause_reason=f"API解析が{failures}回連続で失敗したため自動停止しました。")
        return False, f"API解析が{failures}回連続で失敗したため自動停止しました。"
    return True, ""


def record_api_attempt(image_id: int, *, trigger_kind: str, allowed: bool, result_status: str, reason: str = "") -> int:
    init_ai_cost_control_schema()
    with closing(get_connection()) as con:
        cur = con.execute(
            """INSERT INTO photo_ai_api_attempts(
                   image_id,trigger_kind,allowed,result_status,reason,created_at
               ) VALUES(?,?,?,?,?,?)""",
            (int(image_id), str(trigger_kind)[:40], 1 if allowed else 0,
             str(result_status)[:40], str(reason)[:500], _now()),
        )
        con.commit()
        return int(cur.lastrowid)


def finish_api_attempt(attempt_id: int, *, status: str, reason: str = "") -> None:
    if attempt_id <= 0:
        return
    with closing(get_connection()) as con:
        con.execute(
            "UPDATE photo_ai_api_attempts SET result_status=?,reason=? WHERE id=?",
            (str(status)[:40], str(reason)[:500], int(attempt_id)),
        )
        con.commit()
