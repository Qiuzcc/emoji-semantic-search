"""数据层：加载 public/ 下最新的 emoji 数据文件并归一化为检索条目。

数据文件由 scripts/build-emoji-data.py 生成，字段：
    cp / codepoint / name / keywords / description
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from . import config

# 保留字段（对外返回与展示）
RESULT_FIELDS = ("cp", "codepoint", "name", "keywords", "description")

# 兼容 {"data": [...]} / {"emojis": [...]} 之类的包装结构
_LIST_WRAPPER_KEYS = ("emojis", "data", "items", "list", "records")


def find_latest_data_file() -> Path:
    """定位最新数据文件。

    文件名形如 emoji_2026_09_18_15_31.json，字典序即时间序，故取倒序首个；
    没有带时间戳的文件时回退到 public/*.json。
    """
    if not config.DATA_DIR.is_dir():
        raise FileNotFoundError(f"数据目录不存在：{config.DATA_DIR}")
    candidates = sorted(config.DATA_DIR.glob("emoji_*.json"), reverse=True)
    if not candidates:
        candidates = sorted(config.DATA_DIR.glob("*.json"), reverse=True)
    if not candidates:
        raise FileNotFoundError(f"{config.DATA_DIR} 下未找到 emoji_*.json 数据文件")
    return candidates[0]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _normalize(raw: Any) -> dict[str, Any] | None:
    """单条记录归一化；缺少 cp 的记录视为无效。"""
    if not isinstance(raw, dict):
        return None
    cp = str(raw.get("cp") or "").strip()
    if not cp:
        return None
    keywords = [str(k).strip() for k in (raw.get("keywords") or []) if str(k).strip()]
    return {
        "cp": cp,
        "codepoint": str(raw.get("codepoint") or "").strip(),
        "name": str(raw.get("name") or "").strip(),
        "keywords": keywords,
        "description": str(raw.get("description") or "").strip(),
    }


def load_entries(path: str | Path | None = None) -> tuple[list[dict[str, Any]], Path]:
    """加载数据文件，返回 (条目列表, 数据文件路径)。"""
    data_path = Path(path) if path else find_latest_data_file()
    if not data_path.is_file():
        raise FileNotFoundError(f"数据文件不存在：{data_path}")

    payload = json.loads(data_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        for key in _LIST_WRAPPER_KEYS:
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise ValueError(f"{data_path} 结构不符合预期：期望 emoji 数组")
    if not isinstance(payload, list):
        raise ValueError(f"{data_path} 结构不符合预期：期望 emoji 数组")

    entries: list[dict[str, Any]] = []
    seen: set[str] = set()
    for raw in payload:
        entry = _normalize(raw)
        if entry is None or entry["cp"] in seen:
            continue
        seen.add(entry["cp"])
        entries.append(entry)
    if not entries:
        raise ValueError(f"{data_path} 中没有有效条目")
    return entries, data_path


def doc_text(entry: dict[str, Any]) -> str:
    """稠密编码使用的文档文本：名称 + 关键词 + 描述。

    描述字段本身就是为提升语义效果而生成的，与关键词拼接后能显著改善召回。
    """
    keywords = "、".join(entry.get("keywords") or [])
    return (
        f"名称：{entry.get('name', '')}\n"
        f"关键词：{keywords}\n"
        f"描述：{entry.get('description', '')}"
    )


def build_doc_texts(entries: list[dict[str, Any]]) -> list[str]:
    return [doc_text(entry) for entry in entries]


def result_fields(entry: dict[str, Any]) -> dict[str, Any]:
    """抽取对外返回/前端展示所需字段。"""
    return {field: entry.get(field) for field in RESULT_FIELDS}
