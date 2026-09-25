"""汇总学员事件，为方案生成与统计报告提供连续数据。"""

from __future__ import annotations

import json
from typing import Any


def course_outcomes(connection) -> dict[str, dict[str, int]]:
    """按课程汇总全部期间的结业人数与就业人数。"""

    rows = connection.execute(
        "SELECT course_id, event_type, payload_json FROM student_events "
        "WHERE event_type IN ('graduate', 'employment') AND course_id IS NOT NULL"
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = result.setdefault(row["course_id"], {"graduated": 0, "employed": 0})
        if row["event_type"] == "graduate":
            entry["graduated"] += 1
        else:
            payload = json.loads(row["payload_json"])
            if payload.get("employed"):
                entry["employed"] += 1
    return result


def period_outcomes(connection, period: str) -> dict[str, dict[str, int]]:
    """按课程汇总指定统计期间的入学、转入、结业与就业人数。"""

    rows = connection.execute(
        "SELECT course_id, event_type, payload_json FROM student_events "
        "WHERE period=? AND course_id IS NOT NULL",
        (period,),
    ).fetchall()
    result: dict[str, dict[str, int]] = {}
    for row in rows:
        entry = result.setdefault(
            row["course_id"],
            {"enrolled": 0, "transferred_in": 0, "graduated": 0, "employed": 0},
        )
        if row["event_type"] == "enroll":
            entry["enrolled"] += 1
        elif row["event_type"] == "transfer":
            entry["transferred_in"] += 1
        elif row["event_type"] == "graduate":
            entry["graduated"] += 1
        else:
            payload = json.loads(row["payload_json"])
            if payload.get("employed"):
                entry["employed"] += 1
    return result


def graduated_students(connection, course_ids: list[str]) -> int:
    """统计完成指定课程集合中任意课程结业的去重学员数。"""

    if not course_ids:
        return 0
    placeholders = ",".join("?" for _ in course_ids)
    row = connection.execute(
        f"SELECT COUNT(DISTINCT student_id) AS count FROM student_events "
        f"WHERE event_type='graduate' AND course_id IN ({placeholders})",
        course_ids,
    ).fetchone()
    return int(row["count"])


def employment_rate(outcome: dict[str, int] | None) -> float | None:
    """由结业与就业人数计算就业率，缺少历史数据时返回 None。"""

    if not outcome or outcome["graduated"] == 0:
        return None
    return outcome["employed"] / outcome["graduated"]


def serialize_rate(rate: float | None) -> Any:
    """把就业率转换为可序列化且便于核对的形态。"""

    return None if rate is None else round(rate, 4)
