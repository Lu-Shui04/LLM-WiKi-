"""编译流程里纯函数部分的测试。不联网、不打 API。

重点在**防回归**。这里两条规则出错的形态都是静默的：不报错、不抛异常，
只是「每次 ingest 都重编、页面永远停在旧规则的产物上」——
靠人工翻产物根本看不出来，只有测试能钉住。

规则一：摘要页路径由代码定死（summaries/<源文件名>），不听模型的。
    路径一旦写歪，source_sha/compiler_fp 就落到别的页面上，增量跳过永久失效。
规则二：skip 的页面不能进生成环节（提示词写着「skip 就一个字都不写」）。
"""
from app.wiki import compiler, store
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


def test_大小写不同但同名的页面要报错():
    # NTFS 不分大小写：这两个是同一个文件。不报错的话两份都写进去，
    # 后写的那份 os.replace 盖掉前一份，最后 wiki/ 里少一页而计划里列着两条。
    try:
        compiler._items(_plan([_p("RAG"), _p("rag")]), "我的简历.md")
    except CompileError as exc:
        assert "撞到同一路径" in str(exc)
    else:
        raise AssertionError("仅大小写不同的两个 slug 应当报错")


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


# ─── 日志围栏（推理正文可能自带反引号）───────────────
def test_推理里没有反引号时用三根():
    assert compiler._fence_for("普通推理正文") == "```"


def test_推理里有三根反引号时升到四根():
    # 不升的话：模型吐的 ``` 会把围栏提前闭合，后面的推理漏成正文渲染
    assert compiler._fence_for("前面\n```\n后面") == "````"


def test_围栏总比最长反引号串长一根():
    assert compiler._fence_for("`````") == "``````"


def test_单根反引号不影响围栏():
    assert compiler._fence_for("`code`") == "```"


# ─── 正文兜底取摘要 ──────────────────────────────────
def test_缺_summary_时从正文取第一句():
    # schema 要求 summary 非空，缺了就让**整次** ingest 校验失败、一个字都不写。
    # 计划外页面（_ad_hoc）的 item.summary 是空的，模型又常不写 summary。
    assert compiler._first_line("# 标题\n\n正文第一句。\n\n第二句") == "标题"


def test_取摘要会跳过空行和列表符号():
    assert compiler._first_line("\n\n> 引用的话\n后面") == "引用的话"
    assert compiler._first_line("- 列表项") == "列表项"


def test_取摘要给的长度有上限():
    assert len(compiler._first_line("字" * 500)) == 80


def test_正文为空时取摘要返回空串():
    assert compiler._first_line("") == ""
    assert compiler._first_line("   \n\n  ") == ""


# ─── _finalize 的字段合并 ────────────────────────────
def _fin(rel, meta, body, item, old):
    return compiler._finalize(rel, meta, body, item, "来源.md",
                              "2026-01-01", "sha", "fp", old)


def test_contradictions_字段缺失时粘住旧值():
    # 模型整页重写时照模板写、不带这个字段是常态。原来一律落成 none，
    # 一条未处理的矛盾就这样被无声埋掉，没有任何提示。
    old = store.Page("concepts/甲.md", {"contradictions": "open"}, "旧")
    got = _fin("concepts/甲.md",
               {"title": "甲", "type": "concept", "summary": "s"}, "新正文",
               Item("concept", "update", "甲", "s"), old)
    assert got.meta["contradictions"] == "open"


def test_contradictions_显式写就听模型的():
    old = store.Page("concepts/甲.md", {"contradictions": "open"}, "旧")
    got = _fin("concepts/甲.md",
               {"title": "甲", "type": "concept", "summary": "s",
                "contradictions": "resolved"}, "新正文",
               Item("concept", "update", "甲", "s"), old)
    assert got.meta["contradictions"] == "resolved"


def test_没有旧页时_contradictions_默认_none():
    got = _fin("concepts/甲.md",
               {"title": "甲", "type": "concept", "summary": "s"}, "正文",
               Item("concept", "create", "甲", "s"), None)
    assert got.meta["contradictions"] == "none"


def test_缺_summary_时_finalize_从正文兜底():
    got = _fin("concepts/甲.md", {"title": "甲", "type": "concept"},
               "正文第一句\n\n后面", Item("concept", "create", "甲", ""), None)
    assert got.meta["summary"] == "正文第一句"


def test_有旧页时_sources_累积而不是覆盖():
    old = store.Page("concepts/甲.md", {"sources": ["A文档.md"], "created": "2020-01-01"}, "旧")
    got = _fin("concepts/甲.md",
               {"title": "甲", "type": "concept", "summary": "s"}, "正文",
               Item("concept", "update", "甲", "s"), old)
    assert got.meta["sources"] == ["A文档.md", "来源.md"]
    assert got.meta["created"] == "2020-01-01"


# ─── 计划外页面的兜底 ────────────────────────────────
def test_计划外页面按目录反推类型():
    it = compiler._ad_hoc("entities/速购AI客服系统.md")
    assert it.type == "entity" and it.slug == "速购AI客服系统" and it.op == "create"


def test_计划外总览页识别为_overview():
    assert compiler._ad_hoc("overview.md").type == "overview"
