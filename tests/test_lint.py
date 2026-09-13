"""lint 六项检查。

fixture wiki_small 里故意留了一个死链（订单Agent.md 的 [[不存在的页]]）
和一个孤立页（entities/速购AI客服系统.md 没人链）。
"""
from pathlib import Path

from app.wiki import lint

FIXTURES = Path(__file__).parent / "fixtures"
SMALL = FIXTURES / "wiki_small"


def _kinds(issues, level=None):
    return [i.kind for i in issues if level is None or i.level == level]


def _write(root: Path, rel: str, text: str) -> None:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


# ─── fixture ────────────────────────────────────────
def test_死链被报成_error():
    dead = [i for i in lint.run(SMALL) if i.kind == "dead-link"]
    assert len(dead) == 1
    assert dead[0].level == "error"
    assert "不存在的页" in dead[0].message


def test_孤立页被报出():
    orphan = [i for i in lint.run(SMALL) if i.kind == "orphan"]
    assert len(orphan) == 1
    assert orphan[0].rel == "entities/速购AI客服系统.md"


def test_fixture_除这两条外没有别的():
    assert sorted(_kinds(lint.run(SMALL))) == ["dead-link", "orphan"]


def test_fixture_退出码为一(capsys):
    assert lint.report(lint.run(SMALL)) == 1


# ─── 边界 ───────────────────────────────────────────
def test_空目录不崩():
    issues = lint.run(FIXTURES / "这个目录不存在")
    assert len(issues) == 1
    assert issues[0].kind == "empty"


def test_没有问题时退出码为零(capsys):
    assert lint.report([]) == 0
    assert "通过" in capsys.readouterr().out


# ─── 动态构造 ────────────────────────────────────────
def _page(title, type_="concept", summary="摘要", body="## 摘要\n\n正文\n", **meta):
    head = f"---\ntitle: {title}\ntype: {type_}\nsummary: {summary}\n"
    for k, v in meta.items():
        head += f"{k}: {v}\n"
    return head + "---\n\n" + body


def test_有要点却没来源_报_no_source(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page(
        "甲", sources="", body="## 摘要\n\n正文\n\n## 要点\n\n- 一条要点\n"))
    assert any(i.kind == "no-source" for i in lint.run(tmp_path))


def test_要点配来源_不报(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page(
        "甲", sources="[x.md]", body="## 摘要\n\n正文\n\n## 要点\n\n- 一条要点\n"))
    assert not any(i.kind == "no-source" for i in lint.run(tmp_path))


def test_contradictions_open_报错(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page("甲", sources="[x.md]", contradictions="open"))
    assert any(i.kind == "contradiction" and i.level == "error" for i in lint.run(tmp_path))


def test_有矛盾块却是none_报错(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page(
        "甲", sources="[x.md]", contradictions="none",
        body="## 摘要\n\n> [!WARNING] 矛盾\n> A 与 B 打架\n"))
    assert any(i.kind == "contradiction" and i.level == "error" for i in lint.run(tmp_path))


def test_同目录标题高度相似_报重复(tmp_path):
    for name in ("RAG切块", "RAG切块策略"):
        _write(tmp_path, f"concepts/{name}.md", _page(name, sources="[x.md]"))
    assert any(i.kind == "duplicate" for i in lint.run(tmp_path))


def test_不同目录同名_不算重复(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page("甲", sources="[x.md]"))
    _write(tmp_path, "entities/甲.md", _page("甲", "entity", sources="[x.md]"))
    assert not any(i.kind == "duplicate" for i in lint.run(tmp_path))


def test_摘要页与总览不算孤立(tmp_path):
    _write(tmp_path, "summaries/我的简历.md", _page(
        "我的简历", "summary", sources="[我的简历.md]"))
    _write(tmp_path, "overview.md", _page("总览", "overview", sources="[我的简历.md]"))
    assert not any(i.kind == "orphan" for i in lint.run(tmp_path))


def test_被链接的页面不算孤立(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page("甲", sources="[x.md]"))
    _write(tmp_path, "concepts/乙.md", _page(
        "乙", sources="[x.md]", body="## 摘要\n\n见 [[甲]]。\n"))
    orphans = {i.rel for i in lint.run(tmp_path) if i.kind == "orphan"}
    assert "concepts/甲.md" not in orphans
    assert "concepts/乙.md" in orphans       # 乙自己没人链


def test_type与目录不符_报_schema错(tmp_path):
    _write(tmp_path, "concepts/甲.md", _page("甲", "entity", sources="[x.md]"))
    assert any(i.kind == "schema" and i.level == "error" for i in lint.run(tmp_path))
