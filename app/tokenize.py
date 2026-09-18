"""中文分词（BM25 稀疏检索专用）。

优先使用 jieba；jieba 不可用时回退「CJK 单字 + 相邻 bigram + ASCII 词 + 符号」
策略，保证任何环境下稀疏召回都可用。

分词结果统一小写化、过滤纯标点，并保留：
- emoji 字符本身（整串，如 "👨‍👩‍👧"、"❤️" 归一化后）
- U+XXXX 码点（形如 u+1f680 的整体 token，便于按码点精确检索）
"""
from __future__ import annotations

import logging
import re
import string

logger = logging.getLogger(__name__)

# 汉字、日文假名、韩文音节：这些字符按词/字切分
CJK_RANGES = "\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uac00-\ud7af"

_CODEPOINT_RE = re.compile(r"u\+[0-9a-f]{4,6}", re.IGNORECASE)
_CHUNK_RE = re.compile(rf"[{CJK_RANGES}]+|[a-z0-9]+|[^\s]")
_CJK_ONLY_RE = re.compile(rf"[{CJK_RANGES}]+")
_HAS_TEXT_RE = re.compile(rf"[0-9a-z{CJK_RANGES}]", re.IGNORECASE)

# 需要过滤掉的噪声字符：ASCII 标点、全角标点、不可见字符
_PUNCT_CHARS = set(string.punctuation) | set(
    "　。，、；：？！“”‘’（）【】《》〈〉「」『』〔〕…—～·ー・＋－＝％＃＠＆＊｜／＼＄￥"
)
_PUNCT_CHARS |= {"\u200b", "\ufe0f", "\u200d"}  # 零宽/变体选择符/ZWJ 单字符噪声

_jieba_module = None
_jieba_checked = False


def _load_jieba():
    """懒加载 jieba；加载失败时返回 None 并永久走回退分词。"""
    global _jieba_module, _jieba_checked
    if _jieba_checked:
        return _jieba_module
    _jieba_checked = True
    try:
        import jieba  # noqa: PLC0415
        jieba.setLogLevel(logging.WARNING)
        jieba.initialize()
        _jieba_module = jieba
    except Exception as exc:  # noqa: BLE001 - 任何异常都退化为回退分词
        logger.warning("jieba 不可用（%s），稀疏检索回退到字符 bigram 分词", exc)
        _jieba_module = None
    return _jieba_module


def backend_name() -> str:
    """当前分词后端名称（写入索引 manifest，用于变更检测）。"""
    module = _load_jieba()
    if module is None:
        return "char-bigram-fallback"
    return f"jieba-{getattr(module, '__version__', 'unknown')}"


def _is_noise(token: str) -> bool:
    return all(ch.isspace() or ch in _PUNCT_CHARS for ch in token)


def _cut_fallback(text: str) -> list[str]:
    """无 jieba 时的回退切分：CJK 单字 + 相邻 bigram，其余按词/单字符。"""
    tokens: list[str] = []
    for chunk in _CHUNK_RE.findall(text):
        if _CJK_ONLY_RE.fullmatch(chunk):
            tokens.extend(chunk)
            tokens.extend(chunk[i : i + 2] for i in range(len(chunk) - 1))
        else:
            tokens.append(chunk)
    return tokens


def _cut(text: str) -> list[str]:
    module = _load_jieba()
    if module is None:
        return _cut_fallback(text)
    return list(module.cut(text, cut_all=False, HMM=True))


def tokenize(text: str) -> list[str]:
    """把任意文本切成 BM25 token 序列（小写、去标点）。"""
    if not text:
        return []
    lowered = str(text).lower()

    tokens: list[str] = [match.group(0) for match in _CODEPOINT_RE.finditer(lowered)]
    remainder = _CODEPOINT_RE.sub(" ", lowered)

    for raw in _cut(remainder):
        token = raw.strip()
        if not token or _is_noise(token):
            continue
        tokens.append(token)
    return tokens


def tokenize_query(query: str) -> list[str]:
    """查询分词：补充「整体 emoji 序列」token，支持精确字符检索。

    例如查询 "👨‍👩‍👧" 时，除了逐字符 token 外还额外加入整体 token，
    与索引侧按 cp 整串写入的 token 精确对齐。
    """
    text = (query or "").strip()
    if not text:
        return []
    tokens = tokenize(text)
    compact = text.replace("\ufe0f", "").strip().lower()
    if compact and len(compact) <= 16 and not _HAS_TEXT_RE.search(compact) and compact not in tokens:
        tokens.insert(0, compact)
    return tokens
