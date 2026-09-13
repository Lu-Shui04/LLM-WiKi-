"""Wiki 模式的对话流。事件协议和 chat.py 完全一致，换掉的只是「知识从哪来」。

旧的 RAG 链路每次提问都拿余弦分数猜哪些切片相关——相关性裁决发生在查询那一刻，
手上只有一个标量分数，而正负样本在分数上是重叠的（实测见 vectors.py）。这里改成
直接读编译好的页面：相关性在 ingest 时由模型读过全文定好了，查询时不再猜。
选页沿用的也是 `query.collect`（页面不多就全量注入，多了才用字符 n-gram 预筛），
**全程不碰向量库**。

事件多了一类 `cited`：答完才知道模型实际引了哪几页，单独发一条让前端把对应的
来源 chip 点亮。「读进去了 10 页」和「这 3 页真的支撑了这段回答」是两回事。
"""
from collections.abc import AsyncIterator
from pathlib import Path

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

from app.api.sse import StripMarks, chunk_text, scour, sse, stage
from app.config import settings
from app.core import prompt
from app.core.llm import get_chat_model
from app.core.session import Session
from app.db import sqlite
from app.wiki import frontmatter, query, store

MAX_CONTEXT = 20        # 发给模型的历史条数上限；DB 里保留全部

# 页面类型 → 中文标签。和 store.GROUP_ORDER 的用词保持一致，别处看着才不打架
TYPE_LABEL = {
    "summary": "摘要",
    "entity": "实体",
    "concept": "概念",
    "comparison": "对比",
    "query": "问答",
    "overview": "总览",
}

_ROLE_MAP = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}


def _title(p: store.Page) -> str:
    return str(p.meta.get("title", "")).strip() or Path(p.rel).stem


def _system(pages: list[store.Page]) -> str:
    """话术在 prompts/wiki_qa.md 里（mtime 热加载），这里只负责把页面拼上去。"""
    base = prompt.load("wiki_qa")
    if not pages:
        return (
            f"{base}\n\n# 知识库\n\n（wiki/ 里一个页面都没有，本次没有任何资料可依据。"
            f"请照实说明你手上没有资料，不要凭通用知识替他编。）"
        )
    return f"{base}\n\n# 知识库页面\n\n{query.build_context(pages)}"


def _sources(pages: list[store.Page]) -> dict:
    """喂给模型的页面清单。

    `doc` 是去掉 .md 的短路径（`concepts/意图路由`）而不是完整路径——
    前端 chip 宽度有限，全路径会把那一行撑开（这一条是上一轮就踩过的坑）。
    正文**不**放进事件：一页几 KB，十页就是几十 KB 塞进流里；前端点开时
    再走 /api/wiki/page 单独取。
    """
    items = []
    for i, p in enumerate(pages, 1):
        t = str(p.meta.get("type", "")).strip()
        items.append({
            "n": i,
            "rel": p.rel,
            "doc": p.rel[:-3] if p.rel.endswith(".md") else p.rel,
            "title": _title(p),
            "type": t,
            "typeLabel": TYPE_LABEL.get(t, ""),
        })
    return {"type": "sources", "searched": True, "items": items}


async def stream(session: Session, text: str) -> AsyncIterator[str]:
    yield sse({"type": "start", "model": settings.deepseek_model})

    # 读盘是同步的、只要几毫秒（十来个文件），不值得为它加一层 to_thread
    pages = query.collect(text)
    yield sse(_sources(pages))

    answer, error = "", None
    marks = StripMarks()
    yield sse(stage("compose", "组织回答…"))
    try:
        # 整段生成期间持锁：同一用户同时来两个请求会排队，不会读到同一份历史各自追加
        async with session.lock:
            session.history.append({"role": "user", "content": text})
            await sqlite.append_message(session.user_id, "user", text)

            messages = [SystemMessage(content=_system(pages))] + [
                _ROLE_MAP.get(m["role"], HumanMessage)(content=scour(m["content"]))
                for m in session.history[-MAX_CONTEXT:]
            ]
            async for chunk in get_chat_model(streaming=True).astream(messages):
                reason = chunk.additional_kwargs.get("reasoning_content")
                if reason:
                    yield sse({"type": "thinking", "text": reason})
                piece = chunk_text(chunk)
                if piece:
                    # 落盘存清洗后的文本。存原文的话历史里永远留着编号，
                    # 下一轮模型又能照着抄回来，就永远摘不干净了
                    visible = marks.feed(piece)
                    answer += visible
                    if visible:
                        yield sse({"type": "token", "text": visible})
            if tail := marks.flush():
                answer += tail
                yield sse({"type": "token", "text": tail})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    # 断开连接时生成器在 yield 处被关掉，走不到这里；正常情况下把回答落盘
    if answer:
        session.history.append({"role": "assistant", "content": answer})
        await sqlite.append_message(session.user_id, "assistant", answer)

        known = {_title(p) for p in pages} | {Path(p.rel).stem for p in pages}
        cited = sorted({c for c in frontmatter.links(answer) if c in known})
        if cited:
            yield sse({"type": "cited", "titles": cited})

    yield sse({"type": "error", "message": error} if error else {"type": "done"})
