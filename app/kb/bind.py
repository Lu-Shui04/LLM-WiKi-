"""知识单元 ↔ 证据的绑定。纯代码，不碰模型。

没有这一步，引用只能告诉你「来自哪一页」；有了它，引用能告诉你
「来自《我的简历》第 21 到 156 字那一段原文」。这就是「真实知识库」和
「把文档塞进上下文」的区别。

**判据是覆盖率，不是相似度。** 一个知识单元常常是原文的**并集**（把两段
合起来说），所以「相似度」会被长度差压扁。反过来看才准：

    覆盖率 = |证据的二元组 ∩ 单元的二元组| / |证据的二元组|

也就是「这条证据有多少比例被这个单元用到了」。只取一句、写进一个长单元里，
覆盖率仍然是 1.0；而一条没被用到的证据，覆盖率接近 0。

**来源标注先把范围缩小。** 编译提示词让每个细节块写成「### 来自《我的简历》」，
所以这些单元只在《我的简历》的证据里找，不在全库乱找——这一步把误绑
降下来一大截，因为两份资料里有大量重复表达（同一个项目，简历和 FAQ 都写了）。

**摘要/要点类单元是模型的话，不是原文。** 它们覆盖率为 0 是正常的，不该硬绑。
它们继承**同页面**里细节块绑到的证据——摘要是对整页的概括，它的依据就是
那一页所有细节块的依据。
"""
import re
from collections import defaultdict

COVERAGE_MIN = 0.45     # 低于它就不算「用到」了
PER_UNIT_MAX = 5        # 一个单元最多绑几条，防止一个单元吃掉整个库
INHERIT_MAX = 3         # 摘要/要点类单元从同页面继承几条

_WORD = re.compile(r"[a-z0-9][a-z0-9_+#.\-]*")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]+")


def grams(text: str) -> set[str]:
    """字符二元组 + 小写英文词。中文按二元组切，和检索时的口径一致。"""
    lowered = (text or "").lower()
    out = set(_WORD.findall(lowered))
    for run in _CJK.findall(lowered):
        if len(run) == 1:
            out.add(run)
        else:
            out.update(run[i:i + 2] for i in range(len(run) - 1))
    return out


def _stem(name: str) -> str:
    """我的简历.md / 我的简历 → 我的简历。用来对齐「来自《我的简历》」和文件名。"""
    return re.sub(r"\.(md|markdown|txt)$", "", (name or "").strip(), flags=re.I)


def bind(units, evidence, *, coverage_min: float = COVERAGE_MIN,
         per_unit: int = PER_UNIT_MAX, inherit: int = INHERIT_MAX):
    """→ (links, 统计)。

    links 是 [(kid, eid, rank)]，rank 用证据在单元里第一次被命中的位置，
    这样注入时顺序和单元正文一致，读起来是顺的。
    """
    by_source: dict[str, list] = defaultdict(list)
    for item in evidence:
        by_source[_stem(item.source)].append(item)

    unit_grams = {u.kid: grams(u.text) for u in units}
    links: list[tuple[str, str, int]] = []
    bound: dict[str, list] = {}
    stats = {"direct": 0, "inherited": 0, "none": 0}

    for unit in units:
        ug = unit_grams[unit.kid]
        pool = by_source.get(_stem(unit.source_hint)) if unit.source_hint else None
        if pool is None:
            pool = evidence
        scored = []
        for item in pool:
            eg = grams(item.quote)
            if not eg:
                continue
            hit = len(eg & ug) / len(eg)
            if hit >= coverage_min:
                scored.append((hit, item.seq, item))
        # 两步排序，方向是刻意的：
        #   挑哪几条  -> 按覆盖率（最贴切的先进）
        #   挑完之后 -> 按证据在原文里的先后
        # 只用覆盖率排会出现注入顺序和原文相反的情况（实测 2286 / 2363 / 2550 / 2219），
        # 模型读到的是打乱的原文；只按位置排又会让「勉强够 0.45」的挤掉「0.99 的」。
        scored.sort(key=lambda x: (-x[0], x[1]))
        chosen = sorted((item for _, _, item in scored[:per_unit]), key=lambda i: i.seq)
        bound[unit.kid] = chosen
        if chosen:
            stats["direct"] += 1
        for rank, item in enumerate(chosen):
            links.append((unit.kid, item.eid, rank))

    # 第二遍：摘要/要点这类「模型自己写的话」继承同页面细节块的证据
    page_bound: dict[str, list] = defaultdict(list)
    for unit in units:
        page_bound[unit.page_rel].extend(bound.get(unit.kid, []))

    for unit in units:
        if bound.get(unit.kid):
            continue
        seen, picked = set(), []
        for item in page_bound.get(unit.page_rel, []):
            if item.eid in seen:
                continue
            seen.add(item.eid)
            picked.append(item)
            if len(picked) >= inherit:
                break
        picked.sort(key=lambda i: i.seq)
        if picked:
            stats["inherited"] += 1
            bound[unit.kid] = picked
            for rank, item in enumerate(picked):
                links.append((unit.kid, item.eid, rank))
        else:
            stats["none"] += 1

    return links, stats
