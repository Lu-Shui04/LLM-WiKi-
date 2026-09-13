"""FastAPI 入口。启动：uvicorn app.main:app --reload"""
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app.api import auth as auth_api
from app.api import wiki as wiki_api
from app.api.chat import router as chat_router
from app.config import settings
from app.db import sqlite, vectors
from app.wiki import store as wiki_store

WEB_DIR = Path(__file__).resolve().parent.parent / "web"


@asynccontextmanager
async def lifespan(_: FastAPI):
    await sqlite.init_db()          # 建表，幂等
    # 旧的向量库仍要初始化：chat_backend=legacy 时要用，而且它只是读本地
    # SQLite + numpy 索引，起一次几毫秒，不值得为省这点时间加一层分支。
    await vectors.init()
    kb = await vectors.stats()
    print(f"[startup] SQLite 就绪 {sqlite.DB_PATH}")
    print(f"[startup] 对话模型 {settings.deepseek_model}")
    if settings.chat_backend == "wiki":
        print(f"[startup] 知识后端 Wiki {len(wiki_store.read_all())} 页  目录 {settings.wiki_dir}")
    else:
        print(f"[startup] 知识后端 legacy 向量 {kb['chunks']} 块  目录 {settings.knowledge_dir}")
    yield


app = FastAPI(title="我的 Agent", lifespan=lifespan)
app.include_router(auth_api.router)
app.include_router(wiki_api.router)
app.include_router(chat_router)


@app.get("/health")
def health() -> dict:
    return {"ok": True, "model": settings.deepseek_model}


# 静态前端放最后挂，否则 "/" 会把 /api 和 /health 一起吞掉
if WEB_DIR.exists():
    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
