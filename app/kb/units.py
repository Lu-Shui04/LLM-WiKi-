"""wiki 页面 → 知识单元 + 关系。纯代码，不碰模型。

**为什么不重新编译。** 编译产物里的小节本来就已经是「一小块」了：
实测你的小节中位 290 字，FF-LLM-Wiki知识库 的知识单元中位 290 字——粒度是一样的。
那十几倍的注入差距不在编译，在**召回时拿什么当单位**：它检索到单元后只注入
证据片段，旧链路检索到页面后注入整页。所以这里只做一件事：
把已经存在的页面按它自己的小标题拆开，让检索有「小块」可用。

**来源标注是白捡的。** 编译提示词让每个细节块写成这样：

    ### 来自《我的简历》
    （这一段是源文档的原文或改写）

这个标题同时给了两样东西：单元边界，和它属于哪个源文件。绑定证据时先按这个
提示把范围缩到那一个源文件，命中率比全库乱找高得多。

**「相关」那一节不是知识单元，是关系。** 它里面只有 [[页面名]] 列表，本身没有
任何事实可检索；但它是页面之间显式声明的边，召回不够 8 条时沿它扩一跳正好用得上
（照 FF-LLM-Wiki知识库 的 search_knowledge 里那段 relation 扩展）。
"""
import hashlib
import re
from dataclasses import dataclass, field

from app.kb.chunk import FENCE, HEADING, MIN_CHARS, split_long

# 一节里只剩 [[链接]]、没有别的内容 —— 那就是关系，不是知识
_LINK = re.compile(r"\[\[([^\[\]]+)\]\]")
_SOURCE_HINT = re.compile(r"来自\s*[《<]([^》>]+)[》>]")

LINK_ONLY_MAX = 400     # 「相关」节一般很短；超过这个长度说明它有正文，不算纯链接
UNITS_MAX = 700         # 超过它就在句子边界再切一刀，见 _blocks 末尾


@dataclass(frozen=True)
class Unit:
    """一个知识单元。检索和注入都以它为单位。"""

    kid: str                # 稳定 ID：页面路径 + 小节路径的哈希，改内容不会换 ID
    page_rel: str           # concepts/订单Agent工具调用.md
    page_title: str         # 订单Agent与工具调用
    page_type: str          # concept / entity / summary / overview
    heading: str            # 最深一级小节标题
    trail: str              # 给人看的定位，如 "细节 > 来自《我的简历》"
    text: str               # 正文
    source_hint: str = ""   # 来自《X》里的 X，没有就是空
    page_summary: str = ""  # 页面的一句话摘要，进 FTS 的 summary 字段（权重 3）

    @property
    def fts_title(self) -> str:
        """进 FTS 的 title 字段（权重 8）。

        必须把**页面标题**也拼进去。实测过：只写小节标题的话，十个页面都有
        「摘要 / 要点 / 细节」这几个同名小节，标题字段的区分度直接归零，
        「你的联系方式是什么」会把「订单Agent与工具调用 › 要点」排到第二名。
        """
        return f"{self.page_title} {self.trail}".strip()

    @property
    def label(self) -> str:
        """喂给模型和前端时显示的名字。"""
        return f"{self.page_title} › {self.trail}" if self.trail else self.page_title


@dataclass(frozen=True)
class Link:
    """页面之间显式声明的边（来自「相关」那一节）。"""

    page_rel: str
    target: str


@dataclass
class Scan:
    units: list[Unit] = field(default_factory=list)
    links: list[Link] = field(default_factory=list)


def _blocks(body: str) -> list[tuple[str, str, str]]:
    """页面正文 → [(小节路径, 最深层标题, 正文)]，按出现顺序。

    小节路径保留层级（"细节 > 来自《我的简历》"），最深层标题单独给一份，
    因为绑定证据时要一眼看出这块来自哪个源文件。
    """
    out: list[tuple[str, str, str]] = []
    stack: list[tuple[int, str]] = []
    buf: list[str] = []
    in_fence = False

    def flush() -> None:
        text = "\n".join(buf).strip()
        if stack and text:
            trail = " > ".join(t for _, t in stack)
            out.append((trail, stack[-1][1], text))

    for line in body.splitlines():
        if FENCE.match(line):
            in_fence = not in_fence
            buf.append(line)
            continue
        match = None if in_fence else HEADING.match(line.rstrip())
        if match:
            flush()
            buf = []
            level, title = len(match.group(1)), match.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            continue
        buf.append(line)
    flush()

    # 只有标题、正文不到 MIN_CHARS 的容器块（比如「细节」后面直接跟「### 来自…」）
    # 往后并进它的第一个子块——留着它只会多一条检索不到东西的单元
    merged: list[tuple[str, str, str]] = []
    for trail, heading, text in out:
        if merged and len(merged[-1][2]) < MIN_CHARS:
            ptrail = merged[-1][0]
            merged[-1] = (ptrail, heading, merged[-1][2] + "\n\n" + text)
            continue
        merged.append((trail, heading, text))

    # 超大块再切一刀。实测有一节「要点」长到 1424 字，那已经不叫小块了：
    # 检索时它一个块占掉三个名额，注入兜底时又会一次塞进上千字。
    # 切点在句号上，所以每一片单独读仍然通顺。
    final: list[tuple[str, str, str]] = []
    for trail, heading, text in merged:
        pieces = split_long(text) if len(text) > UNITS_MAX else [text]
        if len(pieces) == 1:
            final.append((trail, heading, text))
            continue
        for index, piece in enumerate(pieces, 1):
            # 带序号是必须的：kid 是 trail 的哈希，两片同名会撞成同一个 ID
            final.append((f"{trail}（{index}/{len(pieces)}）", heading, piece))
    return final


def _kid(page_rel: str, trail: str) -> str:
    return "U-" + hashlib.sha1(f"{page_rel}#{trail}".encode("utf-8")).hexdigest()[:12]


def _is_link_only(text: str) -> bool:
    """整块只剩 [[链接]] 和空行 —— 那是关系表，不是知识。"""
    if len(text) > LINK_ONLY_MAX or not _LINK.search(text):
        return False
    stripped = _LINK.sub("", text)
    stripped = "\n".join(line for line in stripped.splitlines() if line.strip(" -*)>"))
    return len(stripped.strip()) < MIN_CHARS


def scan(pages) -> Scan:
    """wiki 的全部页面 → 知识单元 + 关系。pages 是 store.read_all() 的结果。"""
    result = Scan()
    for page in pages:
        title = str(page.meta.get("title", "")).strip() or page.rel
        ptype = str(page.meta.get("type", "")).strip()
        for trail, heading, text in _blocks(page.body):
            if _is_link_only(text):
                for target in dict.fromkeys(_LINK.findall(text)):
                    result.links.append(Link(page_rel=page.rel, target=target))
                continue
            hint = _SOURCE_HINT.search(heading)
            result.units.append(Unit(
                kid=_kid(page.rel, trail),
                page_rel=page.rel,
                page_title=title,
                page_type=ptype,
                heading=heading,
                trail=trail,
                text=text,
                source_hint=hint.group(1).strip() if hint else "",
                page_summary=str(page.meta.get("summary", "")).strip(),
            ))
    return result
