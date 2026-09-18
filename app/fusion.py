"""RRF（Reciprocal Rank Fusion）融合排序。

    score(d) = Σ_r  w_r / (k + rank_r(d))

- r 遍历各召回路径（本项目的 dense / sparse 两路）
- rank_r(d) 从 1 开始；文档未出现在某一路时，该路不贡献分数（即视作不入围）
- k 为平滑常数（默认 60），w_r 为路径权重

RRF 只依赖名次而非分数量纲，因此天然适合融合余弦相似度与 BM25 分数这两类
不可直接比较的得分。
"""
from __future__ import annotations

from typing import Mapping, Sequence


def rrf_fuse(
    rankings: Mapping[str, Sequence[int]],
    weights: Mapping[str, float] | None = None,
    k: int = 60,
    top_n: int | None = None,
) -> list[tuple[int, float, dict[str, int]]]:
    """融合多路有序召回结果。

    参数：
        rankings  形如 {"dense": [doc_id, ...], "sparse": [doc_id, ...]}，列表按各自分数降序
        weights   各路径权重，缺省为 1.0
        k         平滑常数
        top_n     截断条数，None 表示不截断

    返回：
        [(doc_id, fused_score, {"dense": rank, "sparse": rank}), ...]
        按融合分降序；分数相同时按 doc_id 升序，保证结果稳定可复现。
    """
    if k < 1:
        raise ValueError("RRF 平滑常数 k 必须 >= 1")
    weights = weights or {}

    scores: dict[int, float] = {}
    ranks: dict[int, dict[str, int]] = {}

    for path, ordered_ids in rankings.items():
        weight = float(weights.get(path, 1.0))
        if weight == 0.0:
            continue
        seen: set[int] = set()
        for rank, doc_id in enumerate(ordered_ids, start=1):
            doc_id = int(doc_id)
            if doc_id in seen:  # 同一路径内重复出现时只保留最优名次，避免重复加分
                continue
            seen.add(doc_id)
            scores[doc_id] = scores.get(doc_id, 0.0) + weight / (k + rank)
            ranks.setdefault(doc_id, {})[path] = rank

    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if top_n is not None:
        ordered = ordered[: max(int(top_n), 0)]
    return [(doc_id, score, ranks[doc_id]) for doc_id, score in ordered]
