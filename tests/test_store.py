"""store 的两阶段提交与索引渲染。

全部用 tmp_path，不碰真实的 wiki/ 目录。
"""
import pytest

from app.wiki import store
from app.wiki.store import CommitError, Page


def _page(rel, title, type_, summary="一句话", body=None, **extra):
    meta = {"title": title, "type": type_, "summary": summary}
    meta.update(extra)
    return Page(rel=rel, meta=meta, body=body if body is not None else f"## 摘要\n\n{summary}\n")


def test_校验失败时磁盘零变化(tmp_path):
    writes = [
        _page("concepts/甲.md", "甲", "concept"),
        # 缺 type / summary，正文也空 —— 这一条会把整批拖下水
        Page(rel="concepts/乙.md", meta={"title": "乙"}, body=""),
    ]
    with pytest.raises(CommitError):
        store.commit(writes, root=tmp_path)
    assert list(tmp_path.rglob("*.md")) == []


def test_提交后写出页面与索引(tmp_path):
    store.commit([_page("concepts/甲.md", "甲", "concept")], root=tmp_path)
    assert (tmp_path / "concepts/甲.md").is_file()
    index = (tmp_path / "index.md").read_text(encoding="utf-8")
    assert "## 概念" in index
    assert "[甲](concepts/甲.md)" in index


def test_死链降级为纯文本(tmp_path):
    body = "## 摘要\n\n见 [[不存在的页]]。\n"
    store.commit([_page("concepts/甲.md", "甲", "concept", body=body)], root=tmp_path)
    text = (tmp_path / "concepts/甲.md").read_text(encoding="utf-8")
    assert "[[不存在的页]]" not in text
    assert "见 不存在的页。" in text


def test_指向已存在页面的链接保留(tmp_path):
    store.commit([_page("concepts/甲.md", "甲", "concept")], root=tmp_path)
    body = "## 摘要\n\n见 [[甲]]。\n"
    store.commit([_page("concepts/乙.md", "乙", "concept", body=body)], root=tmp_path)
    text = (tmp_path / "concepts/乙.md").read_text(encoding="utf-8")
    assert "[[甲]]" in text


def test_指向本批将创建页面的链接保留(tmp_path):
    body = "## 摘要\n\n见 [[乙]]。\n"
    writes = [
        _page("concepts/甲.md", "甲", "concept", body=body),
        _page("concepts/乙.md", "乙", "concept"),
    ]
    store.commit(writes, root=tmp_path)
    text = (tmp_path / "concepts/甲.md").read_text(encoding="utf-8")
    assert "[[乙]]" in text


def test_索引空库不崩(tmp_path):
    assert "还没有页面" in store.render_index([])


def test_索引按类型分组且顺序固定(tmp_path):
    pages = [
        _page("concepts/甲.md", "甲", "concept"),
        _page("summaries/我的简历.md", "我的简历", "summary"),
    ]
    text = store.render_index(pages)
    assert text.index("## 来源摘要") < text.index("## 概念")


def test_索引里的竖线被转义(tmp_path):
    pages = [_page("concepts/甲.md", "甲", "concept", summary="a|b")]
    assert "a\\|b" in store.render_index(pages)


def test_日志首次写带表头(tmp_path):
    store.append_log("## 2026-09-13 ingest 我的简历.md\n\n- 模型：x\n", root=tmp_path)
    text = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert text.startswith("# 操作日志")
    assert "ingest 我的简历.md" in text
    # 追加而非覆盖
    store.append_log("## 第二条\n", root=tmp_path)
    text = (tmp_path / "log.md").read_text(encoding="utf-8")
    assert text.count("# 操作日志") == 1
    assert "## 第二条" in text
