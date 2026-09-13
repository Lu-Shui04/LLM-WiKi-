"""编译流程里纯函数部分的测试。不联网、不打 API。

重点在**防回归**。这里两条规则出错的形态都是静默的：不报错、不抛异常，
只是「每次 ingest 都重编、页面永远停在旧规则的产物上」——
靠人工翻产物根本看不出来，只有测试能钉住。

规则一：摘要页路径由代码定死（summaries/<源文件名>），不听模型的。
    路径一旦写歪，source_sha/compiler_fp 就落到别的页面上，增量跳过永久失效。
规则二：skip 的页面不能进生成环节（提示词写着「skip 就一个字都不写」）。
"""
from app.wiki import compiler
from app.wiki.compiler import CompileError, Item


def _plan(pages=(), **kw):
    return {"pages": list(pages), "contradictions": [], "gaps": [], **kw}


def _p(slug, type_="concept", op="create", title="", summary="摘要"):
    return {"slug": slug, "type": type_, "op": op,
            "title": title or slug, "summary": summary}


# ─── 摘要页路径（规则一）─────────────────────────────
def test_摘要页_slug_由代码纠正():
    # 模型把摘要页写成了别的名字
    items = compiler._items(_plan([_p("简历摘要", "summary")]), "我的简历.md")
    assert [i.rel for i in items if i.type == "summary"] == ["summaries/我的简历.md"]


def test_摘要页_slug_带扩展名也能纠正():
    items = compiler._items(_plan([_p("随便什么.md", "summary")]), "常见问题回答.md")
    assert [i.rel for i in items if i.type == "summary"] == ["summaries/常见问题回答.md"]


def test_模型漏掉摘要页时补一个():
    # 漏掉的话 source_sha/compiler_fp 没地方落，增量跳过永久失效
    items = compiler._items(_plan([_p("意图路由")]), "我的简历.md")
    assert "summaries/我的简历.md" in [i.rel for i in items]


def test_补出来的摘要页排在第一个():
    items = compiler._items(_plan([_p("意图路由")]), "我的简历.md")
    assert items[0].rel == "summaries/我的简历.md"


def test_摘要页只补一个_不会重复():
    # 模型写了两个摘要页（slug 不同）→ 纠正后撞到同一路径，必须报错而不是静默覆盖
    try:
        compiler._items(_plan([_p("甲", "summary"), _p("乙", "summary")]), "简历.md")
    except CompileError as exc:
        assert "撞到同一路径" in str(exc)
    else:
        raise AssertionError("两个摘要页撞车时应当报错")


# ─── op 的合法性 ─────────────────────────────────────
def test_非法_op_退化成_create():
    items = compiler._items(_plan([_p("意图路由", op="rewrite")]), "我的简历.md")
    got = [i for i in items if i.rel == "concepts/意图路由.md"][0]
    assert got.op == "create"


def test_没有_pages_时报错():
    try:
        compiler._items(_plan([]), "我的简历.md")
    except CompileError as exc:
        assert "没有 pages" in str(exc)
    else:
        raise AssertionError("空 pages 应当报错")


# ─── Item.rel ────────────────────────────────────────
def test_各类型落在对应目录():
    assert Item("concept", "create", "x", "", "甲").rel == "concepts/甲.md"
    assert Item("entity", "create", "x", "", "甲").rel == "entities/甲.md"
    assert Item("comparison", "create", "x", "", "甲").rel == "comparisons/甲.md"
    assert Item("summary", "create", "x", "", "甲").rel == "summaries/甲.md"


def test_总览页在根目录():
    assert Item("overview", "create", "知识库总览", "", "知识库总览").rel == "overview.md"


def test_未知类型报错():
    try:
        Item("nonsense", "create", "x", "", "甲").rel
    except CompileError as exc:
        assert "未知的页面类型" in str(exc)
    else:
        raise AssertionError("未知类型应当报错")


# ─── 分析输出的宽松解析 ──────────────────────────────
def test_JSON_前后有废话也能解析():
    got = compiler.parse_json('好的，这是结果：\n{"pages": [1]}\n希望有帮助')
    assert got == {"pages": [1]}


def test_JSON_被围栏包住也能解析():
    got = compiler.parse_json('```json\n{"pages": [1]}\n```')
    assert got == {"pages": [1]}


def test_解析不出_JSON_时报错并带上字数():
    try:
        compiler.parse_json("这不是 JSON")
    except CompileError as exc:
        assert "没有返回可解析的 JSON" in str(exc)
    else:
        raise AssertionError("非 JSON 应当报错")


# ─── 计划外页面的兜底 ────────────────────────────────
def test_计划外页面按目录反推类型():
    it = compiler._ad_hoc("entities/速购AI客服系统.md")
    assert it.type == "entity" and it.slug == "速购AI客服系统" and it.op == "create"


def test_计划外总览页识别为_overview():
    assert compiler._ad_hoc("overview.md").type == "overview"
