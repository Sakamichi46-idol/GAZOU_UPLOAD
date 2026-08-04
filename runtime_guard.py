"""Shared concurrency limits for expensive user-panel operations."""
from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(value, maximum))


MAX_CONCURRENT = _env_int("PHOTO_USER_MAX_CONCURRENT", 6, 1, 30)
WAIT_TIMEOUT = _env_float("PHOTO_USER_QUEUE_TIMEOUT", 45.0, 3.0, 300.0)
_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT)
_USER_LOCKS: dict[int, asyncio.Lock] = {}
_LOCKS_GUARD = asyncio.Lock()


async def _get_user_lock(user_id: int) -> asyncio.Lock:
    async with _LOCKS_GUARD:
        return _USER_LOCKS.setdefault(int(user_id), asyncio.Lock())


async def _discard_user_lock(user_id: int, lock: asyncio.Lock) -> None:
    async with _LOCKS_GUARD:
        current = _USER_LOCKS.get(int(user_id))
        if current is lock and not lock.locked():
            _USER_LOCKS.pop(int(user_id), None)


@asynccontextmanager
async def user_operation(user_id: int):
    """Limit expensive work globally and reject repeated work by one user."""
    uid = int(user_id)
    lock = await _get_user_lock(uid)
    if lock.locked():
        raise RuntimeError("同じユーザーの別操作が実行中です。完了してからもう一度お試しください。")

    acquired_semaphore = False
    await lock.acquire()
    try:
        try:
            await asyncio.wait_for(_SEMAPHORE.acquire(), timeout=WAIT_TIMEOUT)
            acquired_semaphore = True
        except TimeoutError as exc:
            raise RuntimeError("利用が集中しています。少し待ってからもう一度お試しください。") from exc
        yield
    finally:
        if acquired_semaphore:
            _SEMAPHORE.release()
        lock.release()
        await _discard_user_lock(uid, lock)
