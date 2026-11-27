"""团队工作日历：半开区间 [start_minute, end_minute) 的整数分钟口径。

区间按 UTC 日存放，计算时转成绝对 epoch 分钟，便于做跨日求交。
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from . import timeutil


@dataclass
class Calendar:
    by_team: dict[int, list[tuple[int, int]]] = field(default_factory=lambda: defaultdict(list))

    @classmethod
    def load(cls, conn, team_ids: set[int], start: int, end: int) -> "Calendar":
        cal = cls()
        if not team_ids or end <= start:
            return cal
        day_from = timeutil.day_of(start)
        day_to = timeutil.day_of(end - 1)
        placeholders = ",".join("?" for _ in team_ids)
        rows = conn.execute(
            f"""SELECT team_id, day, start_minute, end_minute
                FROM calendar_intervals
                WHERE team_id IN ({placeholders}) AND day >= ? AND day <= ?""",
            (*team_ids, day_from, day_to),
        ).fetchall()
        for row in rows:
            base = timeutil.parse_day(row["day"])
            cal.by_team[row["team_id"]].append(
                (base + row["start_minute"], base + row["end_minute"])
            )
        for intervals in cal.by_team.values():
            intervals.sort()
        return cal

    def minutes(self, team_id: int, start: int, end: int) -> int:
        """团队日历在 [start, end) 内的工作分钟数。"""
        if end <= start:
            return 0
        total = 0
        for s, e in self.by_team.get(team_id, ()):
            if e <= start:
                continue
            if s >= end:
                break
            total += min(e, end) - max(s, start)
        return total


def validate_intervals(raw: list) -> list[tuple[int, int]]:
    """校验并规范化 [(start_minute, end_minute)]：整数、半开、日内、互不重叠。"""
    if not isinstance(raw, list):
        raise ValueError("intervals 必须是列表")
    intervals: list[tuple[int, int]] = []
    for item in raw:
        if isinstance(item, dict):
            s = item.get("start_minute", item.get("start"))
            e = item.get("end_minute", item.get("end"))
        elif isinstance(item, (list, tuple)) and len(item) == 2:
            s, e = item
        else:
            raise ValueError("每个工作区间必须是 [start, end] 或 {start_minute, end_minute}")
        if isinstance(s, bool) or isinstance(e, bool) or not isinstance(s, int) or not isinstance(e, int):
            raise ValueError("工作区间端点必须是整数分钟")
        if not (0 <= s < e <= 1440):
            raise ValueError("工作区间须满足 0 <= start < end <= 1440（半开区间）")
        intervals.append((s, e))
    intervals.sort()
    for (s1, e1), (s2, e2) in zip(intervals, intervals[1:]):
        if s2 < e1:
            raise ValueError("同一团队同一天的工作区间不能重叠")
    return intervals


def replace_day(conn, team_id: int, day: str, intervals: list[tuple[int, int]], actor: str, now: int) -> None:
    conn.execute(
        "DELETE FROM calendar_intervals WHERE team_id = ? AND day = ?", (team_id, day)
    )
    for s, e in intervals:
        conn.execute(
            "INSERT INTO calendar_intervals (team_id, day, start_minute, end_minute, created_at)"
            " VALUES (?, ?, ?, ?, ?)",
            (team_id, day, s, e, now),
        )


def day_intervals(conn, team_id: int, day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, start_minute, end_minute FROM calendar_intervals"
        " WHERE team_id = ? AND day = ? ORDER BY start_minute",
        (team_id, day),
    ).fetchall()
    return [{"id": r["id"], "start_minute": r["start_minute"], "end_minute": r["end_minute"]} for r in rows]
