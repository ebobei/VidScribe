"""Retry helpers for stable VidScribe collection."""

from __future__ import annotations

import random


RATE_LIMIT_MARKERS = (
    "429",
    "too many requests",
    "rate limit",
    "rate-limit",
    "ratelimit",
    "rate limited",
    "rate-limited",
)


def is_rate_limit_error(message: str | None) -> bool:
    if not message:
        return False
    lowered = message.lower()
    return any(marker in lowered for marker in RATE_LIMIT_MARKERS)


def random_video_sleep(min_seconds: int, max_seconds: int) -> int:
    if max_seconds <= 0:
        return 0
    if min_seconds >= max_seconds:
        return max(0, min_seconds)
    return random.randint(max(0, min_seconds), max(0, max_seconds))
