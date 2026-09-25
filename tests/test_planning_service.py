import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError

from training_planning.service import PlanningService
from training_planning.storage import PlanningDatabase


CLOCK = FixedClock(datetime(2026, 9, 25, 8, 0, tzinfo=timezone.utc))
ORG = "o1"


class PlanningServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = PlanningDatabase()
        self.service = PlanningService(self.database, CLOCK)
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id=ORG, name="培训机构一")
        for request_id, actor_id, role in [
                ("a-admin", "a1", "admin"), ("a-planner", "p1", "planner"),
                ("a-director", "d1", "director"), ("a-approver", "ap1", "approver"),
                ("a-registrar", "r1", "registrar"), ("a-auditor", "au1", "auditor")]:
            self.service.register_actor(request_id=request_id, actor_id="bootstrap" if role == "admin" else "a1",
                                        new_actor_id=actor_id, display_name=actor_id,
                                        role=role, organization_id=ORG)
        self.service.register_organization(request_id="org2", actor_id="a1",
                                           organization_id="o2", name="培训机构二")
        self.service.register_actor(request_id="a-planner2", actor_id="a1", new_actor_id="p2",
                                    display_name="p2", role="planner", organization_id="o2")

    def tearDown(self):
        self.database.close()

    # ---------- 场景构造辅助 ----------

    def _submit(self, request_id, demand_key, headcount, start="2026-10-01", end="2026-12-31",
                occupation="人工智能训练师", enterprise="企业甲", region="华东", verify=True):
        self.service.submit_demand(request_id=request_id, actor_id="p1", organization_id=ORG,
                                   demand_key=demand_key, enterprise_name=enterprise,
                                   region=region, occupation=occupation, headcount=headcount,
                                   window_start=start, window_end=end)
        if verify:
            self.service.verify_demand(request_id=f"{request_id}-v", actor_id="d1",
                                       organization_id=ORG, demand_key=demand_key, version_no=1)

    def _simple_course(self, request_id="c-x", course_id="c-x", occupation="人工智能训练师",
                       hours=10, size=10, **kwargs):
        self.service.register_course(request_id=request_id, actor_id="p1", organization_id=ORG,
                                     course_id=course_id, name=course_id, occupation=occupation,
                                     duration_hours=hours, class_size=size, **kwargs)

    def _simple_generation(self, request_id="gen", period_key="2026Q4"):
        self.service.generate_plans(request_id=request_id, actor_id="p1", organization_id=ORG,
                                    period_key=period_key, period_start="2026-10-01",
                                    period_end="2026-12-31")
        plans = self.service.list_plans(actor_id="a1", organization_id=ORG, period_key=period_key)
        latest: dict[str, dict] = {}
        for plan in plans:  # 按代次 DESC 返回,首次出现即最新一代
            latest.setdefault(plan["strategy"], plan)
        return latest

    # ---------- 岗位需求版本 ----------

    def test_demand_versions_and_status_flow(self):
        first = self.service.submit_demand(
            request_id="d1", actor_id="p1", organization_id=ORG, demand_key="dk-1",
            enterprise_name="企业甲", region="华东", occupation="人工智能训练师",
            headcount=30, window_start="2026-10-01", window_end="2026-12-31")
        merged = self.service.submit_demand(
            request_id="d1-again", actor_id="p1", organization_id=ORG, demand_key="dk-1",
            enterprise_name="企业甲", region="华东", occupation="人工智能训练师",
            headcount=30, window_start="2026-10-01", window_end="2026-12-31")
        self.assertEqual(first.resource_id, merged.resource_id)
        changed = self.service.submit_demand(
            request_id="d1-v2", actor_id="p1", organization_id=ORG, demand_key="dk-1",
            enterprise_name="企业甲", region="华东", occupation="人工智能训练师",
            headcount=45, window_start="2026-10-01", window_end="2026-12-31")
        self.assertNotEqual(first.resource_id, changed.resource_id)
        demands = self.service.list_demands(actor_id="au1", organization_id=ORG)
        self.assertEqual(2, len(demands[0]["versions"]))
        self.service.verify_demand(request_id="v2-ok", actor_id="d1", organization_id=ORG,
                                   demand_key="dk-1", version_no=2)
        with self.assertRaises(ConflictError):
            self.service.verify_demand(request_id="v2-again", actor_id="d1", organization_id=ORG,
                                       demand_key="dk-1", version_no=2)
        self.service.withdraw_demand(request_id="w2", actor_id="d1", organization_id=ORG,
                                     demand_key="dk-1", version_no=2, reason="企业取消")
        with self.assertRaises(ConflictError):
            self.service.withdraw_demand(request_id="w2-again", actor_id="d1", organization_id=ORG,
                                         demand_key="dk-1", version_no=2, reason="重复")
        with self.assertRaises(ConflictError):
            self.service.verify_demand(request_id="v2-late", actor_id="d1", organization_id=ORG,
                                       demand_key="dk-1", version_no=2)
        versions = self.service.list_demands(actor_id="au1", organization_id=ORG)[0]["versions"]
        self.assertEqual(["intent", "withdrawn"], [v["status"] for v in versions])

    def test_demand_series_rejects_different_attributes(self):
        self._submit("d1", "dk-1", 30, verify=False)
        with self.assertRaises(ConflictError):
            self.service.submit_demand(request_id="d1-bad", actor_id="p1", organization_id=ORG,
                                       demand_key="dk-1", enterprise_name="企业甲", region="华北",
                                       occupation="人工智能训练师", headcount=30,
                                       window_start="2026-10-01", window_end="2026-12-31")

    def test_demand_validation_and_permissions(self):
        with self.assertRaises(ValidationError):
            self.service.submit_demand(request_id="bad-window", actor_id="p1", organization_id=ORG,
                                       demand_key="dk-9", enterprise_name="企业甲", region="华东",
                                       occupation="人工智能训练师", headcount=10,
                                       window_start="2026-12-31", window_end="2026-10-01")
        with self.assertRaises(PermissionDenied):
            self.service.submit_demand(request_id="bad-role", actor_id="r1", organization_id=ORG,
                                       demand_key="dk-9", enterprise_name="企业甲", region="华东",
                                       occupation="人工智能训练师", headcount=10,
                                       window_start="2026-10-01", window_end="2026-12-31")
        with self.assertRaises(PermissionDenied):
            self.service.submit_demand(request_id="bad-org", actor_id="p2", organization_id=ORG,
                                       demand_key="dk-9", enterprise_name="企业甲", region="华东",
                                       occupation="人工智能训练师", headcount=10,
                                       window_start="2026-10-01", window_end="2026-12-31")
        self.service.submit_demand(request_id="idem", actor_id="p1", organization_id=ORG,
                                   demand_key="dk-8", enterprise_name="企业甲", region="华东",
                                   occupation="人工智能训练师", headcount=10,
                                   window_start="2026-10-01", window_end="2026-12-31")
        with self.assertRaises(ConflictError):
            self.service.submit_demand(request_id="idem", actor_id="p1", organization_id=ORG,
                                       demand_key="dk-8", enterprise_name="企业甲", region="华东",
                                       occupation="人工智能训练师", headcount=11,
                                       window_start="2026-10-01", window_end="2026-12-31")

    # ---------- 课程与资源登记 ----------

    def test_course_prerequisite_rules(self):
        with self.assertRaises(NotFoundError):
            self._simple_course(course_id="c-a", request_id="c-a", prerequisites=["missing"])
        self._simple_course()
        with self.assertRaises(ValidationError):
            self._simple_course(course_id="c-b", request_id="c-b", prerequisites=["c-b"])
        with self.assertRaises(ConflictError):
            self._simple_course(request_id="c-x-again")
        with self.assertRaises(NotFoundError):
            self.service.register_teacher(request_id="t-bad", actor_id="p1", organization_id=ORG,
                                          teacher_id="t-bad", name="教师", available_hours=10,
                                          course_ids=["missing"])

    # ---------- 方案生成约束 ----------

    def _build_constraint_scenario(self):
        self.service.register_equipment(request_id="eq1", actor_id="p1", organization_id=ORG,
                                        equipment_id="eq-1", name="服务器", units=2)
        self._simple_course(request_id="c-a", course_id="c-a", occupation="职业甲",
                            equipment_needs=[{"equipment_id": "eq-1", "units_per_class": 1}])
        self._simple_course(request_id="c-b", course_id="c-b", occupation="职业乙",
                            equipment_needs=[{"equipment_id": "eq-1", "units_per_class": 1}])
        self._simple_course(request_id="c-c", course_id="c-c", occupation="职业丙", hours=50, size=25)
        self.service.register_teacher(request_id="t-1", actor_id="p1", organization_id=ORG,
                                      teacher_id="t-1", name="教师一", available_hours=100,
                                      course_ids=["c-a", "c-b"])
        self.service.register_teacher(request_id="t-2", actor_id="p1", organization_id=ORG,
                                      teacher_id="t-2", name="教师二", available_hours=60,
                                      course_ids=["c-c"])
        self._submit("d-a", "dk-a", 100, occupation="职业甲")
        self._submit("d-b", "dk-b", 50, occupation="职业乙")
        self._submit("d-c", "dk-c", 60, occupation="职业丙")
        self._submit("d-expired", "dk-expired", 30, start="2026-01-01", end="2026-06-30",
                     occupation="职业甲")
        self._submit("d-future", "dk-future", 20, start="2027-01-01", end="2027-03-31",
                     occupation="职业乙")
        self._submit("d-intent", "dk-intent", 20, occupation="职业乙", verify=False)
        self.service.record_stat_period(request_id="sp-q2", actor_id="d1", organization_id=ORG,
                                        period_key="2026Q2", start_on="2026-04-01",
                                        end_on="2026-06-30")
        for index in range(10):
            self.service.record_student_event(
                request_id=f"g-{index}", actor_id="r1", organization_id=ORG,
                event_key=f"grad-{index}", event_type="graduated",
                student_id=f"stu-{index}", course_id="c-a", occurred_on="2026-06-20")
        for index in range(5):
            self.service.record_student_event(
                request_id=f"e-{index}", actor_id="r1", organization_id=ORG,
                event_key=f"emp-{index}", event_type="employed",
                student_id=f"stu-{index}", course_id="c-a", occurred_on="2026-06-25")

    def test_generation_respects_equipment_teacher_and_feedback(self):
        self._build_constraint_scenario()
        plans = self._simple_generation()
        self.assertEqual({"demand_first", "employment_first", "balanced"}, set(plans))
        detail = self.service.get_plan(actor_id="a1", plan_id=plans["demand_first"]["plan_id"])
        quotas = {a["course_id"]: a["quota"] for a in detail["allocations"]}
        # 职业甲就业率 0.5,调整后需求 63;设备只有 2 台,c-a 与 c-b 合计最多 2 个班。
        self.assertEqual(20, quotas["c-a"])
        self.assertEqual(0, quotas["c-b"])
        # 职业丙教师 60 工时只能支撑 1 个班。
        self.assertEqual(25, quotas["c-c"])
        gap_types = {(gap["gap_type"], gap["course_id"]) for gap in detail["gaps"]}
        self.assertIn(("equipment_limited", "c-a"), gap_types)
        self.assertIn(("equipment_limited", "c-b"), gap_types)
        self.assertIn(("teacher_hours_limited", "c-c"), gap_types)
        self.assertIn(("low_historical_employment", "c-a"), gap_types)
        self.assertIn(("demand_expired", None), gap_types)
        self.assertIn(("demand_not_yet_effective", None), gap_types)
        adjusted = {a["course_id"]: a["adjusted_demand"] for a in detail["allocations"]}
        self.assertEqual(63, adjusted["c-a"])
        self.assertEqual(50, adjusted["c-b"])
        # 仅意向需求不进入方案输入。
        snapshot_keys = {d["demand_key"] for d in detail["snapshot"]["demands"]}
        self.assertNotIn("dk-intent", snapshot_keys)
        # 均衡策略与需求优先策略在资源竞争下结果不同。
        balanced = self.service.get_plan(actor_id="a1", plan_id=plans["balanced"]["plan_id"])
        balanced_quotas = {a["course_id"]: a["quota"] for a in balanced["allocations"]}
        self.assertEqual(10, balanced_quotas["c-a"])
        self.assertEqual(10, balanced_quotas["c-b"])

    def test_prerequisite_limits_advanced_course(self):
        self._simple_course(request_id="c-p", course_id="c-p", occupation="基础职业")
        self._simple_course(request_id="c-q", course_id="c-q", occupation="高级职业",
                            prerequisites=["c-p"])
        self.service.register_teacher(request_id="t-p", actor_id="p1", organization_id=ORG,
                                      teacher_id="t-p", name="基础教师", available_hours=10,
                                      course_ids=["c-p"])
        self.service.register_teacher(request_id="t-q", actor_id="p1", organization_id=ORG,
                                      teacher_id="t-q", name="高级教师", available_hours=100,
                                      course_ids=["c-q"])
        self._submit("d-q", "dk-q", 30, occupation="高级职业")
        plans = self._simple_generation()
        detail = self.service.get_plan(actor_id="a1", plan_id=plans["demand_first"]["plan_id"])
        quotas = {a["course_id"]: a["quota"] for a in detail["allocations"]}
        self.assertEqual(10, quotas["c-p"])
        self.assertEqual(10, quotas["c-q"])
        gap_types = {(gap["gap_type"], gap["course_id"]) for gap in detail["gaps"]}
        self.assertIn(("prerequisite_limited", "c-q"), gap_types)
        basic = next(a for a in detail["allocations"] if a["course_id"] == "c-p")
        self.assertEqual(0, basic["raw_demand"])
        self.assertTrue(any(link["indirect"] for link in basic["demand_sources"]))

    def test_regeneration_stales_previous_drafts(self):
        self._simple_course()
        self._submit("d-1", "dk-1", 25)
        first = self._simple_generation()
        second = self._simple_generation(request_id="gen-2")
        self.assertEqual(2, second["demand_first"]["generation_no"])
        stale = self.service.get_plan(actor_id="a1", plan_id=first["demand_first"]["plan_id"])
        self.assertEqual("stale", stale["status"])
        with self.assertRaises(ConflictError):
            self.service.freeze_plan(request_id="fr-stale", actor_id="d1",
                                     plan_id=first["demand_first"]["plan_id"])

    # ---------- 冻结、审批与名额 ----------

    def _approved_simple_plan(self):
        self._simple_course()
        self.service.register_teacher(request_id="t-x", actor_id="p1", organization_id=ORG,
                                      teacher_id="t-x", name="教师", available_hours=100,
                                      course_ids=["c-x"])
        self._submit("d-1", "dk-1", 25)
        plans = self._simple_generation()
        plan_id = plans["demand_first"]["plan_id"]
        self.service.freeze_plan(request_id="fr-1", actor_id="d1", plan_id=plan_id)
        return plan_id

    def test_freeze_and_approve_creates_quotas(self):
        plan_id = self._approved_simple_plan()
        frozen = self.service.get_plan(actor_id="a1", plan_id=plan_id)
        self.assertEqual("frozen", frozen["status"])
        self.assertEqual("d1", frozen["frozen_by"])
        receipt = self.service.approve_plan(request_id="ap-1", actor_id="ap1", plan_id=plan_id)
        self.assertFalse(receipt.replayed)
        quotas = self.service.list_quotas(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        self.assertEqual(1, len(quotas))
        self.assertEqual(30, quotas[0]["quota"])
        replay = self.service.approve_plan(request_id="ap-1", actor_id="ap1", plan_id=plan_id)
        self.assertTrue(replay.replayed)
        self.assertEqual(1, len(self.service.list_quotas(actor_id="a1", organization_id=ORG)))
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="ap-2", actor_id="ap1", plan_id=plan_id)

    def test_only_one_effective_plan_per_period(self):
        plan_id = self._approved_simple_plan()
        plans = self.service.list_plans(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        other = next(p for p in plans if p["plan_id"] != plan_id)
        with self.assertRaises(ConflictError):
            self.service.freeze_plan(request_id="fr-other", actor_id="d1",
                                     plan_id=other["plan_id"])
        self.service.approve_plan(request_id="ap-1", actor_id="ap1", plan_id=plan_id)
        with self.assertRaises(ConflictError):
            self.service.freeze_plan(request_id="fr-other-2", actor_id="d1",
                                     plan_id=other["plan_id"])

    def test_reject_frees_freeze_slot(self):
        plan_id = self._approved_simple_plan()
        plans = self.service.list_plans(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        other = next(p for p in plans if p["plan_id"] != plan_id and p["status"] == "draft")
        with self.assertRaises(ConflictError):
            self.service.freeze_plan(request_id="fr-blocked", actor_id="d1",
                                     plan_id=other["plan_id"])
        self.service.reject_plan(request_id="rj-1", actor_id="ap1", plan_id=plan_id,
                                 reason="需求依据不足")
        self.service.freeze_plan(request_id="fr-other", actor_id="d1", plan_id=other["plan_id"])
        self.service.approve_plan(request_id="ap-other", actor_id="ap1", plan_id=other["plan_id"])
        quotas = self.service.list_quotas(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        self.assertEqual(1, len(quotas))
        approved = [p for p in self.service.list_plans(actor_id="a1", organization_id=ORG)
                    if p["status"] == "approved"]
        self.assertEqual(1, len(approved))

    def test_approval_permissions(self):
        plan_id = self._approved_simple_plan()
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="ap-bad", actor_id="p1", plan_id=plan_id)
        with self.assertRaises(PermissionDenied):
            self.service.reject_plan(request_id="rj-bad", actor_id="p1", plan_id=plan_id,
                                     reason="无权限")
        plans = self.service.list_plans(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        other = next(p for p in plans if p["plan_id"] != plan_id)
        with self.assertRaises(PermissionDenied):
            self.service.freeze_plan(request_id="fr-bad", actor_id="ap1",
                                     plan_id=other["plan_id"])
        with self.assertRaises(PermissionDenied):
            self.service.generate_plans(request_id="gen-bad", actor_id="d1", organization_id=ORG,
                                        period_key="2027Q1", period_start="2027-01-01",
                                        period_end="2027-03-31")

    def test_demand_changes_after_freeze_create_impact_notices(self):
        plan_id = self._approved_simple_plan()
        self.service.approve_plan(request_id="ap-1", actor_id="ap1", plan_id=plan_id)
        self.service.submit_demand(request_id="d-1-v2", actor_id="p1", organization_id=ORG,
                                   demand_key="dk-1", enterprise_name="企业甲", region="华东",
                                   occupation="人工智能训练师", headcount=60,
                                   window_start="2026-10-01", window_end="2026-12-31")
        notices = self.service.list_impact_notices(actor_id="a1", organization_id=ORG,
                                                   plan_id=plan_id)
        self.assertEqual(1, len(notices))
        self.assertEqual("snapshot_input_changed", notices[0]["impact"])
        self.service.withdraw_demand(request_id="w-1", actor_id="d1", organization_id=ORG,
                                     demand_key="dk-1", version_no=1, reason="企业撤单")
        notices = self.service.list_impact_notices(actor_id="a1", organization_id=ORG,
                                                   plan_id=plan_id)
        self.assertEqual(2, len(notices))
        self.assertEqual("snapshot_demand_withdrawn", notices[1]["impact"])
        # 与方案无关的变化不产生提示:职业不匹配、期间不重叠、撤回未入快照的意向版本。
        self.service.submit_demand(request_id="d-other", actor_id="p1", organization_id=ORG,
                                   demand_key="dk-other", enterprise_name="企业乙", region="华北",
                                   occupation="其他职业", headcount=10,
                                   window_start="2026-10-01", window_end="2026-12-31")
        self.service.submit_demand(request_id="d-far", actor_id="p1", organization_id=ORG,
                                   demand_key="dk-far", enterprise_name="企业丙", region="华东",
                                   occupation="人工智能训练师", headcount=10,
                                   window_start="2028-01-01", window_end="2028-03-31")
        self.service.withdraw_demand(request_id="w-other", actor_id="d1", organization_id=ORG,
                                     demand_key="dk-other", version_no=1, reason="意向取消")
        notices = self.service.list_impact_notices(actor_id="a1", organization_id=ORG,
                                                   plan_id=plan_id)
        self.assertEqual(2, len(notices))
        # 方案与名额本身不被改写。
        self.assertEqual("approved", self.service.get_plan(actor_id="a1", plan_id=plan_id)["status"])
        self.assertEqual(30, self.service.list_quotas(actor_id="a1", organization_id=ORG)[0]["quota"])

    def test_quota_trace_shows_sources_and_tradeoffs(self):
        self._build_constraint_scenario()
        plans = self._simple_generation()
        plan_id = plans["demand_first"]["plan_id"]
        self.service.freeze_plan(request_id="fr-1", actor_id="d1", plan_id=plan_id)
        self.service.approve_plan(request_id="ap-1", actor_id="ap1", plan_id=plan_id)
        quotas = self.service.list_quotas(actor_id="a1", organization_id=ORG, period_key="2026Q4")
        quota_a = next(q for q in quotas if q["course_id"] == "c-a")
        trace = self.service.trace_quota(actor_id="au1", quota_id=quota_a["quota_id"])
        self.assertEqual({("dk-a", 1)},
                         {(s["demand_key"], s["version_no"]) for s in trace["demand_sources"]})
        self.assertTrue(any(t["gap_type"] == "equipment_limited"
                            for t in trace["capacity_tradeoffs"]))
        self.assertTrue(any(t["gap_type"] == "low_historical_employment"
                            for t in trace["capacity_tradeoffs"]))
        self.assertEqual("ap1", trace["plan"]["decided_by"])
        self.assertEqual("d1", trace["plan"]["frozen_by"])
        self.assertTrue(trace["plan"]["input_hash"])
        quota_c = next(q for q in quotas if q["course_id"] == "c-c")
        trace_c = self.service.trace_quota(actor_id="au1", quota_id=quota_c["quota_id"])
        self.assertEqual(["teacher_hours_limited"],
                         [t["gap_type"] for t in trace_c["capacity_tradeoffs"]])
        with self.assertRaises(PermissionDenied):
            self.service.trace_quota(actor_id="p2", quota_id=quota_a["quota_id"])

    # ---------- 学员事件与报告更正 ----------

    def _prepare_period_and_course(self):
        self._simple_course()
        self._simple_course(request_id="c-y", course_id="c-y", occupation="其他职业")
        self.service.record_stat_period(request_id="sp-q3", actor_id="d1", organization_id=ORG,
                                        period_key="2026Q3", start_on="2026-07-01",
                                        end_on="2026-09-30")

    def test_student_events_merge_by_business_key(self):
        self._prepare_period_and_course()
        first = self.service.record_student_event(
            request_id="ev-1", actor_id="r1", organization_id=ORG, event_key="evt-001",
            event_type="enrolled", student_id="stu-1", course_id="c-x",
            occurred_on="2026-08-01")
        self.assertFalse(first.replayed)
        replay = self.service.record_student_event(
            request_id="ev-1", actor_id="r1", organization_id=ORG, event_key="evt-001",
            event_type="enrolled", student_id="stu-1", course_id="c-x",
            occurred_on="2026-08-01")
        self.assertTrue(replay.replayed)
        merged = self.service.record_student_event(
            request_id="ev-1b", actor_id="r1", organization_id=ORG, event_key="evt-001",
            event_type="enrolled", student_id="stu-1", course_id="c-x",
            occurred_on="2026-08-01")
        self.assertFalse(merged.replayed)
        events = self.service.list_student_events(actor_id="a1", organization_id=ORG)
        self.assertEqual(1, len(events))
        with self.assertRaises(ConflictError):
            self.service.record_student_event(
                request_id="ev-1c", actor_id="r1", organization_id=ORG, event_key="evt-001",
                event_type="graduated", student_id="stu-1", course_id="c-x",
                occurred_on="2026-08-01")
        with self.assertRaises(ValidationError):
            self.service.record_student_event(
                request_id="ev-2", actor_id="r1", organization_id=ORG, event_key="evt-002",
                event_type="enrolled", student_id="stu-1", course_id="c-x",
                occurred_on="2026-10-01")
        with self.assertRaises(ValidationError):
            self.service.record_student_event(
                request_id="ev-3", actor_id="r1", organization_id=ORG, event_key="evt-003",
                event_type="employed", student_id="stu-1", course_id="c-x",
                occurred_on="2026-08-01", payload="bad")

    def test_transfer_event_updates_both_courses(self):
        self._prepare_period_and_course()
        with self.assertRaises(ValidationError):
            self.service.record_student_event(
                request_id="ev-t0", actor_id="r1", organization_id=ORG, event_key="evt-t0",
                event_type="transferred", student_id="stu-1", course_id="c-x",
                occurred_on="2026-08-01")
        self.service.record_student_event(
            request_id="ev-t1", actor_id="r1", organization_id=ORG, event_key="evt-t1",
            event_type="transferred", student_id="stu-1", course_id="c-x",
            occurred_on="2026-08-01", payload={"to_course_id": "c-y"})
        self.service.publish_report(request_id="rp-1", actor_id="d1", organization_id=ORG,
                                    period_key="2026Q3")
        revisions = self.service.list_report_revisions(actor_id="au1", organization_id=ORG,
                                                       period_key="2026Q3")
        courses = revisions["reports"][0]["content"]["courses"]
        self.assertEqual(1, courses["c-x"]["transferred_out"])
        self.assertEqual(1, courses["c-y"]["transferred_in"])

    def test_late_feedback_enters_correction_flow(self):
        self._prepare_period_and_course()
        self.service.record_student_event(
            request_id="ev-1", actor_id="r1", organization_id=ORG, event_key="evt-001",
            event_type="enrolled", student_id="stu-1", course_id="c-x", occurred_on="2026-08-01")
        self.service.record_student_event(
            request_id="ev-2", actor_id="r1", organization_id=ORG, event_key="evt-002",
            event_type="graduated", student_id="stu-1", course_id="c-x", occurred_on="2026-09-10")
        self.service.publish_report(request_id="rp-1", actor_id="d1", organization_id=ORG,
                                    period_key="2026Q3")
        late = self.service.record_student_event(
            request_id="ev-3", actor_id="r1", organization_id=ORG, event_key="evt-003",
            event_type="employed", student_id="stu-1", course_id="c-x",
            occurred_on="2026-09-15", payload={"employer": "企业甲"})
        self.assertFalse(late.replayed)
        events = self.service.list_student_events(actor_id="a1", organization_id=ORG)
        self.assertTrue(next(e for e in events if e["event_key"] == "evt-003")["is_late"])
        revisions = self.service.list_report_revisions(actor_id="au1", organization_id=ORG,
                                                       period_key="2026Q3")
        self.assertEqual(1, len(revisions["reports"]))
        self.assertEqual(0, revisions["reports"][0]["content"]["courses"]["c-x"]["employed"])
        self.assertEqual("pending", revisions["corrections"][0]["status"])
        self.service.publish_report(request_id="rp-2", actor_id="d1", organization_id=ORG,
                                    period_key="2026Q3")
        revisions = self.service.list_report_revisions(actor_id="au1", organization_id=ORG,
                                                       period_key="2026Q3")
        first, second = revisions["reports"]
        self.assertEqual("superseded", first["status"])
        self.assertEqual(0, first["content"]["courses"]["c-x"]["employed"])
        self.assertEqual("published", second["status"])
        self.assertEqual(1, second["content"]["courses"]["c-x"]["employed"])
        self.assertEqual(1, second["corrections_applied"])
        self.assertEqual("applied", revisions["corrections"][0]["status"])
        self.assertEqual(second["report_id"], revisions["corrections"][0]["report_id"])

    def test_stat_periods_must_not_overlap(self):
        self.service.record_stat_period(request_id="sp-1", actor_id="d1", organization_id=ORG,
                                        period_key="2026Q3", start_on="2026-07-01",
                                        end_on="2026-09-30")
        with self.assertRaises(ConflictError):
            self.service.record_stat_period(request_id="sp-2", actor_id="d1", organization_id=ORG,
                                            period_key="2026H2", start_on="2026-09-01",
                                            end_on="2026-12-31")

    # ---------- 并发审批 ----------

    def test_concurrent_approvals_yield_single_effective_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "race.sqlite3"
            setup = PlanningDatabase(path)
            service = PlanningService(setup, CLOCK)
            service.register_organization(request_id="org", actor_id="bootstrap",
                                          organization_id=ORG, name="培训机构一")
            service.register_actor(request_id="a-admin", actor_id="bootstrap", new_actor_id="a1",
                                   display_name="a1", role="admin", organization_id=ORG)
            for request_id, actor_id, role in [("a-p", "p1", "planner"), ("a-d", "d1", "director"),
                                               ("a-a1", "ap1", "approver"), ("a-a2", "ap2", "approver")]:
                service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                       display_name=actor_id, role=role, organization_id=ORG)
            service.register_course(request_id="c-x", actor_id="p1", organization_id=ORG,
                                    course_id="c-x", name="c-x", occupation="人工智能训练师",
                                    duration_hours=10, class_size=10)
            service.register_teacher(request_id="t-x", actor_id="p1", organization_id=ORG,
                                     teacher_id="t-x", name="教师", available_hours=100,
                                     course_ids=["c-x"])
            service.submit_demand(request_id="d-1", actor_id="p1", organization_id=ORG,
                                  demand_key="dk-1", enterprise_name="企业甲", region="华东",
                                  occupation="人工智能训练师", headcount=25,
                                  window_start="2026-10-01", window_end="2026-12-31")
            service.verify_demand(request_id="d-1-v", actor_id="d1", organization_id=ORG,
                                  demand_key="dk-1", version_no=1)
            service.generate_plans(request_id="gen", actor_id="p1", organization_id=ORG,
                                   period_key="2026Q4", period_start="2026-10-01",
                                   period_end="2026-12-31")
            plans = service.list_plans(actor_id="a1", organization_id=ORG, period_key="2026Q4")
            plan_id = next(p for p in plans if p["strategy"] == "demand_first")["plan_id"]
            service.freeze_plan(request_id="fr-1", actor_id="d1", plan_id=plan_id)
            setup.close()

            results: list[str] = []
            lock = threading.Lock()

            def approve(actor_id, request_id):
                database = PlanningDatabase(path)
                try:
                    PlanningService(database, CLOCK).approve_plan(
                        request_id=request_id, actor_id=actor_id, plan_id=plan_id)
                    outcome = "approved"
                except ConflictError:
                    outcome = "conflict"
                finally:
                    database.close()
                with lock:
                    results.append(outcome)

            threads = [threading.Thread(target=approve, args=(f"ap{n}", f"ap-race-{i}"))
                       for i, n in enumerate((1, 2, 1, 2))]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
            self.assertEqual(1, results.count("approved"))
            self.assertEqual(3, results.count("conflict"))
            check = PlanningDatabase(path)
            service = PlanningService(check, CLOCK)
            approved = [p for p in service.list_plans(actor_id="a1", organization_id=ORG)
                        if p["status"] == "approved"]
            self.assertEqual(1, len(approved))
            self.assertEqual(1, len(service.list_quotas(actor_id="a1", organization_id=ORG)))
            check.close()


if __name__ == "__main__":
    unittest.main()
