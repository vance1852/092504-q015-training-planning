import unittest

from training_planning.api import route
from training_planning.service import PlanningService
from training_planning.storage import PlanningDatabase


class PlanningApiTest(unittest.TestCase):
    def setUp(self):
        self.database = PlanningDatabase()
        self.service = PlanningService(self.database)
        route(self.service, "POST", "/organizations",
              {"request_id": "org", "organization_id": "o1", "name": "培训机构"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "a1", "new_actor_id": "admin", "display_name": "管理员",
               "role": "admin", "organization_id": "o1"},
              {"X-Actor-Id": "bootstrap"})
        route(self.service, "POST", "/actors",
              {"request_id": "p1", "new_actor_id": "planner", "display_name": "规划员",
               "role": "planner", "organization_id": "o1"},
              {"X-Actor-Id": "admin"})

    def tearDown(self):
        self.database.close()

    def test_health_falls_back_to_base_route(self):
        status, payload = route(self.service, "GET", "/health", None)
        self.assertEqual(200, status)
        self.assertEqual("ok", payload["status"])

    def test_unknown_planning_route_returns_404(self):
        status, payload = route(self.service, "GET", "/planning/unknown", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(404, status)
        self.assertEqual("route_not_found", payload["error"])

    def test_write_requires_known_actor(self):
        status, payload = route(self.service, "POST", "/planning/equipment",
                                {"request_id": "eq", "organization_id": "o1",
                                 "equipment_id": "eq-1", "name": "服务器", "units": 2})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])

    def test_equipment_roundtrip_and_replay(self):
        body = {"request_id": "eq", "organization_id": "o1", "equipment_id": "eq-1",
                "name": "服务器", "units": 2}
        status, payload = route(self.service, "POST", "/planning/equipment", body,
                                {"X-Actor-Id": "planner"})
        self.assertEqual(201, status)
        self.assertFalse(payload["replayed"])
        status, payload = route(self.service, "POST", "/planning/equipment", body,
                                {"X-Actor-Id": "planner"})
        self.assertEqual(200, status)
        self.assertTrue(payload["replayed"])

    def test_demand_lifecycle_over_http(self):
        demand = {"request_id": "d1", "organization_id": "o1", "demand_key": "dk-1",
                  "enterprise_name": "企业甲", "region": "华东", "occupation": "人工智能训练师",
                  "headcount": 30, "window_start": "2026-10-01", "window_end": "2026-12-31"}
        status, _ = route(self.service, "POST", "/planning/demands", demand,
                          {"X-Actor-Id": "planner"})
        self.assertEqual(201, status)
        status, _ = route(self.service, "POST", "/planning/demands/dk-1/verify",
                          {"request_id": "d1-v", "organization_id": "o1", "version_no": 1},
                          {"X-Actor-Id": "admin"})
        self.assertEqual(201, status)
        status, payload = route(self.service, "GET", "/planning/demands?organization_id=o1",
                                None, {"X-Actor-Id": "admin"})
        self.assertEqual(200, status)
        self.assertEqual("verified", payload["items"][0]["versions"][0]["status"])

    def test_missing_query_param_returns_400(self):
        status, payload = route(self.service, "GET", "/planning/demands", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(400, status)
        self.assertEqual("validation_error", payload["error"])

    def test_trace_missing_quota_returns_404(self):
        status, payload = route(self.service, "GET", "/planning/quotas/missing/trace", None,
                                {"X-Actor-Id": "admin"})
        self.assertEqual(404, status)
        self.assertEqual("not_found", payload["error"])


if __name__ == "__main__":
    unittest.main()
