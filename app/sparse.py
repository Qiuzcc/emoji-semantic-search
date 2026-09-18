"""稀疏召回：rank_bm25 的 BM25Okapi 索引封装。

语料只持久化 token 化结果（corpus_tokens.json），加载时重建 BM25 模型，
避免 pickle 在库版本 / 分词后端变化时的兼容问题。
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from . import config
from .tokenize import tokenize, tokenize_query

logger = logging.getLogger(__name__)

# 空文档占位 token，避免 BM25Okapi 在 doc_len = 0 时出现异常统计
_EMPTY_SENTINEL = "_"


def doc_tokens(entry: dict[str, Any]) -> list[str]:
    """构造单条 emoji 的 BM25 token。

    字段构成：名称 + 关键词 + 描述（自然语言匹配）+ 码点 / emoji 字符（精确匹配）。
    """
    tokens: list[str] = []
    tokens.extend(tokenize(entry.get("name", "")))
    for keyword in entry.get("keywords") or []:
        tokens.extend(tokenize(keyword))
    tokens.extend(tokenize(entry.get("description", "")))
    tokens.extend(tokenize(entry.get("codepoint", "")))  # U+1F680 -> u+1f680，支持按码点精确检索

    cp = str(entry.get("cp") or "").strip()
    if cp:
        tokens.append(cp)  # 整体 emoji 序列（含 ZWJ / 变体选择符）
        normalized = cp.replace("\ufe0f", "").strip()
        if normalized and normalized != cp:
            tokens.append(normalized)

    return tokens or [_EMPTY_SENTINEL]


def build_corpus_tokens(entries: Sequence[dict[str, Any]]) -> list[list[str]]:
    return [doc_tokens(entry) for entry in entries]


class SparseIndex:
    """BM25 稀疏索引。"""

    def __init__(self, corpus_tokens: Sequence[Sequence[str]]):
        from rank_bm25 import BM25Okapi  # 延迟导入，保持模块导入轻量

        self.corpus_tokens: list[list[str]] = [
            list(tokens) or [_EMPTY_SENTINEL] for tokens in corpus_tokens
        ]
        self._bm25 = BM25Okapi(self.corpus_tokens)
        # 文档频率统计：用于查询词剪枝（剔除无区分度的高频词，加 1 平滑分母）
        self._term_df: dict[str, int] = {}
        for tokens in self.corpus_tokens:
            for term in set(tokens):
                self._term_df[term] = self._term_df.get(term, 0) + 1
        self._max_df = max(int(config.BM25_MAX_DF_RATIO * len(self.corpus_tokens)), 1)

    @property
    def count(self) -> int:
        return len(self.corpus_tokens)

    def prune_query_tokens(self, tokens: Sequence[str]) -> list[str]:
        """剪掉文档频率过高、几乎无区分度的查询 token。"""
        if config.BM25_MAX_DF_RATIO >= 1.0:
            return list(tokens)
        return [token for token in tokens if self._term_df.get(token, 0) <= self._max_df]

    def search(self, query: str, top_k: int) -> list[tuple[int, float]]:
        """返回 [(doc_id, bm25_score), ...]，按分数降序，且只保留正分命中。"""
        query_tokens = self.prune_query_tokens(tokenize_query(query))
        if not query_tokens or not self.corpus_tokens:
            return []

        scores = np.asarray(self._bm25.get_scores(query_tokens), dtype="float64")
        if scores.size == 0:
            return []

        # BM25 对未命中文档给 0 分（负 IDF 场景下还可能给负分），必须过滤，
        # 否则无关键词重叠时会返回一批噪声结果。
        positive = np.flatnonzero(scores > 0)
        if positive.size == 0:
            return []

        limit = min(int(top_k), positive.size)
        order = np.argsort(-scores[positive], kind="stable")[:limit]
        return [(int(positive[i]), float(scores[positive[i]])) for i in order]

    # ------------------------------------------------------------- 持久化
    def save(self, index_dir: Path) -> Path:
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        path = index_dir / config.CORPUS_TOKENS_FILE
        path.write_text(json.dumps(self.corpus_tokens, ensure_ascii=False), encoding="utf-8")
        return path

    @classmethod
    def load(cls, index_dir: Path) -> "SparseIndex":
        path = Path(index_dir) / config.CORPUS_TOKENS_FILE
        corpus_tokens = json.loads(path.read_text(encoding="utf-8"))
        return cls(corpus_tokens)
