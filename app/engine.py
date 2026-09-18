"""检索引擎：索引构建 / 加载 / 陈旧自检 / 双路召回 + RRF 融合编排。

检索流程：
    query ──┬─► 稠密：BGE 编码 + FAISS 内积召回 top-N ─┐
            └─► 稀疏：BM25 召回 top-N ────────────────┴─► RRF 融合 ─► top-10
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import config
from . import data as data_mod
from . import tokenize as tk
from .dense import DenseIndex
from .fusion import rrf_fuse
from .sparse import SparseIndex, build_corpus_tokens

logger = logging.getLogger(__name__)

VALID_MODES = ("fusion", "dense", "sparse")


class SearchEngine:
    """稠密 + 稀疏双路召回检索引擎。"""

    def __init__(self, index_dir: str | Path | None = None):
        self.index_dir = Path(index_dir) if index_dir else config.INDEX_DIR
        self.entries: list[dict[str, Any]] = []
        self.manifest: dict[str, Any] = {}
        self.dense: DenseIndex | None = None
        self.sparse: SparseIndex | None = None
        self._lock = threading.RLock()  # 串行化构建与检索，避免重复构建/设备并发

    # ------------------------------------------------------------------ 状态
    @property
    def ready(self) -> bool:
        return bool(self.entries) and self.dense is not None and self.sparse is not None

    def _index_files(self) -> tuple[Path, ...]:
        return tuple(
            self.index_dir / name
            for name in (
                config.MANIFEST_FILE,
                config.FAISS_FILE,
                config.CORPUS_TOKENS_FILE,
                config.ENTRIES_FILE,
            )
        )

    def index_exists(self) -> bool:
        return all(path.is_file() for path in self._index_files())

    def _signature(self) -> dict[str, Any]:
        """参与陈旧判定的配置签名（变更即触发重建）。"""
        return {
            "embed_model": config.EMBED_MODEL,
            "query_instruction": config.QUERY_INSTRUCTION,
            "tokenizer": tk.backend_name(),
            "rrf": {"k": config.RRF_K, "weights": dict(config.RRF_WEIGHTS)},
            "dense_min_score": config.DENSE_MIN_SCORE,
        }

    def staleness_reason(self) -> str | None:
        """返回索引需要重建的原因；索引存在且最新时返回 None。"""
        if not self.index_exists():
            missing = ", ".join(p.name for p in self._index_files() if not p.is_file())
            return f"索引文件缺失（{missing}）"

        try:
            manifest = json.loads((self.index_dir / config.MANIFEST_FILE).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return f"manifest 读取失败：{exc}"

        if int(manifest.get("version", 0)) != config.INDEX_VERSION:
            return f"索引版本变更：{manifest.get('version')} -> {config.INDEX_VERSION}"
        for key, current in self._signature().items():
            if manifest.get(key) != current:
                return f"{key} 变更：{manifest.get(key)} -> {current}"

        try:
            data_file = data_mod.find_latest_data_file()
        except FileNotFoundError as exc:
            return str(exc)
        if manifest.get("source_file") != data_file.name:
            return f"数据文件已更新：{manifest.get('source_file')} -> {data_file.name}"
        if manifest.get("source_sha256") != data_mod.file_sha256(data_file):
            return "数据文件内容已变化（sha256 不一致）"
        return None

    # ------------------------------------------------------------------ 构建
    def build(
        self,
        data_path: str | Path | None = None,
        force: bool = False,
        batch_size: int | None = None,
    ) -> dict[str, Any]:
        """构建稠密 + 稀疏索引并落盘。force=False 且索引可用时直接复用。"""
        with self._lock:
            if not force and self.staleness_reason() is None and self.load():
                logger.info("索引已是最新，跳过重建")
                return self.manifest

            started = time.perf_counter()
            entries, source = data_mod.load_entries(data_path)
            logger.info("加载数据：%s（%d 条）", source, len(entries))

            texts = data_mod.build_doc_texts(entries)
            dense = DenseIndex()
            total = dense.build(texts, batch_size=batch_size)
            logger.info("稠密索引完成：%d 条，维度 %s（device=%s）", total, dense.dim, dense.device)

            self.index_dir.mkdir(parents=True, exist_ok=True)
            dense.save(self.index_dir)

            sparse = SparseIndex(build_corpus_tokens(entries))
            sparse.save(self.index_dir)

            entries_file = self.index_dir / config.ENTRIES_FILE
            entries_file.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")

            manifest: dict[str, Any] = {
                "version": config.INDEX_VERSION,
                "source_file": source.name,
                "source_sha256": data_mod.file_sha256(source),
                "count": total,
                "dim": dense.dim,
                "embed_model": config.EMBED_MODEL,
                "query_instruction": config.QUERY_INSTRUCTION,
                "device": dense.device,
                "tokenizer": tk.backend_name(),
                "rrf": {"k": config.RRF_K, "weights": dict(config.RRF_WEIGHTS)},
                "dense_min_score": config.DENSE_MIN_SCORE,
                "bm25_max_df_ratio": config.BM25_MAX_DF_RATIO,
                "recall_depth": config.RECALL_DEPTH,
                "built_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                "build_seconds": round(time.perf_counter() - started, 2),
            }
            (self.index_dir / config.MANIFEST_FILE).write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
            )

            self.entries, self.manifest, self.dense, self.sparse = entries, manifest, dense, sparse
            logger.info(
                "索引构建完成：%d 条，用时 %.2fs，产物目录 %s",
                total,
                manifest["build_seconds"],
                self.index_dir,
            )
            return manifest

    # ------------------------------------------------------------------ 加载
    def load(self) -> bool:
        """从磁盘加载索引；失败时返回 False（不透出异常，交由调用方决定是否重建）。"""
        with self._lock:
            if not self.index_exists():
                return False
            try:
                manifest = json.loads(
                    (self.index_dir / config.MANIFEST_FILE).read_text(encoding="utf-8")
                )
                entries = json.loads(
                    (self.index_dir / config.ENTRIES_FILE).read_text(encoding="utf-8")
                )
                dense = DenseIndex()
                dense.load(self.index_dir)
                sparse = SparseIndex.load(self.index_dir)
            except Exception as exc:  # noqa: BLE001 - 任何损坏都视为不可用
                logger.warning("索引加载失败：%s", exc)
                return False

            if dense.count != len(entries) or sparse.count != len(entries):
                logger.warning(
                    "索引与元数据条目数不一致（entries=%d, faiss=%d, bm25=%d），视为不可用",
                    len(entries),
                    dense.count,
                    sparse.count,
                )
                return False

            self.manifest, self.entries, self.dense, self.sparse = manifest, entries, dense, sparse
            return True

    def load_or_build(self, auto_build: bool = True) -> dict[str, Any]:
        """启动自检：索引缺失/陈旧时自动重建，否则加载复用。"""
        with self._lock:
            reason = self.staleness_reason()
            if reason is None and self.load():
                logger.info(
                    "索引已加载：%d 条（模型 %s）",
                    len(self.entries),
                    self.manifest.get("embed_model"),
                )
                return self.manifest
            if not auto_build:
                raise RuntimeError(
                    f"索引不可用（{reason or '加载失败'}），请先执行：python -m app build-index"
                )
            logger.warning("索引需要重建：%s", reason or "加载失败")
            return self.build(force=True)

    # ------------------------------------------------------------------ 检索
    def search(
        self, query: str, top_k: int | None = None, mode: str = "fusion"
    ) -> dict[str, Any]:
        """双路召回 + RRF 融合检索。

        mode: fusion（默认，RRF 融合）/ dense（仅稠密）/ sparse（仅稀疏，调试用）
        返回条数恒不超过 config.MAX_RESULTS（10）。
        """
        text = (query or "").strip()
        mode = mode if mode in VALID_MODES else "fusion"
        limit = config.clamp_top_k(top_k)

        if not text:
            return {
                "query": text,
                "mode": mode,
                "top_k": limit,
                "results": [],
                "elapsed_ms": 0.0,
                "recall_depth": 0,
                "recalled": {"dense": 0, "sparse": 0, "fused": 0},
            }
        if not self.ready:
            raise RuntimeError("索引尚未就绪，请先执行：python -m app build-index")

        depth = config.recall_depth_for(limit)
        _ = self.dense.model  # 预热模型（首次为懒加载），加载耗时不计入检索耗时
        started = time.perf_counter()
        with self._lock:
            query_vector = self.dense.encode_query(text)
            dense_hits = self.dense.search(query_vector, depth)
            sparse_hits = self.sparse.search(text, depth)

        # 稠密召回兜底过滤：剔除相似度明显偏低的近邻（FAISS 总是返回 k 个近邻，不论是否相关）
        if config.DENSE_MIN_SCORE > 0:
            dense_hits = [
                (doc_id, score) for doc_id, score in dense_hits if score >= config.DENSE_MIN_SCORE
            ]

        dense_ids = [doc_id for doc_id, _ in dense_hits]
        sparse_ids = [doc_id for doc_id, _ in sparse_hits]
        dense_scores = dict(dense_hits)
        sparse_scores = dict(sparse_hits)
        dense_ranks = {doc_id: rank for rank, doc_id in enumerate(dense_ids, start=1)}
        sparse_ranks = {doc_id: rank for rank, doc_id in enumerate(sparse_ids, start=1)}

        fused = rrf_fuse(
            {"dense": dense_ids, "sparse": sparse_ids},
            weights=dict(config.RRF_WEIGHTS),
            k=config.RRF_K,
        )
        rrf_scores = {doc_id: score for doc_id, score, _ in fused}

        if mode == "dense":
            ordered_ids = dense_ids[:limit]
        elif mode == "sparse":
            ordered_ids = sparse_ids[:limit]
        else:
            ordered_ids = [doc_id for doc_id, _, _ in fused[:limit]]

        results: list[dict[str, Any]] = []
        for rank, doc_id in enumerate(ordered_ids, start=1):
            item = data_mod.result_fields(self.entries[doc_id])
            item["rank"] = rank
            item["rrf_score"] = round(rrf_scores[doc_id], 6) if doc_id in rrf_scores else None
            item["dense"] = (
                {"rank": dense_ranks[doc_id], "score": round(dense_scores[doc_id], 4)}
                if doc_id in dense_ranks
                else None
            )
            item["sparse"] = (
                {"rank": sparse_ranks[doc_id], "score": round(sparse_scores[doc_id], 4)}
                if doc_id in sparse_ranks
                else None
            )
            results.append(item)

        return {
            "query": text,
            "mode": mode,
            "top_k": limit,
            "results": results,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
            "recall_depth": depth,
            "recalled": {
                "dense": len(dense_ids),
                "sparse": len(sparse_ids),
                "fused": len(fused),
            },
        }

    # ------------------------------------------------------------------ 健康检查
    def health(self) -> dict[str, Any]:
        manifest = self.manifest or {}
        return {
            "status": "ready" if self.ready else "not_ready",
            "count": len(self.entries),
            "dim": self.dense.dim if self.dense else None,
            "embed_model": manifest.get("embed_model") or config.EMBED_MODEL,
            "device": (self.dense.device if self.dense else None) or config.EMBED_DEVICE,
            "tokenizer": manifest.get("tokenizer") or tk.backend_name(),
            "source_file": manifest.get("source_file"),
            "built_at": manifest.get("built_at"),
            "build_seconds": manifest.get("build_seconds"),
            "rrf": manifest.get("rrf")
            or {"k": config.RRF_K, "weights": dict(config.RRF_WEIGHTS)},
            "dense_min_score": config.DENSE_MIN_SCORE,
            "bm25_max_df_ratio": config.BM25_MAX_DF_RATIO,
            "recall_depth": manifest.get("recall_depth") or config.RECALL_DEPTH,
            "max_results": config.MAX_RESULTS,
            "index_dir": str(self.index_dir),
        }
