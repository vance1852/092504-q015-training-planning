"""招生方案生成算法:在需求、资源与历史反馈约束下生成多套确定性方案。

算法规则:
- 仅使用已验证且有效期与规划期间重叠的需求版本;有效期不匹配的需求进入缺口解释;
- 历史就业率低于目标值的职业按比例下调需求,并记录 low_historical_employment 缺口;
- 课程班级数不得超过每门先修课程已排班级数,先修不足记录 prerequisite_limited;
- 教师工时与设备容量是共享资源池,按策略顺序消耗,耗尽分别记录
  teacher_hours_limited 与 equipment_limited;
- 三套策略:demand_first 需求优先、employment_first 就业优先、balanced 均衡轮询。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable

TARGET_EMPLOYMENT_RATE = 0.8
STRATEGIES = ("demand_first", "employment_first", "balanced")


@dataclass(frozen=True)
class DemandInput:
    """一条已验证岗位需求版本的算法输入。"""

    version_id: str
    series_id: str
    demand_key: str
    enterprise_name: str
    region: str
    occupation: str
    headcount: int
    window_start: str
    window_end: str


@dataclass(frozen=True)
class CourseInput:
    """一门课程的算法输入。"""

    course_id: str
    occupation: str
    duration_hours: int
    class_size: int
    prerequisites: tuple[str, ...]
    equipment_needs: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class TeacherInput:
    """一名教师在规划期内的可用工时与可授课程。"""

    teacher_id: str
    available_hours: int
    course_ids: tuple[str, ...]


@dataclass(frozen=True)
class EquipmentInput:
    """一类设备在规划期内的可用台数。"""

    equipment_id: str
    units: int


@dataclass(frozen=True)
class FeedbackStat:
    """一个职业的历史结业与就业反馈计数。"""

    occupation: str
    graduated: int
    employed: int


@dataclass
class AllocationDraft:
    """一套方案中一门课程的排产结果。"""

    course_id: str
    classes: int
    quota: int
    raw_demand: int
    adjusted_demand: int
    demand_links: list[dict[str, Any]]
    constraints: list[dict[str, Any]]


@dataclass
class PlanDraft:
    """一套完整的候选招生方案。"""

    strategy: str
    allocations: list[AllocationDraft]
    gaps: list[dict[str, Any]]
    summary: dict[str, Any]
    snapshot: dict[str, Any]


def generate_plans(*, demands: list[DemandInput], courses: list[CourseInput],
                   teachers: list[TeacherInput], equipment: list[EquipmentInput],
                   feedback: list[FeedbackStat], period_key: str,
                   period_start: str, period_end: str) -> list[PlanDraft]:
    """按固定策略顺序生成三套候选方案,同一输入永远得到同一输出。"""

    course_list = sorted(courses, key=lambda c: c.course_id)
    by_id = {c.course_id: c for c in course_list}
    active, excluded = _partition_demands(demands, period_start, period_end)
    feedback_map = {f.occupation: f for f in feedback}

    raw: dict[str, int] = {}
    adjusted: dict[str, int] = {}
    direct_links: dict[str, list[dict[str, Any]]] = {}
    scaling_gaps: list[dict[str, Any]] = []
    for course in course_list:
        cid = course.course_id
        matched = [d for d in active if d.occupation == course.occupation]
        raw[cid] = sum(d.headcount for d in matched)
        direct_links[cid] = [
            {"version_id": d.version_id, "headcount": d.headcount, "indirect": False}
            for d in matched
        ]
        factor = 1.0
        stat = feedback_map.get(course.occupation)
        if stat is not None and stat.graduated > 0 and raw[cid] > 0:
            rate = min(1.0, stat.employed / stat.graduated)
            if rate < TARGET_EMPLOYMENT_RATE:
                factor = rate / TARGET_EMPLOYMENT_RATE
                scaling_gaps.append({
                    "course_id": cid,
                    "gap_type": "low_historical_employment",
                    "detail": {
                        "occupation": stat.occupation,
                        "graduated": stat.graduated,
                        "employed": stat.employed,
                        "employment_rate": round(rate, 4),
                        "target_rate": TARGET_EMPLOYMENT_RATE,
                        "raw_demand": raw[cid],
                        "adjusted_demand": math.ceil(raw[cid] * factor),
                    },
                })
        adjusted[cid] = math.ceil(raw[cid] * factor) if raw[cid] else 0

    dependents = _dependents(course_list)
    own_classes = {c.course_id: _classes_for(adjusted[c.course_id], c.class_size)
                   for c in course_list}
    # 先修支撑传播:高级课程的班级数需要先修课程至少同样的班级数承接。
    desired = dict(own_classes)
    for cid in reversed(_topo_order(course_list, None)[0]):
        for dep in dependents[cid]:
            desired[cid] = max(desired[cid], desired[dep])

    snapshot = {
        "period_key": period_key,
        "period_start": period_start,
        "period_end": period_end,
        "demands": [
            {"version_id": d.version_id, "series_id": d.series_id, "demand_key": d.demand_key,
             "enterprise_name": d.enterprise_name, "region": d.region, "occupation": d.occupation,
             "headcount": d.headcount, "window_start": d.window_start, "window_end": d.window_end}
            for d in sorted(demands, key=lambda d: (d.demand_key, d.version_id))
        ],
        "courses": [
            {"course_id": c.course_id, "occupation": c.occupation,
             "duration_hours": c.duration_hours, "class_size": c.class_size,
             "prerequisites": sorted(c.prerequisites),
             "equipment_needs": [{"equipment_id": e, "units_per_class": u}
                                 for e, u in sorted(c.equipment_needs)]}
            for c in course_list
        ],
        "teachers": [
            {"teacher_id": t.teacher_id, "available_hours": t.available_hours,
             "course_ids": sorted(t.course_ids)}
            for t in sorted(teachers, key=lambda t: t.teacher_id)
        ],
        "equipment": [
            {"equipment_id": e.equipment_id, "units": e.units}
            for e in sorted(equipment, key=lambda e: e.equipment_id)
        ],
        "feedback": [
            {"occupation": f.occupation, "graduated": f.graduated, "employed": f.employed}
            for f in sorted(feedback, key=lambda f: f.occupation)
        ],
    }

    return [
        _build_plan(strategy=strategy, course_list=course_list, by_id=by_id,
                    dependents=dependents, desired=desired, own_classes=own_classes,
                    raw=raw, adjusted=adjusted, direct_links=direct_links,
                    teachers=teachers, equipment=equipment, feedback_map=feedback_map,
                    scaling_gaps=scaling_gaps, active=active, excluded=excluded,
                    snapshot=snapshot)
        for strategy in STRATEGIES
    ]


def _partition_demands(demands: list[DemandInput], period_start: str,
                       period_end: str) -> tuple[list[DemandInput], list[dict[str, Any]]]:
    """按有效期与规划期间的重叠关系拆分需求。"""

    active: list[DemandInput] = []
    excluded: list[dict[str, Any]] = []
    for demand in sorted(demands, key=lambda d: (d.demand_key, d.version_id)):
        if demand.window_end < period_start:
            excluded.append({"demand": demand, "reason": "demand_expired"})
        elif demand.window_start > period_end:
            excluded.append({"demand": demand, "reason": "demand_not_yet_effective"})
        else:
            active.append(demand)
    return active, excluded


def _classes_for(headcount: int, class_size: int) -> int:
    return math.ceil(headcount / class_size) if headcount > 0 else 0


def _dependents(course_list: list[CourseInput]) -> dict[str, list[str]]:
    ids = {c.course_id for c in course_list}
    dependents: dict[str, list[str]] = {c.course_id: [] for c in course_list}
    for course in course_list:
        for prerequisite in course.prerequisites:
            if prerequisite in ids:
                dependents[prerequisite].append(course.course_id)
    return dependents


def _topo_order(course_list: list[CourseInput],
                key: Callable[[str], tuple] | None) -> tuple[list[str], dict[str, list[str]]]:
    """按先修关系拓扑排序;key 决定同一就绪集合内的优先级。"""

    ids = {c.course_id for c in course_list}
    indegree = {c.course_id: 0 for c in course_list}
    dependents = _dependents(course_list)
    for course in course_list:
        for prerequisite in course.prerequisites:
            if prerequisite in ids:
                indegree[course.course_id] += 1
    ready = [cid for cid, degree in indegree.items() if degree == 0]
    order: list[str] = []
    while ready:
        ready.sort(key=lambda cid: (key(cid) if key else (), cid))
        cid = ready.pop(0)
        order.append(cid)
        for dependent in dependents[cid]:
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
    if len(order) != len(course_list):
        raise ValueError("课程先修关系存在环")
    return order, dependents


def _employment_rate(stat: FeedbackStat | None) -> float:
    if stat is None or stat.graduated <= 0:
        return 1.0
    return min(1.0, stat.employed / stat.graduated)


def _prereq_cap(course: CourseInput, planned: dict[str, int]) -> float:
    caps = [planned[p] for p in course.prerequisites if p in planned]
    return min(caps) if caps else math.inf


def _teacher_max(teacher_ids: list[str], remaining: dict[str, int], hours: int) -> int:
    return sum(remaining[t] // hours for t in teacher_ids)


def _equipment_max(course: CourseInput, remaining: dict[str, int]) -> float:
    caps = [remaining[e] // units for e, units in course.equipment_needs]
    return min(caps) if caps else math.inf


def _consume(course: CourseInput, classes: int, teacher_ids: list[str],
             teacher_remaining: dict[str, int], equipment_remaining: dict[str, int]) -> None:
    for _ in range(classes):
        # 每个班消耗一名合格教师的整段课时,优先使用剩余工时最多的教师。
        best = max((t for t in teacher_ids if teacher_remaining[t] >= course.duration_hours),
                   key=lambda t: (teacher_remaining[t], t), default=None)
        if best is None:
            raise ValueError("教师工时不足以支撑已分配的班级")
        teacher_remaining[best] -= course.duration_hours
    for equipment_id, units in course.equipment_needs:
        equipment_remaining[equipment_id] -= classes * units


def _allocate_priority(order: list[str], by_id: dict[str, CourseInput],
                       desired: dict[str, int], planned: dict[str, int],
                       qualified: dict[str, list[str]],
                       teacher_remaining: dict[str, int],
                       equipment_remaining: dict[str, int]) -> None:
    for cid in order:
        course = by_id[cid]
        take = int(min(desired[cid], _prereq_cap(course, planned),
                       _teacher_max(qualified[cid], teacher_remaining, course.duration_hours),
                       _equipment_max(course, equipment_remaining)))
        if take <= 0:
            continue
        _consume(course, take, qualified[cid], teacher_remaining, equipment_remaining)
        planned[cid] = take


def _allocate_round_robin(order: list[str], by_id: dict[str, CourseInput],
                          desired: dict[str, int], planned: dict[str, int],
                          qualified: dict[str, list[str]],
                          teacher_remaining: dict[str, int],
                          equipment_remaining: dict[str, int]) -> None:
    while True:
        progressed = False
        for cid in order:
            course = by_id[cid]
            if planned[cid] >= desired[cid]:
                continue
            if planned[cid] >= _prereq_cap(course, planned):
                continue
            if _teacher_max(qualified[cid], teacher_remaining, course.duration_hours) < 1:
                continue
            if _equipment_max(course, equipment_remaining) < 1:
                continue
            _consume(course, 1, qualified[cid], teacher_remaining, equipment_remaining)
            planned[cid] += 1
            progressed = True
        if not progressed:
            return


def _blockers(course: CourseInput, planned: dict[str, int],
              qualified: dict[str, list[str]], teacher_remaining: dict[str, int],
              equipment_remaining: dict[str, int], desired: dict[str, int]) -> list[dict[str, Any]]:
    """解释一门课程在最终状态下为什么不能继续扩班。"""

    cid = course.course_id
    if planned[cid] >= desired[cid]:
        return []
    blockers: list[dict[str, Any]] = []
    prerequisites = [p for p in course.prerequisites if p in planned]
    if prerequisites:
        lowest = min(planned[p] for p in prerequisites)
        if planned[cid] >= lowest:
            limiting = sorted(p for p in prerequisites if planned[p] == lowest)
            blockers.append({
                "type": "prerequisite_limited",
                "detail": {"limiting_prerequisites": limiting,
                           "prerequisite_classes": {p: planned[p] for p in limiting}},
            })
    if _teacher_max(qualified[cid], teacher_remaining, course.duration_hours) < 1:
        blockers.append({
            "type": "teacher_hours_limited",
            "detail": {"qualified_teachers": len(qualified[cid]),
                       "remaining_hours": sum(teacher_remaining[t] for t in qualified[cid]),
                       "hours_per_class": course.duration_hours},
        })
    if _equipment_max(course, equipment_remaining) < 1:
        limiting = sorted(e for e, u in course.equipment_needs
                          if equipment_remaining[e] // u < 1)
        blockers.append({
            "type": "equipment_limited",
            "detail": {"limiting_equipment": limiting,
                       "remaining_units": {e: equipment_remaining[e] for e in limiting}},
        })
    return blockers


def _build_plan(*, strategy: str, course_list: list[CourseInput],
                by_id: dict[str, CourseInput], dependents: dict[str, list[str]],
                desired: dict[str, int], own_classes: dict[str, int],
                raw: dict[str, int], adjusted: dict[str, int],
                direct_links: dict[str, list[dict[str, Any]]],
                teachers: list[TeacherInput], equipment: list[EquipmentInput],
                feedback_map: dict[str, FeedbackStat],
                scaling_gaps: list[dict[str, Any]], active: list[DemandInput],
                excluded: list[dict[str, Any]], snapshot: dict[str, Any]) -> PlanDraft:
    teacher_remaining = {t.teacher_id: t.available_hours for t in teachers}
    equipment_remaining = {e.equipment_id: e.units for e in equipment}
    qualified = {
        c.course_id: sorted(t.teacher_id for t in teachers if c.course_id in t.course_ids)
        for c in course_list
    }
    planned = {c.course_id: 0 for c in course_list}

    if strategy == "balanced":
        order, _ = _topo_order(course_list, None)
        _allocate_round_robin(order, by_id, desired, planned, qualified,
                              teacher_remaining, equipment_remaining)
    else:
        if strategy == "demand_first":
            key: Callable[[str], tuple] = lambda cid: (-adjusted[cid],)
        else:
            key = lambda cid: (-_employment_rate(feedback_map.get(by_id[cid].occupation)),
                               -adjusted[cid])
        order, _ = _topo_order(course_list, key)
        _allocate_priority(order, by_id, desired, planned, qualified,
                           teacher_remaining, equipment_remaining)

    allocations: list[AllocationDraft] = []
    gaps: list[dict[str, Any]] = []
    for course in course_list:
        cid = course.course_id
        quota = planned[cid] * course.class_size
        constraints = _blockers(course, planned, qualified, teacher_remaining,
                                equipment_remaining, desired)
        links = list(direct_links[cid])
        if desired[cid] > own_classes[cid]:
            # 超出自身需求的班级由下游课程的需求间接驱动,记录间接来源。
            for dependent in dependents[cid]:
                for link in direct_links[dependent]:
                    links.append({**link, "indirect": True})
        links.sort(key=lambda link: (link["version_id"], link["indirect"]))
        allocations.append(AllocationDraft(
            course_id=cid, classes=planned[cid], quota=quota,
            raw_demand=raw[cid], adjusted_demand=adjusted[cid],
            demand_links=links, constraints=constraints))
        unmet = max(0, adjusted[cid] - min(quota, adjusted[cid]))
        if unmet > 0:
            for blocker in constraints:
                gaps.append({
                    "course_id": cid,
                    "gap_type": blocker["type"],
                    "detail": {**blocker["detail"], "wanted_classes": desired[cid],
                               "planned_classes": planned[cid], "unmet_headcount": unmet},
                })
    gaps.extend(scaling_gaps)
    for item in excluded:
        demand = item["demand"]
        gaps.append({
            "course_id": None,
            "gap_type": item["reason"],
            "detail": {"demand_key": demand.demand_key, "version_id": demand.version_id,
                       "enterprise_name": demand.enterprise_name, "region": demand.region,
                       "occupation": demand.occupation, "headcount": demand.headcount,
                       "window_start": demand.window_start, "window_end": demand.window_end},
        })
    summary = {
        "active_demand": sum(d.headcount for d in active),
        "excluded_demand": sum(item["demand"].headcount for item in excluded),
        "total_quota": sum(a.quota for a in allocations),
        "gap_count": len(gaps),
    }
    return PlanDraft(strategy=strategy, allocations=allocations, gaps=gaps,
                     summary=summary, snapshot=snapshot)
