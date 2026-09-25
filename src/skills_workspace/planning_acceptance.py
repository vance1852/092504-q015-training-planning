"""运行新职业培训名额与就业反馈规划服务的离线端到端验收。"""

from __future__ import annotations

import json
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from .clock import FixedClock
from .errors import ConflictError
from .outcomes import OutcomeService
from .storage import Database

OCCUPATION = "人工智能训练师"
REGION = "华东"


def _seed_context(service: OutcomeService) -> None:
    """登记组织、角色与上一期的结业就业历史。"""

    service.register_organization(request_id="req-org", actor_id="bootstrap",
                                  organization_id="org-001", name="新职业培训示范机构")
    service.register_actor(request_id="req-admin", actor_id="bootstrap", new_actor_id="admin-001",
                           display_name="系统管理员", role="admin", organization_id="org-001")
    for request_id, actor_id, name, role in (
            ("req-planner", "planner-001", "规划负责人", "planner"),
            ("req-approver", "approver-001", "审批人", "approver"),
            ("req-enterprise", "ent-001", "企业对接人", "enterprise"),
            ("req-registrar", "registrar-001", "学籍登记员", "registrar"),
            ("req-reviewer", "reviewer-001", "审核员", "reviewer")):
        service.register_actor(request_id=request_id, actor_id="admin-001", new_actor_id=actor_id,
                               display_name=name, role=role, organization_id="org-001")
    service.register_course(request_id="req-course-basic", actor_id="admin-001",
                            course_id="AI-BASIC", occupation=OCCUPATION, name="人工智能基础",
                            duration_hours=40, equipment_type="实训工位", max_class_size=20)
    service.register_course(request_id="req-course-adv", actor_id="admin-001",
                            course_id="AI-ADV", occupation=OCCUPATION, name="人工智能进阶",
                            duration_hours=60, equipment_type="进阶实训台", max_class_size=15,
                            prerequisites=["AI-BASIC"])
    # 上一期（2026Q3）的需求、方案与结业就业历史
    service.submit_demand(request_id="req-d0", actor_id="ent-001", demand_id="d0",
                          enterprise_id="ent-a", occupation=OCCUPATION, region=REGION,
                          window_start="2026-07-01", window_end="2026-09-30",
                          headcount=5, valid_until="2026-09-30")
    service.verify_demand(request_id="req-d0-v", actor_id="reviewer-001", demand_id="d0")
    service.set_teacher_capacity(request_id="req-t-q3", actor_id="admin-001", teacher_id="t1",
                                 course_id="AI-BASIC", period="2026Q3", available_hours=80)
    service.set_equipment_capacity(request_id="req-e-q3", actor_id="admin-001",
                                   equipment_type="实训工位", period="2026Q3", seats=25)
    batch = service.generate_plans(request_id="req-gen-q3", actor_id="planner-001",
                                   occupation=OCCUPATION, region=REGION, period="2026Q3")
    plan_id = batch.response["plans"][0]["plan_id"]
    service.select_plan(request_id="req-sel-q3", actor_id="planner-001", plan_id=plan_id)
    approved = service.approve_plan(request_id="req-app-q3", actor_id="approver-001",
                                    plan_id=plan_id)
    quota_id = next(item["quota_id"] for item in approved.response["quotas"]
                    if item["course_id"] == "AI-BASIC")
    for index, employed in ((1, True), (2, False)):
        student_id = f"st{index}"
        service.record_student_event(request_id=f"req-en-{index}", actor_id="registrar-001",
                                     event_id=f"ev-en-{index}", student_id=student_id,
                                     event_type="enroll", period="2026Q3",
                                     payload={"quota_id": quota_id})
        service.record_student_event(request_id=f"req-gr-{index}", actor_id="registrar-001",
                                     event_id=f"ev-gr-{index}", student_id=student_id,
                                     event_type="graduate", period="2026Q3",
                                     payload={"quota_id": quota_id})
        service.record_student_event(request_id=f"req-em-{index}", actor_id="registrar-001",
                                     event_id=f"ev-em-{index}", student_id=student_id,
                                     event_type="employment", period="2026Q3",
                                     payload={"employed": employed})


def run() -> dict[str, object]:
    """执行需求版本、方案审批、学员事件与报告更正的完整链路。"""

    with tempfile.TemporaryDirectory() as directory:
        database = Database(Path(directory) / "planning_acceptance.sqlite3")
        service = OutcomeService(database, FixedClock(datetime(2026, 9, 25, 8, 0,
                                                             tzinfo=timezone.utc)))
        _seed_context(service)

        # 本期（2026Q4）岗位需求：已验证、意向、撤回与过期四种形态
        service.submit_demand(request_id="req-d1", actor_id="ent-001", demand_id="d1",
                              enterprise_id="ent-a", occupation=OCCUPATION, region=REGION,
                              window_start="2026-10-01", window_end="2026-12-31",
                              headcount=30, valid_until="2026-12-31")
        service.verify_demand(request_id="req-d1-v", actor_id="reviewer-001", demand_id="d1")
        service.submit_demand(request_id="req-d2", actor_id="ent-001", demand_id="d2",
                              enterprise_id="ent-b", occupation=OCCUPATION, region=REGION,
                              window_start="2026-10-01", window_end="2026-12-31",
                              headcount=10, valid_until="2026-12-31")
        service.submit_demand(request_id="req-d3", actor_id="ent-001", demand_id="d3",
                              enterprise_id="ent-c", occupation=OCCUPATION, region=REGION,
                              window_start="2026-10-01", window_end="2026-12-31",
                              headcount=4, valid_until="2026-12-31")
        service.withdraw_demand(request_id="req-d3-w", actor_id="ent-001", demand_id="d3")
        service.submit_demand(request_id="req-d4", actor_id="ent-001", demand_id="d4",
                              enterprise_id="ent-d", occupation=OCCUPATION, region=REGION,
                              window_start="2026-10-01", window_end="2026-12-31",
                              headcount=8, valid_until="2026-09-30")

        service.set_teacher_capacity(request_id="req-t1-q4", actor_id="admin-001", teacher_id="t1",
                                     course_id="AI-BASIC", period="2026Q4", available_hours=80)
        service.set_teacher_capacity(request_id="req-t2-q4", actor_id="admin-001", teacher_id="t2",
                                     course_id="AI-ADV", period="2026Q4", available_hours=60)
        service.set_equipment_capacity(request_id="req-e-q4", actor_id="admin-001",
                                       equipment_type="实训工位", period="2026Q4", seats=25)
        service.set_equipment_capacity(request_id="req-e2-q4", actor_id="admin-001",
                                       equipment_type="进阶实训台", period="2026Q4", seats=10)

        batch = service.generate_plans(request_id="req-gen-q4", actor_id="planner-001",
                                       occupation=OCCUPATION, region=REGION, period="2026Q4")
        plans = {plan["strategy"]: plan["plan_id"] for plan in batch.response["plans"]}
        balanced = service.get_plan(plans["balanced"])
        balanced_lines = {line["course_id"]: line for line in balanced["lines"]}

        # 选择稳健方案后冻结快照，之后的需求变化只产生影响提示
        service.select_plan(request_id="req-sel-q4", actor_id="planner-001",
                            plan_id=plans["balanced"])
        service.submit_demand(request_id="req-d1-b", actor_id="ent-001", demand_id="d1",
                              enterprise_id="ent-a", occupation=OCCUPATION, region=REGION,
                              window_start="2026-10-01", window_end="2026-12-31",
                              headcount=36, valid_until="2026-12-31")
        service.withdraw_demand(request_id="req-d2-w", actor_id="ent-001", demand_id="d2")
        notices = service.list_impact_notices(plans["balanced"])

        approved = service.approve_plan(request_id="req-app-q4", actor_id="approver-001",
                                        plan_id=plans["balanced"])
        quotas = {item["course_id"]: item for item in approved.response["quotas"]}
        service.select_plan(request_id="req-sel-q4-b", actor_id="planner-001",
                            plan_id=plans["aggressive"])
        second_approval_blocked = False
        try:
            service.approve_plan(request_id="req-app-q4-b", actor_id="approver-001",
                                 plan_id=plans["aggressive"])
        except ConflictError:
            second_approval_blocked = True

        # 学员事件按业务编号幂等归并
        basic_quota = quotas["AI-BASIC"]["quota_id"]
        adv_quota = quotas["AI-ADV"]["quota_id"]
        first = service.record_student_event(request_id="req-en3", actor_id="registrar-001",
                                             event_id="ev-en-3", student_id="st3",
                                             event_type="enroll", period="2026Q4",
                                             payload={"quota_id": basic_quota})
        service.record_student_event(request_id="req-en4", actor_id="registrar-001",
                                     event_id="ev-en-4", student_id="st4",
                                     event_type="enroll", period="2026Q4",
                                     payload={"quota_id": basic_quota})
        merged = service.record_student_event(request_id="req-en3-b", actor_id="registrar-001",
                                              event_id="ev-en-3", student_id="st3",
                                              event_type="enroll", period="2026Q4",
                                              payload={"quota_id": basic_quota})
        service.record_student_event(request_id="req-tr4", actor_id="registrar-001",
                                     event_id="ev-tr-4", student_id="st4",
                                     event_type="transfer", period="2026Q4",
                                     payload={"from_quota_id": basic_quota,
                                              "to_quota_id": adv_quota})
        service.record_student_event(request_id="req-gr4", actor_id="registrar-001",
                                     event_id="ev-gr-4", student_id="st4",
                                     event_type="graduate", period="2026Q4",
                                     payload={"quota_id": adv_quota})
        service.record_student_event(request_id="req-em4", actor_id="registrar-001",
                                     event_id="ev-em-4", student_id="st4",
                                     event_type="employment", period="2026Q4",
                                     payload={"employed": True})

        report_v1 = service.publish_report(request_id="req-rep1", actor_id="reviewer-001",
                                           period="2026Q4")
        # 迟到反馈进入更正流程，已发布报告不被改写
        service.record_student_event(request_id="req-gr3", actor_id="registrar-001",
                                     event_id="ev-gr-3", student_id="st3",
                                     event_type="graduate", period="2026Q4",
                                     payload={"quota_id": basic_quota})
        late = service.record_student_event(request_id="req-em3", actor_id="registrar-001",
                                            event_id="ev-em-3", student_id="st3",
                                            event_type="employment", period="2026Q4",
                                            payload={"employed": True})
        pending = service.list_corrections("2026Q4", status="pending")
        report_v2 = service.publish_report(request_id="req-rep2", actor_id="reviewer-001",
                                           period="2026Q4")
        reports = service.list_reports("2026Q4")
        trace = service.trace_quota(basic_quota)
        valid, event_count = service.verify_audit()
        result = {
            "status": "ok",
            "audit_valid": valid,
            "audit_events": event_count,
            "excluded_demands": len(batch.response["exclusions"]),
            "balanced_basic_quota": balanced_lines["AI-BASIC"]["quota"],
            "balanced_basic_limiting": balanced_lines["AI-BASIC"]["limiting_factor"],
            "balanced_adv_limiting": balanced_lines["AI-ADV"]["limiting_factor"],
            "impact_notices": len(notices),
            "second_approval_blocked": second_approval_blocked,
            "first_enroll_merged": first.response["merged"],
            "replayed_enroll_merged": merged.response["merged"],
            "late_feedback_correction": late.response["correction"],
            "pending_corrections": len(pending),
            "report_v1": report_v1.response["version"],
            "report_v2": report_v2.response["version"],
            "report_v2_corrections": report_v2.response["corrections_applied"],
            "report_versions": [item["version"] for item in reports],
            "trace_limiting_factor": trace["capacity_tradeoff"]["limiting_factor"],
            "trace_demand_sources": len(trace["demand_sources"]),
            "trace_report_revisions": len(trace["report_revisions"]),
        }
        database.close()
        return result


def main() -> int:
    """打印验收结果并设置退出码。"""

    result = run()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "ok" and result["audit_valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
