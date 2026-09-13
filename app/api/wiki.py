"""Wiki 的只读接口：给前端「点开来源看原文」和「翻知识库」用。

**只读。** wiki/ 的唯一写入者是 `python -m app.wiki ingest`——这条路要是也能写，
两阶段提交、死链降级、source_sha 增量判定这些保证就都被绕过去了。

按标题或相对路径找页面，都是拿 `store.read_all()` 读出来的页面去**比对**，
而不是把用户传的字符串拼进路径。`store.read_all()` 的结果天然只来自 wiki/ 内部，
所以 `../../.env` 这类输入连匹配的机会都没有，不必再写一遍路径白名单。
"""
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from app.api.auth import current_user
from app.api.wiki_chat import TYPE_LABEL
from app.wiki import frontmatter, store

router = APIRouter(prefix="/api/wiki", tags=["wiki"])


def _brief(p: store.Page) -> dict:
    rel = p.rel
    tags = frontmatter.as_list(p.meta.get("tags"))
    t = str(p.meta.get("type", "")).strip()
    return {
        "rel": rel,
        "doc": rel[:-3] if rel.endswith(".md") else rel,
        "title": str(p.meta.get("title", "")).strip() or Path(rel).stem,
        "type": t,
        "typeLabel": TYPE_LABEL.get(t, ""),
        "summary": str(p.meta.get("summary", "")).strip(),
        "tags": tags,
    }


def _match(pages: list[store.Page], rel: str, title: str) -> store.Page | None:
    if rel:
        want = rel if rel.endswith(".md") else f"{rel}.md"
        key = want.casefold()
        return next((p for p in pages if p.rel.casefold() == key), None)
    if title:
        key = title.strip()
        for p in pages:
            if str(p.meta.get("title", "")).strip() == key or Path(p.rel).stem == key:
                return p
    return None


@router.get("/pages")
async def pages(user: dict = Depends(current_user)) -> dict:
    """全部页面的目录。给人翻着看用，所以不带正文。"""
    return {"pages": [_brief(p) for p in store.read_all()]}


@router.get("/page")
async def page(
    rel: str = "",
    title: str = "",
    user: dict = Depends(current_user),
) -> dict:
    if not rel and not title:
        raise HTTPException(status_code=400, detail="要传 rel 或 title")

    found = _match(store.read_all(), rel, title)
    if found is None:
        raise HTTPException(status_code=404, detail=f"wiki 里没有这一页：{rel or title}")

    out = _brief(found)
    out["body"] = found.body.strip()
    out["updated"] = str(found.meta.get("updated", "")).strip()
    out["sources"] = frontmatter.as_list(found.meta.get("sources"))
    return out
