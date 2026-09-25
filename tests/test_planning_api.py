import unittest

from skills_workspace.api import route
from skills_workspace.outcomes import OutcomeService
from skills_workspace.storage import Database


class PlanningApiTest(unittest.TestCase):
    def setUp(self):
        self.database = Database()
        self.service = OutcomeService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "培训机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "admin", "new_actor_id": "a1", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"}, {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "planner", "new_actor_id": "p1", "display_name": "规划",
               "role": "planner", "organization_id": "o1"}, {"X-Actor-Id": "a1"})
        route(self.service, "POST", "/actors",
              {"request_id": "enterprise", "new_actor_id": "e1", "display_name": "企业",
               "role": "enterprise", "organization_id": "o1"}, {"X-Actor-Id": "a1"})

    def tearDown(self):
        self.database.close()

    def test_demand_submit_and_list_roundtrip(self):
        status, payload = route(self.service, "POST", "/demands",
                                {"request_id": "d1", "demand_id": "d1", "enterprise_id": "ent-a",
                                 "occupation": "人工智能训练师", "region": "华东",
                                 "window_start": "2026-10-01", "window_end": "2026-12-31",
                                 "headcount": 30, "valid_until": "2026-12-31"},
                                {"X-Actor-Id": "e1"})
        self.assertEqual(201, status)
        self.assertEqual("job_demand", payload["resource_type"])
        self.assertEqual("d1#1", payload["resource_id"])
        status, payload = route(self.service, "POST", "/demands",
                                {"request_id": "d1", "demand_id": "d1", "enterprise_id": "ent-a",
                                 "occupation": "人工智能训练师", "region": "华东",
                                 "window_start": "2026-10-01", "window_end": "2026-12-31",
                                 "headcount": 30, "valid_until": "2026-12-31"},
                                {"X-Actor-Id": "e1"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])
        status, payload = route(self.service, "GET", "/demands?region=华东", None)
        self.assertEqual(200, status)
        self.assertEqual(1, len(payload["items"]))
        self.assertEqual("intent", payload["items"][0]["status"])

    def test_domain_error_maps_to_status(self):
        status, payload = route(self.service, "POST", "/demands/verify",
                                {"request_id": "v1", "demand_id": "missing"},
                                {"X-Actor-Id": "a1"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])
        status, payload = route(self.service, "POST", "/plans/generate",
                                {"request_id": "g1", "occupation": "人工智能训练师",
                                 "region": "华东", "period": "2026Q4"},
                                {"X-Actor-Id": "e1"})
        self.assertEqual(403, status)
        self.assertEqual("permission_denied", payload["error"])

    def test_invalid_period_returns_400(self):
        status, payload = route(self.service, "POST", "/plans/generate",
                                {"request_id": "g1", "occupation": "人工智能训练师",
                                 "region": "华东", "period": "2026-Q4"},
                                {"X-Actor-Id": "p1"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_trace_quota_requires_identifier(self):
        status, payload = route(self.service, "GET", "/trace/quota", None)
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])


if __name__ == "__main__":
    unittest.main()
