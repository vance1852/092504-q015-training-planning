"""新职业培训名额规划：需求版本、方案生成、冻结审批与影响提示。"""

from __future__ import annotations

import json
import re
import uuid
from datetime import date, timedelta
from typing import Any

from .audit import append_event, digest
from .errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from .models import WriteReceipt
from .service import DomainService
from .stats import course_outcomes, employment_rate, graduated_students, serialize_rate

PERIOD_PATTERN = re.compile(r"^\d{4}Q[1-4]$")
DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")
STRATEGIES = ("conservative", "balanced", "aggressive")
LIMITING_ORDER = ("demand", "teacher", "equipment", "prerequisite")


def period_bounds(period: str) -> tuple[str, str]:
    """把 2026Q4 形式的统计期间转换为起止日期。"""

    year = int(period[:4])
    quarter = int(period[-1])
    start_month = (quarter - 1) * 3 + 1
    end_month = start_month + 2
    if end_month == 12:
        last_day = 31
    else:
        last_day = (date(year, end_month + 1, 1) - timedelta(days=1)).day
    return f"{year:04d}-{start_month:02d}-01", f"{year:04d}-{end_month:02d}-{last_day:02d}"


class PlanningService(DomainService):
    """协调岗位需求、教学容量与招生方案的业务规则。"""

    def _period(self, value: str, field: str = "period") -> str:
        value = str(value).strip()
        if not PERIOD_PATTERN.fullmatch(value):
            raise ValidationError(f"{field} 必须是 2026Q4 形式的统计期间")
        return value

    def _date(self, value: str, field: str) -> str:
        value = str(value).strip()
        if not DATE_PATTERN.fullmatch(value):
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 形式的日期")
        try:
            date.fromisoformat(value)
        except ValueError as exc:
            raise ValidationError(f"{field} 不是有效日期") from exc
        return value

    def _count(self, value: Any, field: str, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValidationError(f"{field} 必须是不小于 {minimum} 的整数")
        return value

    # ------------------------------------------------------------------
    # 岗位需求版本
    # ------------------------------------------------------------------

    def _latest_demand(self, connection, demand_id: str):
        return connection.execute(
            "SELECT * FROM job_demands WHERE demand_id=? ORDER BY version DESC LIMIT 1",
            (demand_id,),
        ).fetchone()

    def _append_demand_version(self, connection, *, demand_id: str, enterprise_id: str,
                               occupation: str, region: str, window_start: str, window_end: str,
                               headcount: int, valid_until: str, status: str,
                               actor_id: str) -> dict[str, Any]:
        latest = self._latest_demand(connection, demand_id)
        version = 1 if latest is None else int(latest["version"]) + 1
        connection.execute(
            "INSERT INTO job_demands(demand_id,version,enterprise_id,occupation,region,window_start,"
            "window_end,headcount,valid_until,status,submitted_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (demand_id, version, enterprise_id, occupation, region, window_start,
             window_end, headcount, valid_until, status, actor_id, self._now()),
        )
        return {"demand_id": demand_id, "version": version, "status": status}

    def _emit_impact_notices(self, connection, *, demand_id: str, change_type: str) -> None:
        """需求变化时，为已冻结（已选择或已生效）的同职业同地区方案生成影响提示。"""

        latest = self._latest_demand(connection, demand_id)
        plans = connection.execute(
            "SELECT * FROM plans WHERE occupation=? AND region=? AND status IN ('selected','approved')",
            (latest["occupation"], latest["region"]),
        ).fetchall()
        for plan in plans:
            snapshot = json.loads(plan["snapshot_json"])
            frozen = next((item for item in snapshot["demands"] if item["demand_id"] == demand_id), None)
            if change_type == "submitted":
                if frozen is None:
                    kind = "added"
                elif frozen["headcount"] != latest["headcount"] or frozen["status"] != latest["status"]:
                    kind = "updated"
                else:
                    kind = "reaffirmed"
            else:
                kind = change_type
            detail = {
                "demand_id": demand_id,
                "new_version": latest["version"],
                "new_status": latest["status"],
                "new_headcount": latest["headcount"],
                "frozen_version": None if frozen is None else frozen["version"],
                "frozen_status": None if frozen is None else frozen["status"],
                "frozen_headcount": None if frozen is None else frozen["headcount"],
                "plan_status": plan["status"],
            }
            notice_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO impact_notices(notice_id,plan_id,demand_id,change_type,detail_json,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (notice_id, plan["plan_id"], demand_id, kind, json.dumps(detail, ensure_ascii=False,
                 sort_keys=True), self._now()),
            )
            append_event(connection, actor_id="system", action="impact_notice.created",
                         resource_type="plan", resource_id=plan["plan_id"],
                         detail={"notice_id": notice_id, "demand_id": demand_id, "change_type": kind},
                         occurred_at=self._now())

    def submit_demand(self, *, request_id: str, actor_id: str, demand_id: str, enterprise_id: str,
                      occupation: str, region: str, window_start: str, window_end: str,
                      headcount: int, valid_until: str) -> WriteReceipt:
        """企业按地区与时间窗提交岗位需求，每次提交追加一个新版本（意向状态）。"""

        payload = {"actor_id": actor_id, "demand_id": demand_id, "enterprise_id": enterprise_id,
                   "occupation": occupation, "region": region, "window_start": window_start,
                   "window_end": window_end, "headcount": headcount, "valid_until": valid_until}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "enterprise")
            demand_id = self._identifier(demand_id, "demand_id")
            enterprise_id = self._identifier(enterprise_id, "enterprise_id")
            occupation = self._text(occupation, "occupation", 80)
            region = self._text(region, "region", 80)
            window_start = self._date(window_start, "window_start")
            window_end = self._date(window_end, "window_end")
            if window_start > window_end:
                raise ValidationError("window_start 不能晚于 window_end")
            headcount = self._count(headcount, "headcount", 1)
            valid_until = self._date(valid_until, "valid_until")

            def create() -> tuple[str, str, dict[str, Any]]:
                result = self._append_demand_version(
                    connection, demand_id=demand_id, enterprise_id=enterprise_id, occupation=occupation,
                    region=region, window_start=window_start, window_end=window_end,
                    headcount=headcount, valid_until=valid_until, status="intent", actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action="demand.submitted",
                             resource_type="job_demand", resource_id=demand_id,
                             detail={**result, "occupation": occupation, "region": region,
                                     "headcount": headcount, "valid_until": valid_until},
                             occurred_at=self._now())
                self._emit_impact_notices(connection, demand_id=demand_id, change_type="submitted")
                resource_id = f"{demand_id}#{result['version']}"
                return "job_demand", resource_id, result

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_demand", payload=payload, create=create)

    def _transition_demand(self, *, request_id: str, actor_id: str, demand_id: str,
                           target: str, allowed_sources: tuple[str, ...], roles: tuple[str, ...],
                           action: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "demand_id": demand_id, "target": target}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, *roles)
            demand_id = self._identifier(demand_id, "demand_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                latest = self._latest_demand(connection, demand_id)
                if latest is None:
                    raise NotFoundError("岗位需求不存在")
                if latest["status"] not in allowed_sources:
                    raise ConflictError(f"当前状态 {latest['status']} 不允许转换为 {target}")
                result = self._append_demand_version(
                    connection, demand_id=demand_id, enterprise_id=latest["enterprise_id"],
                    occupation=latest["occupation"], region=latest["region"],
                    window_start=latest["window_start"], window_end=latest["window_end"],
                    headcount=latest["headcount"], valid_until=latest["valid_until"],
                    status=target, actor_id=actor_id)
                append_event(connection, actor_id=actor_id, action=f"demand.{action}",
                             resource_type="job_demand", resource_id=demand_id,
                             detail=result, occurred_at=self._now())
                self._emit_impact_notices(connection, demand_id=demand_id, change_type=target)
                resource_id = f"{demand_id}#{result['version']}"
                return "job_demand", resource_id, result

            return self._idempotent(connection, request_id=request_id,
                                    action=f"{action}_demand", payload=payload, create=create)

    def verify_demand(self, *, request_id: str, actor_id: str, demand_id: str) -> WriteReceipt:
        """把意向需求核验为已验证状态。"""

        return self._transition_demand(request_id=request_id, actor_id=actor_id, demand_id=demand_id,
                                       target="verified", allowed_sources=("intent",),
                                       roles=("admin", "reviewer"), action="verified")

    def withdraw_demand(self, *, request_id: str, actor_id: str, demand_id: str) -> WriteReceipt:
        """撤回意向或已验证的需求。"""

        return self._transition_demand(request_id=request_id, actor_id=actor_id, demand_id=demand_id,
                                       target="withdrawn", allowed_sources=("intent", "verified"),
                                       roles=("admin", "operator", "enterprise"), action="withdrawn")

    def list_demands(self, occupation: str | None = None, region: str | None = None,
                     include_history: bool = False) -> list[dict[str, Any]]:
        """按职业与地区列出需求版本，默认只返回每个需求的最新版本。"""

        conditions: list[str] = []
        parameters: list[Any] = []
        if occupation:
            conditions.append("occupation=?")
            parameters.append(occupation)
        if region:
            conditions.append("region=?")
            parameters.append(region)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.database.connection.execute(
            f"SELECT * FROM job_demands{where} ORDER BY demand_id, version", parameters
        ).fetchall()
        items = [dict(row) for row in rows]
        if include_history:
            return items
        latest: dict[str, dict[str, Any]] = {}
        for item in items:
            latest[item["demand_id"]] = item
        return list(latest.values())

    # ------------------------------------------------------------------
    # 课程、教师工时与设备容量
    # ------------------------------------------------------------------

    def _prerequisite_map(self, connection, course_ids: list[str]) -> dict[str, list[str]]:
        result: dict[str, list[str]] = {course_id: [] for course_id in course_ids}
        if not course_ids:
            return result
        placeholders = ",".join("?" for _ in course_ids)
        rows = connection.execute(
            f"SELECT course_id, prerequisite_id FROM course_prerequisites WHERE course_id IN ({placeholders})",
            course_ids,
        ).fetchall()
        for row in rows:
            result[row["course_id"]].append(row["prerequisite_id"])
        return result

    def _would_cycle(self, connection, course_id: str, prerequisites: list[str]) -> bool:
        stack = list(prerequisites)
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current == course_id:
                return True
            if current in seen:
                continue
            seen.add(current)
            rows = connection.execute(
                "SELECT prerequisite_id FROM course_prerequisites WHERE course_id=?", (current,)
            ).fetchall()
            stack.extend(row["prerequisite_id"] for row in rows)
        return False

    def register_course(self, *, request_id: str, actor_id: str, course_id: str, occupation: str,
                        name: str, duration_hours: int, equipment_type: str, max_class_size: int,
                        prerequisites: list[str] | None = None) -> WriteReceipt:
        """登记课程及其先修关系、课时、设备类型与班容。"""

        prerequisites = list(prerequisites or [])
        payload = {"actor_id": actor_id, "course_id": course_id, "occupation": occupation, "name": name,
                   "duration_hours": duration_hours, "equipment_type": equipment_type,
                   "max_class_size": max_class_size, "prerequisites": prerequisites}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            course_id = self._identifier(course_id, "course_id")
            occupation = self._text(occupation, "occupation", 80)
            name = self._text(name, "name")
            duration_hours = self._count(duration_hours, "duration_hours", 1)
            equipment_type = self._text(equipment_type, "equipment_type", 80)
            max_class_size = self._count(max_class_size, "max_class_size", 1)
            prerequisites = [self._identifier(item, "prerequisite") for item in prerequisites]
            if len(set(prerequisites)) != len(prerequisites):
                raise ValidationError("先修课程存在重复")
            if course_id in prerequisites:
                raise ValidationError("课程不能以自身为先修")

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM courses WHERE course_id=?",
                                      (course_id,)).fetchone():
                    raise ConflictError("课程编号已经存在")
                for prerequisite in prerequisites:
                    if connection.execute("SELECT 1 FROM courses WHERE course_id=?",
                                          (prerequisite,)).fetchone() is None:
                        raise NotFoundError(f"先修课程 {prerequisite} 不存在")
                if self._would_cycle(connection, course_id, prerequisites):
                    raise ValidationError("先修关系会形成循环")
                connection.execute(
                    "INSERT INTO courses(course_id,occupation,name,duration_hours,equipment_type,"
                    "max_class_size,created_at) VALUES(?,?,?,?,?,?,?)",
                    (course_id, occupation, name, duration_hours, equipment_type,
                     max_class_size, self._now()),
                )
                for prerequisite in prerequisites:
                    connection.execute(
                        "INSERT INTO course_prerequisites(course_id,prerequisite_id) VALUES(?,?)",
                        (course_id, prerequisite),
                    )
                append_event(connection, actor_id=actor_id, action="course.registered",
                             resource_type="course", resource_id=course_id,
                             detail={"occupation": occupation, "name": name,
                                     "duration_hours": duration_hours, "equipment_type": equipment_type,
                                     "max_class_size": max_class_size, "prerequisites": prerequisites},
                             occurred_at=self._now())
                return "course", course_id, {"course_id": course_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_course", payload=payload, create=create)

    def list_courses(self, occupation: str | None = None) -> list[dict[str, Any]]:
        parameters: list[Any] = []
        query = "SELECT * FROM courses"
        if occupation:
            query += " WHERE occupation=?"
            parameters.append(occupation)
        query += " ORDER BY course_id"
        rows = self.database.connection.execute(query, parameters).fetchall()
        course_ids = [row["course_id"] for row in rows]
        prerequisites = self._prerequisite_map(self.database.connection, course_ids)
        return [{**dict(row), "prerequisites": prerequisites[row["course_id"]]} for row in rows]

    def set_teacher_capacity(self, *, request_id: str, actor_id: str, teacher_id: str,
                             course_id: str, period: str, available_hours: int) -> WriteReceipt:
        """登记或更新教师在指定期间对某课程可用的工时。"""

        payload = {"actor_id": actor_id, "teacher_id": teacher_id, "course_id": course_id,
                   "period": period, "available_hours": available_hours}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            teacher_id = self._identifier(teacher_id, "teacher_id")
            course_id = self._identifier(course_id, "course_id")
            period = self._period(period)
            available_hours = self._count(available_hours, "available_hours", 0)

            def create() -> tuple[str, str, dict[str, Any]]:
                if connection.execute("SELECT 1 FROM courses WHERE course_id=?",
                                      (course_id,)).fetchone() is None:
                    raise NotFoundError("课程不存在")
                connection.execute(
                    "INSERT INTO teacher_capacities(teacher_id,course_id,period,available_hours,updated_at) "
                    "VALUES(?,?,?,?,?) ON CONFLICT(teacher_id,course_id,period) "
                    "DO UPDATE SET available_hours=excluded.available_hours, updated_at=excluded.updated_at",
                    (teacher_id, course_id, period, available_hours, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="teacher_capacity.set",
                             resource_type="teacher_capacity",
                             resource_id=f"{teacher_id}:{course_id}:{period}",
                             detail={"available_hours": available_hours}, occurred_at=self._now())
                resource_id = f"{teacher_id}:{course_id}:{period}"
                return "teacher_capacity", resource_id, {"teacher_capacity_id": resource_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_teacher_capacity", payload=payload, create=create)

    def set_equipment_capacity(self, *, request_id: str, actor_id: str, equipment_type: str,
                               period: str, seats: int) -> WriteReceipt:
        """登记或更新设备类型在指定期间可同时培训的工位数。"""

        payload = {"actor_id": actor_id, "equipment_type": equipment_type,
                   "period": period, "seats": seats}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator")
            equipment_type = self._text(equipment_type, "equipment_type", 80)
            period = self._period(period)
            seats = self._count(seats, "seats", 0)

            def create() -> tuple[str, str, dict[str, Any]]:
                connection.execute(
                    "INSERT INTO equipment_capacities(equipment_type,period,seats,updated_at) "
                    "VALUES(?,?,?,?) ON CONFLICT(equipment_type,period) "
                    "DO UPDATE SET seats=excluded.seats, updated_at=excluded.updated_at",
                    (equipment_type, period, seats, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="equipment_capacity.set",
                             resource_type="equipment_capacity", resource_id=f"{equipment_type}:{period}",
                             detail={"seats": seats}, occurred_at=self._now())
                resource_id = f"{equipment_type}:{period}"
                return "equipment_capacity", resource_id, {"equipment_capacity_id": resource_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="set_equipment_capacity", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 招生方案生成
    # ------------------------------------------------------------------

    def _active_demands(self, connection, occupation: str, region: str,
                        period: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """取出参与规划的有效需求（最新版本），并列出被排除的需求及原因。"""

        period_start, period_end = period_bounds(period)
        rows = connection.execute(
            "SELECT d.* FROM job_demands d JOIN (SELECT demand_id, MAX(version) AS version "
            "FROM job_demands GROUP BY demand_id) latest "
            "ON d.demand_id=latest.demand_id AND d.version=latest.version "
            "WHERE d.occupation=? AND d.region=? ORDER BY d.demand_id",
            (occupation, region),
        ).fetchall()
        active: list[dict[str, Any]] = []
        excluded: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            if item["status"] == "withdrawn":
                excluded.append({"demand_id": item["demand_id"], "version": item["version"],
                                 "reason": "需求已撤回"})
            elif item["valid_until"] < period_start:
                excluded.append({"demand_id": item["demand_id"], "version": item["version"],
                                 "reason": f"需求有效期 {item['valid_until']} 早于规划期间开始"})
            elif item["window_start"] > period_end or item["window_end"] < period_start:
                excluded.append({"demand_id": item["demand_id"], "version": item["version"],
                                 "reason": "需求时间窗与规划期间不重叠"})
            else:
                active.append(item)
        return active, excluded

    def _topo_order(self, courses: list[dict[str, Any]],
                    prerequisites: dict[str, list[str]]) -> list[dict[str, Any]]:
        """按先修关系对课程拓扑排序，先修课程优先获得容量分配。"""

        ids = [course["course_id"] for course in courses]
        known = set(ids)
        indegree = {course_id: 0 for course_id in ids}
        followers: dict[str, list[str]] = {course_id: [] for course_id in ids}
        for course_id in ids:
            for prerequisite in prerequisites.get(course_id, []):
                if prerequisite in known:
                    followers[prerequisite].append(course_id)
                    indegree[course_id] += 1
        queue = [course_id for course_id in ids if indegree[course_id] == 0]
        order: list[str] = []
        while queue:
            current = queue.pop(0)
            order.append(current)
            for follower in followers[current]:
                indegree[follower] -= 1
                if indegree[follower] == 0:
                    queue.append(follower)
        if len(order) != len(ids):
            raise ValidationError("课程先修关系存在循环")
        by_id = {course["course_id"]: course for course in courses}
        return [by_id[course_id] for course_id in order]

    def _strategy_scale(self, strategy: str, rate: float | None) -> float:
        if rate is None:
            return 1.0
        if strategy == "conservative":
            return rate
        if strategy == "balanced":
            return (1.0 + rate) / 2.0
        return 1.0

    def _build_plan_lines(self, connection, *, strategy: str, courses: list[dict[str, Any]],
                          demands: list[dict[str, Any]], teacher_hours: dict[str, int],
                          equipment_seats: dict[str, int], outcomes: dict[str, dict[str, int]],
                          prerequisites: dict[str, list[str]]) -> list[dict[str, Any]]:
        verified = sum(item["headcount"] for item in demands if item["status"] == "verified")
        intent = sum(item["headcount"] for item in demands if item["status"] == "intent")
        if strategy == "conservative":
            demand = verified
        elif strategy == "balanced":
            demand = verified + intent // 2
        else:
            demand = verified + intent
        equipment_remaining = dict(equipment_seats)
        lines: list[dict[str, Any]] = []
        for course in courses:
            course_id = course["course_id"]
            rate = employment_rate(outcomes.get(course_id))
            scale = self._strategy_scale(strategy, rate)
            target = int(demand * scale)
            hours = teacher_hours.get(course_id, 0)
            classes = hours // course["duration_hours"]
            teacher_cap = classes * course["max_class_size"]
            equipment_cap = equipment_remaining.get(course["equipment_type"], 0)
            prerequisite_ids = prerequisites.get(course_id, [])
            prerequisite_cap = (graduated_students(connection, prerequisite_ids)
                                if prerequisite_ids else None)
            caps: dict[str, int] = {"demand": target, "teacher": teacher_cap,
                                    "equipment": equipment_cap}
            if prerequisite_cap is not None:
                caps["prerequisite"] = prerequisite_cap
            quota = min(caps.values())
            limiting = next(key for key in LIMITING_ORDER if caps[key] == quota)
            equipment_remaining[course["equipment_type"]] = max(0, equipment_cap - quota)
            gap = target - quota
            lines.append({
                "course_id": course_id,
                "quota": quota,
                "demand_headcount": target,
                "limiting_factor": limiting,
                "gap_explanation": self._explain_gap(
                    strategy=strategy, course=course, limiting=limiting, quota=quota,
                    target=target, gap=gap, demand=demand, verified=verified, intent=intent,
                    rate=rate, scale=scale, hours=hours, classes=classes,
                    teacher_cap=teacher_cap, equipment_cap=equipment_cap,
                    prerequisite_cap=prerequisite_cap),
            })
        return lines

    def _explain_gap(self, *, strategy: str, course: dict[str, Any], limiting: str, quota: int,
                     target: int, gap: int, demand: int, verified: int, intent: int,
                     rate: float | None, scale: float, hours: int, classes: int,
                     teacher_cap: int, equipment_cap: int,
                     prerequisite_cap: int | None) -> str:
        strategy_label = {"conservative": "保守", "balanced": "稳健", "aggressive": "积极"}[strategy]
        rate_text = "无历史结业数据，就业率按 1.00 处理" if rate is None else (
            f"历史就业率 {rate:.2f}，{strategy_label}策略折算系数 {scale:.2f}")
        base = (f"需求基数 {demand} 人（已验证 {verified} 人、意向 {intent} 人），{rate_text}，"
                f"目标 {target} 人")
        if limiting == "demand":
            return (f"{base}；名额 {quota} 人由需求规模决定，教师可支撑 {teacher_cap} 人、"
                    f"设备剩余 {equipment_cap} 个工位，容量充足")
        if limiting == "teacher":
            return (f"{base}；教师工时不足：可用 {hours} 小时 ÷ 单课 {course['duration_hours']} 小时 "
                    f"= {classes} 个班 × 班容 {course['max_class_size']} 人 = 可支撑 {teacher_cap} 人，"
                    f"名额 {quota} 人，缺口 {gap} 人")
        if limiting == "equipment":
            return (f"{base}；设备容量不足：{course['equipment_type']} 剩余 {equipment_cap} 个工位，"
                    f"名额 {quota} 人，缺口 {gap} 人")
        return (f"{base}；先修结业人数不足：仅 {prerequisite_cap} 名学员完成先修课程，"
                f"名额 {quota} 人，缺口 {gap} 人")

    def generate_plans(self, *, request_id: str, actor_id: str, occupation: str,
                       region: str, period: str) -> WriteReceipt:
        """按当前需求版本、容量与历史结业反馈一次生成多套招生方案。"""

        payload = {"actor_id": actor_id, "occupation": occupation,
                   "region": region, "period": period}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "planner")
            occupation = self._text(occupation, "occupation", 80)
            region = self._text(region, "region", 80)
            period = self._period(period)

            def create() -> tuple[str, str, dict[str, Any]]:
                course_rows = connection.execute(
                    "SELECT * FROM courses WHERE occupation=? ORDER BY course_id", (occupation,)
                ).fetchall()
                if not course_rows:
                    raise ValidationError("该职业尚未登记课程")
                courses = [dict(row) for row in course_rows]
                course_ids = [course["course_id"] for course in courses]
                prerequisites = self._prerequisite_map(connection, course_ids)
                courses = self._topo_order(courses, prerequisites)
                demands, exclusions = self._active_demands(connection, occupation, region, period)
                teacher_rows = connection.execute(
                    "SELECT course_id, SUM(available_hours) AS hours FROM teacher_capacities "
                    "WHERE period=? GROUP BY course_id", (period,)
                ).fetchall()
                teacher_hours = {row["course_id"]: int(row["hours"]) for row in teacher_rows}
                equipment_rows = connection.execute(
                    "SELECT equipment_type, seats FROM equipment_capacities WHERE period=?", (period,)
                ).fetchall()
                equipment_seats = {row["equipment_type"]: int(row["seats"]) for row in equipment_rows}
                outcomes = course_outcomes(connection)

                batch_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO plan_batches(batch_id,occupation,region,period,exclusions_json,"
                    "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                    (batch_id, occupation, region, period,
                     json.dumps(exclusions, ensure_ascii=False, sort_keys=True),
                     actor_id, self._now()),
                )
                plans: list[dict[str, Any]] = []
                for strategy in STRATEGIES:
                    lines = self._build_plan_lines(
                        connection, strategy=strategy, courses=courses, demands=demands,
                        teacher_hours=teacher_hours, equipment_seats=equipment_seats,
                        outcomes=outcomes, prerequisites=prerequisites)
                    snapshot = {
                        "generated_at": self._now(),
                        "occupation": occupation,
                        "region": region,
                        "period": period,
                        "strategy": strategy,
                        "demands": [{"demand_id": item["demand_id"], "version": item["version"],
                                     "status": item["status"], "headcount": item["headcount"],
                                     "valid_until": item["valid_until"],
                                     "window_start": item["window_start"],
                                     "window_end": item["window_end"]} for item in demands],
                        "courses": [{"course_id": course["course_id"],
                                     "duration_hours": course["duration_hours"],
                                     "equipment_type": course["equipment_type"],
                                     "max_class_size": course["max_class_size"],
                                     "prerequisites": prerequisites.get(course["course_id"], [])}
                                    for course in courses],
                        "teacher_hours": {course_id: teacher_hours.get(course_id, 0)
                                          for course_id in course_ids},
                        "equipment_seats": equipment_seats,
                        "history": {course_id: {
                            "graduated": outcomes.get(course_id, {}).get("graduated", 0),
                            "employed": outcomes.get(course_id, {}).get("employed", 0),
                            "employment_rate": serialize_rate(employment_rate(outcomes.get(course_id))),
                        } for course_id in course_ids},
                    }
                    snapshot_json = json.dumps(snapshot, ensure_ascii=False, sort_keys=True)
                    plan_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO plans(plan_id,batch_id,occupation,region,period,strategy,status,"
                        "snapshot_json,snapshot_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (plan_id, batch_id, occupation, region, period, strategy, "draft",
                         snapshot_json, digest(snapshot), self._now()),
                    )
                    for line in lines:
                        connection.execute(
                            "INSERT INTO plan_lines(plan_id,course_id,quota,demand_headcount,"
                            "limiting_factor,gap_explanation) VALUES(?,?,?,?,?,?)",
                            (plan_id, line["course_id"], line["quota"], line["demand_headcount"],
                             line["limiting_factor"], line["gap_explanation"]),
                        )
                    plans.append({"plan_id": plan_id, "strategy": strategy,
                                  "lines": [{key: line[key] for key in
                                             ("course_id", "quota", "limiting_factor")}
                                            for line in lines]})
                append_event(connection, actor_id=actor_id, action="plan_batch.generated",
                             resource_type="plan_batch", resource_id=batch_id,
                             detail={"occupation": occupation, "region": region, "period": period,
                                     "demands": len(demands), "exclusions": len(exclusions),
                                     "plans": [plan["plan_id"] for plan in plans]},
                             occurred_at=self._now())
                return "plan_batch", batch_id, {"batch_id": batch_id, "plans": plans,
                                                "exclusions": exclusions}

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_plans", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 方案选择、冻结与审批
    # ------------------------------------------------------------------

    def _load_plan(self, connection, plan_id: str):
        row = connection.execute("SELECT * FROM plans WHERE plan_id=?", (plan_id,)).fetchone()
        if row is None:
            raise NotFoundError("招生方案不存在")
        return row

    def select_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        """负责人选择方案，同时冻结其输入快照，此后需求变化只产生影响提示。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "planner")
            plan_id = self._identifier(plan_id, "plan_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._load_plan(connection, plan_id)
                if plan["status"] != "draft":
                    raise ConflictError("只有待选状态的方案可以被选择")
                connection.execute(
                    "UPDATE plans SET status='selected', selected_by=?, selected_at=? WHERE plan_id=?",
                    (actor_id, self._now(), plan_id),
                )
                append_event(connection, actor_id=actor_id, action="plan.selected",
                             resource_type="plan", resource_id=plan_id,
                             detail={"snapshot_hash": plan["snapshot_hash"],
                                     "strategy": plan["strategy"]},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": "selected",
                                         "snapshot_hash": plan["snapshot_hash"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="select_plan", payload=payload, create=create)

    def reject_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        """否决尚未生效的方案。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "planner")
            plan_id = self._identifier(plan_id, "plan_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._load_plan(connection, plan_id)
                if plan["status"] not in ("draft", "selected"):
                    raise ConflictError("只有待选或已选择的方案可以被否决")
                connection.execute("UPDATE plans SET status='rejected' WHERE plan_id=?", (plan_id,))
                append_event(connection, actor_id=actor_id, action="plan.rejected",
                             resource_type="plan", resource_id=plan_id,
                             detail={"previous_status": plan["status"]}, occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_plan", payload=payload, create=create)

    def approve_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        """审批已选择的方案：通过后才形成招生名额，同一职业地区期间最多一个生效方案。"""

        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "approver")
            plan_id = self._identifier(plan_id, "plan_id")

            def create() -> tuple[str, str, dict[str, Any]]:
                plan = self._load_plan(connection, plan_id)
                if plan["status"] != "selected":
                    raise ConflictError("方案未处于待审批状态")
                if plan["selected_by"] == actor_id:
                    raise PermissionDenied("方案选择与审批不能由同一人完成")
                existing = connection.execute(
                    "SELECT plan_id FROM plans WHERE occupation=? AND region=? AND period=? "
                    "AND status='approved'",
                    (plan["occupation"], plan["region"], plan["period"]),
                ).fetchone()
                if existing:
                    raise ConflictError("该职业与地区在本期已存在生效方案")
                try:
                    connection.execute(
                        "UPDATE plans SET status='approved', approved_by=?, approved_at=? "
                        "WHERE plan_id=?",
                        (actor_id, self._now(), plan_id),
                    )
                except Exception as exc:
                    raise ConflictError("该职业与地区在本期已存在生效方案") from exc
                lines = connection.execute(
                    "SELECT * FROM plan_lines WHERE plan_id=? ORDER BY course_id", (plan_id,)
                ).fetchall()
                quotas: list[dict[str, Any]] = []
                for line in lines:
                    quota_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO quotas(quota_id,plan_id,course_id,period,headcount,enrolled,"
                        "created_at) VALUES(?,?,?,?,?,0,?)",
                        (quota_id, plan_id, line["course_id"], plan["period"],
                         line["quota"], self._now()),
                    )
                    quotas.append({"quota_id": quota_id, "course_id": line["course_id"],
                                   "headcount": line["quota"]})
                append_event(connection, actor_id=actor_id, action="plan.approved",
                             resource_type="plan", resource_id=plan_id,
                             detail={"snapshot_hash": plan["snapshot_hash"],
                                     "quotas": quotas},
                             occurred_at=self._now())
                return "plan", plan_id, {"plan_id": plan_id, "status": "approved",
                                         "quotas": quotas}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_plan", payload=payload, create=create)

    # ------------------------------------------------------------------
    # 查询与追溯
    # ------------------------------------------------------------------

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = self._load_plan(connection, plan_id)
        lines = connection.execute(
            "SELECT * FROM plan_lines WHERE plan_id=? ORDER BY course_id", (plan_id,)
        ).fetchall()
        batch = connection.execute(
            "SELECT * FROM plan_batches WHERE batch_id=?", (plan["batch_id"],)
        ).fetchone()
        return {
            **dict(plan),
            "snapshot": json.loads(plan["snapshot_json"]),
            "lines": [dict(line) for line in lines],
            "exclusions": json.loads(batch["exclusions_json"]),
        }

    def list_plans(self, *, batch_id: str | None = None, occupation: str | None = None,
                   region: str | None = None, period: str | None = None) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        for field, value in (("batch_id", batch_id), ("occupation", occupation),
                             ("region", region), ("period", period)):
            if value:
                conditions.append(f"{field}=?")
                parameters.append(value)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.database.connection.execute(
            f"SELECT plan_id,batch_id,occupation,region,period,strategy,status,snapshot_hash,"
            f"selected_by,approved_by,created_at FROM plans{where} ORDER BY created_at, plan_id",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def list_quotas(self, *, plan_id: str | None = None,
                    period: str | None = None) -> list[dict[str, Any]]:
        conditions: list[str] = []
        parameters: list[Any] = []
        if plan_id:
            conditions.append("plan_id=?")
            parameters.append(plan_id)
        if period:
            conditions.append("period=?")
            parameters.append(period)
        where = (" WHERE " + " AND ".join(conditions)) if conditions else ""
        rows = self.database.connection.execute(
            f"SELECT * FROM quotas{where} ORDER BY plan_id, course_id", parameters
        ).fetchall()
        return [dict(row) for row in rows]

    def list_impact_notices(self, plan_id: str) -> list[dict[str, Any]]:
        rows = self.database.connection.execute(
            "SELECT * FROM impact_notices WHERE plan_id=? ORDER BY created_at, notice_id",
            (plan_id,),
        ).fetchall()
        return [{**dict(row), "detail": json.loads(row["detail_json"])} for row in rows]

    def trace_quota(self, quota_id: str) -> dict[str, Any]:
        """追溯名额的需求来源、容量取舍、影响提示与报告修订。"""

        connection = self.database.connection
        quota = connection.execute("SELECT * FROM quotas WHERE quota_id=?", (quota_id,)).fetchone()
        if quota is None:
            raise NotFoundError("招生名额不存在")
        plan = self._load_plan(connection, quota["plan_id"])
        line = connection.execute(
            "SELECT * FROM plan_lines WHERE plan_id=? AND course_id=?",
            (plan["plan_id"], quota["course_id"]),
        ).fetchone()
        snapshot = json.loads(plan["snapshot_json"])
        notices = self.list_impact_notices(plan["plan_id"])
        reports = connection.execute(
            "SELECT report_id,period,version,status,published_by,published_at FROM reports "
            "WHERE period=? ORDER BY version", (quota["period"],)
        ).fetchall()
        corrections = connection.execute(
            "SELECT status, COUNT(*) AS count FROM report_corrections WHERE period=? GROUP BY status",
            (quota["period"],),
        ).fetchall()
        return {
            "quota": dict(quota),
            "plan": {key: plan[key] for key in
                     ("plan_id", "batch_id", "occupation", "region", "period", "strategy",
                      "status", "snapshot_hash", "selected_by", "selected_at",
                      "approved_by", "approved_at")},
            "capacity_tradeoff": None if line is None else {
                "quota": line["quota"],
                "demand_headcount": line["demand_headcount"],
                "limiting_factor": line["limiting_factor"],
                "gap_explanation": line["gap_explanation"],
            },
            "demand_sources": snapshot["demands"],
            "capacity_inputs": {
                "teacher_hours": snapshot["teacher_hours"].get(quota["course_id"], 0),
                "equipment_seats": snapshot["equipment_seats"],
                "history": snapshot["history"].get(quota["course_id"]),
                "course": next((item for item in snapshot["courses"]
                                if item["course_id"] == quota["course_id"]), None),
            },
            "impact_notices": notices,
            "report_revisions": [dict(row) for row in reports],
            "correction_counts": {row["status"]: row["count"] for row in corrections},
        }
