"""FastAPI 服务：检索 API + web/ 静态 GUI。

启动流程（lifespan）：
    索引自检（缺失/陈旧则自动重建） -> 预热一次检索 -> 对外服务

接口：
    GET /api/health                                服务与索引状态
    GET /api/search?q=&top_k=10&mode=fusion        双路召回 + RRF 融合检索
    GET /analytics.js                              前端统计脚本（百度统计，未配置站点 ID 时禁用）
    GET /                                           GUI 页面（web/ 静态资源）
"""
from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import config
from .engine import VALID_MODES, SearchEngine

logger = logging.getLogger(__name__)

# 页面图标：内联 SVG，避免额外静态资源
_FAVICON = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
    '<rect width="64" height="64" rx="14" fill="#4f46e5"/>'
    '<text x="32" y="44" font-size="34" text-anchor="middle">&#128269;</text></svg>'
)

# 百度统计站点 ID（hm.js? 后的 16~64 位十六进制）；未配置或非法时前端不加载统计脚本
_ANALYTICS_ID_RE = re.compile(r"[0-9a-fA-F]{16,64}")


def analytics_script(site_id: str) -> str:
    """生成 /analytics.js 内容：站点 ID 合法时注入百度统计，否则下发禁用开关。"""
    if not _ANALYTICS_ID_RE.fullmatch(site_id or ""):
        return "window.__ANALYTICS__ = { enabled: false };\n"
    return (
        "window.__ANALYTICS__ = { enabled: true };\n"
        "(function () {\n"
        "  var _hmt = (window._hmt = window._hmt || []);\n"
        "  var hm = document.createElement('script');\n"
        f"  hm.src = 'https://hm.baidu.com/hm.js?{site_id}';\n"
        "  hm.async = true;\n"
        "  (document.head || document.getElementsByTagName('head')[0]).appendChild(hm);\n"
        "})();\n"
    )


def create_app(auto_build: bool = True, warmup: bool = True) -> FastAPI:
    engine = SearchEngine()

    if config.BAIDU_ANALYTICS_ID and not _ANALYTICS_ID_RE.fullmatch(config.BAIDU_ANALYTICS_ID):
        logger.warning(
            "EMOJI_BAIDU_ANALYTICS_ID 格式不合法（应为 16~64 位十六进制），前端统计已禁用"
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        app.state.engine = engine
        app.state.error = None
        try:
            # 索引自检与构建属于 CPU/IO 密集操作，放到线程池避免阻塞事件循环
            await run_in_threadpool(engine.load_or_build, auto_build)
        except Exception as exc:  # noqa: BLE001 - 初始化失败不阻断启动，通过 /api/health 暴露
            app.state.error = f"{type(exc).__name__}: {exc}"
            logger.error("索引初始化失败：%s", exc)
        else:
            if warmup and engine.ready:
                try:
                    await run_in_threadpool(engine.search, "预热", 1, "fusion")
                except Exception as exc:  # noqa: BLE001
                    logger.warning("预热检索失败：%s", exc)
        yield

    app = FastAPI(
        title="Emoji 语义搜索",
        description="稠密（bge-base-zh-v1.5 + FAISS）与稀疏（BM25）双路召回，RRF 融合排序",
        version="1.0.0",
        lifespan=lifespan,
    )

    def get_engine() -> SearchEngine:
        return getattr(app.state, "engine", engine)

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon() -> Response:
        return Response(content=_FAVICON, media_type="image/svg+xml")

    @app.get("/analytics.js", include_in_schema=False)
    def analytics() -> Response:
        return Response(
            content=analytics_script(config.BAIDU_ANALYTICS_ID),
            media_type="application/javascript; charset=utf-8",
            headers={"Cache-Control": "no-cache"},
        )

    @app.get("/api/health", summary="服务与索引状态")
    def health() -> JSONResponse:
        info = get_engine().health()
        info["error"] = getattr(app.state, "error", None)
        status_code = 200 if info["status"] == "ready" else 503
        return JSONResponse(info, status_code=status_code)

    @app.get("/api/search", summary="emoji 语义检索")
    async def search(
        q: str = Query("", description="查询文本，支持自然语言 / 关键词 / emoji 字符 / U+XXXX 码点"),
        top_k: int = Query(config.DEFAULT_TOP_K, description=f"返回条数，服务端硬上限 {config.MAX_RESULTS}"),
        mode: str = Query("fusion", description="fusion（RRF 融合）| dense（仅稠密）| sparse（仅稀疏）"),
    ) -> dict:
        text = (q or "").strip()
        if not text:
            raise HTTPException(status_code=400, detail="查询词不能为空")
        if mode not in VALID_MODES:
            raise HTTPException(
                status_code=400, detail=f"mode 仅支持：{', '.join(VALID_MODES)}"
            )

        active = get_engine()
        if not active.ready:
            raise HTTPException(
                status_code=503,
                detail=getattr(app.state, "error", None) or "索引尚未就绪",
            )
        # 编码与召回是 CPU 密集型同步逻辑，交给线程池执行
        return await run_in_threadpool(active.search, text, top_k, mode)

    web_dir = Path(config.WEB_DIR)
    if web_dir.is_dir():
        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="web")
    else:
        logger.warning("未找到前端资源目录：%s（仅提供 API）", web_dir)

    return app


def run_server(host: str = "127.0.0.1", port: int = 8000, auto_build: bool = True) -> None:
    """启动 GUI 服务（阻塞）。"""
    import uvicorn

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s | %(message)s"
    )
    for noisy in ("httpx", "httpcore", "urllib3", "filelock", "huggingface_hub"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    app = create_app(auto_build=auto_build)
    logger.info("Emoji 语义搜索 GUI：http://%s:%s", host, port)
    uvicorn.run(app, host=host, port=port, log_level="info")
