# 初始数据构造指南

本目录脚本用于构造 emoji 语义搜索的**初始数据**：从 CLDR 中文注解与 Unicode RGI 白名单抽取、清洗 emoji 条目，再调用 LLM 为每条生成中文语义描述，输出 `public/emoji_*.json` 供检索服务建索引使用。

## 脚本与运行环境

| 脚本 | 运行时要求 | 依赖 |
| --- | --- | --- |
| `build-emoji-data.py` | Python >= 3.8 | 仅标准库，零依赖 |
| `build-emoji-data.mjs` | Node.js >= 18 | 仅内置模块，零依赖 |

两个版本逻辑完全一致，且共享同一份 `.cache/` 缓存目录，用哪个运行都可以。

## 数据源与处理流程

```
CLDR zh.xml（中文名称 / 关键词）─┐
                                  ├─► RGI 白名单过滤 ─► LLM 生成描述 ─► emoji_yyyy_mm_dd_hh_mm.json
Unicode RGI 序列（emoji-sequences / zwj-sequences）─┘
```

1. **加载数据源**：优先使用 `.cache/` 本地缓存，缺失或加 `--refresh` 时下载（GitHub raw 失败自动回退 jsDelivr CDN，带重试）
2. **解析 RGI 白名单**：展开区间码点，序列统一去除 `U+FE0F` 归一化
3. **解析 CLDR `zh.xml`**：`type="tts"` 的注解作为名称（name），其余作为关键词（keywords）
4. **过滤与转换**：按白名单过滤非标准 emoji，生成 `codepoint`（`U+XXXX` 大写十六进制，多码点以空格分隔），按码点排序
5. **生成语义描述**：LLM 分批 + 并发 + 重试生成 `description`，结果逐批次写入缓存，可断点续传
6. **输出**：写入 `public/`（该目录存在时），文件名为 `emoji_yyyy_mm_dd_hh_mm.json`

## 环境变量（生成 description 时必需）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `LLM_API_KEY` | 无 | API Key；缺省时依次回退读取 `DEEPSEEK_API_KEY`、`OPENAI_API_KEY` |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` | OpenAI 兼容接口地址 |
| `LLM_MODEL` | `deepseek-flash` | 模型名 |

未配置任何 Key 时必须加 `--skip-description` 运行，否则脚本直接报错退出。

## 使用方法

```bash
# 完整生成（需要 LLM Key，export 其一即可）
export LLM_API_KEY="your-key"        # 或 DEEPSEEK_API_KEY / OPENAI_API_KEY

# Python 版（推荐）
python3 scripts/build-emoji-data.py

# Node.js 版（与 Python 版等价，共享缓存）
node scripts/build-emoji-data.mjs
```

常用示例：

```bash
# 快速验证：只处理前 20 条并跳过 LLM 描述
python3 scripts/build-emoji-data.py --limit 20 --skip-description

# 跟进 CLDR / Unicode 更新：强制重新下载数据源
python3 scripts/build-emoji-data.py --refresh

# 调整 LLM 吞吐（每请求 25 条、并发 4，即默认值）
python3 scripts/build-emoji-data.py --batch-size 25 --concurrency 4
```

命令行选项：

| 选项 | 默认 | 说明 |
| --- | --- | --- |
| `--refresh` | 关 | 强制重新下载数据源（忽略本地缓存） |
| `--skip-description` | 关 | 跳过 LLM 描述生成，`description` 置空 |
| `--limit n` | 不限 | 仅处理前 n 条（便于快速验证） |
| `--batch-size n` | `25` | 每次 LLM 请求包含的条目数 |
| `--concurrency n` | `4` | LLM 请求并发数 |
| `-h, --help` | - | 显示帮助 |

## 缓存与断点续传

`.cache/` 目录内容：

| 文件 | 内容 |
| --- | --- |
| `cldr-zh-annotations.xml` | CLDR 中文注解（数据源缓存） |
| `unicode-emoji-sequences.txt` | RGI 基础序列（数据源缓存） |
| `unicode-emoji-zwj-sequences.txt` | RGI ZWJ 序列（数据源缓存） |
| `descriptions.json` | LLM 描述缓存（cp -> 描述文本） |

- 每个批次成功后立即落盘 `descriptions.json`，中断后重跑会跳过已有描述继续生成
- 只想重新生成描述：删除 `descriptions.json` 或其中对应条目后重跑
- 只想更新数据源：加 `--refresh`（不影响描述缓存）

## 输出格式

条目按码点排序，字段结构：

```json
{
  "cp": "😄",
  "codepoint": "U+1F604",
  "name": "微笑的眼睛",
  "keywords": ["笑", "开心", "笑脸"],
  "description": "……"
}
```

## 与检索服务衔接

生成新的 `public/emoji_*.json` 后，重启检索服务即可：启动自检会检测到数据文件新增或内容变化（sha256），自动重建索引，无需手动操作。详见上级 [README](../README.md)。

## 常见问题

**数据源下载失败**：GitHub raw 不稳定的网络会自动回退 jsDelivr CDN 并重试 3 次；仍失败时检查网络（或代理）后重跑。已下载过的数据源在 `.cache/` 中，删除后才会重新下载。

**部分描述生成失败**：终端会提示失败条数（description 留空），重新运行脚本即可基于缓存续传，只有缺失条目会重新请求。

**输出落到了 `scripts/` 目录**：说明 `public/` 目录不存在，脚本回退输出到脚本同级目录；在项目根目录创建 `public/` 后重跑即可。
