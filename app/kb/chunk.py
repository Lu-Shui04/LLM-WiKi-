"""源文档 → 证据片段。纯代码，不碰模型。

一条证据 = 源文档里的一段连续原文 + 它在全文中的字符区间。区间是精确的：
source_text[f.char_start:f.char_end] 恒等于 f.quote，所以前端点开引用能高亮到
原文那一段，而不是只告诉你「来自哪个文件」。

切法照 FF-LLM-Wiki知识库：按 Markdown 标题层级定「路径」，再在段落边界切开，
太碎的合并、太长的按句切开。参数是按这两份资料的实际结构定的：

    目标 60-300 字，最短 24 字，最长 500 字

为什么按段落切、不按固定字数窗口：固定窗口会把一句话劈成两半，而证据片段是
**直接要喂给模型的**——劈开的半句话单独读没有意义，模型也接不上。

为什么最短 24 字：更短的碎片（光秃秃一个标题、一条分隔线）检索不到任何东西，
只会把索引撑大，还把平均分拉低。

两条不变量，测试里盯着它们：

    区间精确    text[char_start:char_end] == quote
    区间不重叠  相邻两条里，后一条的起点不早于前一条的终点

重叠比遗漏更糟：同一段文字进了两条证据，检索时会被算两次分，
排出来的「前 8 条」其实只有 4 条内容。
"""
import re
from dataclasses import dataclass

MIN_CHARS = 24
TARGET_MAX = 300        # 超过它就在段/句边界切开
HARD_MAX = 500          # 实在切不开（没有句号的长行）就按这个硬切
MAX_DEPTH = 3           # 路径最多保留几级标题

_TICK = chr(96)
HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
# 围栏代码块的开闭标记。不用字面量反引号写：三个反引号会截断生成这个文件的
# 外层模板串，已经踩过一次。
FENCE = re.compile(r"^\s*(" + _TICK * 3 + r"|~{3})")
_SENTENCE = re.compile(r"(?<=[。！？；!?;])")
DECOR = re.compile(r"^[\s\-=*_>|]+$")      # 分隔线、空引用这类没有信息量的行


@dataclass(frozen=True)
class Fragment:
    """一段证据。char_start/char_end 是它在**源文档全文**中的字符下标。"""

    path: str               # 章节路径，如 "项目背景 > 技术方案"
    char_start: int
    char_end: int
    quote: str              # 原文片段，恒等于 source_text[char_start:char_end]

    @property
    def chars(self) -> int:
        return self.char_end - self.char_start


def _line_offsets(text: str) -> list[int]:
    """每一行在全文中的起始下标。用 splitlines(keepends=True) 保证偏移可加。"""
    out, pos = [], 0
    for line in text.splitlines(keepends=True):
        out.append(pos)
        pos += len(line)
    return out


def _sections(text: str) -> list[tuple[str, int, int]]:
    """按标题层级切成 (路径, 起, 止)。围栏代码块里的 # 不算标题。"""
    lines = text.splitlines(keepends=True)
    offsets = _line_offsets(text)
    heads: list[tuple[int, int, str]] = []      # (行号, 级别, 标题)
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = HEADING.match(line.rstrip("\n"))
        if match:
            heads.append((index, len(match.group(1)), match.group(2).strip()))

    if not heads:
        return [("", 0, len(text))]

    out: list[tuple[str, int, int]] = []
    # 首个标题之前的内容（前言）也算一节，否则开头那段永远进不了索引
    if heads[0][0] > 0:
        out.append(("", 0, offsets[heads[0][0]]))
    stack: list[tuple[int, str]] = []
    for position, (line_index, level, title) in enumerate(heads):
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        path = " > ".join(t for _, t in stack[-MAX_DEPTH:])
        start = offsets[line_index]
        end = offsets[heads[position + 1][0]] if position + 1 < len(heads) else len(text)
        out.append((path, start, end))
    return out


def _slice(text: str, start: int, end: int) -> tuple[int, int, str]:
    """掐头去尾的空白，返回调整过的区间和文本。保证 text[起:止] == 文本。"""
    piece = text[start:end]
    lead = len(piece) - len(piece.lstrip())
    body = piece.strip()
    return start + lead, start + lead + len(body), body


def _is_heading_only(quote: str) -> bool:
    """整块只剩标题行，正文不到 MIN_CHARS。

    「### 2023.9 - 2027.6 新疆工程学院」这种单独做证据没有用：
    检索到了也给不出任何事实。它该做的是**往后并进它的正文**，
    所以合并不是为了凑够字数，是为了让标题跟着它管的那段话。
    """
    body = "\n".join(
        line for line in quote.splitlines()
        if line.strip() and not HEADING.match(line.strip())
    )
    return len(body) < MIN_CHARS


def split_long(text: str) -> list[str]:
    """超长的一段：先按句号切，还超长就硬切。切点落在句子边界上。

    拼起来恒等于入参（_SENTENCE 是零宽断言，切分不丢字符），
    所以调用方可以按顺序在原文里 find 到每一片。
    """
    if len(text) <= TARGET_MAX:
        return [text]
    out: list[str] = []
    buf = ""
    for sentence in _SENTENCE.split(text):
        if not sentence:
            continue
        if len(buf) + len(sentence) <= TARGET_MAX:
            buf += sentence
            continue
        if buf:
            out.append(buf)
        buf = sentence
    if buf:
        out.append(buf)

    final: list[str] = []
    for piece in out:
        while len(piece) > HARD_MAX:            # 通篇没有句号的（比如一长串枚举）
            final.append(piece[:HARD_MAX])
            piece = piece[HARD_MAX:]
        if piece:
            final.append(piece)
    return final


def _paragraphs(text: str, start: int, end: int) -> list[tuple[int, int, str]]:
    """一节内部的段落，按原文顺序、**互不重叠**。

    偏移只用一个 pos 游标：分隔符也占位置，所以它必须跟着走。
    上一版在这里同时用了「按段落长度前进」和「按 find 到位置前进」两个游标，
    于是每处理一段就多跳一段，后一段的 find 起点跑到原文更后面，找不到就退回
    游标位置——结果是 23 处区间重叠、覆盖率算出 116%。
    """
    out: list[tuple[int, int, str]] = []
    pos = start
    for chunk in re.split(r"(\n\s*\n)", text[start:end]):
        length = len(chunk)
        if chunk.strip() and not DECOR.match(chunk):
            stripped = chunk.strip()
            base = pos + (len(chunk) - len(chunk.lstrip()))
            for piece in split_long(stripped):
                at = text.find(piece, base, end)
                if at < 0:
                    # 找不到只可能是切分逻辑改了；退回 base 至少保证区间连续，
                    # 后面的 _slice 仍能保证 text[起:止] == 文本
                    at = base
                out.append((at, at + len(piece), piece))
                base = at + len(piece)
        pos += length
    return out


def _merge(blocks: list[tuple[int, int, str]], text: str) -> list[tuple[int, int, str]]:
    """把太短的块并掉：短块总是**向后**并进它后面那块。

    方向是刻意选的。标题在前、正文在后，往后并才能让「### 某个项目」和它下面
    那段正文待在一起；往前并的话标题会挂到上一节去，路径和内容对不上。
    """
    out: list[tuple[int, int, str]] = []
    for bs, be, quote in blocks:
        if out:
            ps, _, previous = out[-1]
            if len(previous) < MIN_CHARS or _is_heading_only(previous):
                merged = text[ps:be].strip()
                if len(merged) <= HARD_MAX:
                    out[-1] = (ps, be, merged)
                    continue
        out.append((bs, be, quote))

    # 末尾还挂着短块 / 光标题：没有「后面」可并了，只好并进上一块
    if len(out) >= 2:
        ps, _, previous = out[-1]
        if len(previous) < MIN_CHARS or _is_heading_only(previous):
            head = out[-2][0]
            merged = text[head:out[-1][1]].strip()
            if len(merged) <= HARD_MAX:
                out[-2] = (head, out[-1][1], merged)
                out.pop()
    return out


def split(text: str) -> list[Fragment]:
    """源文档全文 → 证据片段，按在原文里的先后顺序返回。"""
    found: list[Fragment] = []
    for path, start, end in _sections(text):
        blocks = _merge(_paragraphs(text, start, end), text)
        for bs, be, quote in blocks:
            bs, be, quote = _slice(text, bs, be)
            if len(quote) >= MIN_CHARS:
                found.append(Fragment(path=path, char_start=bs, char_end=be, quote=quote))
    found.sort(key=lambda f: f.char_start)
    return found
