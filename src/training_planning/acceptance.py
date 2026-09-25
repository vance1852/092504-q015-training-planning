"""运行规划服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError

from .service import PlanningService
from .storage import PlanningDatabase


def _expect(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(f"验收失败: {message}")


def run() -> dict[str, Any]:
    """执行需求到名额再到就业反馈更正的完整链路并返回结果。"""

    with tempfile.TemporaryDirectory() as directory:
        database = PlanningDatabase(Path(directory) / "planning-acceptance.sqlite3")
        service = PlanningService(database, FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc)))
        org = "org-train"
        service.register_organization(request_id="acc-org", actor_id="bootstrap",
                                      organization_id=org, name="新职业培训示范机构")
        for request_id, actor_id, name, role in [
                ("acc-a-admin", "admin-1", "系统管理员", "admin"),
                ("acc-a-planner", "planner-1", "规划员", "planner"),
                ("acc-a-director", "director-1", "负责人", "director"),
                ("acc-a-approver", "approver-1", "审批人", "approver"),
                ("acc-a-registrar", "registrar-1", "教务登记员", "registrar"),
                ("acc-a-auditor", "auditor-1", "审计员", "auditor")]:
            service.register_actor(request_id=request_id, actor_id="bootstrap" if role == "admin" else "admin-1",
                                   new_actor_id=actor_id, display_name=name, role=role,
                                   organization_id=org)

        # 教学资源:课程先修、教师工时、设备容量。
        service.register_equipment(request_id="acc-eq-1", actor_id="planner-1", organization_id=org,
                                   equipment_id="eq-server", name="算力服务器", units=12)
        service.register_equipment(request_id="acc-eq-2", actor_id="planner-1", organization_id=org,
                                   equipment_id="eq-lab", name="标注工位", units=2)
        service.register_course(request_id="acc-c-1", actor_id="planner-1", organization_id=org,
                                course_id="c-basic", name="人工智能基础", occupation="人工智能训练师",
                                duration_hours=40, class_size=20,
                                equipment_needs=[{"equipment_id": "eq-server", "units_per_class": 1}])
        service.register_course(request_id="acc-c-2", actor_id="planner-1", organization_id=org,
                                course_id="c-adv", name="人工智能进阶", occupation="人工智能训练师",
                                duration_hours=60, class_size=15, prerequisites=["c-basic"],
                                equipment_needs=[{"equipment_id": "eq-server", "units_per_class": 2}])
        service.register_course(request_id="acc-c-3", actor_id="planner-1", organization_id=org,
                                course_id="c-label", name="数据标注实务", occupation="数据标注员",
                                duration_hours=50, class_size=25,
                                equipment_needs=[{"equipment_id": "eq-lab", "units_per_class": 1}])
        service.register_teacher(request_id="acc-t-1", actor_id="planner-1", organization_id=org,
                                 teacher_id="t-1", name="主讲教师甲", available_hours=400,
                                 course_ids=["c-basic", "c-adv"])
        service.register_teacher(request_id="acc-t-2", actor_id="planner-1", organization_id=org,
                                 teacher_id="t-2", name="主讲教师乙", available_hours=100,
                                 course_ids=["c-basic"])
        service.register_teacher(request_id="acc-t-3", actor_id="planner-1", organization_id=org,
                                 teacher_id="t-3", name="标注讲师", available_hours=60,
                                 course_ids=["c-label"])

        # 统计期间与历史结业反馈:10 人结业、6 人就业,就业率 0.6 低于目标 0.8。
        service.record_stat_period(request_id="acc-p-2", actor_id="director-1", organization_id=org,
                                   period_key="2026Q2", start_on="2026-04-01", end_on="2026-06-30")
        service.record_stat_period(request_id="acc-p-3", actor_id="director-1", organization_id=org,
                                   period_key="2026Q3", start_on="2026-07-01", end_on="2026-09-30")
        for index in range(10):
            service.record_student_event(
                request_id=f"acc-g-{index}", actor_id="registrar-1", organization_id=org,
                event_key=f"grad-{index}", event_type="graduated",
                student_id=f"stu-g{index:02d}", course_id="c-basic", occurred_on="2026-06-20")
        for index in range(6):
            service.record_student_event(
                request_id=f"acc-e-{index}", actor_id="registrar-1", organization_id=org,
                event_key=f"emp-{index}", event_type="employed",
                student_id=f"stu-g{index:02d}", course_id="c-basic", occurred_on="2026-06-25")

        # 岗位需求:已验证、过期、仅意向三种形态。
        demands = [
            ("d-1", "云启科技", "华东", "人工智能训练师", 50, "2026-10-01", "2027-03-31", True),
            ("d-2", "数联智造", "华北", "人工智能训练师", 30, "2026-10-01", "2026-12-31", True),
            ("d-3", "晨星数据", "华东", "数据标注员", 60, "2026-10-01", "2026-12-31", True),
            ("d-4", "远航物流", "华南", "人工智能训练师", 40, "2026-01-01", "2026-06-30", True),
            ("d-5", "蓝海电商", "华东", "数据标注员", 20, "2026-10-01", "2026-12-31", False),
        ]
        for demand_key, enterprise, region, occupation, headcount, start, end, verify in demands:
            service.submit_demand(request_id=f"acc-{demand_key}", actor_id="planner-1",
                                  organization_id=org, demand_key=demand_key,
                                  enterprise_name=enterprise, region=region,
                                  occupation=occupation, headcount=headcount,
                                  window_start=start, window_end=end)
            if verify:
                service.verify_demand(request_id=f"acc-{demand_key}-v", actor_id="director-1",
                                      organization_id=org, demand_key=demand_key, version_no=1)

        # 生成三套方案并核对关键缺口解释。
        generation = service.generate_plans(request_id="acc-gen", actor_id="planner-1",
                                            organization_id=org, period_key="2026Q4",
                                            period_start="2026-10-01", period_end="2026-12-31")
        plans = service.list_plans(actor_id="admin-1", organization_id=org, period_key="2026Q4")
        _expect(len(plans) == 3, "应生成三套策略方案")
        by_strategy = {plan["strategy"]: plan for plan in plans}
        detail = service.get_plan(actor_id="admin-1", plan_id=by_strategy["demand_first"]["plan_id"])
        gap_types = {gap["gap_type"] for gap in detail["gaps"]}
        _expect("demand_expired" in gap_types, "过期需求应进入缺口解释")
        _expect("low_historical_employment" in gap_types, "低就业率应进入缺口解释")
        _expect("teacher_hours_limited" in gap_types, "教师工时不足应进入缺口解释")
        quotas_by_course = {a["course_id"]: a["quota"] for a in detail["allocations"]}
        _expect(quotas_by_course["c-basic"] == 80, "基础班名额应为 4 班 80 人")
        _expect(quotas_by_course["c-adv"] == 60, "进阶班名额应为 4 班 60 人")
        _expect(quotas_by_course["c-label"] == 25, "标注班名额应受教师工时限制为 25 人")

        # 负责人冻结、审批人审批形成名额;期间已有生效方案后不能再冻结。
        service.freeze_plan(request_id="acc-freeze", actor_id="director-1",
                            plan_id=by_strategy["demand_first"]["plan_id"])
        approved = service.approve_plan(request_id="acc-approve", actor_id="approver-1",
                                        plan_id=by_strategy["demand_first"]["plan_id"])
        quotas = service.list_quotas(actor_id="admin-1", organization_id=org, period_key="2026Q4")
        _expect(len(quotas) == 3, "审批通过应形成三个课程名额")
        try:
            service.freeze_plan(request_id="acc-freeze-2", actor_id="director-1",
                                plan_id=by_strategy["balanced"]["plan_id"])
            raise RuntimeError("验收失败: 生效方案存在时不应再冻结其他方案")
        except ConflictError:
            pass

        # 生效之后的需求变化只触发影响提示。
        service.submit_demand(request_id="acc-d-1-v2", actor_id="planner-1", organization_id=org,
                              demand_key="d-1", enterprise_name="云启科技", region="华东",
                              occupation="人工智能训练师", headcount=90,
                              window_start="2026-10-01", window_end="2027-03-31")
        service.withdraw_demand(request_id="acc-d-2-w", actor_id="director-1", organization_id=org,
                                demand_key="d-2", version_no=1, reason="企业缩编")
        notices = service.list_impact_notices(actor_id="admin-1", organization_id=org,
                                              plan_id=by_strategy["demand_first"]["plan_id"])
        _expect(len(notices) == 2, "应产生两条影响提示")
        _expect({n["impact"] for n in notices} == {"snapshot_input_changed", "snapshot_demand_withdrawn"},
                "影响提示应区分快照变化与快照需求撤回")

        # 当期学员事件与迟到反馈更正流程。
        service.record_student_event(request_id="acc-s-1", actor_id="registrar-1", organization_id=org,
                                     event_key="enroll-001", event_type="enrolled",
                                     student_id="stu-101", course_id="c-basic",
                                     occurred_on="2026-08-01")
        service.record_student_event(request_id="acc-s-2", actor_id="registrar-1", organization_id=org,
                                     event_key="enroll-002", event_type="enrolled",
                                     student_id="stu-102", course_id="c-basic",
                                     occurred_on="2026-08-02")
        service.record_student_event(request_id="acc-s-3", actor_id="registrar-1", organization_id=org,
                                     event_key="grad-101", event_type="graduated",
                                     student_id="stu-101", course_id="c-basic",
                                     occurred_on="2026-09-20")
        service.publish_report(request_id="acc-r-1", actor_id="director-1",
                               organization_id=org, period_key="2026Q3")
        late = service.record_student_event(
            request_id="acc-s-4", actor_id="registrar-1", organization_id=org,
            event_key="emp-101", event_type="employed", student_id="stu-101",
            course_id="c-basic", occurred_on="2026-09-21",
            payload={"employer": "云启科技"})
        _expect(not late.replayed, "迟到反馈应正常登记")
        replay = service.record_student_event(
            request_id="acc-s-4", actor_id="registrar-1", organization_id=org,
            event_key="emp-101", event_type="employed", student_id="stu-101",
            course_id="c-basic", occurred_on="2026-09-21",
            payload={"employer": "云启科技"})
        _expect(replay.replayed, "相同请求应返回原回执")
        merged = service.record_student_event(
            request_id="acc-s-4b", actor_id="registrar-1", organization_id=org,
            event_key="emp-101", event_type="employed", student_id="stu-101",
            course_id="c-basic", occurred_on="2026-09-21",
            payload={"employer": "云启科技"})
        _expect(not merged.replayed, "业务编号归并不算请求重放")
        revisions_before = service.list_report_revisions(actor_id="auditor-1", organization_id=org,
                                                         period_key="2026Q3")
        _expect(revisions_before["reports"][0]["content"]["courses"]["c-basic"]["employed"] == 0,
                "已发布报告不应被迟到反馈改写")
        _expect(len(revisions_before["corrections"]) == 1
                and revisions_before["corrections"][0]["status"] == "pending",
                "迟到反馈应形成挂起更正")
        service.publish_report(request_id="acc-r-2", actor_id="director-1",
                               organization_id=org, period_key="2026Q3")
        revisions = service.list_report_revisions(actor_id="auditor-1", organization_id=org,
                                                  period_key="2026Q3")
        _expect(len(revisions["reports"]) == 2, "应形成两个报告版本")
        _expect(revisions["reports"][0]["status"] == "superseded"
                and revisions["reports"][0]["content"]["courses"]["c-basic"]["employed"] == 0,
                "首版报告内容应保持不变")
        _expect(revisions["reports"][1]["content"]["courses"]["c-basic"]["employed"] == 1
                and revisions["reports"][1]["corrections_applied"] == 1,
                "新版报告应应用更正")
        _expect(revisions["corrections"][0]["status"] == "applied", "更正应标记为已应用")

        # 名额追溯:需求来源与容量取舍。
        label_quota = next(q for q in quotas if q["course_id"] == "c-label")
        trace = service.trace_quota(actor_id="auditor-1", quota_id=label_quota["quota_id"])
        _expect(any(t["gap_type"] == "teacher_hours_limited" for t in trace["capacity_tradeoffs"]),
                "标注班名额应能追溯教师工时取舍")
        basic_quota = next(q for q in quotas if q["course_id"] == "c-basic")
        trace = service.trace_quota(actor_id="auditor-1", quota_id=basic_quota["quota_id"])
        source_keys = {(s["demand_key"], s["version_no"]) for s in trace["demand_sources"]
                       if not s["indirect"]}
        _expect(source_keys == {("d-1", 1), ("d-2", 1)}, "基础班名额应追溯到两个已验证需求版本")

        valid, event_count = service.verify_audit()
        result = {"status": "ok", "plans": len(plans), "quotas": len(quotas),
                  "impact_notices": len(notices),
                  "report_versions": len(revisions["reports"]),
                  "corrections_applied": revisions["reports"][1]["corrections_applied"],
                  "audit_events": event_count, "audit_valid": valid,
                  "approved_replayed": approved.replayed}
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
