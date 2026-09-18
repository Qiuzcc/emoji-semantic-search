"""检索参数约束测试：结果条数上限 10（需求硬约束）。"""
from __future__ import annotations

import unittest

from app import config


class TestTopKClamp(unittest.TestCase):
    def test_never_exceeds_max_results(self):
        self.assertEqual(config.clamp_top_k(999), config.MAX_RESULTS)
        self.assertEqual(config.clamp_top_k(config.MAX_RESULTS), config.MAX_RESULTS)

    def test_lower_bound(self):
        self.assertEqual(config.clamp_top_k(0), 1)
        self.assertEqual(config.clamp_top_k(-5), 1)

    def test_invalid_input_falls_back_to_default(self):
        self.assertEqual(config.clamp_top_k(None), config.DEFAULT_TOP_K)
        self.assertEqual(config.clamp_top_k("abc"), config.DEFAULT_TOP_K)

    def test_default_is_within_limit(self):
        self.assertLessEqual(config.DEFAULT_TOP_K, config.MAX_RESULTS)
        self.assertEqual(config.MAX_RESULTS, 10)

    def test_recall_depth_covers_top_k(self):
        self.assertGreaterEqual(config.recall_depth_for(1), config.MAX_RESULTS)
        self.assertGreaterEqual(config.recall_depth_for(10), 10)


if __name__ == "__main__":
    unittest.main()
