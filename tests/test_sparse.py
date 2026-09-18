"""BM25 稀疏召回测试：零分过滤与高频查询词剪枝（使用 ASCII 语料，不依赖具体分词实现）。"""
from __future__ import annotations

import unittest

from app import config
from app.sparse import SparseIndex


def build_index(corpus: list[list[str]]) -> SparseIndex:
    return SparseIndex(corpus)


class TestSparseIndex(unittest.TestCase):
    def test_search_returns_positive_scores_only(self):
        # "alpha" 出现在 2/6 文档（33%，低于剪枝阈值；且 IDF > 0 时才有正分）
        index = build_index(
            [
                ["alpha", "beta"],
                ["gamma", "delta"],
                ["epsilon", "zeta"],
                ["alpha", "eta"],
                ["theta", "iota"],
                ["kappa", "lambda"],
            ]
        )
        hits = index.search("alpha", top_k=10)
        self.assertTrue(hits)
        self.assertTrue(all(score > 0 for _, score in hits))
        self.assertEqual(sorted(doc_id for doc_id, _ in hits), [0, 3])

    def test_no_overlap_returns_empty(self):
        index = build_index([["alpha", "beta"], ["gamma", "delta"]])
        self.assertEqual(index.search("zzzz", top_k=10), [])

    def test_top_k_limits_results(self):
        # "shared" 出现 12/32 文档（37.5%，低于剪枝阈值）
        corpus = [[f"term{i}", "shared"] for i in range(12)]
        corpus += [[f"term{i}"] for i in range(12, 32)]
        index = build_index(corpus)
        self.assertEqual(len(index.search("shared", top_k=5)), 5)
        self.assertEqual(len(index.search("shared", top_k=100)), 12)

    def test_high_document_frequency_tokens_are_pruned(self):
        # "common" 出现在全部 4 篇文档中，超过 50% 阈值，应被剪掉
        index = build_index(
            [
                ["common", "apple"],
                ["common", "banana"],
                ["common", "cherry"],
                ["common", "durian"],
            ]
        )
        self.assertEqual(index.prune_query_tokens(["common"]), [])
        self.assertEqual(index.prune_query_tokens(["common", "apple"]), ["apple"])

    def test_pruning_can_be_disabled(self):
        index = build_index([["common", "apple"], ["common", "banana"]])
        original = config.BM25_MAX_DF_RATIO
        try:
            config.BM25_MAX_DF_RATIO = 1.0
            self.assertEqual(index.prune_query_tokens(["common"]), ["common"])
        finally:
            config.BM25_MAX_DF_RATIO = original

    def test_common_token_alone_yields_no_sparse_hits(self):
        index = build_index(
            [
                ["common", "apple"],
                ["common", "banana"],
                ["common", "cherry"],
                ["common", "durian"],
            ]
        )
        self.assertEqual(index.search("common", top_k=10), [])

    def test_empty_document_uses_sentinel(self):
        index = build_index([[]])
        self.assertEqual(index.count, 1)
        self.assertEqual(index.corpus_tokens[0], ["_"])


if __name__ == "__main__":
    unittest.main()
