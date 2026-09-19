/**
 * Emoji 语义搜索 GUI（原生 ES module，无构建步骤）
 *
 * 后端接口：
 *   GET /api/search?q=&top_k=&mode=fusion|dense|sparse
 */

const MAX_RESULTS = 10;
const EXAMPLES = ["我想鼓励别人", "表达感谢", "钱", "生气", "生日快乐", "夜晚", "🚀"];

const dom = {
  form: document.getElementById("search-form"),
  input: document.getElementById("query"),
  submit: document.getElementById("submit"),
  examples: document.getElementById("examples"),
  meta: document.getElementById("meta"),
  results: document.getElementById("results"),
  toast: document.getElementById("toast"),
};

const state = { query: "", loading: false };

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

// ------------------------------------------------------------------ 状态渲染
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
  const count = 6;
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
  title.append(name);

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

  const actions = document.createElement("div");
  actions.className = "card__actions";
  actions.append(
    actionButton("复制 Emoji", () => copyText(item.cp, `已复制 ${item.cp}`)),
    actionButton("复制 Emoji + 名称", () => copyText(`${item.cp} ${item.name}`.trim(), "已复制 emoji 与名称")),
  );

  card.append(head);
  if (keywords.childElementCount > 0) card.append(keywords);
  card.append(description, actions);
  return card;
}

function renderResults(data) {
  const results = (data.results || []).slice(0, MAX_RESULTS);
  dom.results.replaceChildren();

  if (results.length === 0) {
    renderEmpty("没有匹配的 emoji，试试换一种说法，或改用更具体的关键词。");
    setMeta("没有找到匹配的 emoji");
  } else {
    results.forEach((item) => dom.results.append(createCard(item)));
    setMeta(`共 <strong>${results.length}</strong> 个结果`);
  }
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
    const url = `/api/search?q=${encodeURIComponent(text)}&top_k=${MAX_RESULTS}&mode=fusion`;
    const data = await fetchJSON(url);
    renderResults(data);
  } catch {
    renderEmpty("检索失败，请稍后重试。");
    setMeta("检索失败，请稍后重试。", true);
  } finally {
    setLoading(false);
  }
}

function resetResults() {
  renderEmpty("输入查询开始检索，或点击上方示例快速体验。");
  setMeta("当前未发起查询。");
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

function bindEvents() {
  dom.form.addEventListener("submit", (event) => {
    event.preventDefault();
    if (!state.loading) runSearch();
  });

  dom.input.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      dom.input.value = "";
      state.query = "";
      resetResults();
    }
  });
}

function init() {
  initExamples();
  bindEvents();
  resetResults();
  dom.input.focus();
}

init();
