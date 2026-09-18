# Emoji 语义搜索

基于 **稠密 + 稀疏双路召回** 的 emoji 语义检索服务：用自然语言描述需求，找到最贴切的 emoji。

```
查询 "我想鼓励别人"
   │
   ├─► 稠密召回：BAAI/bge-base-zh-v1.5 编码 + FAISS 内积检索（语义相似）   ─┐
   └─► 稀疏召回：rank_bm25 BM25Okapi（关键词字面命中）                      ─┴─► RRF 融合 ─► 前 10 条
```

## 技术选型

| 环节 | 方案 |
| --- | --- |
| 稠密向量 | `sentence-transformers` + `BAAI/bge-base-zh-v1.5`（768 维，查询侧加 BGE 中文指令前缀） |
| 稠密检索 | `FAISS` `IndexFlatIP`（向量 L2 归一化，内积等价余弦相似度） |
| 稀疏检索 | `rank_bm25` 的 `BM25Okapi`（k1=1.5, b=0.75），jieba 中文分词 |
| 融合排序 | **RRF**（Reciprocal Rank Fusion）：`score(d) = Σ_r w_r / (k + rank_r(d))`，k=60，双路等权 |
| 结果上限 | 硬约束 **10 条**（服务端钳制，前端最大只能选 10） |
| GUI | FastAPI 提供 API + 托管原生 HTML/CSS/JS 页面（无构建步骤） |

## 目录结构

```
emoji-semantic-search/
├── app/                 # 检索服务（python -m app 入口）
│   ├── config.py        # 全部可调参数（支持环境变量覆盖）
│   ├── data.py          # 读取 public/ 下最新数据文件，构造检索文档
│   ├── tokenize.py      # jieba 分词（缺失时回退字符 bigram）
│   ├── dense.py         # BGE 编码 + FAISS 索引
│   ├── sparse.py        # BM25 索引
│   ├── fusion.py        # RRF 融合
│   ├── engine.py        # 构建 / 加载 / 陈旧自检 / 检索编排
│   ├── server.py        # FastAPI 应用
│   └── __main__.py      # CLI：build-index / search / serve
├── web/                 # GUI（index.html + style.css + app.js）
├── tests/               # 零依赖 unittest（RRF 数值、分词、参数钳制）
├── public/              # 数据：emoji_*.json（由 scripts/ 抽取生成）
├── scripts/             # 数据抽取与清洗脚本（上游工程，独立于检索服务）
├── index/               # 索引产物（自动生成，已 gitignore）
└── models/              # HF 模型缓存（自动生成，已 gitignore）
```

## 初始数据准备

`public/emoji_*.json` 由 [scripts/](scripts/readme.md) 下的零依赖脚本生成：从 CLDR 中文注解 + Unicode RGI 白名单抽取、清洗条目，并调用 LLM 为每条生成中文语义描述。仓库已附带生成好的数据（1606 条，可直接跳过本节）；如需重新生成或更新数据（如跟进 CLDR / Unicode 版本），见 [scripts/readme.md](scripts/readme.md)。

## 快速开始

```bash
# 1) 创建虚拟环境并安装依赖（uv + Python 3.11）
bash setup.sh

# 2) 构建索引（首次会下载 bge 模型约 400MB，之后 1606 条约需十几秒）
.venv/bin/python -m app build-index

# 3) 启动 GUI
.venv/bin/python -m app serve
# 浏览器打开 http://127.0.0.1:8000
```

> Python 需 3.10~3.12：`torch` / `faiss-cpu` 尚未提供 3.13+ 的轮子。
> macOS 上 `faiss-cpu` 的 arm64 轮子要求系统 ≥ 14.0。

## 命令行

```bash
# 构建索引（--force 强制重建；--data 指定数据文件）
.venv/bin/python -m app build-index [--force] [--data public/emoji_xxx.json]

# 命令行检索（--json 输出原始结果，--mode 切换召回模式）
.venv/bin/python -m app search "我想鼓励别人"
.venv/bin/python -m app search "钱" --top-k 5 --mode fusion

# 启动 GUI（--no-auto-build 时索引缺失会直接报错而非自动重建）
.venv/bin/python -m app serve --host 127.0.0.1 --port 8000

# 单元测试
.venv/bin/python -m unittest discover tests -v
```

## HTTP API

| 接口 | 说明 |
| --- | --- |
| `GET /api/search?q=&top_k=10&mode=fusion` | 检索。`mode` 可选 `fusion` / `dense` / `sparse`；`top_k` 超过 10 会被钳制为 10 |
| `GET /api/health` | 服务与索引状态（条目数、模型、设备、构建时间）；索引未就绪返回 503 |
| `GET /` | GUI 页面 |

```bash
curl "http://127.0.0.1:8000/api/search?q=开心&top_k=10" | python3 -m json.tool
```

响应结构（节选）：

```json
{
  "query": "开心",
  "mode": "fusion",
  "top_k": 10,
  "elapsed_ms": 12.3,
  "recall_depth": 50,
  "recalled": { "dense": 50, "sparse": 50, "fused": 63 },
  "results": [
    {
      "rank": 1,
      "cp": "😄",
      "codepoint": "U+1F604",
      "name": "微笑的眼睛",
      "keywords": ["笑", "开心", "笑脸"],
      "description": "……",
      "rrf_score": 0.032522,
      "dense": { "rank": 1, "score": 0.8132 },
      "sparse": { "rank": 3, "score": 12.44 }
    }
  ]
}
```

## 索引与数据更新

- 索引落在 `index/`：`emoji.faiss`（FAISS）、`embeddings.npy`（向量）、`corpus_tokens.json`（BM25 语料 token）、`entries.json`（条目快照）、`manifest.json`（元信息）
- 服务启动时自检 `manifest.json`：数据文件新增/内容变化（sha256）、模型、查询指令、分词后端、RRF 参数、稠密下限、索引版本（`INDEX_VERSION`，索引内容构造方式变更时递增）任一不一致即自动重建；索引文件缺失或损坏同样自动重建
- 索引版本升级（如 v1 → v2 补充了码点语料）后首次启动会自动重建一次，属预期行为
- [scripts/](scripts/readme.md) 下的数据脚本生成新的 `public/emoji_*.json` 后，直接重启服务即可，无需手动重建

## 配置项（环境变量）

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `EMOJI_DATA_DIR` | `public` | 数据目录 |
| `EMOJI_INDEX_DIR` | `index` | 索引产物目录 |
| `EMOJI_EMBED_MODEL` | `BAAI/bge-base-zh-v1.5` | 稠密向量模型 |
| `EMOJI_QUERY_INSTRUCTION` | `为这个句子生成表示以用于检索相关文章：` | 查询侧指令前缀（文档侧不加） |
| `EMOJI_EMBED_DEVICE` | `auto` | `auto` 优先 CUDA/MPS，否则 CPU |
| `EMOJI_BATCH_SIZE` | `64` | 编码批大小 |
| `EMOJI_RECALL_DEPTH` | `50` | 每路召回深度 |
| `EMOJI_RRF_K` | `60` | RRF 平滑常数 |
| `EMOJI_RRF_W_DENSE` / `EMOJI_RRF_W_SPARSE` | `1.0` / `1.0` | 双路权重 |
| `EMOJI_DENSE_MIN_SCORE` | `0.30` | 稠密相似度下限，低于该分数的候选不参与融合（`0` 表示不过滤） |
| `EMOJI_BM25_MAX_DF_RATIO` | `0.5` | BM25 查询词剪枝阈值：文档频率超过该占比的词（如「的」「下」）不参与打分（`1.0` 表示不剪枝） |
| `EMOJI_TOP_K` | `10` | 默认展示条数（上限恒为 10） |
| `EMOJI_HF_ENDPOINT` | `https://hf-mirror.com` | 模型下载源（默认走镜像，兼容国内网络） |
| `EMOJI_MODEL_CACHE_DIR` | `models` | HF 缓存目录（`HF_HOME`） |

## 常见问题

**模型下载失败**：默认使用 `hf-mirror.com` 镜像；如你所在的网络可直连 HuggingFace，设置 `HF_ENDPOINT=https://huggingface.co` 后重跑构建即可；也可手动将模型放入 `models/` 后设置 `EMOJI_EMBED_MODEL` 指向本地路径。

**依赖安装失败**：`setup.sh` 会在默认源失败时自动回退清华 PyPI 镜像；也可手动执行
`uv pip install --python .venv/bin/python -r requirements.txt --index-url https://pypi.tuna.tsinghua.edu.cn/simple`。

**为什么结果不超过 10 条**：需求规定最终展示数量上限为 10，`app/config.py` 的 `MAX_RESULTS` 为硬约束，服务端对 `top_k` 做钳制，前端下拉框最大 10。

**稀疏召回为什么过滤 0 分**：BM25 对无关键词重叠的文档给 0 分，若不剔除，无命中查询会返回一批无关结果。

**无关查询为什么返回空结果**（而非 10 条噪声）：三道相关性防线共同保证 —— BM25 只保留正分命中；文档频率超过 `EMOJI_BM25_MAX_DF_RATIO`（默认 0.5）的无区分度查询词被剪枝；稠密相似度低于 `EMOJI_DENSE_MIN_SCORE`（默认 0.30）的候选被剔除。实测本数据集相关查询 top 得分约 0.40~0.55、无关查询约 0.20~0.36，0.30 为保守下限；若你的查询偏短导致误杀，可调低该值（会被 manifest 记录并触发重建）。

**想调双路权重**：设置 `EMOJI_RRF_W_DENSE` / `EMOJI_RRF_W_SPARSE` 后重建索引（权重参与索引 manifest 的陈旧判定，会自动重建）。

## 数据来源与许可

`public/emoji_*.json` 为衍生数据：名称（name）与关键词（keywords）来自 [Unicode CLDR](https://cldr.unicode.org/) 中文注解，条目范围由 [Unicode Emoji 序列数据](https://www.unicode.org/reports/tr51/)（RGI 白名单）确定，描述（description）由 LLM 生成。CLDR 与 Unicode 数据文件均采用 [Unicode License V3](https://www.unicode.org/license.txt)（宽松许可）：再分发或以其衍生形式发布时，需保留声明「Copyright © Unicode, Inc.，依 Unicode License V3 提供」（随数据副本或关联文档提供均可）。

- BAAI/bge-base-zh-v1.5 模型为 MIT 许可，首次构建索引时由使用者自行下载，不随本仓库分发
- 「Unicode」字标与徽标为 Unicode, Inc. 商标；本项目与其无隶属关系，不使用其徽标或暗示任何形式的背书
