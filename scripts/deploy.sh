#!/usr/bin/env bash
#
# 一键部署：本地源码 → ECS（同步 + 按变更智能重建 + 健康检查）
# 流程说明见 DEPLOY.md「日常更新流程」。
#
# 用法：
#   scripts/deploy.sh              自动检测变更并部署
#   scripts/deploy.sh --force      无变更时也强制重建
#   scripts/deploy.sh --dry-run    只显示将要执行的操作，不做修改（只读）
#   scripts/deploy.sh --status     查看服务器、容器与服务状态
#
# 环境变量：SSH_HOST（默认 aliyun，取自 ~/.ssh/config，需配置为可访问服务器的别名）
#           REMOTE_DIR（默认 /opt/emoji-semantic-search）
#           SERVICE_URL（健康检查地址；缺省时经 SSH 在服务器本地检查 127.0.0.1:8000）
#           HEALTH_TIMEOUT（健康检查等待上限秒数，默认 360）
set -euo pipefail

SSH_HOST="${SSH_HOST:-aliyun}"
REMOTE_DIR="${REMOTE_DIR:-/opt/emoji-semantic-search}"
SERVICE_URL="${SERVICE_URL:-}"
IMAGE="emoji-semantic-search:1.0"
HEALTH_TIMEOUT="${HEALTH_TIMEOUT:-360}"

SSH_OPTS=(-o BatchMode=yes -o ConnectTimeout=10 -o ServerAliveInterval=30 -o ServerAliveCountMax=6)
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# 同步清单（与 DEPLOY.md 一致；scripts/ 仅带数据构造脚本，不含 .cache/）
SYNC_PATHS=(
  .dockerignore .gitignore Dockerfile docker-compose.yml README.md requirements.txt setup.sh
  app tests web public
  scripts/readme.md scripts/build-emoji-data.py scripts/build-emoji-data.mjs
)
TAR_OPTS=(--no-xattrs --exclude '*__pycache__*' --exclude '*.pyc' --exclude '*.DS_Store' --exclude '*.pytest_cache*')

# 变更指纹分组：对文件内容流求 sha256，两端比对决定重建方式
HEAVY_PATHS=(Dockerfile .dockerignore requirements.txt app public)  # 影响 build-index 层 → 须先停容器
WEB_PATHS=(web)                                                     # UI 独立层 → 可在线构建
CFG_PATHS=(docker-compose.yml)                                      # 容器配置 → 重建收编

FP_FIND='find "$@" -type f ! -path "*__pycache__*" ! -path "*/.pytest_cache/*" ! -name "*.pyc" ! -name ".DS_Store" 2>/dev/null | LC_ALL=C sort | while IFS= read -r f; do cat "$f"; done'

say()  { printf '==> %s\n' "$*"; }
fail() { printf '错误：%s\n' "$*" >&2; exit 1; }

usage() {
  cat <<'EOF'
一键部署：本地源码 → ECS（同步 + 智能重建 + 健康检查）

用法：scripts/deploy.sh [选项]
  （无选项）     自动检测变更并部署
  -f, --force    无变更时也强制重建
  --dry-run      只显示将要执行的操作，不做修改
  --status       查看服务器、容器与服务状态
  -h, --help     显示本帮助

变更分级（自动判断）：
  app/ public/ requirements.txt Dockerfile 等 → 完整重建（先停容器，防低内存 OOM）
  web/ docker-compose.yml                    → 增量重建（在线构建，服务基本不中断）
  README/测试/脚本等其他文件                  → 仅同步，跳过重建

环境变量：SSH_HOST（默认 aliyun）、REMOTE_DIR（默认 /opt/emoji-semantic-search）、
         SERVICE_URL（健康检查地址，缺省时经 SSH 检查服务器本地 127.0.0.1:8000）、HEALTH_TIMEOUT（默认 360s）
EOF
}

# ---------- 基础能力 ----------

preflight() {
  local missing=() cmd
  for cmd in ssh tar shasum curl; do
    command -v "$cmd" >/dev/null 2>&1 || missing+=("$cmd")
  done
  [ "${#missing[@]}" -eq 0 ] || fail "缺少本地命令：${missing[*]}"
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" true 2>/dev/null \
    || fail "无法连接 ${SSH_HOST}，请检查 ~/.ssh/config 与密钥"
}

fp_local() { bash -c "$FP_FIND" _ "$@" | shasum -a 256 | awk '{print $1}'; }

fp_remote() {
  local args
  args=$(printf '%q ' "$@")
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
    "cd '$REMOTE_DIR' 2>/dev/null && bash -c '$FP_FIND' _ $args | sha256sum | cut -d' ' -f1" \
    || echo unavailable
}

sync_sources() {
  say "同步源码 → ${SSH_HOST}:${REMOTE_DIR}"
  COPYFILE_DISABLE=1 tar "${TAR_OPTS[@]}" -czf - -C "$ROOT" "${SYNC_PATHS[@]}" \
    | ssh "${SSH_OPTS[@]}" "$SSH_HOST" "mkdir -p '$REMOTE_DIR' && tar -xzf - -C '$REMOTE_DIR'"
}

remote_compose() {  # 在服务器项目目录执行命令（$1 为命令文本）
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" "cd '$REMOTE_DIR' && $1"
}

# ---------- 部署动作 ----------

deploy_heavy() {
  say "完整重建：先停旧容器释放内存（低内存实例约 10~15 分钟，构建期间服务不可用）"
  remote_compose "docker tag $IMAGE $IMAGE-backup 2>/dev/null || true; docker compose down && docker compose up -d --build"
}

deploy_light() {
  say "增量重建：在线构建，旧容器继续服务（预计 1~2 分钟）"
  remote_compose "docker tag $IMAGE $IMAGE-backup 2>/dev/null || true; docker compose up -d --build"
}

health_body() {  # 健康检查响应体；SERVICE_URL 未设置时经 SSH 在服务器本地获取
  if [ -n "$SERVICE_URL" ]; then
    curl -fsS --max-time 8 "$SERVICE_URL" 2>/dev/null
  else
    ssh "${SSH_OPTS[@]}" "$SSH_HOST" "curl -fsS --max-time 8 http://127.0.0.1:8000/api/health" \
      2>/dev/null
  fi
}

wait_ready() {  # 轮询健康检查直到 ready，$1 为超时秒数
  local timeout="${1:-$HEALTH_TIMEOUT}" elapsed=0
  printf '==> 等待服务就绪'
  while [ "$elapsed" -lt "$timeout" ]; do
    if health_body | grep -q '"status":"ready"'; then
      printf ' 就绪（%ss）\n' "$elapsed"
      return 0
    fi
    sleep 3
    elapsed=$((elapsed + 3))
    printf '.'
  done
  printf ' 超时（%ss）\n' "$timeout"
  return 1
}

show_status() {
  say "容器状态（${SSH_HOST}）"
  ssh "${SSH_OPTS[@]}" "$SSH_HOST" \
    "docker ps --filter name=emoji-search --format '  {{.Names}}  {{.Status}}  镜像 {{.Image}}'" \
    || fail "无法连接 ${SSH_HOST}"
  say "服务健康（${SERVICE_URL:-SSH 本地 127.0.0.1:8000}）"
  local body
  if body=$(health_body); then
    printf '  %s\n' "$body"
  else
    printf '  不可达或未就绪\n'
  fi
}

# ---------- 主流程 ----------

FORCE=0
DRY_RUN=0
DO_STATUS=0
for arg in "$@"; do
  case "$arg" in
    -f|--force) FORCE=1 ;;
    --dry-run)  DRY_RUN=1 ;;
    --status)   DO_STATUS=1 ;;
    -h|--help)  usage; exit 0 ;;
    *) fail "未知参数：$arg（-h 查看帮助）" ;;
  esac
done

if [ "$DO_STATUS" -eq 1 ]; then
  show_status
  exit 0
fi

preflight
say "部署目标：${SSH_HOST}:${REMOTE_DIR}"

heavy_diff=0
web_diff=0
cfg_diff=0
[ "$(fp_local "${HEAVY_PATHS[@]}")" = "$(fp_remote "${HEAVY_PATHS[@]}")" ] || heavy_diff=1
[ "$(fp_local "${WEB_PATHS[@]}")" = "$(fp_remote "${WEB_PATHS[@]}")" ] || web_diff=1
[ "$(fp_local "${CFG_PATHS[@]}")" = "$(fp_remote "${CFG_PATHS[@]}")" ] || cfg_diff=1

if [ "$heavy_diff" -eq 1 ]; then
  path=heavy
  say "检测到变更：逻辑/数据/依赖 → 完整重建（先停容器）"
elif [ "$web_diff" -eq 1 ] || [ "$cfg_diff" -eq 1 ]; then
  path=light
  say "检测到变更：UI/容器配置 → 增量重建（在线构建）"
else
  path=none
  say "未检测到构建相关变更"
fi

if [ "$FORCE" -eq 1 ] && [ "$path" != heavy ]; then
  path=light
  say "--force：强制执行增量重建"
fi

if [ "$DRY_RUN" -eq 1 ]; then
  say "计划执行（dry-run，未做任何修改）："
  printf '  1. 同步源码（%d 项）→ %s\n' "${#SYNC_PATHS[@]}" "${SSH_HOST}:${REMOTE_DIR}"
  case "$path" in
    heavy) printf '  2. 完整重建：docker compose down && docker compose up -d --build\n' ;;
    light) printf '  2. 增量重建：docker compose up -d --build（在线构建）\n' ;;
    none)  printf '  2. 跳过重建（无构建相关变更）\n' ;;
  esac
  printf '  3. 健康检查：%s\n' "${SERVICE_URL:-SSH 本地 127.0.0.1:8000}"
  exit 0
fi

sync_sources || fail "源码同步失败"

case "$path" in
  none)
    say "跳过重建（仅同步源码）"
    if ! wait_ready 60; then
      fail "服务当前不可用，可运行 scripts/deploy.sh --status 排查"
    fi
    say "部署完成：${SERVICE_URL:-服务已就绪}（总耗时 ${SECONDS}s）"
    exit 0
    ;;
  heavy)
    if ! deploy_heavy; then
      cat >&2 <<EOF

构建失败，服务当前处于停止状态。恢复方式二选一：
  1) 修复问题后重新部署：scripts/deploy.sh --force
  2) 用旧镜像立即恢复：ssh $SSH_HOST "cd $REMOTE_DIR && docker compose up -d"
     （构建失败不会覆盖 $IMAGE 标签；:1.0-backup 为本次构建前备份）
EOF
      exit 1
    fi
    ;;
  light)
    if ! deploy_light; then
      printf '构建失败：旧容器仍在运行，服务未受影响；请检查上方构建日志。\n' >&2
      exit 1
    fi
    ;;
esac

if ! wait_ready; then
  fail "健康检查超时：ssh $SSH_HOST \"docker logs --tail 50 emoji-search\" 查看日志"
fi

say "部署完成：${SERVICE_URL:-服务已就绪}（总耗时 ${SECONDS}s）"
