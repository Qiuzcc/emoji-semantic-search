# Emoji 语义搜索服务镜像
# 多阶段构建：构建期下载模型并预建索引（随镜像分发），运行期零外网依赖。
#
# 构建：docker compose build   （或 docker build -t emoji-semantic-search:1.0 .）
# 启动：docker compose up -d

# ---------------- 构建阶段：依赖 + 模型 + 索引 ----------------
FROM python:3.11-slim AS builder

# faiss-cpu 需要 OpenMP 运行时（libgomp1）
RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# 网络加速：PyPI 走阿里云镜像，模型走 hf-mirror（与项目默认配置一致）
ENV PIP_INDEX_URL=https://mirrors.aliyun.com/pypi/simple/ \
    PIP_TRUSTED_HOST=mirrors.aliyun.com \
    HF_ENDPOINT=https://hf-mirror.com \
    HF_HOME=/app/models \
    EMOJI_EMBED_DEVICE=cpu \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

# 先装 CPU 版 torch（避免拉取带 CUDA 的数 GB 版本），再装其余依赖
COPY requirements.txt .
RUN python -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir --upgrade pip \
    && /opt/venv/bin/pip install --no-cache-dir torch \
        --index-url https://download.pytorch.org/whl/cpu \
    && /opt/venv/bin/pip install --no-cache-dir -r requirements.txt

# 应用代码与数据（scripts/ 为数据构造工具，不参与服务运行）
COPY app/ app/
COPY web/ web/
COPY public/ public/

# 下载 bge 模型到 /app/models 并构建索引到 /app/index
RUN /opt/venv/bin/python -m app build-index

# ---------------- 运行阶段：仅携带运行所需产物 ----------------
FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends libgomp1 curl \
    && rm -rf /var/lib/apt/lists/*

ENV PATH="/opt/venv/bin:$PATH" \
    HF_HOME=/app/models \
    HF_HUB_OFFLINE=1 \
    EMOJI_EMBED_DEVICE=cpu \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /app /app

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=60s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8000/api/health || exit 1

CMD ["python", "-m", "app", "serve", "--host", "0.0.0.0", "--port", "8000"]
