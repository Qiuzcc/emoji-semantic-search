#!/usr/bin/env node
/**
 * emoji 数据抽取与清洗脚本（零依赖，Node.js >= 18）
 *
 * 数据流：
 *   1. 加载数据源（优先本地缓存 scripts/.cache/，缺失或 --refresh 时下载）：
 *      - CLDR common/annotations/zh.xml                         -> cp / keywords / tts
 *      - Unicode emoji-sequences.txt / emoji-zwj-sequences.txt  -> RGI 白名单
 *   2. 解析 RGI 白名单：区间码点展开；序列统一去掉 U+FE0F 归一化
 *      （zh.xml 文件头声明其 cp 已去除 U+FE0F，两侧归一化后即可精确比对）。
 *   3. 解析 zh.xml 的 <annotation>：type="tts" 为名称（name 取该值），
 *      其余为关键词（" | " 分隔）。
 *   4. 按白名单过滤，生成 codepoint（U+XXXX，多码点以空格分隔），并按码点排序。
 *   5. 通过 LLM（OpenAI 兼容 /chat/completions 接口，分批 + 并发 + 重试 +
 *      本地缓存可断点续传）依据 name / keywords 生成 description。
 *   6. 写出 emoji_yyyy_mm_dd_hh_mm.json：
 *      ../public/ 目录存在则写入该目录，否则写入脚本同级目录。
 *
 * 环境变量（生成 description 时必需）：
 *   LLM_API_KEY    API Key（缺省时依次回退读取 DEEPSEEK_API_KEY、OPENAI_API_KEY）；
 *                  未配置时须加 --skip-description 运行
 *   LLM_BASE_URL   接口地址，默认 https://api.deepseek.com/v1
 *   LLM_MODEL      模型名，默认 deepseek-flash
 *
 * 用法：node scripts/build-emoji-data.mjs [选项]
 *   --refresh              强制重新下载数据源（忽略本地缓存）
 *   --skip-description     跳过 LLM 描述生成，description 置空
 *   --limit <n>            仅处理前 n 条（便于快速验证）
 *   --batch-size <n>       每次 LLM 请求包含的条目数，默认 25
 *   --concurrency <n>      LLM 请求并发数，默认 4
 *   -h, --help             显示帮助
 */
import {
  existsSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const CACHE_DIR = join(SCRIPT_DIR, ".cache");

const SOURCES = [
  {
    name: "CLDR zh 注解 (zh.xml)",
    urls: [
      "https://raw.githubusercontent.com/unicode-org/cldr/main/common/annotations/zh.xml",
      // GitHub raw 在部分网络环境下不稳定，逐级回退备用地址
      "https://cdn.jsdelivr.net/gh/unicode-org/cldr@main/common/annotations/zh.xml",
    ],
    file: "cldr-zh-annotations.xml",
  },
  {
    name: "Unicode emoji-sequences.txt",
    urls: ["https://www.unicode.org/Public/emoji/latest/emoji-sequences.txt"],
    file: "unicode-emoji-sequences.txt",
  },
  {
    name: "Unicode emoji-zwj-sequences.txt",
    urls: [
      "https://www.unicode.org/Public/emoji/latest/emoji-zwj-sequences.txt",
    ],
    file: "unicode-emoji-zwj-sequences.txt",
  },
];

const DESCRIPTION_CACHE_FILE = join(CACHE_DIR, "descriptions.json");

// ---------------------------------------------------------------------------
// 命令行参数
// ---------------------------------------------------------------------------

function parseArgs(argv) {
  const opts = {
    refresh: false,
    skipDescription: false,
    limit: Infinity,
    batchSize: 25,
    concurrency: 4,
    help: false,
  };
  const needNumber = (value, flag) => {
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0)
      throw new Error(`参数 ${flag} 需要一个正数`);
    return n;
  };
  for (let i = 0; i < argv.length; i++) {
    const arg = argv[i];
    const next = () => {
      if (i + 1 >= argv.length) throw new Error(`参数 ${arg} 缺少取值`);
      return argv[++i];
    };
    switch (arg) {
      case "--refresh":
        opts.refresh = true;
        break;
      case "--skip-description":
        opts.skipDescription = true;
        break;
      case "--limit":
        opts.limit = needNumber(next(), arg);
        break;
      case "--batch-size":
        opts.batchSize = needNumber(next(), arg);
        break;
      case "--concurrency":
        opts.concurrency = needNumber(next(), arg);
        break;
      case "-h":
      case "--help":
        opts.help = true;
        break;
      default:
        throw new Error(`未知参数: ${arg}（使用 --help 查看用法）`);
    }
  }
  return opts;
}

function printHelp() {
  console.log(`用法: node scripts/build-emoji-data.mjs [选项]

选项:
  --refresh              强制重新下载数据源（忽略本地缓存）
  --skip-description     跳过 LLM 描述生成，description 置空
  --limit <n>            仅处理前 n 条（便于快速验证）
  --batch-size <n>       每次 LLM 请求包含的条目数，默认 25
  --concurrency <n>      LLM 请求并发数，默认 4
  -h, --help             显示帮助

环境变量 (生成 description 时必需):
  LLM_API_KEY    API Key（缺省时依次回退读取 DEEPSEEK_API_KEY、OPENAI_API_KEY）
  LLM_BASE_URL   接口地址，默认 https://api.deepseek.com/v1
  LLM_MODEL      模型名，默认 deepseek-flash

输出:
  ../public/ 目录存在则写入该目录，否则写入脚本同级目录；
  文件名为 emoji_yyyy_mm_dd_hh_mm.json`);
}

// ---------------------------------------------------------------------------
// 数据源加载
// ---------------------------------------------------------------------------

function formatSize(bytes) {
  return bytes >= 1024 ? `${(bytes / 1024).toFixed(1)} KB` : `${bytes} B`;
}

function describeError(error) {
  const cause = error?.cause?.message || error?.cause?.code;
  return cause ? `${error.message} (${cause})` : error.message;
}

async function loadSource(source, refresh) {
  const cachePath = join(CACHE_DIR, source.file);
  if (!refresh && existsSync(cachePath) && statSync(cachePath).size > 0) {
    const text = readFileSync(cachePath, "utf8");
    console.log(
      `  - ${source.name}: 本地缓存 (${formatSize(Buffer.byteLength(text))})`,
    );
    return text;
  }
  let lastError;
  for (let i = 0; i < source.urls.length; i++) {
    const url = source.urls[i];
    for (let attempt = 1; attempt <= 3; attempt++) {
      try {
        const res = await fetch(url, { redirect: "follow" });
        if (!res.ok) {
          const error = new Error(`HTTP ${res.status} ${res.statusText}`);
          error.noRetry = res.status < 500 && res.status !== 429; // 4xx 无需重试，换备用地址
          throw error;
        }
        const text = await res.text();
        if (!text.trim()) throw new Error("下载内容为空");
        writeFileSync(cachePath, text);
        console.log(
          `  - ${source.name}: 已下载 (${formatSize(Buffer.byteLength(text))})`,
        );
        return text;
      } catch (error) {
        lastError = error;
        if (error.noRetry) break;
        if (attempt < 3) await sleep(1000 * attempt);
      }
    }
    if (i < source.urls.length - 1) {
      console.warn(
        `  ! ${source.name} 下载失败，尝试备用地址: ${describeError(lastError)}`,
      );
    }
  }
  throw new Error(
    `无法获取 ${source.name}（${describeError(lastError)}），请检查网络后重试`,
  );
}

// ---------------------------------------------------------------------------
// RGI 白名单解析
// ---------------------------------------------------------------------------

/** 序列归一化：去除 U+FE0F 后输出 "1F468 200D 2764" 形式的大写十六进制串 */
function normalizeSequence(codepoints) {
  return codepoints
    .filter((cp) => cp !== 0xfe0f)
    .map((cp) => cp.toString(16).toUpperCase())
    .join(" ");
}

/** 展开 "1F600..1F64F" 区间或 "1F600 200D xxx" 单序列，返回归一化后的序列 key 数组 */
function expandCodepointField(field) {
  const rangeMatch = field.match(/^([0-9A-Fa-f]+)\.\.([0-9A-Fa-f]+)$/);
  if (rangeMatch) {
    const start = parseInt(rangeMatch[1], 16);
    const end = parseInt(rangeMatch[2], 16);
    const keys = [];
    for (let cp = start; cp <= end; cp++) keys.push(normalizeSequence([cp]));
    return keys;
  }
  return [
    normalizeSequence(field.split(/\s+/).map((hex) => parseInt(hex, 16))),
  ];
}

function parseRgiWhitelist(...texts) {
  const whitelist = new Set();
  let lineCount = 0;
  for (const text of texts) {
    for (const rawLine of text.split("\n")) {
      const line = rawLine.trim();
      if (!line || line.startsWith("#")) continue;
      const body = line.split("#")[0].trim(); // 去掉行尾注释
      if (!body) continue;
      const [cpField, typeField] = body.split(";").map((part) => part.trim());
      if (!cpField || !typeField) continue;
      for (const key of expandCodepointField(cpField)) whitelist.add(key);
      lineCount++;
    }
  }
  return { whitelist, lineCount };
}

// ---------------------------------------------------------------------------
// CLDR zh.xml 解析
// ---------------------------------------------------------------------------

const XML_ENTITIES = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'" };

function unescapeXml(text) {
  return text.replace(/&(#x?[0-9A-Fa-f]+|[a-z]+);/g, (whole, body) => {
    if (body.startsWith("#")) {
      const hex = body[1] === "x" || body[1] === "X";
      return String.fromCodePoint(
        parseInt(hex ? body.slice(2) : body.slice(1), hex ? 16 : 10),
      );
    }
    return XML_ENTITIES[body] ?? whole;
  });
}

/** 返回 Map<cp, { keywords: string[], tts: string }>，保持文件中的出现顺序 */
function parseCldrAnnotations(xmlText) {
  const map = new Map();
  const re = /<annotation\b([^>]*?)>([\s\S]*?)<\/annotation>/g;
  let match;
  while ((match = re.exec(xmlText)) !== null) {
    const attrs = match[1];
    const cpMatch = attrs.match(/\bcp="([^"]*)"/);
    if (!cpMatch) continue;
    const cp = unescapeXml(cpMatch[1]);
    const typeMatch = attrs.match(/\btype="([^"]*)"/);
    const value = unescapeXml(match[2]).trim();
    let item = map.get(cp);
    if (!item) {
      item = { keywords: [], tts: "" };
      map.set(cp, item);
    }
    if (typeMatch && typeMatch[1] === "tts") {
      item.tts = value;
    } else {
      item.keywords = value
        .split("|")
        .map((keyword) => keyword.trim())
        .filter(Boolean);
    }
  }
  return map;
}

// ---------------------------------------------------------------------------
// 过滤与转换
// ---------------------------------------------------------------------------

function toCodepointString(codepoints) {
  return codepoints
    .map((cp) => `U+${cp.toString(16).toUpperCase().padStart(4, "0")}`)
    .join(" ");
}

function compareNumberArrays(a, b) {
  const len = Math.min(a.length, b.length);
  for (let i = 0; i < len; i++) {
    if (a[i] !== b[i]) return a[i] - b[i];
  }
  return a.length - b.length;
}

function buildEntries(annotationMap, whitelist) {
  const rows = [];
  let skipped = 0;
  for (const [cp, info] of annotationMap) {
    const codepoints = Array.from(cp, (ch) => ch.codePointAt(0));
    if (!whitelist.has(normalizeSequence(codepoints))) {
      skipped++;
      continue;
    }
    rows.push({
      codepoints,
      entry: {
        cp,
        codepoint: toCodepointString(codepoints),
        name: info.tts,
        keywords: info.keywords,
        description: "",
      },
    });
  }
  rows.sort((a, b) => compareNumberArrays(a.codepoints, b.codepoints));
  return { entries: rows.map((row) => row.entry), skipped };
}

// ---------------------------------------------------------------------------
// LLM 描述生成（OpenAI 兼容 /chat/completions）
// ---------------------------------------------------------------------------

const SYSTEM_PROMPT = `你是 emoji 语义检索数据集的构建助手。用户会提供一批 emoji 的中文名称（name）与关键词（keywords）。
请为每个 emoji 生成一段简短的中文自然语言描述，用于提升语义检索效果，要求：
1. 用 1~2 句话（约 40~80 字）概括该 emoji 的字面含义、表达的情绪或态度、典型使用场景；
2. 自然地融入与关键词相关的近义表达，但不要简单罗列关键词；
3. 不要描述画面细节，不要输出 emoji 字符本身，不要使用“这个表情”之类的空洞措辞；
4. 仅输出一个 JSON 对象，键必须严格使用给定 cp 字段中的 emoji 字符，值为对应的描述文本，不要输出任何其他内容。`;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

function extractContent(data) {
  const content = data?.choices?.[0]?.message?.content;
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => (typeof part?.text === "string" ? part.text : ""))
      .join("");
  }
  return "";
}

function parseJsonObject(text) {
  let t = String(text ?? "").trim();
  const fence = t.match(/```(?:json)?\s*([\s\S]*?)```/i);
  if (fence) t = fence[1].trim();
  const start = t.indexOf("{");
  const end = t.lastIndexOf("}");
  if (start === -1 || end <= start) throw new Error("响应中未找到 JSON 对象");
  return JSON.parse(t.slice(start, end + 1));
}

async function requestBatch(items, cfg) {
  const payload = {
    model: cfg.model,
    messages: [
      { role: "system", content: SYSTEM_PROMPT },
      {
        role: "user",
        content: JSON.stringify(
          items.map((entry) => ({
            cp: entry.cp,
            name: entry.name,
            keywords: entry.keywords,
          })),
        ),
      },
    ],
    temperature: 0.5,
    max_tokens: Math.min(8192, Math.max(1000, items.length * 240 + 500)),
  };
  const res = await fetch(`${cfg.baseUrl}/chat/completions`, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      authorization: `Bearer ${cfg.apiKey}`,
    },
    body: JSON.stringify(payload),
    signal: AbortSignal.timeout(120_000),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(
      `HTTP ${res.status} ${res.statusText} ${body.slice(0, 300)}`,
    );
  }
  const parsed = parseJsonObject(extractContent(await res.json()));
  const result = {};
  for (const entry of items) {
    const description = parsed[entry.cp];
    if (typeof description === "string" && description.trim()) {
      result[entry.cp] = description.trim();
    }
  }
  if (!Object.keys(result).length) throw new Error("响应中未解析出任何描述");
  return result;
}

async function requestBatchWithRetry(items, cfg, maxAttempts = 3) {
  let lastError;
  for (let attempt = 1; attempt <= maxAttempts; attempt++) {
    try {
      return await requestBatch(items, cfg);
    } catch (error) {
      lastError = error;
      if (attempt < maxAttempts) {
        await sleep(1000 * 2 ** (attempt - 1) + Math.random() * 500);
      }
    }
  }
  throw lastError;
}

function loadDescriptionCache() {
  if (!existsSync(DESCRIPTION_CACHE_FILE)) return {};
  try {
    return JSON.parse(readFileSync(DESCRIPTION_CACHE_FILE, "utf8"));
  } catch {
    console.warn("  ! 描述缓存文件损坏，已忽略并重建");
    return {};
  }
}

function saveDescriptionCache(cache) {
  writeFileSync(DESCRIPTION_CACHE_FILE, JSON.stringify(cache, null, 2) + "\n");
}

function resolveApiKey() {
  return (
    process.env.LLM_API_KEY ||
    process.env.DEEPSEEK_API_KEY ||
    process.env.OPENAI_API_KEY ||
    ""
  );
}

async function generateDescriptions(entries, opts) {
  const cache = loadDescriptionCache();
  for (const entry of entries) {
    const cached = cache[entry.cp];
    if (typeof cached === "string" && cached.trim())
      entry.description = cached.trim();
  }
  const pending = entries.filter((entry) => !entry.description);
  console.log(
    `  - ${entries.length - pending.length} 条命中缓存，${pending.length} 条待生成`,
  );
  if (!pending.length) return;

  const apiKey = resolveApiKey();
  if (!apiKey) {
    throw new Error(
      "未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY），无法生成 description；" +
        "可配置后重试，或加 --skip-description 跳过描述生成",
    );
  }
  const cfg = {
    apiKey,
    baseUrl: (
      process.env.LLM_BASE_URL || "https://api.deepseek.com/v1"
    ).replace(/\/+$/, ""),
    model: process.env.LLM_MODEL || "deepseek-flash",
  };
  console.log(`  - 接口 ${cfg.baseUrl}，模型 ${cfg.model}`);

  const batches = [];
  for (let i = 0; i < pending.length; i += opts.batchSize) {
    batches.push(pending.slice(i, i + opts.batchSize));
  }
  let done = 0;
  let failed = 0;
  const workerCount = Math.min(opts.concurrency, batches.length);
  const workers = Array.from({ length: workerCount }, async () => {
    while (batches.length) {
      const batch = batches.shift();
      try {
        const result = await requestBatchWithRetry(batch, cfg);
        for (const entry of batch) {
          if (result[entry.cp]) {
            entry.description = result[entry.cp];
            cache[entry.cp] = entry.description;
          } else {
            failed++;
          }
        }
        saveDescriptionCache(cache); // 每个批次成功后即落盘，支持断点续传
      } catch (error) {
        failed += batch.length;
        console.warn(
          `  ! 批次生成失败（${batch.length} 条）: ${error.message}`,
        );
      }
      done += batch.length;
      console.log(`  - 描述生成进度 ${done}/${pending.length}`);
    }
  });
  await Promise.all(workers);
  if (failed) {
    console.warn(
      `  ! ${failed} 条描述生成失败（description 留空），重新运行脚本可基于缓存续传`,
    );
  }
}

// ---------------------------------------------------------------------------
// 输出
// ---------------------------------------------------------------------------

function resolveOutputDir() {
  const publicDir = join(SCRIPT_DIR, "..", "public");
  try {
    if (statSync(publicDir).isDirectory()) return publicDir;
  } catch {
    // 目录不存在时回退到脚本同级目录
  }
  return SCRIPT_DIR;
}

function timestamp(date = new Date()) {
  const pad = (n) => String(n).padStart(2, "0");
  return [
    date.getFullYear(),
    pad(date.getMonth() + 1),
    pad(date.getDate()),
    pad(date.getHours()),
    pad(date.getMinutes()),
  ].join("_");
}

// ---------------------------------------------------------------------------
// 主流程
// ---------------------------------------------------------------------------

async function main() {
  const opts = parseArgs(process.argv.slice(2));
  if (opts.help) {
    printHelp();
    return;
  }
  if (!opts.skipDescription && !resolveApiKey()) {
    throw new Error(
      "未配置 LLM_API_KEY（或 DEEPSEEK_API_KEY / OPENAI_API_KEY），无法生成 description；" +
        "可配置后重试，或加 --skip-description 跳过描述生成",
    );
  }
  mkdirSync(CACHE_DIR, { recursive: true });

  console.log("[1/5] 加载数据源 ...");
  const [zhXml, seqText, zwjText] = await Promise.all(
    SOURCES.map((source) => loadSource(source, opts.refresh)),
  );

  console.log("[2/5] 解析 RGI 白名单 ...");
  const { whitelist, lineCount } = parseRgiWhitelist(seqText, zwjText);
  console.log(
    `  - 序列条目 ${lineCount} 条，展开/归一化后共 ${whitelist.size} 个序列`,
  );

  console.log("[3/5] 解析 CLDR zh 注解 ...");
  const annotationMap = parseCldrAnnotations(zhXml);
  console.log(`  - 共 ${annotationMap.size} 个 cp`);

  console.log("[4/5] 按白名单过滤 ...");
  let { entries, skipped } = buildEntries(annotationMap, whitelist);
  if (Number.isFinite(opts.limit)) entries = entries.slice(0, opts.limit);
  const noName = entries.filter((entry) => !entry.name).length;
  console.log(
    `  - 保留 ${entries.length} 条，过滤 ${skipped} 条` +
      (noName ? `，其中 ${noName} 条无名称` : ""),
  );

  console.log("[5/5] 生成语义描述 ...");
  if (opts.skipDescription) {
    console.log("  - 已按 --skip-description 跳过，description 留空");
  } else {
    await generateDescriptions(entries, opts);
  }

  const outPath = join(resolveOutputDir(), `emoji_${timestamp()}.json`);
  writeFileSync(outPath, JSON.stringify(entries, null, 2) + "\n");
  console.log(`完成：${entries.length} 条数据已写入 ${outPath}`);
}

main().catch((error) => {
  console.error(`错误: ${error.message}`);
  process.exitCode = 1;
});
