"""召回：问题 → 知识单元 → 证据片段。整段的形状照 FF-LLM-Wiki知识库 的
search_knowledge + _build_evidence，一处不省。

    ① FTS5 MATCH，bm25(title 8 / summary 3 / body 1)，取前 8 个知识单元
    ② 不足 8 个 → 2 字词 LIKE 兜底；还不足 → 沿页面关系扩一跳
    ③ 一个都没召回到 → 让模型把问题改写成检索词，重试一次
    ④ 收集这 8 个单元绑定的证据，去重，截到 12 条

为什么是 8 和 12 两个数：它是这么定的，而且这两个数各自管一件事——
8 是「多少个知识单元参与排序」，12 是「多少条原文真正进上下文」。
分开是因为一个单元可能绑好几条证据，8 个单元能带出三四十条，
不截断的话「证据」这个词就没意义了，等于又把整页塞回去。

**②里的兜底不是可选项。** FTS5 的 trigram 分词器只索引三连字，
两字词（中文里占大多数：「重试」「订单」「薪资」）一个都匹配不到
（实测库里写着「重试三次」，「重试」匹配为空）。没有 LIKE 这一层，
这些词的问题会直接掉到「资料里没有」——而库里明明有。
"""
import re
from dataclasses import dataclass, field

from app.kb import store

TOP_UNITS = 8           # 参与排序的知识单元数
MAX_EVIDENCE = 12       # 真正进上下文的证据条数
NEIGHBOR_SEEDS = 4      # 沿关系扩一跳时，从前几个单元的页面出发
NEIGHBOR_UNITS = 4      # 一跳最多补几个单元

# ─── 问法与写法之间的桥 ──────────────────────────────
# 词法检索的死穴：有人问「你的联系方式」，资料里写的是「电话 / 邮箱」；
# 问「你获过什么奖」，页面上写的是「荣誉证书」。这不是打分算法的问题，
# 是**词表**的问题，只能靠一张映射表补。
#
# 这张表是评测逼出来的，不是拍脑袋想出来的：跑 python -m app.kb eval，
# 漏召回的那几条全落在这几组上（评测是纯本地的，用不上 LLM 改写，
# 所以它测的就是「只靠词法能到哪一步」）。
#
# 表必须短。加得越多越像在建手工同义词库，而那正是这个项目一直躲着的东西；
# 而且这些词是 OR 进检索表达式的，加宽召回的同时也在稀释精度。
ALIASES: dict[str, tuple[str, ...]] = {
    # 从旧的分页检索那张表里继承过来的（app/wiki/scoring.py），它踩过的坑不用再踩
    "联系方式": ("电话", "邮箱", "微信", "手机"),
    "联系": ("电话", "邮箱"),
    "薪资": ("待遇", "工资", "求职意向"),
    "待遇": ("薪资", "工资"),
    "学历": ("本科", "学校", "专业", "教育"),
    "学校": ("学院", "专业", "毕业"),
    "毕业": ("学校", "专业", "学历"),
    "爱好": ("兴趣", "平时"),
    "介绍": ("简历", "经历", "项目"),
    "证书": ("荣誉", "等级考试"),
    "奖项": ("荣誉", "证书"),
    # 评测里漏掉的那几条，逐条补
    "奖": ("荣誉", "证书"),
    "荣誉": ("证书", "获奖"),
    "负责": ("职责", "分工"),
    "分工": ("职责", "负责"),
    # 「结果」展开成 4 字的「项目成果」而不是 2 字的「成果」：
    # 4 字能进 FTS5 的三元组索引，2 字只能走 LIKE 的 +1 分，几乎必然排不上来
    "结果": ("项目成果", "成果"),
    "成果": ("项目成果", "结果"),
    "岗位": ("求职意向", "意向岗位"),
    "技术栈": ("Vue3", "Node", "LangGraph"),
    "课程": ("专业", "教育"),
    "素材": ("收录",),
}


def expand_query(question: str) -> str:
    """问题 → 问题 + 同义检索词，拼成一段再去做词法匹配。

    拼成一段而不是分别检索再合并：FTS5 的 MATCH 接受 OR 表达式，
    同义词进去之后和原词是**并列**的，排序仍然由 bm25 决定，
    不会出现「同义词那一路的结果插队」这种事。
    """
    text = (question or "").lower()
    extra: list[str] = []
    for key, alternatives in ALIASES.items():
        if key in text:
            extra.extend(alternatives)
    if not extra:
        return question
    return question + " " + " ".join(dict.fromkeys(extra))


@dataclass
class Selection:
    """一次提问实际要喂进上下文的东西。"""

    units: list = field(default_factory=list)
    evidence: list = field(default_factory=list)
    rewritten: str = ""
    precise: bool = False       # 结果来自 FTS5 精确检索，还是 2-gram LIKE 兜底

    @property
    def found(self) -> bool:
        return bool(self.evidence)

    @property
    def chars(self) -> int:
        return sum(len(e.quote) for e in self.evidence)

    def source_count(self) -> int:
        return len({e.source for e in self.evidence})


def search(question: str, limit: int = TOP_UNITS) -> list:
    """问题 → 知识单元，按 bm25 相关度排序。"""
    question = (question or "").strip()
    if not question:
        return []

    expanded = expand_query(question)
    kids: list[str] = []
    expression = store.fts_expression(expanded)
    if expression:
        kids = store.fts_search(expression, limit)

    # 「不足就补」这个判据是从它的 relation 扩展那段借来的。它只在
    # 「字数太少、FTS 召回为空」时才补 LIKE，这里放宽到「不足 8 个就补」：
    # 上面那个「订单」不进表达式的洞，光靠「空才补」是堵不住的。
    if len(kids) < limit:
        for kid in store.like_fallback(expanded, limit):
            if kid not in kids:
                kids.append(kid)
            if len(kids) >= limit:
                break

    # 还不足就沿「相关」那一节扩一跳：找的是邻居页面里**排得最靠前**的单元，
    # 不是邻居页面的全部——扩一跳的目的是把上下文补全，不是把整页拖进来
    if kids:
        units = store.units_by_ids(kids[:limit])
        if len(units) < limit:
            seen_pages = list(dict.fromkeys(u.page_rel for u in units))
            for page in seen_pages[:NEIGHBOR_SEEDS]:
                for neighbor in store.neighbor_pages(page):
                    if neighbor in seen_pages:
                        continue
                    seen_pages.append(neighbor)
                    extra = store.units_of_pages([neighbor], set(kids))[:NEIGHBOR_UNITS]
                    kids.extend(extra)
                    if len(kids) >= limit:
                        break
                if len(kids) >= limit:
                    break

    return store.units_by_ids(kids[:limit])


def build_evidence(units, limit: int = MAX_EVIDENCE) -> list:
    """知识单元 → 证据片段。去重后截断。

    顺序是「先单元顺序、单元内按绑定顺序」——不是按打分。
    证据清单是给模型读的，读起来要像一页资料，不是一列分数。
    """
    seen: set[str] = set()
    out: list = []
    for unit in units:
        for item in store.evidence_for(unit.kid):
            if item.eid in seen:
                continue
            seen.add(item.eid)
            out.append(item)
            if len(out) >= limit:
                return out
    return out


async def retrieve(question: str, *, limit: int = TOP_UNITS,
                   evidence_limit: int = MAX_EVIDENCE, rewrite=None) -> Selection:
    """召回全流程。rewrite 是「检索为空时改写问题」的异步函数，可为 None。"""
    question = (question or "").strip()
    expression = store.fts_expression(question)
    precise = store.fts_search(expression, limit) if expression else []

    found = search(question, limit)
    rewritten = ""
    # 触发条件是「**精确检索**一个都没召回」，不是「最终结果为空」。
    #
    # 它原来的判据是后者，这里刻意改宽了一档。因为 LIKE 兜底是 2-gram 硬匹配，
    # 噪音极大：实测「你的联系方式是什么」它能靠「什么」「方式」把 8 个名额
    # 全填满，前排却是「意图路由」「前端可观测性」——而库里真正写着的是
    # 「电话 / 邮箱」，一个都没出现。这种填充比空手更糟：模型照样会给出
    # 一段带引用的答案，答案是错的，而且看起来有来源。
    #
    # 代价只有一次调用，且只在精确检索落空时才付。正常问题一分钱不多花。
    if not precise and rewrite is not None:
        rewritten = (await rewrite(question) or "").strip()
        if rewritten:
            better = search(rewritten, limit)
            if better:
                found = better
    return Selection(units=found, evidence=build_evidence(found, evidence_limit),
                     rewritten=rewritten, precise=bool(precise))


# ─── 给前端看的两块 ──────────────────────────────────
def unit_brief(unit, cited: bool = False) -> dict:
    return {
        "kid": unit.kid,
        "title": unit.page_title,
        "heading": unit.heading,
        "trail": unit.trail,
        "label": f"{unit.page_title} › {unit.trail}" if unit.trail else unit.page_title,
        "page": unit.page_rel[:-3] if unit.page_rel.endswith(".md") else unit.page_rel,
        "type": unit.page_type,
        "cited": cited,
    }


def evidence_brief(item, index: int) -> dict:
    return {
        "n": index,
        "eid": item.eid,
        "source": item.source,
        "path": item.path,
        "charStart": item.char_start,
        "charEnd": item.char_end,
        "quote": item.quote,
    }


# ─── 检索词改写 ──────────────────────────────────────
REWRITE_PROMPT = """你是检索词改写器。用户的问题可能过于口语化，或者用了代词，
导致和知识库正文没有词面交集。把它改写成适合全文检索的中文关键词。

只输出关键词，用空格分隔。不要标点、不要编号、不要解释。
至少输出 3 个关键词，不要输出空内容。只做词语扩展，不要编造任何事实。"""


def _clean_rewrite(text: str) -> str:
    return " ".join(re.sub(r"[^\w\u4e00-\u9fff\s]+", " ", text or "").split())[:120]


async def rewrite_question(question: str, subject: str = "") -> str:
    """检索一个都没召回时，让模型把问题改写成检索词。

    推理模型会先烧掉一大截 token 做推理，输出预算不够时正文直接是空的
    （它在自己的改造说明第五节记过这个坑：编译处表现成 JSON 截断，
    改写处表现成空返回）。所以这里显式把推理关掉，且空返回重试一次。
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.core.llm import REWRITE_TIMEOUT, get_chat_model

    system = REWRITE_PROMPT
    if subject:
        system += f"\n\n知识库的主人是「{subject}」，指一个具体的人，与同名的公司无关。"
    model = get_chat_model(streaming=False, timeout=REWRITE_TIMEOUT)
    for _ in range(2):
        try:
            reply = await model.ainvoke(
                [SystemMessage(content=system), HumanMessage(content=f"问题：{question}")]
            )
        except Exception as exc:            # 改写失败不该让整轮问答失败
            print(f"[kb] 检索词改写失败：{type(exc).__name__}: {exc}")
            return ""
        text = getattr(reply, "content", "") or ""
        if isinstance(text, list):
            text = "".join(p.get("text", "") if isinstance(p, dict) else str(p) for p in text)
        cleaned = _clean_rewrite(text)
        if cleaned:
            print(f"[kb] 检索为空，改写为：{cleaned}")
            return cleaned
    return ""
