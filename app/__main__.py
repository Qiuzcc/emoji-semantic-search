"""命令行入口：python -m app {build-index|search|serve}

    python -m app build-index [--data public/emoji_xxx.json] [--force]
    python -m app search "鼓励别人" [--top-k 10] [--mode fusion] [--json]
    python -m app serve [--host 127.0.0.1] [--port 8000] [--no-auto-build]
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import unicodedata
from typing import Any, Sequence

from . import config
from .engine import SearchEngine

try:  # 保证中文输出在任意终端编码下不报错
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    # 抑制第三方库（模型下载、HTTP 客户端）的刷屏日志
    for noisy in ("httpx", "httpcore", "urllib3", "filelock", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------- 终端表格工具
def _display_width(text: str) -> int:
    """终端显示宽度（中日韩全角字符按 2 列计）。"""
    return sum(2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1 for ch in text)


def _truncate(text: str, width: int) -> str:
    if _display_width(text) <= width:
        return text
    out = ""
    for ch in text:
        if _display_width(out) + _display_width(ch) > width - 1:
            return out + "…"
        out += ch
    return out


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _path_cell(info: dict[str, Any] | None) -> str:
    """渲染单路召回的「名次/分数」单元格。"""
    if not info:
        return "-"
    return f"#{info['rank']} {info['score']}"


def _print_table(result: dict[str, Any]) -> None:
    results = result.get("results") or []
    print(
        f"\n查询：{result['query']} | 模式：{result['mode']} | "
        f"每路召回：{result['recall_depth']} | 耗时：{result['elapsed_ms']} ms"
    )
    recalled = result.get("recalled") or {}
    print(
        f"召回：稠密 {recalled.get('dense', 0)} 条 / 稀疏 {recalled.get('sparse', 0)} 条 / "
        f"融合去重 {recalled.get('fused', 0)} 条 | 展示 {len(results)} 条（上限 {config.MAX_RESULTS}）"
    )
    if not results:
        print("\n（无匹配结果）")
        return

    header = (
        _pad("#", 3)
        + _pad("Emoji", 6)
        + _pad("名称", 20)
        + _pad("Dense", 14)
        + _pad("BM25", 14)
        + _pad("RRF", 10)
        + "描述"
    )
    print("\n" + header)
    print("-" * 120)
    for item in results:
        print(
            _pad(str(item["rank"]), 3)
            + _pad(item["cp"], 6)
            + _pad(_truncate(item["name"], 18), 20)
            + _pad(_path_cell(item.get("dense")), 14)
            + _pad(_path_cell(item.get("sparse")), 14)
            + _pad("-" if item.get("rrf_score") is None else f"{item['rrf_score']:.5f}", 10)
            + _truncate(item["description"], 64)
        )
    print()


# ---------------------------------------------------------------- 子命令
def _cmd_build_index(args: argparse.Namespace) -> int:
    engine = SearchEngine()
    manifest = engine.build(data_path=args.data, force=args.force)
    rows = [
        ("数据文件", manifest.get("source_file")),
        ("条目数", manifest.get("count")),
        ("向量维度", manifest.get("dim")),
        ("Embedding 模型", manifest.get("embed_model")),
        ("编码设备", manifest.get("device")),
        ("分词后端", manifest.get("tokenizer")),
        ("RRF 参数", f"k={manifest['rrf']['k']}, weights={manifest['rrf']['weights']}"),
        ("稠密分数下限", manifest.get("dense_min_score")),
        ("构建耗时", f"{manifest.get('build_seconds')} s"),
        ("构建时间", manifest.get("built_at")),
        ("索引目录", str(engine.index_dir)),
    ]
    print("\n索引构建完成：")
    for key, value in rows:
        print(f"  {_pad(key, 16)}{value}")
    print()
    return 0


def _cmd_search(args: argparse.Namespace) -> int:
    engine = SearchEngine()
    engine.load_or_build(auto_build=not args.no_auto_build)
    result = engine.search(args.query, top_k=args.top_k, mode=args.mode)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_table(result)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    from .server import run_server

    run_server(host=args.host, port=args.port, auto_build=not args.no_auto_build)
    return 0


# ---------------------------------------------------------------- CLI
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app",
        description="emoji 语义搜索：稠密（bge + FAISS）+ 稀疏（BM25）双路召回，RRF 融合",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser_ = subparsers.add_parser("build-index", help="构建并持久化检索索引")
    build_parser_.add_argument("--data", help="指定数据文件（默认 public/ 下最新的 emoji_*.json）")
    build_parser_.add_argument("--force", action="store_true", help="强制重建，即使索引已是最新")
    build_parser_.set_defaults(func=_cmd_build_index)

    search_parser = subparsers.add_parser("search", help="命令行检索（便于无 GUI 验证）")
    search_parser.add_argument("query", help="查询文本")
    search_parser.add_argument(
        "--top-k",
        type=int,
        default=config.DEFAULT_TOP_K,
        help=f"返回条数，上限 {config.MAX_RESULTS}（默认 {config.DEFAULT_TOP_K}）",
    )
    search_parser.add_argument(
        "--mode", choices=("fusion", "dense", "sparse"), default="fusion", help="召回模式"
    )
    search_parser.add_argument("--json", action="store_true", help="输出原始 JSON")
    search_parser.add_argument(
        "--no-auto-build", action="store_true", help="索引缺失/陈旧时不自动重建"
    )
    search_parser.set_defaults(func=_cmd_search)

    serve_parser = subparsers.add_parser("serve", help="启动 GUI 服务")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8000)
    serve_parser.add_argument(
        "--no-auto-build", action="store_true", help="索引缺失/陈旧时不自动重建"
    )
    serve_parser.set_defaults(func=_cmd_serve)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _setup_logging(args.verbose)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\n已中断", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - CLI 友好报错
        logging.error("%s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
