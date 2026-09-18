#!/usr/bin/env bash
# 创建 .venv 并安装全部依赖（基于 uv；Python 3.11 —— torch / faiss 暂不支持 3.13+）
set -euo pipefail
cd "$(dirname "$0")"

PY_VERSION="${PY_VERSION:-3.11}"

if ! command -v uv >/dev/null 2>&1; then
  echo "未检测到 uv，请先安装：brew install uv" >&2
  exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "==> 创建虚拟环境 .venv（Python ${PY_VERSION}）"
  uv venv --python "${PY_VERSION}" .venv
else
  echo "==> 复用已有虚拟环境 .venv"
fi

echo "==> 安装依赖（首次下载 torch 等，约需数分钟）"
if ! uv pip install --python .venv/bin/python -r requirements.txt; then
  echo "==> 默认源安装失败，回退清华 PyPI 镜像重试" >&2
  uv pip install --python .venv/bin/python -r requirements.txt \
    --index-url https://pypi.tuna.tsinghua.edu.cn/simple
fi

cat <<'EOF'

==> 依赖安装完成，后续命令：
    .venv/bin/python -m app build-index     # 构建索引（首次会下载 bge 模型，约 400MB）
    .venv/bin/python -m app search "鼓励别人"
    .venv/bin/python -m app serve           # 启动 GUI：http://127.0.0.1:8000
EOF
