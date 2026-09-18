#!/usr/bin/env python3
"""emoji 数据抽取与清洗脚本（零依赖，Python >= 3.8；与 build-emoji-data.mjs 逻辑一致、共享缓存）

数据流：
  1. 加载数据源（优先本地缓存 scripts/.cache/，缺失或 --refresh 时下载）：
     - CLDR common/annotations/zh.xml                         -> cp / name / keywords
     - Unicode emoji-sequences.txt / emoji-zwj-sequences.txt  -> RGI 白名单
  2. 解析 RGI 白名单：区间码点展开；序列统一去掉 U+FE0F 归一化
     （zh.xml 文件头声明其 cp 已去除 U+FE0F，两侧归一化后即可精确比对）。
  3. 解析 zh.xml 的 <annotation>：type="tts" 为名称（name 取该值），
     其余为关键词（" | " 分隔）。
  4. 按白名单过滤，生成 codepoint（U+XXXX，多码点以空格分隔），并按码点排序。
  5. 通过 LLM（OpenAI 兼容 /chat/completions 接口，分批 + 并发 + 重试 +
     本地缓存可断点续传）依据 name / keywords 生成 description。
  6. 写出 emoji_yyyy_mm_dd_hh_mm.json：
     ../public/ 目录存在则写入该目录，否则写入脚本同级目录。

环境变量（生成 description 时必需）：
  LLM_API_KEY    API Key（缺省时依次回退读取 DEEPSEEK_API_KEY、OPENAI_API_KEY）；
                 未配置时须加 --skip-description 运行
  LLM_BASE_URL   接口地址，默认 https://api.deepseek.com/v1
  LLM_MODEL      模型名，默认 deepseek-flash

用法：python3 scripts/build-emoji-data.py [选项]
"""
import argparse
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

try:  # 保证中文输出在任意终端编码下不报错
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:  # noqa: BLE001
    pass

SCRIPT_DIR = Path(__file__).resolve().parent
CACHE_DIR = SCRIPT_DIR / ".cache"

SOURCES = [
    {
        "name": "CLDR zh 注解 (zh.xml)",
        "urls": [
            "https://raw.githubusercontent.com/unicode-org/cldr/main/common/annotations/zh.xml",
            # GitHub raw 在部分网络环境下不稳定，逐级回退备用地址
            "https://cdn.jsdelivr.net/gh/unicode-org/cldr@main/common/annotations/zh.xml",
        ],
        "file": "cldr-zh-annotations.xml",
    },
    {
        "name": "Unicode emoji-sequences.txt",
        "urls": ["https://www.unicode.org/Public/emoji/latest/emoji-sequences.txt"],
        "file": "unicode-emoji-sequences.txt",
    },
    {
        "name": "Unicode emoji-zwj-sequences.txt",
        "urls": ["https://www.unicode.org/Public/emoji/latest/emoji-zwj-sequences.txt"],
        "file": "unicode-emoji-zwj-sequences.txt",
    },
]

DESCRIPTION_CACHE_FILE = CACHE_DIR / "descriptions.json"
HTTP_TIMEOUT_SECONDS = 120


# ---------------------------------------------------------------------------
# 命令行参数
# ---------------------------------------------------------------------------

def positive_int(value):
    try:
        number = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"无效的数值: {value}")
    if number <= 0:
        raise argparse.ArgumentTypeError("需要一个正数")
    return number


HELP_EPILOG = """环境变量 (生成 description 时必需):
  LLM_API_KEY    API Key（缺省时依次回退读取 DEEPSEEK_API_KEY、OPENAI_API_KEY）
  LLM_BASE_URL   接口地址，默认 https://api.deepseek.com/v1
  LLM_MODEL      模型名，默认 deepseek-flash

输出:
  ../public/ 目录存在则写入该目录，否则写入脚本同级目录；
  文件名为 emoji_yyyy_mm_dd_hh_mm.json"""


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="build-emoji-data.py",
        usage="%(prog)s [选项]",
        description="emoji 数据抽取与清洗脚本（零依赖，Python >= 3.8）",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        add_help=False,
    )
    parser.add_argument("-h", "--help", action="help", help="显示帮助")
    parser.add_argument("--refresh", action="store_true",
                        help="强制重新下载数据源（忽略本地缓存）")
    parser.add_argument("--skip-description",
                        action="store_true", help="跳过 LLM 描述生成，description 置空")
    parser.add_argument("--limit", type=positive_int,
                        metavar="n", help="仅处理前 n 条（便于快速验证）")
    parser.add_argument(
        "--batch-size", type=positive_int, metavar="n", default=25,
        help="每次 LLM 请求包含的条目数，默认 25",
    )
    parser.add_argument(
        "--concurrency", type=positive_int, metavar="n", default=4,
        help="LLM 请求并发数，默认 4",
    )
    return parser.parse_args(argv)


# ---------------------------------------------------------------------------
# 数据源加载
# ---------------------------------------------------------------------------

def format_size(byte_count):
    return f"{byte_count / 1024:.1f} KB" if byte_count >= 1024 else f"{byte_count} B"


def describe_error(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code} {error.reason}"
    reason = getattr(error, "reason", None)
    if reason is not None and str(reason) not in str(error):
        return f"{error} ({reason})"
    return str(error)


def should_retry_download(error):
    if isinstance(error, urllib.error.HTTPError):
        return error.code >= 500 or error.code == 429  # 4xx 无需重试，换备用地址
    return True


def load_source(source, refresh):
    cache_path = CACHE_DIR / source["file"]
    if not refresh and cache_path.exists() and cache_path.stat().st_size > 0:
        text = cache_path.read_text(encoding="utf-8")
        print(
            f"  - {source['name']}: 本地缓存 ({format_size(len(text.encode('utf-8')))})")
        return text
    last_error = None
    for index, url in enumerate(source["urls"]):
        for attempt in range(1, 4):
            try:
                request = urllib.request.Request(
                    url, headers={"User-Agent": "emoji-data-builder/1.0"})
                with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
                    data = response.read()
                text = data.decode("utf-8")
                if not text.strip():
                    raise ValueError("下载内容为空")
                cache_path.write_text(text, encoding="utf-8")
                print(f"  - {source['name']}: 已下载 ({format_size(len(data))})")
                return text
            except Exception as error:  # noqa: BLE001 - 此处统一做重试/降级处理
                last_error = error
                if not should_retry_download(error):
                    break
                if attempt < 3:
                    time.sleep(attempt)
        if index < len(source["urls"]) - 1:
            print(
                f"  ! {source['name']} 下载失败，尝试备用地址: {describe_error(last_error)}",
                file=sys.stderr,
            )
    raise RuntimeError(
        f"无法获取 {source['name']}（{describe_error(last_error)}），请检查网络后重试")


# ---------------------------------------------------------------------------
# RGI 白名单解析
# ---------------------------------------------------------------------------

def normalize_sequence(codepoints):
    """序列归一化：去除 U+FE0F 后输出 "1F468 200D 2764" 形式的大写十六进制串"""
    return " ".join(format(cp, "X") for cp in codepoints if cp != 0xFE0F)


def expand_codepoint_field(field):
    """展开 "1F600..1F64F" 区间或 "1F600 200D xxx" 单序列，返回归一化后的序列 key 列表"""
    range_match = re.match(r"^([0-9A-Fa-f]+)\.\.([0-9A-Fa-f]+)$", field)
    if range_match:
        start = int(range_match.group(1), 16)
        end = int(range_match.group(2), 16)
        return [normalize_sequence([cp]) for cp in range(start, end + 1)]
    return [normalize_sequence(int(hex_value, 16) for hex_value in field.split())]


def parse_rgi_whitelist(texts):
    whitelist = set()
    line_count = 0
    for text in texts:
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            body = line.split("#")[0].strip()  # 去掉行尾注释
            if not body:
                continue
            parts = [part.strip() for part in body.split(";")]
            if len(parts) < 2 or not parts[0] or not parts[1]:
                continue
            for key in expand_codepoint_field(parts[0]):
                whitelist.add(key)
            line_count += 1
    return whitelist, line_count


# ---------------------------------------------------------------------------
# CLDR zh.xml 解析
# ---------------------------------------------------------------------------

def parse_cldr_annotations(xml_text):
    """返回 dict: cp -> {"keywords": [...], "tts": ""}，保持文件中的出现顺序"""
    annotation_map = {}
    root = ElementTree.fromstring(xml_text.encode("utf-8"))
    for element in root.iter("annotation"):
        cp = element.get("cp")
        if cp is None:
            continue
        value = (element.text or "").strip()
        item = annotation_map.setdefault(cp, {"keywords": [], "tts": ""})
        if element.get("type") == "tts":
            item["tts"] = value
        else:
            item["keywords"] = [keyword.strip()
                                for keyword in value.split("|") if keyword.strip()]
    return annotation_map


# ---------------------------------------------------------------------------
# 过滤与转换
# ---------------------------------------------------------------------------

def to_codepoint_string(codepoints):
    return " ".join(f"U+{cp:04X}" for cp in codepoints)


def build_entries(annotation_map, whitelist):
    rows = []
    skipped = 0
    for cp, info in annotation_map.items():
        codepoints = [ord(character) for character in cp]
        if normalize_sequence(codepoints) not in whitelist:
            skipped += 1
            continue
        rows.append((codepoints, {
            "cp": cp,
            "codepoint": to_codepoint_string(codepoints),
            "name": info["tts"],
            "keywords": info["keywords"],
            "description": "",
        }))
    rows.sort(key=lambda row: row[0])
    return [row[1] for row in rows], skipped


# ---------------------------------------------------------------------------
# LLM 描述生成（OpenAI 兼容 /chat/completions）
# ---------------------------------------------------------------------------

SYSTEM_PROMPT = """你是 emoji 语义检索数据集的构建助手。用户会提供一批 emoji 的中文名称（name）与关键词（keywords）。
请为每个 emoji 生成一段简短的中文自然语言描述，用于提升语义检索效果，要求：
1. 用 1~2 句话（约 40~80 字）概括该 emoji 的字面含义、表达的情绪或态度、典型使用场景；
2. 自然地融入与关键词相关的近义表达，但不要简单罗列关键词；
3. 不要描述画面细节，不要输出 emoji 字符本身，不要使用“这个表情”之类的空洞措辞；
4. 仅输出一个 JSON 对象，键必须严格使用给定 cp 字段中的 emoji 字符，值为对应的描述文本，不要输出任何其他内容。"""


def resolve_api_key():
    return (
        os.environ.get("LLM_API_KEY")
        or os.environ.get("DEEPSEEK_API_KEY")
        or os.environ.get("OPENAI_API_KEY")
        or ""
    )


def extract_content(data):
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(part.get("text", "") if isinstance(part, dict) else "" for part in content)
    return ""


def parse_json_object(text):
    stripped = str(text or "").strip()
    fence_match = re.search(r"```(?:json)?\s*(.*?)```",
                            stripped, re.IGNORECASE | re.DOTALL)
    if fence_match:
        stripped = fence_match.group(1).strip()
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("响应中未找到 JSON 对象")
    return json.loads(stripped[start:end + 1])


def request_batch(items, cfg):
    payload = {
        "model": cfg["model"],
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": json.dumps(
                    [
                        {"cp": entry["cp"], "name": entry["name"],
                            "keywords": entry["keywords"]}
                        for entry in items
                    ],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
            },
        ],
        "temperature": 0.5,
        "max_tokens": min(8192, max(1000, len(items) * 240 + 500)),
    }
    request = urllib.request.Request(
        f"{cfg['base_url']}/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {cfg['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            body = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {error.code} {error.reason} {detail}")
    parsed = parse_json_object(extract_content(json.loads(body)))
    result = {}
    for entry in items:
        description = parsed.get(entry["cp"])
        if isinstance(description, str) and description.strip():
            result[entry["cp"]] = description.strip()
    if not result:
        raise RuntimeError("响应中未解析出任何描述")
    return result


def request_batch_with_retry(items, cfg, max_attempts=3):
    last_error = None
    for attempt in range(1, max_attempts + 1):
        try:
            return request_batch(items, cfg)
        except Exception as error:  # noqa: BLE001
            last_error = error
            if attempt < max_attempts:
                time.sleep(2 ** (attempt - 1) + random.random() * 0.5)
    raise last_error


def load_description_cache():
    if not DESCRIPTION_CACHE_FILE.exists():
        return {}
    try:
        return json.loads(DESCRIPTION_CACHE_FILE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        print("  ! 描述缓存文件损坏，已忽略并重建", file=sys.stderr)
        return {}


def save_description_cache(cache):
    DESCRIPTION_CACHE_FILE.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def generate_descriptions(entries, options):
    cache = load_description_cache()
    for entry in entries:
        cached = cache.get(entry["cp"])
        if isinstance(cached, str) and cached.strip():
            entry["description"] = cached.strip()
    pending = [entry for entry in entries if not entry["description"]]
    print(f"  - {len(entries) - len(pending)} 条命中缓存，{len(pending)} 条待生成")
    if not pending:
        return

    api_key = resolve_api_key()
    if not api_key:
        raise RuntimeError(
            "未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY），无法生成 description；"
            "可配置后重试，或加 --skip-description 跳过描述生成"
        )
    cfg = {
        "api_key": api_key,
        "base_url": (os.environ.get("LLM_BASE_URL") or "https://api.deepseek.com/v1").rstrip("/"),
        "model": os.environ.get("LLM_MODEL") or "deepseek-flash",
    }
    print(f"  - 接口 {cfg['base_url']}，模型 {cfg['model']}")

    batches = [
        pending[index:index + options.batch_size]
        for index in range(0, len(pending), options.batch_size)
    ]
    done = 0
    failed = 0
    worker_count = min(options.concurrency, len(batches))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {executor.submit(
            request_batch_with_retry, batch, cfg): batch for batch in batches}
        for future in as_completed(futures):
            batch = futures[future]
            try:
                result = future.result()
                for entry in batch:
                    if result.get(entry["cp"]):
                        entry["description"] = result[entry["cp"]]
                        cache[entry["cp"]] = entry["description"]
                    else:
                        failed += 1
                save_description_cache(cache)  # 每个批次成功后即落盘，支持断点续传
            except Exception as error:  # noqa: BLE001
                failed += len(batch)
                print(
                    f"  ! 批次生成失败（{len(batch)} 条）: {describe_error(error)}", file=sys.stderr)
            done += len(batch)
            print(f"  - 描述生成进度 {done}/{len(pending)}")
    if failed:
        print(
            f"  ! {failed} 条描述生成失败（description 留空），重新运行脚本可基于缓存续传",
            file=sys.stderr,
        )


# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------

def resolve_output_dir():
    public_dir = SCRIPT_DIR.parent / "public"
    if public_dir.is_dir():
        return public_dir
    return SCRIPT_DIR  # 目录不存在时回退到脚本同级目录


def timestamp_now():
    return datetime.now().strftime("%Y_%m_%d_%H_%M")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main():
    options = parse_args(sys.argv[1:])
    if not options.skip_description and not resolve_api_key():
        raise RuntimeError(
            "未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY），无法生成 description；"
            "可配置后重试，或加 --skip-description 跳过描述生成"
        )
    CACHE_DIR.mkdir(parents=True, exist_ok=True)

    print("[1/5] 加载数据源 ...")
    zh_xml = load_source(SOURCES[0], options.refresh)
    seq_text = load_source(SOURCES[1], options.refresh)
    zwj_text = load_source(SOURCES[2], options.refresh)

    print("[2/5] 解析 RGI 白名单 ...")
    whitelist, line_count = parse_rgi_whitelist([seq_text, zwj_text])
    print(f"  - 序列条目 {line_count} 条，展开/归一化后共 {len(whitelist)} 个序列")

    print("[3/5] 解析 CLDR zh 注解 ...")
    annotation_map = parse_cldr_annotations(zh_xml)
    print(f"  - 共 {len(annotation_map)} 个 cp")

    print("[4/5] 按白名单过滤 ...")
    entries, skipped = build_entries(annotation_map, whitelist)
    if options.limit is not None:
        entries = entries[:options.limit]
    no_name = sum(1 for entry in entries if not entry["name"])
    message = f"  - 保留 {len(entries)} 条，过滤 {skipped} 条"
    if no_name:
        message += f"，其中 {no_name} 条无名称"
    print(message)

    print("[5/5] 生成语义描述 ...")
    if options.skip_description:
        print("  - 已按 --skip-description 跳过，description 留空")
    else:
        generate_descriptions(entries, options)

    out_path = resolve_output_dir() / f"emoji_{timestamp_now()}.json"
    out_path.write_text(json.dumps(
        entries, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"完成：{len(entries)} 条数据已写入 {out_path}")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:  # noqa: BLE001
        print(f"错误: {error}", file=sys.stderr)
        sys.exit(1)
