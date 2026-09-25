import unittest
from datetime import datetime, timezone

from skills_workspace.clock import FixedClock
from skills_workspace.errors import ConflictError, PermissionDenied, ValidationError
from skills_workspace.outcomes import OutcomeService
from skills_workspace.storage import Database

OCCUPATION = "云计算工程技术人员"
REGION = "华南"
PERIOD = "2026Q4"


class OutcomeServiceTest(unittest.TestCase):
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
                ("reviewer", "v1", "reviewer"), ("auditor", "au1", "auditor")):
            self.service.register_actor(request_id=request_id, actor_id="a1", new_actor_id=actor_id,
                                        display_name=actor_id, role=role, organization_id="o1")
        self.service.register_course(request_id="c1", actor_id="a1", course_id="C1",
                                     occupation=OCCUPATION, name="课程一", duration_hours=10,
                                     equipment_type="设备A", max_class_size=10)
        self.service.register_course(request_id="c2", actor_id="a1", course_id="C2",
                                     occupation=OCCUPATION, name="课程二", duration_hours=10,
                                     equipment_type="设备B", max_class_size=10)
        self.service.submit_demand(request_id="d1", actor_id="e1", demand_id="d1",
                                   enterprise_id="ent-a", occupation=OCCUPATION, region=REGION,
                                   window_start="2026-10-01", window_end="2026-12-31",
                                   headcount=2, valid_until="2026-12-31")
        self.service.verify_demand(request_id="d1-v", actor_id="v1", demand_id="d1")
        self.service.set_teacher_capacity(request_id="t1", actor_id="a1", teacher_id="t1",
                                          course_id="C1", period=PERIOD, available_hours=100)
        self.service.set_teacher_capacity(request_id="t2", actor_id="a1", teacher_id="t2",
                                          course_id="C2", period=PERIOD, available_hours=100)
        self.service.set_equipment_capacity(request_id="eq1", actor_id="a1",
                                            equipment_type="设备A", period=PERIOD, seats=50)
        self.service.set_equipment_capacity(request_id="eq2", actor_id="a1",
                                            equipment_type="设备B", period=PERIOD, seats=50)
        batch = self.service.generate_plans(request_id="g1", actor_id="p1",
                                            occupation=OCCUPATION, region=REGION, period=PERIOD)
        plan_id = next(plan["plan_id"] for plan in batch.response["plans"]
                       if plan["strategy"] == "aggressive")
        self.service.select_plan(request_id="s1", actor_id="p1", plan_id=plan_id)
        approved = self.service.approve_plan(request_id="ap1", actor_id="ap1", plan_id=plan_id)
        self.plan_id = plan_id
        self.quotas = {item["course_id"]: item["quota_id"]
                       for item in approved.response["quotas"]}

    def tearDown(self):
        self.database.close()

    def _event(self, request_id, event_id, student_id, event_type, payload,
               period=PERIOD, actor_id="r1"):
        return self.service.record_student_event(
            request_id=request_id, actor_id=actor_id, event_id=event_id,
            student_id=student_id, event_type=event_type, period=period, payload=payload)

    def _enroll(self, student_id, course="C1", suffix=""):
        return self._event(f"en{student_id}{suffix}", f"ev-en-{student_id}", student_id,
                           "enroll", {"quota_id": self.quotas[course]})

    def _graduate(self, student_id, course="C1"):
        return self._event(f"gr{student_id}", f"ev-gr-{student_id}", student_id,
                           "graduate", {"quota_id": self.quotas[course]})

    def _employed(self, student_id, employed=True):
        return self._event(f"em{student_id}", f"ev-em-{student_id}", student_id,
                           "employment", {"employed": employed})

    def _quota(self, course):
        return next(item for item in self.service.list_quotas(plan_id=self.plan_id)
                    if item["course_id"] == course)

    # ------------------------------------------------------------------
    # 幂等归并
    # ------------------------------------------------------------------

    def test_enroll_is_idempotent_by_business_number(self):
        first = self._enroll("st1")
        self.assertFalse(first.response["merged"])
        merged = self._enroll("st1", suffix="-retry")
        self.assertTrue(merged.response["merged"])
        replay = self._enroll("st1")
        self.assertTrue(replay.replayed)
        self.assertEqual(1, self._quota("C1")["enrolled"])
        with self.assertRaises(ConflictError):
            self._event("en-x", "ev-en-st1", "st1", "enroll",
                        {"quota_id": self.quotas["C2"]})

    def test_enroll_checks_capacity_period_and_active_enrollment(self):
        self._enroll("st1")
        self._enroll("st2")
        with self.assertRaises(ConflictError):
            self._enroll("st3")
        with self.assertRaises(ValidationError):
            self._event("en-bad", "ev-en-bad", "st9", "enroll",
                        {"quota_id": self.quotas["C1"]}, period="2027Q1")
        with self.assertRaises(ConflictError):
            self._event("en-dup", "ev-en-dup", "st1", "enroll",
                        {"quota_id": self.quotas["C2"]})

    def test_transfer_moves_seat_between_quotas(self):
        self._enroll("st1")
        self._event("tr1", "ev-tr-1", "st1", "transfer",
                    {"from_quota_id": self.quotas["C1"], "to_quota_id": self.quotas["C2"]})
        self.assertEqual(0, self._quota("C1")["enrolled"])
        self.assertEqual(1, self._quota("C2")["enrolled"])
        with self.assertRaises(ValidationError):
            self._event("tr2", "ev-tr-2", "st2", "transfer",
                        {"from_quota_id": self.quotas["C1"], "to_quota_id": self.quotas["C2"]})
        with self.assertRaises(ValidationError):
            self._event("tr3", "ev-tr-3", "st1", "transfer",
                        {"from_quota_id": self.quotas["C2"], "to_quota_id": self.quotas["C2"]})

    def test_graduate_frees_seat_for_backfill(self):
        self._enroll("st1")
        self._enroll("st2")
        with self.assertRaises(ConflictError):
            self._enroll("st3")
        self._graduate("st1")
        self.assertEqual(1, self._quota("C1")["enrolled"])
        self._enroll("st3")
        with self.assertRaises(ValidationError):
            self._event("gr1b", "ev-gr-1b", "st1", "graduate",
                        {"quota_id": self.quotas["C1"]})

    def test_employment_requires_graduation(self):
        with self.assertRaises(ValidationError):
            self._employed("st9")
        self._enroll("st1")
        self._graduate("st1")
        receipt = self._employed("st1")
        self.assertFalse(receipt.response["correction"])

    def test_registrar_role_is_required_for_events(self):
        with self.assertRaises(PermissionDenied):
            self._event("en-au", "ev-en-au", "st1", "enroll",
                        {"quota_id": self.quotas["C1"]}, actor_id="au1")

    # ------------------------------------------------------------------
    # 统计报告与迟到更正
    # ------------------------------------------------------------------

    def test_late_feedback_enters_correction_flow_without_rewriting_report(self):
        self._enroll("st1")
        self._enroll("st2")
        self._graduate("st1")
        self._employed("st1")
        first = self.service.publish_report(request_id="rep1", actor_id="v1", period=PERIOD)
        self.assertEqual(1, first.response["version"])
        # 报告发布后到达的结业与就业反馈进入更正流程
        late_graduate = self._graduate("st2")
        late_employment = self._employed("st2")
        self.assertTrue(late_graduate.response["correction"])
        self.assertTrue(late_employment.response["correction"])
        pending = self.service.list_corrections(PERIOD, status="pending")
        self.assertEqual(2, len(pending))
        reports = self.service.list_reports(PERIOD)
        self.assertEqual(1, len(reports))
        self.assertEqual(1, reports[0]["summary"]["totals"]["graduated"])
        second = self.service.publish_report(request_id="rep2", actor_id="v1", period=PERIOD)
        self.assertEqual(2, second.response["version"])
        self.assertEqual(2, second.response["corrections_applied"])
        reports = self.service.list_reports(PERIOD)
        self.assertEqual(["superseded", "published"], [item["status"] for item in reports])
        self.assertEqual(1, reports[0]["summary"]["totals"]["graduated"])
        self.assertEqual(2, reports[1]["summary"]["totals"]["graduated"])
        self.assertEqual(2, reports[1]["summary"]["totals"]["employed"])
        self.assertEqual([], self.service.list_corrections(PERIOD, status="pending"))
        self.assertEqual(2, len(self.service.list_corrections(PERIOD, status="applied")))

    def test_republish_without_pending_corrections_conflicts(self):
        self.service.publish_report(request_id="rep1", actor_id="v1", period=PERIOD)
        with self.assertRaises(ConflictError):
            self.service.publish_report(request_id="rep2", actor_id="v1", period=PERIOD)

    def test_report_summary_counts_events_per_course(self):
        self._enroll("st1")
        self._event("tr1", "ev-tr-1", "st1", "transfer",
                    {"from_quota_id": self.quotas["C1"], "to_quota_id": self.quotas["C2"]})
        self._graduate("st1", course="C2")
        self._employed("st1")
        self.service.publish_report(request_id="rep1", actor_id="v1", period=PERIOD)
        report = self.service.list_reports(PERIOD)[0]["summary"]
        courses = {item["course_id"]: item for item in report["courses"]}
        self.assertEqual(1, courses["C1"]["enrolled"])
        self.assertEqual(1, courses["C2"]["transferred_in"])
        self.assertEqual(1, courses["C2"]["graduated"])
        self.assertEqual(1.0, courses["C2"]["employment_rate"])

    def test_trace_quota_includes_report_revisions(self):
        self._enroll("st1")
        self._graduate("st1")
        self.service.publish_report(request_id="rep1", actor_id="v1", period=PERIOD)
        self._employed("st1")
        self.service.publish_report(request_id="rep2", actor_id="v1", period=PERIOD)
        trace = self.service.trace_quota(self.quotas["C1"])
        self.assertEqual([1, 2], [item["version"] for item in trace["report_revisions"]])
        self.assertEqual({"applied": 1}, trace["correction_counts"])


if __name__ == "__main__":
    unittest.main()
