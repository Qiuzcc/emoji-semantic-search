"""全局配置：路径、模型与检索参数。

所有参数均可通过同名环境变量覆盖。模块顶层即注入 HF_* 环境变量，
因此依赖 huggingface_hub 的库（sentence-transformers）必须在导入本模块之后
才被导入 —— app/dense.py 采用函数内延迟导入来保证这一约束。
"""
from __future__ import annotations

import os
from pathlib import Path


def _env_str(name: str, default: str) -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


def _env_int(name: str, default: int) -> int:
    try:
        return int(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(str(os.environ.get(name, "")).strip())
    except ValueError:
        return default


# ---------------------------------------------------------------- 路径
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(_env_str("EMOJI_DATA_DIR", str(PROJECT_ROOT / "public")))
INDEX_DIR = Path(_env_str("EMOJI_INDEX_DIR", str(PROJECT_ROOT / "index")))
WEB_DIR = Path(_env_str("EMOJI_WEB_DIR", str(PROJECT_ROOT / "web")))
MODEL_CACHE_DIR = Path(_env_str("EMOJI_MODEL_CACHE_DIR", str(PROJECT_ROOT / "models")))

# ---------------------------------------------------------------- 模型下载
# huggingface.co 在部分网络环境下不可达，默认走镜像；若用户已显式设置 HF_ENDPOINT 则尊重之
HF_ENDPOINT = _env_str("EMOJI_HF_ENDPOINT", "https://hf-mirror.com")
os.environ.setdefault("HF_ENDPOINT", HF_ENDPOINT)
os.environ.setdefault("HF_HOME", str(MODEL_CACHE_DIR))
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")

# ---------------------------------------------------------------- 稠密编码
EMBED_MODEL = _env_str("EMOJI_EMBED_MODEL", "BAAI/bge-base-zh-v1.5")
# BGE 中文模型官方用法：查询侧加指令前缀，文档侧不加
QUERY_INSTRUCTION = _env_str("EMOJI_QUERY_INSTRUCTION", "为这个句子生成表示以用于检索相关文章：")
EMBED_DEVICE = _env_str("EMOJI_EMBED_DEVICE", "auto")  # auto | cpu | mps | cuda
BATCH_SIZE = _env_int("EMOJI_BATCH_SIZE", 64)

# ---------------------------------------------------------------- 检索
MAX_RESULTS = 10  # 需求硬约束：呈现给用户的结果不超过 10 条
DEFAULT_TOP_K = min(max(_env_int("EMOJI_TOP_K", 10), 1), MAX_RESULTS)
RECALL_DEPTH = max(_env_int("EMOJI_RECALL_DEPTH", 50), MAX_RESULTS)  # 每路召回深度
RRF_K = max(_env_int("EMOJI_RRF_K", 60), 1)
RRF_WEIGHTS = {
    "dense": _env_float("EMOJI_RRF_W_DENSE", 1.0),
    "sparse": _env_float("EMOJI_RRF_W_SPARSE", 1.0),
}
# BM25 查询词剪枝：文档频率超过该占比的 token（如 “的”“下” 等无区分度高频词）不参与打分，
# 避免它们在大量文档上产生微弱噪声得分。1.0 表示不剪枝。
BM25_MAX_DF_RATIO = min(max(_env_float("EMOJI_BM25_MAX_DF_RATIO", 0.5), 0.0), 1.0)
# 稠密召回的相关性下限（余弦相似度）：剔除明显无关的近邻，0 表示不过滤。
# 实测本数据集：相关查询 top 得分约 0.40~0.55，无关/乱码查询约 0.20~0.36，取 0.30 作保守下限。
DENSE_MIN_SCORE = min(max(_env_float("EMOJI_DENSE_MIN_SCORE", 0.30), 0.0), 1.0)

# ---------------------------------------------------------------- 索引产物
INDEX_VERSION = 2  # 索引内容构造方式变更时递增，用于触发自动重建
FAISS_FILE = "emoji.faiss"
EMBEDDINGS_FILE = "embeddings.npy"
CORPUS_TOKENS_FILE = "corpus_tokens.json"
ENTRIES_FILE = "entries.json"
MANIFEST_FILE = "manifest.json"


def clamp_top_k(top_k: int | None) -> int:
    """结果条数钳制：至少 1 条，最多 MAX_RESULTS（10）条。"""
    if top_k is None:
        return DEFAULT_TOP_K
    try:
        value = int(top_k)
    except (TypeError, ValueError):
        return DEFAULT_TOP_K
    return max(1, min(value, MAX_RESULTS))


def recall_depth_for(top_k: int) -> int:
    """每路召回深度：不小于配置值，且必然覆盖最终展示条数。"""
    return max(RECALL_DEPTH, top_k)
