"""时间工具：内部统一使用 epoch 分钟（UTC）。"""

from __future__ import annotations

import datetime as _dt

MINUTES_PER_DAY = 1440
EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def parse_day(day: str) -> int:
    """'YYYY-MM-DD' -> 该日 00:00 UTC 的 epoch 分钟。"""
    d = _dt.date.fromisoformat(day)
    return int(_dt.datetime(d.year, d.month, d.day, tzinfo=_dt.timezone.utc).timestamp() // 60)


def day_of(minute: int) -> str:
    """epoch 分钟 -> 所在 UTC 日 'YYYY-MM-DD'。"""
    return _dt.datetime.fromtimestamp(minute * 60, _dt.timezone.utc).date().isoformat()


def day_index(minute: int) -> int:
    return minute // MINUTES_PER_DAY


def iso(minute: int | None) -> str | None:
    if minute is None:
        return None
    return _dt.datetime.fromtimestamp(minute * 60, _dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(value: str) -> int:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    d = _dt.datetime.fromisoformat(text)
    if d.tzinfo is None:
        d = d.replace(tzinfo=_dt.timezone.utc)
    return int(d.timestamp() // 60)
