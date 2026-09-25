import unittest

from training_planning.acceptance import run


class PlanningAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertEqual(3, result["plans"])
        self.assertEqual(3, result["quotas"])
        self.assertEqual(2, result["impact_notices"])
        self.assertEqual(2, result["report_versions"])
        self.assertEqual(1, result["corrections_applied"])


if __name__ == "__main__":
    unittest.main()
