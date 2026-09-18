"""RRF 融合的数值正确性与稳定性测试（零依赖，可直接用 unittest 运行）。"""
from __future__ import annotations

import unittest

from app.fusion import rrf_fuse


class TestRRFFuse(unittest.TestCase):
    def test_scores_match_manual_computation(self):
        """手算校验：doc 同时被两路召回时，分数为两路倒数名次之和。"""
        fused = rrf_fuse({"dense": [10, 20, 30], "sparse": [20, 10, 40]}, k=60)
        scores = {doc_id: score for doc_id, score, _ in fused}

        self.assertAlmostEqual(scores[10], 1 / 61 + 1 / 62, places=12)
        self.assertAlmostEqual(scores[20], 1 / 62 + 1 / 61, places=12)
        self.assertAlmostEqual(scores[30], 1 / 63, places=12)
        self.assertAlmostEqual(scores[40], 1 / 63, places=12)

        # 同分时按 doc_id 升序，保证结果稳定
        self.assertEqual([doc_id for doc_id, _, _ in fused], [10, 20, 30, 40])

    def test_ranks_are_reported_per_path(self):
        fused = rrf_fuse({"dense": [7, 8], "sparse": [8]}, k=60)
        ranks = {doc_id: rank_map for doc_id, _, rank_map in fused}
        self.assertEqual(ranks[7], {"dense": 1})
        self.assertEqual(ranks[8], {"dense": 2, "sparse": 1})

    def test_top_n_truncates(self):
        rankings = {"dense": list(range(20)), "sparse": list(range(10, 30))}
        self.assertEqual(len(rrf_fuse(rankings, k=60, top_n=10)), 10)

    def test_weights_shift_ordering(self):
        rankings = {"dense": [1], "sparse": [2]}
        # 权重相等时两篇同分，按 doc_id 升序 -> [1, 2]
        self.assertEqual([d for d, _, _ in rrf_fuse(rankings, k=60)], [1, 2])
        # 稀疏权重更大时，sparse 第一位（doc 2）反超
        weighted = rrf_fuse(rankings, weights={"dense": 1.0, "sparse": 3.0}, k=60)
        self.assertEqual([d for d, _, _ in weighted], [2, 1])

    def test_zero_weight_path_is_ignored(self):
        fused = rrf_fuse(
            {"dense": [1, 2], "sparse": [3]}, weights={"sparse": 0.0}, k=60
        )
        self.assertEqual([doc_id for doc_id, _, _ in fused], [1, 2])

    def test_empty_input_returns_empty(self):
        self.assertEqual(rrf_fuse({}, k=60), [])
        self.assertEqual(rrf_fuse({"dense": [], "sparse": []}, k=60), [])

    def test_single_path_keeps_original_order(self):
        fused = rrf_fuse({"dense": [5, 6, 7]}, k=60)
        self.assertEqual([doc_id for doc_id, _, _ in fused], [5, 6, 7])

    def test_duplicate_ids_in_one_path_are_counted_once(self):
        fused = rrf_fuse({"dense": [1, 1, 2]}, k=60)
        scores = {doc_id: score for doc_id, score, _ in fused}
        # 同一路径内重复出现时只保留最优名次，避免重复累加得分
        self.assertAlmostEqual(scores[1], 1 / 61, places=12)
        self.assertEqual(len(fused), 2)

    def test_invalid_k_rejected(self):
        with self.assertRaises(ValueError):
            rrf_fuse({"dense": [1]}, k=0)


if __name__ == "__main__":
    unittest.main()
