/**
 * Emoji 语义搜索 GUI（原生 ES module，无构建步骤）
 *
 * 后端接口：
 *   GET /api/search?q=&top_k=&mode=fusion|dense|sparse
 *   GET /api/health
 */

const MAX_RESULTS = 10;
const EXAMPLES = ["我想鼓励别人", "表达感谢", "钱", "生气", "生日快乐", "夜晚", "U+1F680", "🚀"];
const MODE_LABELS = { fusion: "RRF 融合", dense: "仅稠密", sparse: "仅稀疏" };

const dom = {
  form: document.getElementById("search-form"),
  input: document.getElementById("query"),
  submit: document.getElementById("submit"),
  clear: document.getElementById("clear"),
  examples: document.getElementById("examples"),
  topK: document.getElementById("top-k"),
  mode: document.getElementById("mode"),
  meta: document.getElementById("meta"),
  results: document.getElementById("results"),
  statusText: document.getElementById("status-text"),
  statusDot: document.querySelector("#status .dot"),
  toast: document.getElementById("toast"),
};

const state = { topK: MAX_RESULTS, mode: "fusion", query: "", loading: false };

// ------------------------------------------------------------------ 工具
async function fetchJSON(url) {
  const response = await fetch(url, { headers: { Accept: "application/json" } });
  const raw = await response.text();
  let data = null;
  try {
    data = raw ? JSON.parse(raw) : null;
  } catch {
    data = null;
  }
  if (!response.ok) {
    const detail = data && (data.detail || data.error || data.status);
    throw new Error(detail || `请求失败（HTTP ${response.status}）`);
  }
  return data;
}

async function copyText(text, message) {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
    } else {
      const helper = document.createElement("textarea");
      helper.value = text;
      helper.setAttribute("readonly", "");
      helper.style.position = "fixed";
      helper.style.opacity = "0";
      document.body.append(helper);
      helper.select();
      document.execCommand("copy");
      helper.remove();
    }
    toast(message);
  } catch {
    toast("复制失败，请手动选择复制");
  }
}

let toastTimer = null;
function toast(message) {
  dom.toast.textContent = message;
  dom.toast.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => {
    dom.toast.hidden = true;
  }, 1600);
}

function formatTime(iso) {
  if (!iso) return "未知";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, "0");
  return `${date.getMonth() + 1}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

// ------------------------------------------------------------------ 状态渲染
function setStatus(kind, text) {
  dom.statusDot.className = `dot dot--${kind}`;
  dom.statusText.textContent = text;
}

function setMeta(html, isError = false) {
  dom.meta.className = isError ? "meta meta--error" : "meta";
  dom.meta.innerHTML = html;
}

function setLoading(loading) {
  state.loading = loading;
  dom.submit.disabled = loading;
  dom.submit.textContent = loading ? "检索中…" : "搜索";
}

function renderSkeleton() {
  dom.results.replaceChildren();
  const count = Math.min(state.topK, 6);
  for (let i = 0; i < count; i += 1) {
    const skeleton = document.createElement("div");
    skeleton.className = "skeleton";
    dom.results.append(skeleton);
  }
}

function renderEmpty(html) {
  dom.results.replaceChildren();
  const box = document.createElement("div");
  box.className = "empty";
  box.textContent = html;
  dom.results.append(box);
}

// ------------------------------------------------------------------ 结果卡片
function badge(kind, label, info) {
  if (!info) {
    const missing = document.createElement("span");
    missing.className = "badge badge--muted";
    missing.textContent = `${label} 未召回`;
    return missing;
  }
  const el = document.createElement("span");
  el.className = `badge badge--${kind}`;
  el.textContent = `${label} #${info.rank} · ${info.score}`;
  return el;
}

function rrfBadge(score) {
  const el = document.createElement("span");
  if (score === null || score === undefined) {
    el.className = "badge badge--muted";
    el.textContent = "RRF -";
    return el;
  }
  el.className = "badge badge--rrf";
  el.textContent = `RRF ${Number(score).toFixed(5)}`;
  return el;
}

function actionButton(label, handler) {
  const button = document.createElement("button");
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", handler);
  return button;
}

function createCard(item) {
  const card = document.createElement("article");
  card.className = item.rank === 1 ? "card card--top1" : "card";

  const head = document.createElement("div");
  head.className = "card__head";

  const emoji = document.createElement("button");
  emoji.type = "button";
  emoji.className = "card__emoji";
  emoji.textContent = item.cp || "?";
  emoji.title = "点击复制 emoji";
  emoji.addEventListener("click", () => copyText(item.cp, `已复制 ${item.cp}`));

  const title = document.createElement("div");
  title.className = "card__title";
  const name = document.createElement("h3");
  name.className = "card__name";
  name.textContent = item.name || "(未命名)";
  const codepoint = document.createElement("code");
  codepoint.className = "card__codepoint";
  codepoint.textContent = item.codepoint || "";
  title.append(name, codepoint);

  const rank = document.createElement("span");
  rank.className = "card__rank";
  rank.textContent = `#${item.rank}`;

  head.append(emoji, title, rank);

  const keywords = document.createElement("ul");
  keywords.className = "card__keywords";
  (item.keywords || []).slice(0, 8).forEach((keyword) => {
    const li = document.createElement("li");
    li.textContent = keyword;
    keywords.append(li);
  });

  const description = document.createElement("p");
  description.className = "card__desc";
  description.textContent = item.description || "";

  const scores = document.createElement("div");
  scores.className = "card__scores";
  scores.append(
    badge("dense", "Dense", item.dense),
    badge("sparse", "BM25", item.sparse),
    rrfBadge(item.rrf_score),
  );

  const actions = document.createElement("div");
  actions.className = "card__actions";
  actions.append(
    actionButton("复制 Emoji", () => copyText(item.cp, `已复制 ${item.cp}`)),
    actionButton("复制 Emoji + 名称", () => copyText(`${item.cp} ${item.name}`.trim(), "已复制 emoji 与名称")),
  );

  card.append(head);
  if (keywords.childElementCount > 0) card.append(keywords);
  card.append(description, scores, actions);
  return card;
}

function renderResults(data) {
  const results = data.results || [];
  dom.results.replaceChildren();

  if (results.length === 0) {
    renderEmpty("没有匹配的 emoji，试试换一种说法，或改用更具体的关键词。");
  } else {
    results.slice(0, MAX_RESULTS).forEach((item) => dom.results.append(createCard(item)));
  }

  const recalled = data.recalled || {};
  const modeLabel = MODE_LABELS[data.mode] || data.mode;
  setMeta(
    `展示 <strong>${results.length}</strong> 条（上限 ${MAX_RESULTS}） · ` +
      `耗时 <strong>${data.elapsed_ms}</strong> ms · 模式：${modeLabel} · ` +
      `每路召回 ${data.recall_depth} 条（稠密 ${recalled.dense ?? 0} / 稀疏 ${recalled.sparse ?? 0} / ` +
      `融合去重 ${recalled.fused ?? 0}）`,
  );
}

// ------------------------------------------------------------------ 检索
async function runSearch(rawQuery) {
  const text = String(rawQuery ?? dom.input.value).trim();
  if (!text) {
    dom.input.focus();
    toast("请输入查询内容");
    return;
  }
  state.query = text;
  dom.input.value = text;
  setLoading(true);
  renderSkeleton();
  setMeta("检索中…");

  try {
    const url = `/api/search?q=${encodeURIComponent(text)}&top_k=${state.topK}&mode=${state.mode}`;
    const data = await fetchJSON(url);
    renderResults(data);
  } catch (error) {
    renderEmpty(`检索失败：${error.message}`);
    setMeta(`检索失败：${error.message}`, true);
  } finally {
    setLoading(false);
  }
}

function resetResults() {
  renderEmpty("输入查询开始检索，或点击上方示例快速体验。");
  setMeta("当前未发起查询。");
}

// ------------------------------------------------------------------ 健康状态
async function loadHealth() {
  try {
    const response = await fetch("/api/health", { headers: { Accept: "application/json" } });
    const data = await response.json().catch(() => null);
    if (response.ok && data && data.status === "ready") {
      setStatus(
        "ok",
        `${data.count} 条 emoji · ${data.embed_model} · ${data.device} · 索引构建于 ${formatTime(data.built_at)}`,
      );
      return true;
    }
    const detail = (data && (data.error || data.status)) || `HTTP ${response.status}`;
    setStatus("pending", `索引未就绪（${detail}），服务端可能正在自动构建，稍后重试…`);
    return false;
  } catch (error) {
    setStatus("error", `无法连接服务：${error.message}`);
    return false;
  }
}

async function pollHealth(attempt = 0) {
  const ready = await loadHealth();
  if (!ready && attempt < 60) {
    setTimeout(() => pollHealth(attempt + 1), 3000);
  }
}

// ------------------------------------------------------------------ 初始化
function initExamples() {
  EXAMPLES.forEach((text) => {
    const chip = document.createElement("button");
    chip.type = "button";
    chip.className = "chip";
    chip.textContent = text;
    chip.addEventListener("click", () => runSearch(text));
    dom.examples.append(chip);
  });
}

function initTopK() {
  for (let value = 1; value <= MAX_RESULTS; value += 1) {
    const option = document.createElement("option");
    option.value = String(value);
    option.textContent = `${value} 条`;
    if (value === MAX_RESULTS) option.selected = true;
    dom.topK.append(option);
  }
}

function bindEvents() {
  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.loading) runSearch();
  });

  dom.clear.addEventListener("click", () => {
    dom.input.value = "";
    state.query = "";
    resetResults();
    dom.input.focus();
  });

  dom.input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dom.input.value = "";
      state.query = "";
      resetResults();
    }
  });

  dom.topK.addEventListener("change", () => {
    state.topK = Number(dom.topK.value) || MAX_RESULTS;
    if (state.query && !state.loading) runSearch(state.query);
  });

  dom.mode.addEventListener("click", (event) => {
    const button = event.target.closest(".segmented__item");
    if (!button) return;
    state.mode = button.dataset.mode;
    dom.mode.querySelectorAll(".segmented__item").forEach((item) => {
      item.classList.toggle("is-active", item === button);
    });
    if (state.query && !state.loading) runSearch(state.query);
  });
}

function init() {
  initExamples();
  initTopK();
  bindEvents();
  resetResults();
  pollHealth();
  dom.input.focus();
}

init();
