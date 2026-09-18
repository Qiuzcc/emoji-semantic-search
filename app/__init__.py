"""emoji 语义搜索服务。

架构：
    数据层  public/emoji_*.json（由 scripts/build-emoji-data.py 抽取清洗）
    召回层  稠密 dense.py（BAAI/bge-base-zh-v1.5 + FAISS）
            稀疏 sparse.py（rank_bm25 BM25Okapi）
    融合层  fusion.py（RRF，Reciprocal Rank Fusion）
    服务层  server.py（FastAPI + web/ 原生前端 GUI）

说明：本包整体延迟导入重量级依赖（torch / faiss / sentence-transformers），
仅在实际使用时加载，因此单元测试与纯配置读取不需要安装全部依赖。
"""

__all__ = ["config", "data", "dense", "engine", "fusion", "server", "sparse", "tokenize"]
