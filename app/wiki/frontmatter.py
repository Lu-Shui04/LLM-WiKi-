"""frontmatter 的解析与渲染。只支持扁平 YAML 子集。

为什么不装 PyYAML：能力边界要清晰。不支持嵌套 map、不支持多行块标量，
手写的确定性小函数行为可单测，模型写歪了也只会退化成「这行不认」，
不会整篇解析炸掉。规则见 SCHEMA.md 第三节。

按**第一个**半角 ':' 切分，所以 `title: 三、RAG：从召回即拼接` 这种
标题里带中文全角冒号的情况天然安全。
"""
import re
from typing import Any

_FENCE = "---"
_KV = re.compile(r"^([^:\s][^:]*):\s*(.*)$")
_LIST_ITEM = re.compile(r"^-\s+(.*)$")
_LINK = re.compile(r"\[\[([^\[\]]+)\]\]")
_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*$")

# dump 时的字段顺序。不在这张表里的按插入顺序排到后面
ORDER = (
    "title", "type", "summary", "tags", "contradictions",
    "sources", "created", "updated", "source_sha", "compiler_fp",
)


# ─── 解析 ────────────────────────────────────────────
def parse(text: str) -> tuple[dict[str, Any], str]:
    """(meta, body)。没有 frontmatter、或 --- 不闭合，都按「无 frontmatter」处理。

    不闭合时宁可当普通正文，也不要把半篇内容吞进 meta。
    """
    lines = (text or "").splitlines()
    if not lines or lines[0].strip() != _FENCE:
        return {}, text

    end = None
    for i in range(1, len(lines)):
        if lines[i].strip() == _FENCE:
            end = i
            break
    if end is None:
        return {}, text

    meta = _parse_block(lines[1:end])
    return meta, "\n".join(lines[end + 1:]).strip()


def _parse_block(lines: list[str]) -> dict[str, Any]:
    meta: dict[str, Any] = {}
    key: str | None = None
    items: list[str] = []

    def flush() -> None:
        nonlocal key, items
        if key is not None:
            meta[key] = items if items else ""
        key, items = None, []

    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = _LIST_ITEM.match(line)
        if m and key is not None:
            items.append(_scalar(m.group(1)))
            continue
        m = _KV.match(raw)
        if not m:
            continue                    # 认不出的行直接丢，宽松优先
        flush()
        k, v = m.group(1).strip(), m.group(2).strip()
        if not v:                       # 值可能在下面的块列表里
            key = k
            continue
        meta[k] = _inline_list(v) if v.startswith("[") else _scalar(v)
    flush()
    return meta


def _scalar(v: str) -> str:
    v = v.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in "\"'":
        return v[1:-1]
    return v


def _inline_list(v: str) -> list[str]:
    inner = v.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    return [_scalar(x) for x in inner.split(",") if x.strip()]


# ─── 渲染 ────────────────────────────────────────────
def dump(meta: dict[str, Any], body: str) -> str:
    lines = [_FENCE]
    for k in ORDER:
        if k in meta and meta[k] not in ("", None):
            lines.append(f"{k}: {_fmt(meta[k])}")
    for k, v in meta.items():
        if k not in ORDER and v not in ("", None):
            lines.append(f"{k}: {_fmt(v)}")
    lines.append(_FENCE)
    return "\n".join(lines) + "\n\n" + (body or "").strip() + "\n"


def _fmt(v: Any) -> str:
    if isinstance(v, (list, tuple)):
        return "[" + ", ".join(str(x) for x in v) + "]"
    return str(v)


# ─── 字段操作 ────────────────────────────────────────
def merge_sources(old: Any, new: Any) -> list[str]:
    """来源列表合并：去重、保序（旧的在前）。

    这是「页面被第二次 ingest 更新时，sources 要累积而不是覆盖」的唯一实现。
    """
    out: list[str] = []
    for group in (old, new):
        for x in as_list(group):
            if x and x not in out:
                out.append(x)
    return out


def as_list(v: Any) -> list[str]:
    if v is None or v == "":
        return []
    if isinstance(v, (list, tuple)):
        return [str(x) for x in v]
    return [str(v)]


# ─── 正文里的链接与大纲 ──────────────────────────────
def links(body: str) -> list[str]:
    """正文里全部 [[X]] 的目标（去空白、含重复、按出现顺序）。

    去空白是必须的：模型偶尔写 `[[ 意图路由 ]]`，不去的话拿它跟页面标题比对
    会判成死链，把好好的引用降级掉。
    """
    return [m.strip() for m in _LINK.findall(body or "")]


def degrade(body: str, dead: set[str]) -> tuple[str, list[str]]:
    """把死链 [[X]] 降级成纯文本 X。返回 (新正文, 被降级的链接列表)。

    SCHEMA.md 第十二节红线：不许留悬空 [[ ]]。产物是给人看的，
    点不开的引用在面试前翻看时是纯噪声。
    """
    hit: list[str] = []

    def sub(m: re.Match) -> str:
        target = m.group(1).strip()
        if target in dead:
            hit.append(target)
            return target
        return m.group(0)

    return _LINK.sub(sub, body or ""), hit


def outline(body: str, max_level: int = 2) -> list[str]:
    """一级/二级标题列表，编译第一步当「现有页面大纲」用，省 token。"""
    out: list[str] = []
    for line in (body or "").splitlines():
        m = _HEADING.match(line)
        if m and len(m.group(1)) <= max_level:
            out.append(m.group(2))
    return out
