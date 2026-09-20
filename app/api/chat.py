"""对话接口：只做分派，不碰知识、不碰模型。

按 `settings.chat_backend` 把请求交给其中一条链路。两条链路的 SSE 事件协议
完全一致（start / sources / thinking / token / usage / cited / done / error），
所以前端不需要知道这一轮走的是哪条：

    "wiki"    app/api/wiki_chat.py      读编译好的页面（默认）
    "legacy"  app/legacy/rag_chat.py    旧的向量检索

实现都在各自模块里。这个文件只负责「历史会话怎么取」和「HTTP 怎么包」。
"""
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from app.api.auth import current_user
from app.config import settings
from app.core.session import manager

router = APIRouter(prefix="/api", tags=["chat"])

MAX_TEXT = 8000


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_TEXT)


@router.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(current_user)) -> StreamingResponse:
    # 函数内 import：两条链路都用 app.api.sse 里的助手，顶层导入会绕成一圈。
    # 每个进程只会走其中一个分支，这点导入开销可以忽略。
    backend = settings.chat_backend.strip().lower()
    if backend == "wiki":
        from app.api.wiki_chat import stream
    elif backend == "legacy":
        from app.legacy.rag_chat import stream
    else:
        # 拼错配置名时直接报错，而不是悄悄挑一条跑——
        # 那会表现成「知识库明明有内容却答不上来」，很难往配置上想。
        raise HTTPException(
            status_code=500,
            detail=f"chat_backend 只能是 wiki 或 legacy，当前是 {settings.chat_backend!r}",
        )

    session = await manager.get_session(user["id"], user["username"])
    return StreamingResponse(
        stream(session, req.text.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 别让反向代理把流缓冲成一次性返回
        },
    )
