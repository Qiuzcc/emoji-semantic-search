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

### 一键部署（推荐）

本地执行 `scripts/deploy.sh` 自动完成上述四步，并按变更内容自动选择构建路径：

```bash
scripts/deploy.sh            # 自动检测变更并部署
scripts/deploy.sh --force    # 无变更时也强制重建
scripts/deploy.sh --dry-run  # 只预览将要执行的操作
scripts/deploy.sh --status   # 查看服务器、容器与服务状态
```

| 变更内容 | 构建路径 | 服务影响 |
| --- | --- | --- |
| `app/` `public/` `requirements.txt` `Dockerfile` 等 | 完整重建（先停容器） | 构建期间不可用（约 10~15 分钟，首次更久） |
| `web/` `docker-compose.yml` | 增量重建（在线构建） | 基本不中断（约 1~2 分钟） |
| README / 测试 / 脚本等其他文件 | 仅同步，跳过重建 | 无 |

脚本通过本机 `~/.ssh/config` 中的 SSH 别名连接服务器（默认 `aliyun`，可用环境变量 `SSH_HOST` 指向自己的别名，`REMOTE_DIR` / `SERVICE_URL` 亦可覆盖），部署后自动等待健康检查通过；未设置 `SERVICE_URL` 时经 SSH 在服务器本地检查 `127.0.0.1:8000/api/health`。

> **低内存实例（≤2GB）注意**：构建进程（编码内存峰值约 1GB）与运行中的旧容器（常驻约 950MB）会同时占用内存，物理内存不足时触发 swap 抖动，可导致实例短暂假死（SSH/服务均无响应）。建议**先停容器再构建**：
>
> ```bash
> docker compose down && docker compose up -d --build
> ```
>
> 代价是构建期间服务不可用（2 核经济型实例约 10 分钟）。若构建中途实例无响应，等待构建跑完（内存释放后自动恢复）或在云控制台重启实例。

以下为手工步骤（脚本不可用时参考）：

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

## 前端统计（可选）

UI 已接入百度统计（PV/UV 及检索、复制、GitHub 点击等交互事件）。站点 ID 通过环境变量注入，未配置时前端不加载任何统计脚本。

1. 在[百度统计](https://tongji.baidu.com)添加站点，从「代码获取」取得 `hm.js?` 后的站点 ID（32 位十六进制）；
2. 在服务器项目目录创建 `.env`（docker compose 自动读取；该文件不入库、不随部署覆盖）：

   ```bash
   ssh <user>@<服务器IP> "echo 'EMOJI_BAIDU_ANALYTICS_ID=<站点ID>' >> /opt/emoji-semantic-search/.env"
   ```

3. 重启容器生效（`docker compose up -d`），在服务器上验证：

   ```bash
   curl -s http://127.0.0.1:8000/analytics.js   # 应输出 enabled: true 并引用 hm.js?<站点ID>
   ```

数据在百度统计后台「访问分析」与「事件分析」查看（事件分类：`search` / `copy` / `engage`）。

## 运维

```bash
docker ps                       # 容器状态（healthy）
docker logs -f emoji-search     # 实时日志
docker restart emoji-search     # 重启（约 1 分钟恢复）
docker compose up -d --build    # 重建并替换
```

服务已配置开机自启（Docker 服务 + `restart: unless-stopped`），服务器重启后自动恢复。

## Nginx 反向代理（80 → 8000）

服务器已安装 Nginx 并将 80 端口请求反向代理到本服务（`127.0.0.1:8000`），配置文件 `/etc/nginx/conf.d/emoji-search.conf`：

- 访问方式：`http://<服务器IP>/`（等效于 `http://<服务器IP>:8000`）
- 相关命令：`nginx -t` 校验配置、`systemctl restart nginx` 重启、`tail -f /var/log/nginx/access.log` 查看访问日志
- 已启用开机自启（`systemctl enable nginx`）

> 443 HTTPS 尚未配置。后续如需启用：准备证书（自签名或域名 + Let's Encrypt）后，在 `conf.d/emoji-search.conf` 中新增 443 server 块并放行安全组。

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
