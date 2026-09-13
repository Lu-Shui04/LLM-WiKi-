"""query 的选页逻辑。

只测确定性那部分——打 API 的那段不做自动化测试，靠 CLI 手动验证。
"""
from app.wiki import query
from app.wiki.store import Page


def _page(rel, title, type_="concept", summary="", tags=None):
    return Page(
        rel=rel,
        meta={"title": title, "type": type_, "summary": summary, "tags": tags or []},
        body=f"## 摘要\n\n{summary}\n",
    )


def _stub(monkeypatch, pages):
    monkeypatch.setattr(query.store, "read_all", lambda root=None: pages)


def test_没有页面时返回空(monkeypatch):
    _stub(monkeypatch, [])
    assert query.collect("随便问") == []


def test_页面不多时全量返回(monkeypatch):
    pages = [_page("concepts/甲.md", "甲"), _page("concepts/乙.md", "乙")]
    _stub(monkeypatch, pages)
    assert query.collect("随便问") == pages


def test_页面过多时按分数取前几个(monkeypatch):
    pages = [_page(f"concepts/页{i}.md", f"页{i}", summary="无关内容") for i in range(20)]
    hit = _page("concepts/意图路由.md", "意图路由", summary="意图分类与路由")
    _stub(monkeypatch, pages + [hit])

    got = query.collect("意图路由是怎么做的")
    assert len(got) <= query.TOP_N
    assert "concepts/意图路由.md" in [p.rel for p in got]


def test_手工指定页面(monkeypatch):
    pages = [_page("concepts/甲.md", "甲"), _page("concepts/乙.md", "乙")]
    _stub(monkeypatch, pages)
    assert [p.rel for p in query.collect("x", pages="甲")] == ["concepts/甲.md"]


def test_手工指定可以用标题(monkeypatch):
    _stub(monkeypatch, [_page("concepts/甲.md", "甲")])
    assert [p.rel for p in query.collect("x", pages="甲")] == ["concepts/甲.md"]


def test_指定的页面都不存在时退回全量(monkeypatch):
    pages = [_page("concepts/甲.md", "甲")]
    _stub(monkeypatch, pages)
    assert query.collect("x", pages="查无此页") == pages


def test_build_context_带标题与路径():
    ctx = query.build_context([_page("concepts/甲.md", "甲", summary="摘要")])
    assert "## 甲" in ctx
    assert "concepts/甲.md" in ctx


def test_打分在零到一之间():
    p = _page("concepts/甲.md", "意图路由", summary="意图分类与路由")
    for q in ("意图路由", "完全不相干的东西", ""):
        assert 0.0 <= query._score(q, p) <= 1.0


def test_完全不相干的问题得零分():
    p = _page("concepts/甲.md", "意图路由", summary="意图分类与路由")
    assert query._score("完全不相干的东西", p) == 0.0
