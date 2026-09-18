"""稠密召回：sentence-transformers（BAAI/bge-base-zh-v1.5）+ FAISS IndexFlatIP。

- 向量做 L2 归一化后用内积检索，等价于余弦相似度
- BGE 中文模型官方用法：查询侧拼接指令前缀，文档侧不拼接
- sentence-transformers 采用函数内延迟导入，确保 app.config 中的 HF_ENDPOINT /
  HF_HOME 环境变量在 huggingface_hub 之前生效
"""
from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Sequence

import numpy as np

from . import config

logger = logging.getLogger(__name__)


def resolve_device(preferred: str | None = None) -> str:
    """解析编码设备：auto 时优先 CUDA / MPS，否则 CPU。"""
    value = (preferred or config.EMBED_DEVICE or "auto").strip().lower()
    if value and value != "auto":
        return value
    try:
        import torch  # 延迟导入

        if torch.cuda.is_available():
            return "cuda"
        if torch.backends.mps.is_available():
            return "mps"
    except Exception:  # noqa: BLE001 - 无 torch 时按 CPU 处理
        pass
    return "cpu"


def _quiet_huggingface() -> None:
    """抑制 transformers / huggingface_hub 的进度条与冗余日志（可能不可用，忽略即可）。"""
    try:
        from transformers.utils import logging as hf_logging

        hf_logging.disable_progress_bar()
        hf_logging.set_verbosity_error()
    except Exception:  # noqa: BLE001
        pass


def resolve_local_model_path(model_name: str) -> str | None:
    """模型已下载到本地 HF 缓存时返回快照目录，否则返回 None。

    直接从本地快照加载可以免去启动时的联网校验（etag 检查），既更快也不受网络影响。
    """
    try:
        from huggingface_hub import constants as hf_constants

        hub_dir = Path(hf_constants.HF_HUB_CACHE)
    except Exception:  # noqa: BLE001 - 与 HF 缓存约定保持一致
        cache_root = os.environ.get("HF_HOME") or os.path.expanduser("~/.cache/huggingface")
        hub_dir = Path(cache_root) / "hub"

    repo_dir = hub_dir / f"models--{model_name.replace('/', '--')}"
    if not repo_dir.is_dir():
        return None

    snapshots = repo_dir / "snapshots"
    candidates: list[Path] = []
    ref = repo_dir / "refs" / "main"
    if ref.is_file():
        candidates.append(snapshots / ref.read_text(encoding="utf-8").strip())
    if snapshots.is_dir():
        candidates.extend(
            sorted(
                (path for path in snapshots.iterdir() if path.is_dir()),
                key=lambda path: path.stat().st_mtime,
                reverse=True,
            )
        )
    for candidate in candidates:
        if (candidate / "config.json").is_file():
            return str(candidate)
    return None


class DenseIndex:
    """稠密向量索引（模型 + FAISS 索引）。"""

    def __init__(
        self,
        model_name: str | None = None,
        device: str | None = None,
        query_instruction: str | None = None,
    ):
        self.model_name = model_name or config.EMBED_MODEL
        self.query_instruction = (
            config.QUERY_INSTRUCTION if query_instruction is None else query_instruction
        )
        self.device = resolve_device(device)
        self._model = None
        self._index = None
        self._vectors: np.ndarray | None = None

    # --------------------------------------------------------------- 模型
    @property
    def model(self):
        if self._model is None:
            # 延迟导入：确保 app.config 中的 HF_* 环境变量已先行生效
            from sentence_transformers import SentenceTransformer

            _quiet_huggingface()
            local_path = resolve_local_model_path(self.model_name)
            source = local_path or self.model_name
            logger.info(
                "加载 embedding 模型：%s（device=%s，来源=%s）",
                self.model_name,
                self.device,
                "本地缓存" if local_path else "在线下载",
            )
            self._model = SentenceTransformer(source, device=self.device)
        return self._model

    @property
    def dim(self) -> int | None:
        if self._index is not None:
            return int(self._index.d)
        if self._vectors is not None:
            return int(self._vectors.shape[1])
        return None

    @property
    def count(self) -> int:
        """索引内向量条数。"""
        return int(self._index.ntotal) if self._index is not None else 0

    def _do_encode(self, payload: list[str], batch_size: int | None = None) -> np.ndarray:
        vectors = self.model.encode(
            payload,
            batch_size=batch_size or config.BATCH_SIZE,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        return np.asarray(vectors, dtype="float32")

    def _encode(self, payload: list[str], batch_size: int | None = None) -> np.ndarray:
        try:
            return self._do_encode(payload, batch_size)
        except Exception as exc:  # noqa: BLE001 - 设备（如 MPS）异常时降级 CPU 重试
            if self.device == "cpu":
                raise
            logger.warning("device=%s 编码失败（%s），回退 CPU 重试", self.device, exc)
            self.device = "cpu"
            self._model = None  # 触发按 CPU 重新加载
            return self._do_encode(payload, batch_size)

    def encode_passages(self, texts: Sequence[str], batch_size: int | None = None) -> np.ndarray:
        return self._encode([str(t) for t in texts], batch_size)

    def encode_query(self, query: str) -> np.ndarray:
        return self._encode([f"{self.query_instruction}{query}"], batch_size=1)[0]

    # --------------------------------------------------------------- 索引
    def build(self, texts: Sequence[str], batch_size: int | None = None) -> int:
        import faiss  # 延迟导入

        vectors = self.encode_passages(texts, batch_size)
        if vectors.size == 0:
            raise ValueError("编码结果为空，无法构建 FAISS 索引")
        vectors = np.ascontiguousarray(vectors, dtype="float32")
        index = faiss.IndexFlatIP(vectors.shape[1])
        index.add(vectors)
        self._index = index
        self._vectors = vectors
        return int(index.ntotal)

    def search(self, query_vector: np.ndarray, top_k: int) -> list[tuple[int, float]]:
        if self._index is None or self._index.ntotal == 0:
            return []
        limit = min(int(top_k), int(self._index.ntotal))
        if limit <= 0:
            return []
        matrix = np.ascontiguousarray(query_vector.reshape(1, -1), dtype="float32")
        scores, ids = self._index.search(matrix, limit)
        return [(int(i), float(s)) for i, s in zip(ids[0], scores[0]) if i >= 0]

    # ------------------------------------------------------------- 持久化
    def save(self, index_dir: Path) -> None:
        import faiss  # 延迟导入

        if self._index is None or self._vectors is None:
            raise RuntimeError("索引尚未构建，无法保存")
        index_dir = Path(index_dir)
        index_dir.mkdir(parents=True, exist_ok=True)
        faiss.write_index(self._index, str(index_dir / config.FAISS_FILE))
        np.save(index_dir / config.EMBEDDINGS_FILE, self._vectors)

    def load(self, index_dir: Path) -> None:
        import faiss  # 延迟导入

        index_dir = Path(index_dir)
        self._index = faiss.read_index(str(index_dir / config.FAISS_FILE))
        embeddings_path = index_dir / config.EMBEDDINGS_FILE
        self._vectors = np.load(embeddings_path) if embeddings_path.exists() else None
