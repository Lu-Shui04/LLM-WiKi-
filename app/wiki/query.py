"""查询：读 Wiki 页面，基于页面回答，标注引用。

    python -m app.wiki query "讲一下你的项目"
    python -m app.wiki query "…" --show-pages     只列出会喂给模型的页面

**默认全量页面进上下文，不做 LLM 选页。**
理由见 SCHEMA.md 第十节：选页恰恰是又一次「判断准不准」的问题，
这套知识库原来的 RAG 就栽在这上面。全给还省掉一次往返，首字更快，
问「意图路由和 RAG 的区别」这类跨页问题时也不会因为选页失误缺一半料。

页面数超过 `wiki_inline_max_pages` 时才退化成代码打分预筛（字符 n-gram），
仍然是确定性的、可单测的，**不碰向量检索**。
"""
import asyncio
import re
import sys
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from app.config import settings
from app.core import prompt
from app.core.llm import get_chat_model
from app.wiki import frontmatter, store

TOP_N = 8           # 打分预筛时取前几个


# ─── 选页 ────────────────────────────────────────────
def _ngrams(s: str, n: int = 2) -> set[str]:
    s = re.sub(r"\s+", "", s or "")
    return {s[i:i + n] for i in range(len(s) - n + 1)}


def _score(question: str, page: store.Page) -> float:
    """Jaccard 相似度，只用 title + tags + summary 这三个短字段。

    刻意只看元信息、不看正文：正文一进来，这段打分就又开始扮演「相关性裁决」，
    而那正是要摆脱的东西。它在这里只负责粗筛，筛错了有 --pages 兜底。
    """
    title = str(page.meta.get("title", "")).strip()
    tags = " ".join(frontmatter.as_list(page.meta.get("tags")))
    summary = str(page.meta.get("summary", "")).strip()
    q, h = _ngrams(question), _ngrams(f"{title} {tags} {summary}")
    if not q or not h:
        return 0.0
    return len(q & h) / len(q | h)


def collect(question: str, pages: str | None = None) -> list[store.Page]:
    all_pages = store.read_all()
    if not all_pages:
        return []

    if pages:
        want = {x.strip() for x in pages.split(",") if x.strip()}
        picked = [
            p for p in all_pages
            if p.rel in want
            or Path(p.rel).stem in want
            or str(p.meta.get("title", "")).strip() in want
        ]
        return picked or all_pages          # 指定的都找不到就退回全量，别给空上下文

    if len(all_pages) <= settings.wiki_inline_max_pages:
        return all_pages

    return sorted(all_pages, key=lambda p: -_score(question, p))[:TOP_N]


def build_context(pages: list[store.Page]) -> str:
    parts: list[str] = []
    for p in pages:
        title = str(p.meta.get("title", "")).strip() or Path(p.rel).stem
        parts += [f"## {title}", f"（页面路径：{p.rel}）", "", p.body.strip(), "", "---", ""]
    return "\n".join(parts)


# ─── 回答 ────────────────────────────────────────────
def _text_of(chunk) -> str:
    c = getattr(chunk, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in c)
    return ""


async def _answer(question: str, pages: list[store.Page]) -> int:
    system = prompt.load("wiki_qa")
    user = f"# Wiki 页面\n\n{build_context(pages)}\n# 问题\n\n{question}"

    print(f"（读入 {len(pages)} 个页面，模型 {settings.deepseek_model}）\n", flush=True)

    answer: list[str] = []
    async for chunk in get_chat_model(streaming=True).astream(
        [SystemMessage(content=system), HumanMessage(content=user)]
    ):
        piece = _text_of(chunk)
        if piece:
            answer.append(piece)
            print(piece, end="", flush=True)
    print()

    text = "".join(answer)
    cited = sorted({c for c in frontmatter.links(text)})
    if cited:
        print("\n引用：" + "、".join(f"[[{c}]]" for c in cited))
    return 0


def run(question: str, *, pages: str | None = None, show_pages: bool = False) -> int:
    q = (question or "").strip()
    if not q:
        print("问题不能为空", file=sys.stderr)
        return 1

    picked = collect(q, pages)
    if not picked:
        print("wiki/ 里还没有页面，先跑 ingest", file=sys.stderr)
        return 1

    if show_pages:
        for p in picked:
            print(p.rel)
        return 0

    return asyncio.run(_answer(q, picked))
