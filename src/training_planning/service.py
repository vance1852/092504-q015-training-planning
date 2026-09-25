"""新职业培训名额与就业反馈规划服务的领域服务。

在基础服务的幂等、权限与审计能力之上实现:
- 企业岗位需求的版本化管理(意向/已验证/撤回);
- 结合课程先修、教师工时、设备容量与历史结业反馈生成多套招生方案;
- 负责人冻结方案快照、审批人审批后形成招生名额,并发审批最多一个生效方案;
- 冻结或生效之后的需求变化只登记影响提示;
- 学员入学、转班、结业、就业反馈按业务编号幂等归并,迟到反馈进入更正流程;
- 管理接口追溯每个名额的需求来源、容量取舍与报告修订。
"""

from __future__ import annotations

import json
import uuid
from datetime import date
from typing import Any

from skills_workspace.audit import append_event, canonical_json, digest
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.models import Actor, WriteReceipt
from skills_workspace.service import DomainService

from . import planning
from .storage import PlanningDatabase


EVENT_TYPES = frozenset({"enrolled", "transferred", "graduated", "employed"})


class PlanningService(DomainService):
    """协调岗位需求、招生方案、审批名额与就业反馈更正规则。"""

    database: PlanningDatabase

    # ---------- 通用校验 ----------

    def _organization(self, connection, organization_id: str) -> str:
        organization_id = self._identifier(organization_id, "organization_id")
        row = connection.execute(
            "SELECT 1 FROM organizations WHERE organization_id=?", (organization_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("组织不存在")
        return organization_id

    def _scope(self, actor: Actor, organization_id: str) -> None:
        if actor.role != "admin" and actor.organization_id != organization_id:
            raise PermissionDenied("不能操作其他组织的数据")

    @staticmethod
    def _date(value: str, field: str) -> date:
        try:
            return date.fromisoformat(str(value).strip())
        except ValueError as exc:
            raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期") from exc

    @staticmethod
    def _count(value: int, field: str, minimum: int = 0) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValidationError(f"{field} 必须是不小于 {minimum} 的整数")
        return value

    def _plan_row(self, connection, plan_id: str):
        row = connection.execute(
            "SELECT * FROM enrollment_plans WHERE plan_id=?", (plan_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("方案不存在")
        return row

    def _demand_version_row(self, connection, organization_id: str,
                            demand_key: str, version_no: int):
        row = connection.execute(
            "SELECT dv.*, ds.series_id AS series_id FROM demand_versions dv "
            "JOIN demand_series ds ON ds.series_id = dv.series_id "
            "WHERE ds.organization_id=? AND ds.demand_key=? AND dv.version_no=?",
            (organization_id, demand_key, version_no),
        ).fetchone()
        if row is None:
            raise NotFoundError("需求版本不存在")
        return row

    # ---------- 岗位需求 ----------

    def submit_demand(self, *, request_id: str, actor_id: str, organization_id: str,
                      demand_key: str, enterprise_name: str, region: str, occupation: str,
                      headcount: int, window_start: str, window_end: str,
                      note: str = "") -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "demand_key": demand_key, "enterprise_name": enterprise_name,
                   "region": region, "occupation": occupation, "headcount": headcount,
                   "window_start": window_start, "window_end": window_end, "note": note}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "planner")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            demand_key = self._identifier(demand_key, "demand_key")
            enterprise_name = self._text(enterprise_name, "enterprise_name")
            region = self._text(region, "region", 80)
            occupation = self._text(occupation, "occupation", 80)
            headcount = self._count(headcount, "headcount", 1)
            start = self._date(window_start, "window_start")
            end = self._date(window_end, "window_end")
            if start > end:
                raise ValidationError("需求有效期开始不能晚于结束")
            note = str(note or "").strip()
            if len(note) > 500:
                raise ValidationError("note 不能超过 500 个字符")

            def create() -> tuple[str, str, dict[str, Any]]:
                series = connection.execute(
                    "SELECT * FROM demand_series WHERE organization_id=? AND demand_key=?",
                    (organization_id, demand_key),
                ).fetchone()
                if series is None:
                    series_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO demand_series(series_id,organization_id,demand_key,"
                        "enterprise_name,region,occupation,created_at) VALUES(?,?,?,?,?,?,?)",
                        (series_id, organization_id, demand_key, enterprise_name,
                         region, occupation, self._now()),
                    )
                    next_no = 1
                else:
                    series_id = series["series_id"]
                    if (series["enterprise_name"] != enterprise_name
                            or series["region"] != region or series["occupation"] != occupation):
                        raise ConflictError("需求业务编号已用于其他企业、地区或职业")
                    latest = connection.execute(
                        "SELECT * FROM demand_versions WHERE series_id=? "
                        "ORDER BY version_no DESC LIMIT 1",
                        (series_id,),
                    ).fetchone()
                    next_no = latest["version_no"] + 1
                    if (latest["status"] == "intent" and latest["headcount"] == headcount
                            and latest["window_start"] == start.isoformat()
                            and latest["window_end"] == end.isoformat()
                            and latest["note"] == note):
                        # 与最新意向版本内容一致的重复提交按业务键归并。
                        return "demand_version", latest["version_id"], {
                            "series_id": series_id, "version_id": latest["version_id"],
                            "version_no": latest["version_no"], "status": latest["status"],
                            "merged": True}
                version_id = uuid.uuid4().hex
                connection.execute(
                    "INSERT INTO demand_versions(version_id,series_id,version_no,headcount,"
                    "window_start,window_end,status,note,submitted_by,submitted_at) "
                    "VALUES(?,?,?,?,?,?,'intent',?,?,?)",
                    (version_id, series_id, next_no, headcount, start.isoformat(),
                     end.isoformat(), note, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="demand.submitted",
                             resource_type="demand_version", resource_id=version_id,
                             detail={"series_id": series_id, "demand_key": demand_key,
                                     "version_no": next_no, "enterprise_name": enterprise_name,
                                     "region": region, "occupation": occupation,
                                     "headcount": headcount,
                                     "window_start": start.isoformat(),
                                     "window_end": end.isoformat()},
                             occurred_at=self._now())
                notices = self._record_impact_notices(
                    connection, actor_id=actor_id, organization_id=organization_id,
                    series_id=series_id, version_id=version_id, change_type="submitted")
                return "demand_version", version_id, {
                    "series_id": series_id, "version_id": version_id,
                    "version_no": next_no, "status": "intent",
                    "merged": False, "impact_notices": notices}

            return self._idempotent(connection, request_id=request_id,
                                    action="submit_demand", payload=payload, create=create)

    def verify_demand(self, *, request_id: str, actor_id: str, organization_id: str,
                      demand_key: str, version_no: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "demand_key": demand_key, "version_no": version_no}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "director")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            demand_key = self._identifier(demand_key, "demand_key")
            version_no = self._count(version_no, "version_no", 1)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = self._demand_version_row(connection, organization_id, demand_key, version_no)
                if row["status"] != "intent":
                    raise ConflictError("仅意向状态的需求可以验证")
                connection.execute(
                    "UPDATE demand_versions SET status='verified', decided_by=?, decided_at=?, "
                    "decision_reason=NULL WHERE version_id=?",
                    (actor_id, self._now(), row["version_id"]),
                )
                append_event(connection, actor_id=actor_id, action="demand.verified",
                             resource_type="demand_version", resource_id=row["version_id"],
                             detail={"series_id": row["series_id"], "demand_key": demand_key,
                                     "version_no": version_no},
                             occurred_at=self._now())
                notices = self._record_impact_notices(
                    connection, actor_id=actor_id, organization_id=organization_id,
                    series_id=row["series_id"], version_id=row["version_id"],
                    change_type="verified", was_verified=False)
                return "demand_version", row["version_id"], {
                    "series_id": row["series_id"], "version_id": row["version_id"],
                    "version_no": version_no, "status": "verified", "impact_notices": notices}

            return self._idempotent(connection, request_id=request_id,
                                    action="verify_demand", payload=payload, create=create)

    def withdraw_demand(self, *, request_id: str, actor_id: str, organization_id: str,
                        demand_key: str, version_no: int, reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "demand_key": demand_key, "version_no": version_no, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "planner", "director")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            demand_key = self._identifier(demand_key, "demand_key")
            version_no = self._count(version_no, "version_no", 1)
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                row = self._demand_version_row(connection, organization_id, demand_key, version_no)
                if row["status"] not in ("intent", "verified"):
                    raise ConflictError("已撤回的需求不能重复撤回")
                was_verified = row["status"] == "verified"
                connection.execute(
                    "UPDATE demand_versions SET status='withdrawn', decided_by=?, decided_at=?, "
                    "decision_reason=? WHERE version_id=?",
                    (actor_id, self._now(), reason, row["version_id"]),
                )
                append_event(connection, actor_id=actor_id, action="demand.withdrawn",
                             resource_type="demand_version", resource_id=row["version_id"],
                             detail={"series_id": row["series_id"], "demand_key": demand_key,
                                     "version_no": version_no, "was_verified": was_verified,
                                     "reason": reason},
                             occurred_at=self._now())
                notices = self._record_impact_notices(
                    connection, actor_id=actor_id, organization_id=organization_id,
                    series_id=row["series_id"], version_id=row["version_id"],
                    change_type="withdrawn", was_verified=was_verified)
                return "demand_version", row["version_id"], {
                    "series_id": row["series_id"], "version_id": row["version_id"],
                    "version_no": version_no, "status": "withdrawn", "impact_notices": notices}

            return self._idempotent(connection, request_id=request_id,
                                    action="withdraw_demand", payload=payload, create=create)

    def _record_impact_notices(self, connection, *, actor_id: str, organization_id: str,
                               series_id: str, version_id: str, change_type: str,
                               was_verified: bool = False) -> int:
        """为冻结或生效方案登记需求变化的影响提示,不改动方案本身。"""

        version = connection.execute(
            "SELECT dv.*, ds.demand_key, ds.enterprise_name, ds.region, ds.occupation "
            "FROM demand_versions dv JOIN demand_series ds ON ds.series_id=dv.series_id "
            "WHERE dv.version_id=?",
            (version_id,),
        ).fetchone()
        plans = connection.execute(
            "SELECT * FROM enrollment_plans WHERE organization_id=? "
            "AND status IN ('frozen','approved')",
            (organization_id,),
        ).fetchall()
        count = 0
        for plan in plans:
            if (version["window_end"] < plan["period_start"]
                    or version["window_start"] > plan["period_end"]):
                continue
            matched = connection.execute(
                "SELECT 1 FROM plan_allocations pa JOIN courses c ON c.course_id=pa.course_id "
                "WHERE pa.plan_id=? AND c.occupation=? LIMIT 1",
                (plan["plan_id"], version["occupation"]),
            ).fetchone()
            if matched is None:
                continue
            snapshot = json.loads(plan["snapshot_json"])
            snapshot_versions = {item["version_id"] for item in snapshot.get("demands", [])}
            series_versions = {row["version_id"] for row in connection.execute(
                "SELECT version_id FROM demand_versions WHERE series_id=?", (series_id,))}
            in_snapshot = version_id in snapshot_versions
            if change_type == "withdrawn":
                if in_snapshot:
                    impact = "snapshot_demand_withdrawn"
                elif was_verified or (series_versions & snapshot_versions):
                    impact = "snapshot_input_changed"
                else:
                    continue  # 撤回从未进入快照的意向版本不影响方案
            elif in_snapshot or (series_versions & snapshot_versions):
                impact = "snapshot_input_changed"
            else:
                impact = "new_demand_after_freeze"
            notice_id = uuid.uuid4().hex
            detail = {"demand_key": version["demand_key"], "version_no": version["version_no"],
                      "enterprise_name": version["enterprise_name"], "region": version["region"],
                      "occupation": version["occupation"], "headcount": version["headcount"],
                      "window_start": version["window_start"], "window_end": version["window_end"],
                      "period_key": plan["period_key"], "plan_status": plan["status"]}
            connection.execute(
                "INSERT INTO impact_notices(notice_id,plan_id,series_id,version_id,change_type,"
                "impact,detail_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (notice_id, plan["plan_id"], series_id, version_id, change_type, impact,
                 canonical_json(detail), self._now()),
            )
            append_event(connection, actor_id=actor_id, action="impact_notice.created",
                         resource_type="impact_notice", resource_id=notice_id,
                         detail={**detail, "plan_id": plan["plan_id"], "impact": impact,
                                 "change_type": change_type},
                         occurred_at=self._now())
            count += 1
        return count

    # ---------- 课程、教师与设备 ----------

    def register_course(self, *, request_id: str, actor_id: str, organization_id: str,
                        course_id: str, name: str, occupation: str, duration_hours: int,
                        class_size: int, prerequisites: list[str] | None = None,
                        equipment_needs: list[dict[str, Any]] | None = None) -> WriteReceipt:
        prerequisites = list(prerequisites or [])
        equipment_needs = list(equipment_needs or [])
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "course_id": course_id, "name": name, "occupation": occupation,
                   "duration_hours": duration_hours, "class_size": class_size,
                   "prerequisites": prerequisites, "equipment_needs": equipment_needs}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "planner")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            course_id = self._identifier(course_id, "course_id")
            name = self._text(name, "name")
            occupation = self._text(occupation, "occupation", 80)
            duration_hours = self._count(duration_hours, "duration_hours", 1)
            class_size = self._count(class_size, "class_size", 1)
            seen: set[str] = set()
            for prerequisite in prerequisites:
                prerequisite = self._identifier(prerequisite, "prerequisites")
                if prerequisite == course_id:
                    raise ValidationError("课程不能以自身为先修")
                if prerequisite in seen:
                    raise ValidationError("先修课程重复")
                seen.add(prerequisite)
                row = connection.execute(
                    "SELECT 1 FROM courses WHERE course_id=? AND organization_id=?",
                    (prerequisite, organization_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("先修课程不存在")
            normalized_needs: list[tuple[str, int]] = []
            for need in equipment_needs:
                if not isinstance(need, dict):
                    raise ValidationError("equipment_needs 必须是对象数组")
                equipment_id = self._identifier(need.get("equipment_id", ""), "equipment_id")
                units_per_class = self._count(need.get("units_per_class"), "units_per_class", 1)
                row = connection.execute(
                    "SELECT 1 FROM equipment WHERE equipment_id=? AND organization_id=?",
                    (equipment_id, organization_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("设备不存在")
                normalized_needs.append((equipment_id, units_per_class))

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO courses(course_id,organization_id,name,occupation,"
                        "duration_hours,class_size,active,created_at) VALUES(?,?,?,?,?,?,1,?)",
                        (course_id, organization_id, name, occupation, duration_hours,
                         class_size, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("课程编号已经存在") from exc
                for prerequisite in seen:
                    connection.execute(
                        "INSERT INTO course_prerequisites(course_id,prerequisite_id) VALUES(?,?)",
                        (course_id, prerequisite),
                    )
                for equipment_id, units_per_class in normalized_needs:
                    connection.execute(
                        "INSERT INTO course_equipment(course_id,equipment_id,units_per_class) "
                        "VALUES(?,?,?)",
                        (course_id, equipment_id, units_per_class),
                    )
                append_event(connection, actor_id=actor_id, action="course.registered",
                             resource_type="course", resource_id=course_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "occupation": occupation, "duration_hours": duration_hours,
                                     "class_size": class_size, "prerequisites": sorted(seen),
                                     "equipment_needs": [
                                         {"equipment_id": e, "units_per_class": u}
                                         for e, u in normalized_needs]},
                             occurred_at=self._now())
                return "course", course_id, {"course_id": course_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_course", payload=payload, create=create)

    def register_teacher(self, *, request_id: str, actor_id: str, organization_id: str,
                         teacher_id: str, name: str, available_hours: int,
                         course_ids: list[str] | None = None) -> WriteReceipt:
        course_ids = list(course_ids or [])
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "teacher_id": teacher_id, "name": name,
                   "available_hours": available_hours, "course_ids": course_ids}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "planner")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            teacher_id = self._identifier(teacher_id, "teacher_id")
            name = self._text(name, "name")
            available_hours = self._count(available_hours, "available_hours", 0)
            for course_id in course_ids:
                course_id = self._identifier(course_id, "course_ids")
                row = connection.execute(
                    "SELECT 1 FROM courses WHERE course_id=? AND organization_id=?",
                    (course_id, organization_id),
                ).fetchone()
                if row is None:
                    raise NotFoundError("课程不存在")

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO teachers(teacher_id,organization_id,name,available_hours,"
                        "active,created_at) VALUES(?,?,?,?,1,?)",
                        (teacher_id, organization_id, name, available_hours, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("教师编号已经存在") from exc
                for course_id in dict.fromkeys(course_ids):
                    connection.execute(
                        "INSERT INTO teacher_courses(teacher_id,course_id) VALUES(?,?)",
                        (teacher_id, course_id),
                    )
                append_event(connection, actor_id=actor_id, action="teacher.registered",
                             resource_type="teacher", resource_id=teacher_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "available_hours": available_hours,
                                     "course_ids": sorted(set(course_ids))},
                             occurred_at=self._now())
                return "teacher", teacher_id, {"teacher_id": teacher_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_teacher", payload=payload, create=create)

    def register_equipment(self, *, request_id: str, actor_id: str, organization_id: str,
                           equipment_id: str, name: str, units: int) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "equipment_id": equipment_id, "name": name, "units": units}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "operator", "planner")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            equipment_id = self._identifier(equipment_id, "equipment_id")
            name = self._text(name, "name")
            units = self._count(units, "units", 0)

            def create() -> tuple[str, str, dict[str, Any]]:
                try:
                    connection.execute(
                        "INSERT INTO equipment(equipment_id,organization_id,name,units,active,"
                        "created_at) VALUES(?,?,?,?,1,?)",
                        (equipment_id, organization_id, name, units, self._now()),
                    )
                except Exception as exc:
                    raise ConflictError("设备编号已经存在") from exc
                append_event(connection, actor_id=actor_id, action="equipment.registered",
                             resource_type="equipment", resource_id=equipment_id,
                             detail={"organization_id": organization_id, "name": name,
                                     "units": units},
                             occurred_at=self._now())
                return "equipment", equipment_id, {"equipment_id": equipment_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="register_equipment", payload=payload, create=create)

    # ---------- 招生方案 ----------

    def generate_plans(self, *, request_id: str, actor_id: str, organization_id: str,
                       period_key: str, period_start: str, period_end: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "period_key": period_key, "period_start": period_start,
                   "period_end": period_end}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "planner")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            period_key = self._identifier(period_key, "period_key")
            start = self._date(period_start, "period_start")
            end = self._date(period_end, "period_end")
            if start > end:
                raise ValidationError("规划期间开始不能晚于结束")

            def create() -> tuple[str, str, dict[str, Any]]:
                inputs = self._gather_planning_inputs(connection, organization_id)
                drafts = planning.generate_plans(
                    **inputs, period_key=period_key,
                    period_start=start.isoformat(), period_end=end.isoformat())
                row = connection.execute(
                    "SELECT MAX(generation_no) AS latest FROM enrollment_plans "
                    "WHERE organization_id=? AND period_key=?",
                    (organization_id, period_key),
                ).fetchone()
                generation_no = (row["latest"] or 0) + 1
                connection.execute(
                    "UPDATE enrollment_plans SET status='stale' WHERE organization_id=? "
                    "AND period_key=? AND status='draft'",
                    (organization_id, period_key),
                )
                plan_ids: list[dict[str, str]] = []
                for draft in drafts:
                    plan_id = uuid.uuid4().hex
                    input_hash = digest({"snapshot": draft.snapshot, "strategy": draft.strategy})
                    connection.execute(
                        "INSERT INTO enrollment_plans(plan_id,organization_id,period_key,"
                        "period_start,period_end,strategy,generation_no,status,input_hash,"
                        "snapshot_json,created_by,created_at) VALUES(?,?,?,?,?,?,?,'draft',?,?,?,?)",
                        (plan_id, organization_id, period_key, start.isoformat(),
                         end.isoformat(), draft.strategy, generation_no, input_hash,
                         canonical_json(draft.snapshot), actor_id, self._now()),
                    )
                    for allocation in draft.allocations:
                        connection.execute(
                            "INSERT INTO plan_allocations(plan_id,course_id,classes,quota,"
                            "raw_demand,adjusted_demand) VALUES(?,?,?,?,?,?)",
                            (plan_id, allocation.course_id, allocation.classes,
                             allocation.quota, allocation.raw_demand,
                             allocation.adjusted_demand),
                        )
                        for link in allocation.demand_links:
                            connection.execute(
                                "INSERT INTO plan_allocation_demands(plan_id,course_id,"
                                "version_id,indirect,headcount) VALUES(?,?,?,?,?)",
                                (plan_id, allocation.course_id, link["version_id"],
                                 1 if link["indirect"] else 0, link["headcount"]),
                            )
                    for gap in draft.gaps:
                        connection.execute(
                            "INSERT INTO plan_gaps(gap_id,plan_id,course_id,gap_type,detail_json) "
                            "VALUES(?,?,?,?,?)",
                            (uuid.uuid4().hex, plan_id, gap["course_id"], gap["gap_type"],
                             canonical_json(gap["detail"])),
                        )
                    plan_ids.append({"plan_id": plan_id, "strategy": draft.strategy})
                append_event(connection, actor_id=actor_id, action="plans.generated",
                             resource_type="plan_generation",
                             resource_id=f"{organization_id}:{period_key}:{generation_no}",
                             detail={"organization_id": organization_id, "period_key": period_key,
                                     "generation_no": generation_no, "plans": plan_ids},
                             occurred_at=self._now())
                return "plan_generation", f"{organization_id}:{period_key}:{generation_no}", {
                    "generation_no": generation_no, "plans": plan_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="generate_plans", payload=payload, create=create)

    def _gather_planning_inputs(self, connection, organization_id: str) -> dict[str, Any]:
        """收集方案生成输入:每个需求系列取版本号最高的已验证版本。"""

        rows = connection.execute(
            "SELECT dv.*, ds.demand_key, ds.enterprise_name, ds.region, ds.occupation "
            "FROM demand_versions dv JOIN demand_series ds ON ds.series_id=dv.series_id "
            "WHERE ds.organization_id=? AND dv.status='verified' "
            "ORDER BY dv.series_id, dv.version_no",
            (organization_id,),
        ).fetchall()
        latest: dict[str, Any] = {}
        for row in rows:
            latest[row["series_id"]] = row
        demands = [
            planning.DemandInput(
                version_id=row["version_id"], series_id=row["series_id"],
                demand_key=row["demand_key"], enterprise_name=row["enterprise_name"],
                region=row["region"], occupation=row["occupation"],
                headcount=row["headcount"], window_start=row["window_start"],
                window_end=row["window_end"])
            for row in latest.values()
        ]
        course_rows = connection.execute(
            "SELECT * FROM courses WHERE organization_id=? AND active=1 ORDER BY course_id",
            (organization_id,),
        ).fetchall()
        courses = []
        for row in course_rows:
            prerequisites = tuple(sorted(
                r["prerequisite_id"] for r in connection.execute(
                    "SELECT prerequisite_id FROM course_prerequisites WHERE course_id=?",
                    (row["course_id"],))))
            needs = tuple(sorted(
                (r["equipment_id"], r["units_per_class"]) for r in connection.execute(
                    "SELECT equipment_id, units_per_class FROM course_equipment WHERE course_id=?",
                    (row["course_id"],))))
            courses.append(planning.CourseInput(
                course_id=row["course_id"], occupation=row["occupation"],
                duration_hours=row["duration_hours"], class_size=row["class_size"],
                prerequisites=prerequisites, equipment_needs=needs))
        teachers = []
        for row in connection.execute(
                "SELECT * FROM teachers WHERE organization_id=? AND active=1 ORDER BY teacher_id",
                (organization_id,)):
            course_ids = tuple(sorted(
                r["course_id"] for r in connection.execute(
                    "SELECT course_id FROM teacher_courses WHERE teacher_id=?",
                    (row["teacher_id"],))))
            teachers.append(planning.TeacherInput(
                teacher_id=row["teacher_id"], available_hours=row["available_hours"],
                course_ids=course_ids))
        equipment = [
            planning.EquipmentInput(equipment_id=row["equipment_id"], units=row["units"])
            for row in connection.execute(
                "SELECT * FROM equipment WHERE organization_id=? AND active=1 "
                "ORDER BY equipment_id", (organization_id,))
        ]
        feedback = [
            planning.FeedbackStat(occupation=row["occupation"], graduated=row["graduated"],
                                  employed=row["employed"])
            for row in connection.execute(
                "SELECT c.occupation AS occupation, "
                "SUM(CASE WHEN se.event_type='graduated' THEN 1 ELSE 0 END) AS graduated, "
                "SUM(CASE WHEN se.event_type='employed' THEN 1 ELSE 0 END) AS employed "
                "FROM student_events se JOIN courses c ON c.course_id=se.course_id "
                "WHERE se.organization_id=? GROUP BY c.occupation",
                (organization_id,))
        ]
        return {"demands": demands, "courses": courses, "teachers": teachers,
                "equipment": equipment, "feedback": feedback}

    def freeze_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "director")
            plan = self._plan_row(connection, plan_id)
            self._scope(actor, plan["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] == "stale":
                    raise ConflictError("方案已被新一轮生成取代")
                if plan["status"] != "draft":
                    raise ConflictError("仅草稿方案可以冻结")
                blocker = connection.execute(
                    "SELECT plan_id FROM enrollment_plans WHERE organization_id=? AND period_key=? "
                    "AND status IN ('frozen','approved') LIMIT 1",
                    (plan["organization_id"], plan["period_key"]),
                ).fetchone()
                if blocker is not None:
                    raise ConflictError("本期已存在冻结或生效方案")
                connection.execute(
                    "UPDATE enrollment_plans SET status='frozen', frozen_by=?, frozen_at=? "
                    "WHERE plan_id=?",
                    (actor_id, self._now(), plan_id),
                )
                append_event(connection, actor_id=actor_id, action="plan.frozen",
                             resource_type="enrollment_plan", resource_id=plan_id,
                             detail={"period_key": plan["period_key"],
                                     "strategy": plan["strategy"],
                                     "input_hash": plan["input_hash"]},
                             occurred_at=self._now())
                return "enrollment_plan", plan_id, {
                    "plan_id": plan_id, "status": "frozen", "input_hash": plan["input_hash"]}

            return self._idempotent(connection, request_id=request_id,
                                    action="freeze_plan", payload=payload, create=create)

    def approve_plan(self, *, request_id: str, actor_id: str, plan_id: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "approver")
            plan = self._plan_row(connection, plan_id)
            self._scope(actor, plan["organization_id"])

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "frozen":
                    raise ConflictError("仅冻结方案可以审批通过")
                existing = connection.execute(
                    "SELECT plan_id FROM enrollment_plans WHERE organization_id=? AND period_key=? "
                    "AND status='approved' LIMIT 1",
                    (plan["organization_id"], plan["period_key"]),
                ).fetchone()
                if existing is not None:
                    raise ConflictError("本期已存在生效方案")
                connection.execute(
                    "UPDATE enrollment_plans SET status='approved', decided_by=?, decided_at=? "
                    "WHERE plan_id=?",
                    (actor_id, self._now(), plan_id),
                )
                quota_ids: list[str] = []
                for allocation in connection.execute(
                        "SELECT * FROM plan_allocations WHERE plan_id=? AND quota>0 "
                        "ORDER BY course_id", (plan_id,)):
                    quota_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO enrollment_quotas(quota_id,organization_id,period_key,"
                        "plan_id,course_id,classes,quota,status,created_at) "
                        "VALUES(?,?,?,?,?,?,?,'active',?)",
                        (quota_id, plan["organization_id"], plan["period_key"], plan_id,
                         allocation["course_id"], allocation["classes"], allocation["quota"],
                         self._now()),
                    )
                    quota_ids.append(quota_id)
                append_event(connection, actor_id=actor_id, action="plan.approved",
                             resource_type="enrollment_plan", resource_id=plan_id,
                             detail={"period_key": plan["period_key"],
                                     "strategy": plan["strategy"], "quota_ids": quota_ids},
                             occurred_at=self._now())
                return "enrollment_plan", plan_id, {
                    "plan_id": plan_id, "status": "approved", "quota_ids": quota_ids}

            return self._idempotent(connection, request_id=request_id,
                                    action="approve_plan", payload=payload, create=create)

    def reject_plan(self, *, request_id: str, actor_id: str, plan_id: str,
                    reason: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "plan_id": plan_id, "reason": reason}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "approver")
            plan = self._plan_row(connection, plan_id)
            self._scope(actor, plan["organization_id"])
            reason = self._text(reason, "reason", 500)

            def create() -> tuple[str, str, dict[str, Any]]:
                if plan["status"] != "frozen":
                    raise ConflictError("仅冻结方案可以驳回")
                connection.execute(
                    "UPDATE enrollment_plans SET status='rejected', decided_by=?, decided_at=?, "
                    "decision_reason=? WHERE plan_id=?",
                    (actor_id, self._now(), reason, plan_id),
                )
                append_event(connection, actor_id=actor_id, action="plan.rejected",
                             resource_type="enrollment_plan", resource_id=plan_id,
                             detail={"period_key": plan["period_key"],
                                     "strategy": plan["strategy"], "reason": reason},
                             occurred_at=self._now())
                return "enrollment_plan", plan_id, {
                    "plan_id": plan_id, "status": "rejected"}

            return self._idempotent(connection, request_id=request_id,
                                    action="reject_plan", payload=payload, create=create)

    # ---------- 统计期间与学员事件 ----------

    def record_stat_period(self, *, request_id: str, actor_id: str, organization_id: str,
                           period_key: str, start_on: str, end_on: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "period_key": period_key, "start_on": start_on, "end_on": end_on}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "director")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            period_key = self._identifier(period_key, "period_key")
            start = self._date(start_on, "start_on")
            end = self._date(end_on, "end_on")
            if start > end:
                raise ValidationError("统计期间开始不能晚于结束")

            def create() -> tuple[str, str, dict[str, Any]]:
                overlap = connection.execute(
                    "SELECT period_key FROM stat_periods WHERE organization_id=? "
                    "AND NOT (end_on < ? OR start_on > ?) LIMIT 1",
                    (organization_id, start.isoformat(), end.isoformat()),
                ).fetchone()
                if overlap is not None:
                    raise ConflictError("统计期间与既有期间重叠")
                connection.execute(
                    "INSERT INTO stat_periods(organization_id,period_key,start_on,end_on,"
                    "created_at) VALUES(?,?,?,?,?)",
                    (organization_id, period_key, start.isoformat(), end.isoformat(),
                     self._now()),
                )
                append_event(connection, actor_id=actor_id, action="stat_period.recorded",
                             resource_type="stat_period",
                             resource_id=f"{organization_id}:{period_key}",
                             detail={"organization_id": organization_id,
                                     "period_key": period_key, "start_on": start.isoformat(),
                                     "end_on": end.isoformat()},
                             occurred_at=self._now())
                return "stat_period", f"{organization_id}:{period_key}", {
                    "period_key": period_key}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_stat_period", payload=payload, create=create)

    def record_student_event(self, *, request_id: str, actor_id: str, organization_id: str,
                             event_key: str, event_type: str, student_id: str, course_id: str,
                             occurred_on: str, payload: dict[str, Any] | None = None) -> WriteReceipt:
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ValidationError("payload 必须是对象")
        payload = dict(payload)
        receipt_payload = {"actor_id": actor_id, "organization_id": organization_id,
                           "event_key": event_key, "event_type": event_type,
                           "student_id": student_id, "course_id": course_id,
                           "occurred_on": occurred_on, "payload": payload}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "registrar")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            event_key = self._identifier(event_key, "event_key")
            student_id = self._identifier(student_id, "student_id")
            course_id = self._identifier(course_id, "course_id")
            if event_type not in EVENT_TYPES:
                raise ValidationError("event_type 不在允许范围内")
            course = connection.execute(
                "SELECT 1 FROM courses WHERE course_id=? AND organization_id=?",
                (course_id, organization_id),
            ).fetchone()
            if course is None:
                raise NotFoundError("课程不存在")
            occurred = self._date(occurred_on, "occurred_on")
            period = connection.execute(
                "SELECT * FROM stat_periods WHERE organization_id=? AND start_on<=? AND end_on>=?",
                (organization_id, occurred.isoformat(), occurred.isoformat()),
            ).fetchone()
            if period is None:
                raise ValidationError("该日期不属于任何统计期间")
            if event_type == "transferred":
                to_course_id = payload.get("to_course_id")
                if not to_course_id:
                    raise ValidationError("转班事件必须提供 to_course_id")
                to_course_id = self._identifier(to_course_id, "to_course_id")
                if to_course_id == course_id:
                    raise ValidationError("转班目标课程不能是原课程")
                target = connection.execute(
                    "SELECT 1 FROM courses WHERE course_id=? AND organization_id=?",
                    (to_course_id, organization_id),
                ).fetchone()
                if target is None:
                    raise NotFoundError("转班目标课程不存在")
            content = {"event_type": event_type, "student_id": student_id,
                       "course_id": course_id, "occurred_on": occurred.isoformat(),
                       "payload": payload}
            content_hash = digest(content)

            def create() -> tuple[str, str, dict[str, Any]]:
                existing = connection.execute(
                    "SELECT * FROM student_events WHERE organization_id=? AND event_key=?",
                    (organization_id, event_key),
                ).fetchone()
                if existing is not None:
                    if existing["payload_hash"] != content_hash:
                        raise ConflictError("业务编号已登记不同内容")
                    return "student_event", event_key, {
                        "event_key": event_key, "merged": True,
                        "is_late": bool(existing["is_late"]),
                        "period_key": existing["period_key"]}
                published = connection.execute(
                    "SELECT 1 FROM period_reports WHERE organization_id=? AND period_key=? "
                    "LIMIT 1",
                    (organization_id, period["period_key"]),
                ).fetchone()
                is_late = 1 if published is not None else 0
                connection.execute(
                    "INSERT INTO student_events(organization_id,event_key,event_type,student_id,"
                    "course_id,occurred_on,period_key,payload_json,payload_hash,is_late,"
                    "recorded_by,recorded_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (organization_id, event_key, event_type, student_id, course_id,
                     occurred.isoformat(), period["period_key"], canonical_json(payload),
                     content_hash, is_late, actor_id, self._now()),
                )
                append_event(connection, actor_id=actor_id, action="student_event.recorded",
                             resource_type="student_event",
                             resource_id=f"{organization_id}:{event_key}",
                             detail={"event_key": event_key, "event_type": event_type,
                                     "student_id": student_id, "course_id": course_id,
                                     "period_key": period["period_key"], "is_late": is_late},
                             occurred_at=self._now())
                correction_id = None
                if is_late:
                    # 迟到反馈进入对应期间的更正流程,不改写已发布报告。
                    correction_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO report_corrections(correction_id,organization_id,period_key,"
                        "event_key,status,report_id,created_at) VALUES(?,?,?,?,'pending',NULL,?)",
                        (correction_id, organization_id, period["period_key"], event_key,
                         self._now()),
                    )
                    append_event(connection, actor_id=actor_id,
                                 action="report_correction.created",
                                 resource_type="report_correction", resource_id=correction_id,
                                 detail={"event_key": event_key,
                                         "period_key": period["period_key"]},
                                 occurred_at=self._now())
                return "student_event", event_key, {
                    "event_key": event_key, "merged": False, "is_late": bool(is_late),
                    "period_key": period["period_key"], "correction_id": correction_id}

            return self._idempotent(connection, request_id=request_id,
                                    action="record_student_event",
                                    payload=receipt_payload, create=create)

    def publish_report(self, *, request_id: str, actor_id: str, organization_id: str,
                       period_key: str) -> WriteReceipt:
        payload = {"actor_id": actor_id, "organization_id": organization_id,
                   "period_key": period_key}
        with self.database.transaction(immediate=True) as connection:
            actor = self._actor(connection, actor_id)
            self._require(actor, "admin", "director")
            organization_id = self._organization(connection, organization_id)
            self._scope(actor, organization_id)
            period_key = self._identifier(period_key, "period_key")

            def create() -> tuple[str, str, dict[str, Any]]:
                period = connection.execute(
                    "SELECT * FROM stat_periods WHERE organization_id=? AND period_key=?",
                    (organization_id, period_key),
                ).fetchone()
                if period is None:
                    raise NotFoundError("统计期间不存在")
                events = connection.execute(
                    "SELECT * FROM student_events WHERE organization_id=? AND period_key=? "
                    "ORDER BY event_key",
                    (organization_id, period_key),
                ).fetchall()
                content = self._build_report_content(connection, organization_id,
                                                     period_key, events)
                row = connection.execute(
                    "SELECT MAX(version_no) AS latest FROM period_reports "
                    "WHERE organization_id=? AND period_key=?",
                    (organization_id, period_key),
                ).fetchone()
                version_no = (row["latest"] or 0) + 1
                connection.execute(
                    "UPDATE period_reports SET status='superseded' WHERE organization_id=? "
                    "AND period_key=? AND status='published'",
                    (organization_id, period_key),
                )
                report_id = uuid.uuid4().hex
                pending = connection.execute(
                    "SELECT COUNT(*) AS count FROM report_corrections WHERE organization_id=? "
                    "AND period_key=? AND status='pending'",
                    (organization_id, period_key),
                ).fetchone()["count"]
                connection.execute(
                    "INSERT INTO period_reports(report_id,organization_id,period_key,version_no,"
                    "status,content_json,corrections_applied,published_by,published_at) "
                    "VALUES(?,?,?,?,'published',?,?,?,?)",
                    (report_id, organization_id, period_key, version_no,
                     canonical_json(content), pending, actor_id, self._now()),
                )
                connection.execute(
                    "UPDATE report_corrections SET status='applied', report_id=? "
                    "WHERE organization_id=? AND period_key=? AND status='pending'",
                    (report_id, organization_id, period_key),
                )
                append_event(connection, actor_id=actor_id, action="report.published",
                             resource_type="period_report", resource_id=report_id,
                             detail={"organization_id": organization_id,
                                     "period_key": period_key, "version_no": version_no,
                                     "corrections_applied": pending},
                             occurred_at=self._now())
                return "period_report", report_id, {
                    "report_id": report_id, "period_key": period_key,
                    "version_no": version_no, "corrections_applied": pending}

            return self._idempotent(connection, request_id=request_id,
                                    action="publish_report", payload=payload, create=create)

    def _build_report_content(self, connection, organization_id: str, period_key: str,
                              events) -> dict[str, Any]:
        courses = {
            row["course_id"]: row for row in connection.execute(
                "SELECT course_id, name, occupation FROM courses WHERE organization_id=?",
                (organization_id,))
        }
        stats: dict[str, dict[str, Any]] = {}

        def bucket(course_id: str) -> dict[str, Any]:
            if course_id not in stats:
                course = courses.get(course_id)
                stats[course_id] = {
                    "course_id": course_id,
                    "name": course["name"] if course else course_id,
                    "occupation": course["occupation"] if course else "",
                    "enrolled": 0, "transferred_in": 0, "transferred_out": 0,
                    "graduated": 0, "employed": 0,
                }
            return stats[course_id]

        for event in events:
            entry = bucket(event["course_id"])
            if event["event_type"] == "enrolled":
                entry["enrolled"] += 1
            elif event["event_type"] == "graduated":
                entry["graduated"] += 1
            elif event["event_type"] == "employed":
                entry["employed"] += 1
            elif event["event_type"] == "transferred":
                entry["transferred_out"] += 1
                target = json.loads(event["payload_json"]).get("to_course_id")
                if target:
                    bucket(target)["transferred_in"] += 1
        for entry in stats.values():
            entry["employment_rate"] = (
                round(entry["employed"] / entry["graduated"], 4)
                if entry["graduated"] else None)
        totals = {"enrolled": 0, "transferred_in": 0, "transferred_out": 0,
                  "graduated": 0, "employed": 0}
        for entry in stats.values():
            for key in totals:
                totals[key] += entry[key]
        totals["employment_rate"] = (
            round(totals["employed"] / totals["graduated"], 4)
            if totals["graduated"] else None)
        return {"period_key": period_key,
                "courses": {cid: stats[cid] for cid in sorted(stats)},
                "totals": totals, "event_count": len(events)}

    # ---------- 管理查询与追溯 ----------

    def _reader(self, actor_id: str, organization_id: str | None = None) -> Actor:
        actor = self._actor(self.database.connection, actor_id)
        if (organization_id is not None and actor.role != "admin"
                and actor.organization_id != organization_id):
            raise PermissionDenied("不能查询其他组织的数据")
        return actor

    def list_demands(self, *, actor_id: str, organization_id: str,
                     occupation: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, organization_id)
        parameters: list[Any] = [organization_id]
        query = "SELECT * FROM demand_series WHERE organization_id=?"
        if occupation:
            query += " AND occupation=?"
            parameters.append(occupation)
        query += " ORDER BY demand_key"
        items = []
        for series in self.database.connection.execute(query, parameters):
            versions = [
                {"version_id": row["version_id"], "version_no": row["version_no"],
                 "headcount": row["headcount"], "window_start": row["window_start"],
                 "window_end": row["window_end"], "status": row["status"],
                 "note": row["note"], "submitted_by": row["submitted_by"],
                 "submitted_at": row["submitted_at"], "decided_by": row["decided_by"],
                 "decided_at": row["decided_at"], "decision_reason": row["decision_reason"]}
                for row in self.database.connection.execute(
                    "SELECT * FROM demand_versions WHERE series_id=? ORDER BY version_no",
                    (series["series_id"],))
            ]
            items.append({"series_id": series["series_id"], "demand_key": series["demand_key"],
                          "enterprise_name": series["enterprise_name"],
                          "region": series["region"], "occupation": series["occupation"],
                          "versions": versions})
        return items

    def list_plans(self, *, actor_id: str, organization_id: str,
                   period_key: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, organization_id)
        parameters: list[Any] = [organization_id]
        query = ("SELECT p.*, COALESCE((SELECT SUM(a.quota) FROM plan_allocations a "
                 "WHERE a.plan_id=p.plan_id), 0) AS total_quota "
                 "FROM enrollment_plans p WHERE p.organization_id=?")
        if period_key:
            query += " AND p.period_key=?"
            parameters.append(period_key)
        query += " ORDER BY p.period_key, p.generation_no DESC, p.strategy"
        return [
            {"plan_id": row["plan_id"], "period_key": row["period_key"],
             "period_start": row["period_start"], "period_end": row["period_end"],
             "strategy": row["strategy"], "generation_no": row["generation_no"],
             "status": row["status"], "total_quota": row["total_quota"],
             "input_hash": row["input_hash"], "created_by": row["created_by"],
             "created_at": row["created_at"], "frozen_by": row["frozen_by"],
             "frozen_at": row["frozen_at"], "decided_by": row["decided_by"],
             "decided_at": row["decided_at"]}
            for row in self.database.connection.execute(query, parameters)
        ]

    def get_plan(self, *, actor_id: str, plan_id: str) -> dict[str, Any]:
        connection = self.database.connection
        plan = self._plan_row(connection, plan_id)
        self._reader(actor_id, plan["organization_id"])
        allocations = []
        for row in connection.execute(
                "SELECT pa.*, c.name AS course_name, c.occupation AS occupation "
                "FROM plan_allocations pa JOIN courses c ON c.course_id=pa.course_id "
                "WHERE pa.plan_id=? ORDER BY pa.course_id", (plan_id,)):
            sources = [
                {"version_id": link["version_id"], "version_no": link["version_no"],
                 "demand_key": link["demand_key"], "enterprise_name": link["enterprise_name"],
                 "region": link["region"], "occupation": link["occupation"],
                 "headcount": link["headcount"], "window_start": link["window_start"],
                 "window_end": link["window_end"], "status": link["status"],
                 "indirect": bool(link["indirect"])}
                for link in connection.execute(
                    "SELECT pad.*, dv.version_no, dv.window_start, dv.window_end, dv.status, "
                    "ds.demand_key, ds.enterprise_name, ds.region, ds.occupation "
                    "FROM plan_allocation_demands pad "
                    "JOIN demand_versions dv ON dv.version_id=pad.version_id "
                    "JOIN demand_series ds ON ds.series_id=dv.series_id "
                    "WHERE pad.plan_id=? AND pad.course_id=? "
                    "ORDER BY pad.version_id, pad.indirect",
                    (plan_id, row["course_id"]))
            ]
            allocations.append({
                "course_id": row["course_id"], "course_name": row["course_name"],
                "occupation": row["occupation"], "classes": row["classes"],
                "quota": row["quota"], "raw_demand": row["raw_demand"],
                "adjusted_demand": row["adjusted_demand"], "demand_sources": sources})
        gaps = [
            {"gap_id": row["gap_id"], "course_id": row["course_id"],
             "gap_type": row["gap_type"], "detail": json.loads(row["detail_json"])}
            for row in connection.execute(
                "SELECT * FROM plan_gaps WHERE plan_id=? ORDER BY gap_id", (plan_id,))
        ]
        return {"plan_id": plan_id, "organization_id": plan["organization_id"],
                "period_key": plan["period_key"], "period_start": plan["period_start"],
                "period_end": plan["period_end"], "strategy": plan["strategy"],
                "generation_no": plan["generation_no"], "status": plan["status"],
                "input_hash": plan["input_hash"], "created_by": plan["created_by"],
                "created_at": plan["created_at"], "frozen_by": plan["frozen_by"],
                "frozen_at": plan["frozen_at"], "decided_by": plan["decided_by"],
                "decided_at": plan["decided_at"], "decision_reason": plan["decision_reason"],
                "allocations": allocations, "gaps": gaps,
                "snapshot": json.loads(plan["snapshot_json"])}

    def list_quotas(self, *, actor_id: str, organization_id: str,
                    period_key: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, organization_id)
        parameters: list[Any] = [organization_id]
        query = ("SELECT q.*, c.name AS course_name, c.occupation AS occupation "
                 "FROM enrollment_quotas q JOIN courses c ON c.course_id=q.course_id "
                 "WHERE q.organization_id=?")
        if period_key:
            query += " AND q.period_key=?"
            parameters.append(period_key)
        query += " ORDER BY q.period_key, q.course_id"
        return [
            {"quota_id": row["quota_id"], "period_key": row["period_key"],
             "plan_id": row["plan_id"], "course_id": row["course_id"],
             "course_name": row["course_name"], "occupation": row["occupation"],
             "classes": row["classes"], "quota": row["quota"], "status": row["status"],
             "created_at": row["created_at"]}
            for row in self.database.connection.execute(query, parameters)
        ]

    def trace_quota(self, *, actor_id: str, quota_id: str) -> dict[str, Any]:
        """追溯一个名额的需求来源、容量取舍与审批信息。"""

        connection = self.database.connection
        quota = connection.execute(
            "SELECT * FROM enrollment_quotas WHERE quota_id=?", (quota_id,)
        ).fetchone()
        if quota is None:
            raise NotFoundError("名额不存在")
        self._reader(actor_id, quota["organization_id"])
        plan = self._plan_row(connection, quota["plan_id"])
        course = connection.execute(
            "SELECT * FROM courses WHERE course_id=?", (quota["course_id"],)
        ).fetchone()
        allocation = connection.execute(
            "SELECT * FROM plan_allocations WHERE plan_id=? AND course_id=?",
            (plan["plan_id"], quota["course_id"]),
        ).fetchone()
        demand_sources = [
            {"version_id": link["version_id"], "version_no": link["version_no"],
             "demand_key": link["demand_key"], "enterprise_name": link["enterprise_name"],
             "region": link["region"], "occupation": link["occupation"],
             "headcount": link["headcount"], "window_start": link["window_start"],
             "window_end": link["window_end"], "status": link["status"],
             "indirect": bool(link["indirect"])}
            for link in connection.execute(
                "SELECT pad.*, dv.version_no, dv.window_start, dv.window_end, dv.status, "
                "ds.demand_key, ds.enterprise_name, ds.region, ds.occupation "
                "FROM plan_allocation_demands pad "
                "JOIN demand_versions dv ON dv.version_id=pad.version_id "
                "JOIN demand_series ds ON ds.series_id=dv.series_id "
                "WHERE pad.plan_id=? AND pad.course_id=? ORDER BY pad.version_id, pad.indirect",
                (plan["plan_id"], quota["course_id"]))
        ]
        capacity_tradeoffs = [
            {"gap_type": row["gap_type"], "detail": json.loads(row["detail_json"])}
            for row in connection.execute(
                "SELECT * FROM plan_gaps WHERE plan_id=? AND course_id=? ORDER BY gap_id",
                (plan["plan_id"], quota["course_id"]))
        ]
        return {
            "quota": {"quota_id": quota["quota_id"], "course_id": quota["course_id"],
                      "classes": quota["classes"], "quota": quota["quota"],
                      "status": quota["status"], "period_key": quota["period_key"]},
            "course": {"course_id": course["course_id"], "name": course["name"],
                       "occupation": course["occupation"]},
            "plan": {"plan_id": plan["plan_id"], "strategy": plan["strategy"],
                     "generation_no": plan["generation_no"], "status": plan["status"],
                     "input_hash": plan["input_hash"], "frozen_by": plan["frozen_by"],
                     "frozen_at": plan["frozen_at"], "decided_by": plan["decided_by"],
                     "decided_at": plan["decided_at"]},
            "allocation": {"raw_demand": allocation["raw_demand"],
                           "adjusted_demand": allocation["adjusted_demand"],
                           "classes": allocation["classes"], "quota": allocation["quota"]},
            "demand_sources": demand_sources,
            "capacity_tradeoffs": capacity_tradeoffs,
        }

    def list_impact_notices(self, *, actor_id: str, organization_id: str,
                            plan_id: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, organization_id)
        parameters: list[Any] = [organization_id]
        query = ("SELECT n.* FROM impact_notices n JOIN enrollment_plans p ON p.plan_id=n.plan_id "
                 "WHERE p.organization_id=?")
        if plan_id:
            query += " AND n.plan_id=?"
            parameters.append(plan_id)
        query += " ORDER BY n.created_at, n.rowid"
        return [
            {"notice_id": row["notice_id"], "plan_id": row["plan_id"],
             "series_id": row["series_id"], "version_id": row["version_id"],
             "change_type": row["change_type"], "impact": row["impact"],
             "detail": json.loads(row["detail_json"]), "created_at": row["created_at"]}
            for row in self.database.connection.execute(query, parameters)
        ]

    def list_student_events(self, *, actor_id: str, organization_id: str,
                            period_key: str | None = None) -> list[dict[str, Any]]:
        self._reader(actor_id, organization_id)
        parameters: list[Any] = [organization_id]
        query = "SELECT * FROM student_events WHERE organization_id=?"
        if period_key:
            query += " AND period_key=?"
            parameters.append(period_key)
        query += " ORDER BY occurred_on, event_key"
        return [
            {"event_key": row["event_key"], "event_type": row["event_type"],
             "student_id": row["student_id"], "course_id": row["course_id"],
             "occurred_on": row["occurred_on"], "period_key": row["period_key"],
             "payload": json.loads(row["payload_json"]), "is_late": bool(row["is_late"]),
             "recorded_by": row["recorded_by"], "recorded_at": row["recorded_at"]}
            for row in self.database.connection.execute(query, parameters)
        ]

    def list_report_revisions(self, *, actor_id: str, organization_id: str,
                              period_key: str) -> dict[str, Any]:
        """列出一个统计期间的报告版本链与更正记录。"""

        self._reader(actor_id, organization_id)
        reports = [
            {"report_id": row["report_id"], "version_no": row["version_no"],
             "status": row["status"], "corrections_applied": row["corrections_applied"],
             "published_by": row["published_by"], "published_at": row["published_at"],
             "content": json.loads(row["content_json"])}
            for row in self.database.connection.execute(
                "SELECT * FROM period_reports WHERE organization_id=? AND period_key=? "
                "ORDER BY version_no", (organization_id, period_key))
        ]
        corrections = [
            {"correction_id": row["correction_id"], "event_key": row["event_key"],
             "status": row["status"], "report_id": row["report_id"],
             "created_at": row["created_at"]}
            for row in self.database.connection.execute(
                "SELECT * FROM report_corrections WHERE organization_id=? AND period_key=? "
                "ORDER BY created_at, correction_id", (organization_id, period_key))
        ]
        return {"period_key": period_key, "reports": reports, "corrections": corrections}
