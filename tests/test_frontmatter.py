"""frontmatter 解析与渲染：只支持扁平 YAML 子集，见 SCHEMA.md 第三节。"""
from app.wiki import frontmatter as fm


def test_往返():
    meta = {
        "title": "意图路由",
        "type": "concept",
        "summary": "一句话摘要",
        "tags": ["速购客服", "架构"],
        "contradictions": "none",
    }
    body = "## 摘要\n\n正文。\n\n## 相关\n\n- [[订单Agent]]"
    got_meta, got_body = fm.parse(fm.dump(meta, body))
    assert got_meta == meta
    assert got_body == body


def test_没有_frontmatter():
    text = "就是一段正文\n第二行"
    assert fm.parse(text) == ({}, text)


def test_横线不闭合当普通正文():
    # 宁可当正文，也不要把半篇内容吞进 meta
    text = "---\ntitle: x\n\n正文"
    assert fm.parse(text) == ({}, text)


def test_行内列表():
    meta, _ = fm.parse("---\ntags: [a, b, c]\n---\n\nx")
    assert meta["tags"] == ["a", "b", "c"]


def test_块列表():
    meta, _ = fm.parse("---\ntags:\n  - a\n  - b\n---\n\nx")
    assert meta["tags"] == ["a", "b"]


def test_中文全角冒号不参与切分():
    meta, _ = fm.parse("---\ntitle: 三、RAG：从召回即拼接\n---\n\nx")
    assert meta["title"] == "三、RAG：从召回即拼接"


def test_重复_key_后者胜():
    meta, _ = fm.parse("---\ntitle: 甲\ntitle: 乙\n---\n\nx")
    assert meta["title"] == "乙"


def test_两端引号被剥掉():
    meta, _ = fm.parse('---\ntitle: "带引号的标题"\n---\n\nx')
    assert meta["title"] == "带引号的标题"


def test_merge_sources_去重保序():
    assert fm.merge_sources(["a.md"], ["b.md", "a.md"]) == ["a.md", "b.md"]


def test_merge_sources_兼容字符串与空值():
    assert fm.merge_sources("a.md", "") == ["a.md"]
    assert fm.merge_sources(None, None) == []


def test_links_提取并去空白():
    assert fm.links("见 [[意图路由]] 和 [[ RAG切块策略 ]]") == ["意图路由", "RAG切块策略"]


def test_links_无链接时为空():
    assert fm.links("没有链接") == []


def test_degrade_死链降级为纯文本():
    body = "见 [[意图路由]] 和 [[不存在的页]]"
    new, hit = fm.degrade(body, {"不存在的页"})
    assert new == "见 [[意图路由]] 和 不存在的页"
    assert hit == ["不存在的页"]


def test_degrade_无死链时原样返回():
    body = "见 [[意图路由]]"
    new, hit = fm.degrade(body, set())
    assert new == body
    assert hit == []


def test_outline_只取前两级():
    assert fm.outline("# 一\n## 二\n### 三") == ["一", "二"]
