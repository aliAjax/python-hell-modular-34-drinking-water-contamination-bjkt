import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.rules import assess
from src.domain import DomainError


class RuleTest(unittest.TestCase):
    def test_contamination_score_depends_on_ratio_and_population(self):
        critical = assess({"concentration": 50, "limit": 10, "population": 10000})
        low = assess({"concentration": 1, "limit": 10, "population": 100})
        self.assertEqual(critical["level"], "critical")
        self.assertEqual(low["level"], "low")
        self.assertGreater(critical["score"], low["score"])

    def test_restore_rejects_failed_sample(self):
        item = {
            "status": "sampled",
            "payload": {"limit": 10, "sample_results": [{"concentration": 12}]},
        }
        from src.rules import apply_action
        with self.assertRaises(DomainError) as context:
            apply_action(item, "restore", {"all_zones_cleared": True}, "c", "coordinator")
        self.assertEqual(context.exception.code, "quality_not_met")


if __name__ == "__main__":
    unittest.main()
