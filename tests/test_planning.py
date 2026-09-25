import threading
import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, NotFoundError, PermissionDenied, ValidationError
from skills_workspace.outcomes import OutcomeService
from skills_workspace.storage import Database

OCCUPATION = "人工智能训练师"
REGION = "华东"


class PlanningServiceTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = OutcomeService(self.database, FixedClock(datetime(2026, 9, 25,
                                                                       tzinfo=timezone.utc)))
        self.service.register_organization(request_id="org", actor_id="bootstrap",
                                           organization_id="o1", name="培训机构")
        self.service.register_actor(request_id="admin", actor_id="bootstrap", new_actor_id="a1",
                                    display_name="管理员", role="admin", organization_id="o1")
        for request_id, actor_id, role in (
                ("planner", "p1", "planner"), ("approver", "ap1", "approver"),
                ("enterprise", "e1", "enterprise"), ("registrar", "r1", "registrar"),
                ("reviewer", "v1", "reviewer")):
            self.service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                        display_name=actor_id, role=role, organization_id="o1")
        self.service.register_course(request_id="c1", actor_id="a1", course_id="AI-BASIC",
                                     occupation=OCCUPATION, name="基础", duration_hours=40,
                                     equipment_type="实训工位", max_class_size=20)
        self.service.register_course(request_id="c2", actor_id="a1", course_id="AI-ADV",
                                     occupation=OCCUPATION, name="进阶", duration_hours=60,
                                     equipment_type="进阶实训台", max_class_size=15,
                                     prerequisites=["AI-BASIC"])

    def tearDown(self):
        self.database.close()

    def _submit(self, request_id, demand_id, headcount, valid_until="2026-12-31",
                window=("2026-10-01", "2026-12-31")):
        return self.service.submit_demand(
            request_id=request_id, actor_id="e1", demand_id=demand_id, enterprise_id="ent-a",
            occupation=OCCUPATION, region=REGION, window_start=window[0], window_end=window[1],
            headcount=headcount, valid_until=valid_until)

    def _seed_history(self):
        """上一期培养两名结业学员，其中一人就业，为基础课提供 0.5 的历史就业率。"""
        self._submit("d0", "d0", 5, valid_until="2026-09-30",
                     window=("2026-07-01", "2026-09-30"))
        self.service.verify_demand(request_id="d0-v", actor_id="v1", demand_id="d0")
        self.service.set_teacher_capacity(request_id="t-q3", actor_id="a1", teacher_id="t1",
                                          course_id="AI-BASIC", period="2026Q3", available_hours=80)
        self.service.set_equipment_capacity(request_id="e-q3", actor_id="a1",
                                            equipment_type="实训工位", period="2026Q3", seats=25)
        batch = self.service.generate_plans(request_id="g-q3", actor_id="p1",
                                            occupation=OCCUPATION, region=REGION, period="2026Q3")
        plan_id = batch.response["plans"][0]["plan_id"]
        self.service.select_plan(request_id="s-q3", actor_id="p1", plan_id=plan_id)
        approved = self.service.approve_plan(request_id="a-q3", actor_id="ap1", plan_id=plan_id)
        quota_id = next(item["quota_id"] for item in approved.response["quotas"]
                        if item["course_id"] == "AI-BASIC")
        for index, employed in ((1, True), (2, False)):
            self.service.record_student_event(request_id=f"en{index}", actor_id="r1",
                                              event_id=f"ev-en-{index}", student_id=f"st{index}",
                                              event_type="enroll", period="2026Q3",
                                              payload={"quota_id": quota_id})
            self.service.record_student_event(request_id=f"gr{index}", actor_id="r1",
                                              event_id=f"ev-gr-{index}", student_id=f"st{index}",
                                              event_type="graduate", period="2026Q3",
                                              payload={"quota_id": quota_id})
            self.service.record_student_event(request_id=f"em{index}", actor_id="r1",
                                              event_id=f"ev-em-{index}", student_id=f"st{index}",
                                              event_type="employment", period="2026Q3",
                                              payload={"employed": employed})

    def _prepare_q4(self):
        self._submit("d1", "d1", 30)
        self.service.verify_demand(request_id="d1-v", actor_id="v1", demand_id="d1")
        self._submit("d2", "d2", 10)
        self.service.set_teacher_capacity(request_id="t1-q4", actor_id="a1", teacher_id="t1",
                                          course_id="AI-BASIC", period="2026Q4", available_hours=80)
        self.service.set_teacher_capacity(request_id="t2-q4", actor_id="a1", teacher_id="t2",
                                          course_id="AI-ADV", period="2026Q4", available_hours=60)
        self.service.set_equipment_capacity(request_id="e1-q4", actor_id="a1",
                                            equipment_type="实训工位", period="2026Q4", seats=25)
        self.service.set_equipment_capacity(request_id="e2-q4", actor_id="a1",
                                            equipment_type="进阶实训台", period="2026Q4", seats=10)

    def _generate(self, request_id="g-q4"):
        return self.service.generate_plans(request_id=request_id, actor_id="p1",
                                           occupation=OCCUPATION, region=REGION, period="2026Q4")

    def _lines(self, plan_id):
        return {line["course_id"]: line for line in self.service.get_plan(plan_id)["lines"]}

    # ------------------------------------------------------------------
    # 需求版本
    # ------------------------------------------------------------------

    def test_demand_versions_track_status_transitions(self):
        first = self._submit("d1", "d1", 30)
        self.assertEqual({"demand_id": "d1", "version": 1, "status": "intent"}, first.response)
        verified = self.service.verify_demand(request_id="d1-v", actor_id="v1", demand_id="d1")
        self.assertEqual(2, verified.response["version"])
        withdrawn = self.service.withdraw_demand(request_id="d1-w", actor_id="e1", demand_id="d1")
        self.assertEqual("withdrawn", withdrawn.response["status"])
        latest = self.service.list_demands(occupation=OCCUPATION)
        self.assertEqual(1, len(latest))
        self.assertEqual("withdrawn", latest[0]["status"])
        history = self.service.list_demands(occupation=OCCUPATION, include_history=True)
        self.assertEqual([1, 2, 3], [item["version"] for item in history])

    def test_verify_rejects_illegal_transition(self):
        self._submit("d1", "d1", 30)
        self.service.withdraw_demand(request_id="d1-w", actor_id="e1", demand_id="d1")
        with self.assertRaises(ConflictError):
            self.service.verify_demand(request_id="d1-v", actor_id="v1", demand_id="d1")

    def test_submit_demand_validates_window_and_headcount(self):
        with self.assertRaises(ValidationError):
            self._submit("d1", "d1", 30, window=("2026-12-31", "2026-10-01"))
        with self.assertRaises(ValidationError):
            self._submit("d2", "d2", 0)
        with self.assertRaises(ValidationError):
            self._submit("d3", "d3", 5, valid_until="2026-13-01")

    def test_same_request_replays_demand_receipt(self):
        first = self._submit("d1", "d1", 30)
        replay = self._submit("d1", "d1", 30)
        self.assertFalse(first.replayed)
        self.assertTrue(replay.replayed)
        self.assertEqual(first.resource_id, replay.resource_id)
        with self.assertRaises(ConflictError):
            self._submit("d1", "d1", 31)

    # ------------------------------------------------------------------
    # 方案生成
    # ------------------------------------------------------------------

    def test_generate_plans_applies_capacity_constraints(self):
        self._seed_history()
        self._prepare_q4()
        batch = self._generate()
        plans = {plan["strategy"]: plan["plan_id"] for plan in batch.response["plans"]}
        conservative = self._lines(plans["conservative"])
        self.assertEqual(15, conservative["AI-BASIC"]["quota"])
        self.assertEqual("demand", conservative["AI-BASIC"]["limiting_factor"])
        balanced = self._lines(plans["balanced"])
        self.assertEqual(25, balanced["AI-BASIC"]["quota"])
        self.assertEqual("equipment", balanced["AI-BASIC"]["limiting_factor"])
        self.assertIn("缺口 1 人", balanced["AI-BASIC"]["gap_explanation"])
        aggressive = self._lines(plans["aggressive"])
        self.assertEqual(25, aggressive["AI-BASIC"]["quota"])
        self.assertIn("缺口 15 人", aggressive["AI-BASIC"]["gap_explanation"])
        for plan_id in plans.values():
            self.assertEqual(2, self._lines(plan_id)["AI-ADV"]["quota"])
            self.assertEqual("prerequisite", self._lines(plan_id)["AI-ADV"]["limiting_factor"])

    def test_generate_plans_excludes_withdrawn_expired_and_misaligned_demands(self):
        self._prepare_q4()
        self._submit("d3", "d3", 4)
        self.service.withdraw_demand(request_id="d3-w", actor_id="e1", demand_id="d3")
        self._submit("d4", "d4", 8, valid_until="2026-09-30")
        self._submit("d5", "d5", 6, window=("2027-01-01", "2027-03-31"))
        batch = self._generate()
        reasons = {item["demand_id"]: item["reason"] for item in batch.response["exclusions"]}
        self.assertEqual({"d3", "d4", "d5"}, set(reasons))
        snapshot_demands = self.service.get_plan(batch.response["plans"][0]["plan_id"])
        self.assertEqual({"d1", "d2"},
                         {item["demand_id"] for item in snapshot_demands["snapshot"]["demands"]})

    def test_generate_plans_requires_courses_and_planner_role(self):
        with self.assertRaises(ValidationError):
            self.service.generate_plans(request_id="g-x", actor_id="p1", occupation="冷门职业",
                                        region=REGION, period="2026Q4")
        with self.assertRaises(PermissionDenied):
            self.service.generate_plans(request_id="g-y", actor_id="e1", occupation=OCCUPATION,
                                        region=REGION, period="2026Q4")

    def test_generate_plans_replay_returns_same_batch(self):
        self._prepare_q4()
        first = self._generate()
        replay = self._generate()
        self.assertTrue(replay.replayed)
        self.assertEqual(first.response["batch_id"], replay.response["batch_id"])

    def test_course_registration_validates_prerequisites(self):
        with self.assertRaises(ConflictError):
            self.service.register_course(request_id="c3", actor_id="a1", course_id="AI-BASIC",
                                         occupation=OCCUPATION, name="重复", duration_hours=10,
                                         equipment_type="实训工位", max_class_size=10)
        with self.assertRaises(NotFoundError):
            self.service.register_course(request_id="c4", actor_id="a1", course_id="AI-X",
                                         occupation=OCCUPATION, name="X", duration_hours=10,
                                         equipment_type="实训工位", max_class_size=10,
                                         prerequisites=["AI-MISSING"])
        with self.assertRaises(ValidationError):
            self.service.register_course(request_id="c5", actor_id="a1", course_id="AI-Y",
                                         occupation=OCCUPATION, name="Y", duration_hours=10,
                                         equipment_type="实训工位", max_class_size=10,
                                         prerequisites=["AI-Y"])

    # ------------------------------------------------------------------
    # 冻结、审批与影响提示
    # ------------------------------------------------------------------

    def _selected_balanced_plan(self):
        self._seed_history()
        self._prepare_q4()
        batch = self._generate()
        plans = {plan["strategy"]: plan["plan_id"] for plan in batch.response["plans"]}
        self.service.select_plan(request_id="s-q4", actor_id="p1", plan_id=plans["balanced"])
        return plans

    def test_frozen_plan_only_receives_impact_notices_on_demand_change(self):
        plans = self._selected_balanced_plan()
        before = self.service.get_plan(plans["balanced"])
        self._submit("d1-b", "d1", 36)
        self.service.withdraw_demand(request_id="d2-w", actor_id="e1", demand_id="d2")
        self._submit("d9", "d9", 7)
        notices = self.service.list_impact_notices(plans["balanced"])
        kinds = {notice["demand_id"]: notice["change_type"] for notice in notices}
        self.assertEqual({"d1": "updated", "d2": "withdrawn", "d9": "added"}, kinds)
        after = self.service.get_plan(plans["balanced"])
        self.assertEqual(before["snapshot_hash"], after["snapshot_hash"])
        self.assertEqual(before["lines"], after["lines"])
        draft_notices = self.service.list_impact_notices(plans["aggressive"])
        self.assertEqual([], draft_notices)

    def test_approve_creates_quotas_and_blocks_second_effective_plan(self):
        plans = self._selected_balanced_plan()
        approved = self.service.approve_plan(request_id="a-q4", actor_id="ap1",
                                             plan_id=plans["balanced"])
        quotas = {item["course_id"]: item for item in approved.response["quotas"]}
        self.assertEqual(25, quotas["AI-BASIC"]["headcount"])
        self.assertEqual(2, quotas["AI-ADV"]["headcount"])
        self.service.select_plan(request_id="s-q4-b", actor_id="p1", plan_id=plans["aggressive"])
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="a-q4-b", actor_id="ap1",
                                      plan_id=plans["aggressive"])
        effective = [plan for plan in self.service.list_plans(occupation=OCCUPATION, region=REGION,
                                                              period="2026Q4")
                     if plan["status"] == "approved"]
        self.assertEqual(1, len(effective))

    def test_approval_requires_separation_and_selected_status(self):
        self._prepare_q4()
        batch = self._generate()
        plan_id = batch.response["plans"][0]["plan_id"]
        with self.assertRaises(ConflictError):
            self.service.approve_plan(request_id="a-early", actor_id="ap1", plan_id=plan_id)
        self.service.select_plan(request_id="s-1", actor_id="p1", plan_id=plan_id)
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="a-self", actor_id="p1", plan_id=plan_id)
        with self.assertRaises(PermissionDenied):
            self.service.approve_plan(request_id="a-role", actor_id="e1", plan_id=plan_id)

    def test_concurrent_approval_produces_single_effective_plan(self):
        plans = self._selected_balanced_plan()
        self.service.select_plan(request_id="s-q4-c", actor_id="p1", plan_id=plans["aggressive"])
        barrier = threading.Barrier(2)
        outcomes = []

        def approve(plan_id, request_id):
            barrier.wait()
            try:
                self.service.approve_plan(request_id=request_id, actor_id="ap1", plan_id=plan_id)
                outcomes.append("approved")
            except ConflictError:
                outcomes.append("conflict")

        threads = [threading.Thread(target=approve, args=(plans["balanced"], "a-t1")),
                   threading.Thread(target=approve, args=(plans["aggressive"], "a-t2"))]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(sorted(outcomes), ["approved", "conflict"])
        effective = [plan for plan in self.service.list_plans(period="2026Q4")
                     if plan["status"] == "approved"]
        self.assertEqual(1, len(effective))

    # ------------------------------------------------------------------
    # 追溯
    # ------------------------------------------------------------------

    def test_trace_quota_reports_demand_sources_and_capacity_tradeoff(self):
        plans = self._selected_balanced_plan()
        approved = self.service.approve_plan(request_id="a-q4", actor_id="ap1",
                                             plan_id=plans["balanced"])
        quota_id = next(item["quota_id"] for item in approved.response["quotas"]
                        if item["course_id"] == "AI-BASIC")
        self._submit("d1-b", "d1", 36)
        trace = self.service.trace_quota(quota_id)
        self.assertEqual("equipment", trace["capacity_tradeoff"]["limiting_factor"])
        self.assertEqual({"d1", "d2"}, {item["demand_id"] for item in trace["demand_sources"]})
        self.assertEqual(80, trace["capacity_inputs"]["teacher_hours"])
        self.assertEqual(0.5, trace["capacity_inputs"]["history"]["employment_rate"])
        self.assertEqual(1, len(trace["impact_notices"]))
        self.assertEqual(trace["plan"]["snapshot_hash"],
                         self.service.get_plan(plans["balanced"])["snapshot_hash"])


if __name__ == "__main__":
    unittest.main()
