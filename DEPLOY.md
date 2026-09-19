# 部署指南（Docker + 云服务器）

本项目以 Docker 容器方式部署到 Linux 服务器（已在阿里云 ECS / Alibaba Cloud Linux 4 / 2 核实例实测）。
镜像采用多阶段构建：构建期安装依赖、下载 bge 模型并预建索引；运行期零外网依赖、启动即用。

## 目标环境要求

| 项 | 要求 |
| --- | --- |
| 系统 | 主流 Linux（Ubuntu 22.04+ / Alibaba Cloud Linux 3+ / Debian 12+ 等） |
| 资源 | 内存 ≥2GB（torch + 模型常驻约 1~1.5GB）、磁盘 ≥40GB |
| Docker | 20.10+ 与 compose 插件（`dnf install -y docker docker-compose-plugin` 或 `apt install docker.io docker-compose-v2`） |
| 网络 | 首次构建需访问 PyPI 与 HuggingFace 镜像（Dockerfile 已内置阿里 PyPI 源与 hf-mirror） |

低内存实例（2GB 规格）建议加 2GB swap 防 OOM：

```bash
fallocate -l 2G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile
echo '/swapfile none swap sw 0 0' >> /etc/fstab
```

## 镜像设计要点

- 多阶段构建：Builder 安装依赖（torch 用 CPU 版，避免数 GB 的 CUDA 版本）、下载模型、构建索引；运行阶段仅携带 venv、模型与索引
- **层序优化**：`web/`（UI）作为独立层放在构建阶段的 `build-index` 之后——只改 UI 不会触发索引重建
- 运行阶段完全离线（`HF_HUB_OFFLINE=1`），模型与索引均随镜像分发

## 首次部署

**1. 准备服务器**：安装 Docker 与 compose 插件、按需加 swap（见上）。

**2. 同步代码**（本地执行，rsync 不可用时用 tar + scp）：

```bash
cd <项目目录>
COPYFILE_DISABLE=1 tar -czf /tmp/emoji-src.tgz .dockerignore .gitignore Dockerfile docker-compose.yml README.md requirements.txt setup.sh app tests web public scripts/readme.md scripts/build-emoji-data.py scripts/build-emoji-data.mjs
scp -i <密钥> /tmp/emoji-src.tgz <user>@<服务器IP>:/tmp/
ssh -i <密钥> <user>@<服务器IP> "mkdir -p /opt/emoji-semantic-search && tar -xzf /tmp/emoji-src.tgz -C /opt/emoji-semantic-search"
```

**3. 基础镜像源**（国内服务器直连 Docker Hub 会超时，用镜像代理拉取并打回官方标签）：

```bash
docker pull docker.m.daocloud.io/library/python:3.11-slim
docker tag docker.m.daocloud.io/library/python:3.11-slim python:3.11-slim
```

**4. 构建并启动**：

```bash
cd /opt/emoji-semantic-search
docker compose up -d --build
```

首次构建约 25~30 分钟（依赖下载 + 模型下载 + 索引构建；2 核经济型实例索引构建约 9 分钟）。

**5. 放行访问端口**：云安全组 / 防火墙放行入方向 TCP 8000，浏览器访问 `http://<服务器IP>:8000`。

## 日常更新流程

通用四步：本地修改验证 → 同步到服务器 → 重建重启 → 验证 `http://<服务器IP>:8000/api/health`。

> **低内存实例（≤2GB）注意**：构建进程（编码内存峰值约 1GB）与运行中的旧容器（常驻约 950MB）会同时占用内存，物理内存不足时触发 swap 抖动，可导致实例短暂假死（SSH/服务均无响应）。建议**先停容器再构建**：
>
> ```bash
> docker compose down && docker compose up -d --build
> ```
>
> 代价是构建期间服务不可用（2 核经济型实例约 10 分钟）。若构建中途实例无响应，等待构建跑完（内存释放后自动恢复）或在云控制台重启实例。

### 改 UI（`web/`）——约 1.5 分钟

```bash
scp -i <密钥> web/app.js <user>@<服务器IP>:/opt/emoji-semantic-search/web/   # 传改动文件（或按首次部署第 2 步全量同步）
ssh -i <密钥> <user>@<服务器IP> "cd /opt/emoji-semantic-search && docker compose up -d --build"
```

构建只重建 UI 层（秒级）；容器重启后约 1 分钟恢复（重新加载模型）。

### 改逻辑（`app/`）或数据（`public/`）——约 10 分钟

两者都会使 `build-index` 层缓存失效并重跑（2 核经济型实例编码 1606 条约 9 分钟），流程同上。

### 改依赖（`requirements.txt`）——约 15 分钟

pip 层与 build-index 层均重跑。

## 运维

```bash
docker ps                       # 容器状态（healthy）
docker logs -f emoji-search     # 实时日志
docker restart emoji-search     # 重启（约 1 分钟恢复）
docker compose up -d --build    # 重建并替换
```

服务已配置开机自启（Docker 服务 + `restart: unless-stopped`），服务器重启后自动恢复。

## 回滚

- **镜像回滚**：重建前备份当前标签，出问题时把 `docker-compose.yml` 的 `image:` 指回备份标签再 `docker compose up -d`：

  ```bash
  docker tag emoji-semantic-search:1.0 emoji-semantic-search:1.0-backup
  ```

- **代码回滚**：本地 `git` 检出旧版本后按更新流程重新部署。

## 常见问题

**拉取基础镜像超时**：国内直连 Docker Hub 不通，使用镜像代理（见"首次部署·第 3 步"）。可用性检测：`curl -so /dev/null -w '%{http_code}' https://<镜像站>/v2/`（401/200 表示可用，超时/403 不可用）。

**`build-index` 为什么慢**：编码 1606 条在 2 核经济型实例上约 9 分钟（本地 M1 约 80 秒）。只改 UI 不触发该步骤；改逻辑/数据/依赖时无法避免。

**端口访问不通排查顺序**：① 云安全组入方向是否放行 TCP 8000 → ② 服务器本地 `curl 127.0.0.1:8000/api/health` → ③ 服务器 `ss -tlnp | grep 8000` 确认监听 → ④ 在服务器上 `tcpdump -i any -n 'tcp port 8000'` 抓包，同时从外部发起请求，若抓不到 SYN 则包在到达服务器前被拦截（安全组/网络层）。

**构建期间实例无响应（SSH 与端口均超时）**：低内存实例上构建与运行容器双份内存占用导致 swap 抖动、系统短暂假死。等待构建完成即可自动恢复（或控制台重启）；后续构建按“日常更新流程”的提示先停容器再构建。
