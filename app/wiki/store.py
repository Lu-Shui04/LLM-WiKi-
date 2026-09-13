"""wiki/ 页面的读写、索引渲染、两阶段提交。

两阶段提交：先把本次要写的全部页面校验完（含死链降级），任一失败整体抛错、磁盘零变化；
全通过才统一落盘。避免留下半成品 wiki——那种状态最难受，既不能用也不知道该删哪几个文件。

index.md 和 log.md 由**代码**渲染/追加，LLM 不写：
让模型直接写 index 会漏页、写错路径、忘记更新，而这是纯机械汇总，代码做零成本零幻觉。
"""
from dataclasses import dataclass, field
from pathlib import Path

from app.wiki import frontmatter, paths, schema

INDEX_HEADER = """# 知识库索引

> 本文件由代码渲染，请勿手工编辑。改摘要请改对应页面 frontmatter 里的 summary。
"""

# 索引里的分组顺序与中文标题
GROUP_ORDER = (
    ("overview", "总览"),
    ("summary", "来源摘要"),
    ("concept", "概念"),
    ("entity", "实体"),
    ("comparison", "对比"),
    ("query", "问答归档"),
)

LOG_HEADER = """# 操作日志

本文件由代码追加，记录每次 ingest 的操作与用量。
"""


class CommitError(RuntimeError):
    """校验没过，一个字都没写。"""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__("落盘前校验失败：\n  - " + "\n  - ".join(errors))


@dataclass
class Page:
    rel: str                        # 'concepts/意图路由.md'
    meta: dict
    body: str
    dead: list[str] = field(default_factory=list)   # 被降级掉的死链


# ─── 读 ──────────────────────────────────────────────
def read_page(rel: str, root: Path | None = None) -> Page | None:
    p = paths.page_path(rel, root)
    if not p.is_file():
        return None
    meta, body = frontmatter.parse(p.read_text(encoding="utf-8"))
    return Page(rel=rel, meta=meta, body=body)


def read_all(root: Path | None = None) -> list[Page]:
    """全部内容页。index.md / log.md 是代码渲染的，不算页面；.cache/ 一并跳过。"""
    base = root or paths.wiki_dir()
    out: list[Page] = []
    for p in sorted(base.rglob("*.md")):
        rel = p.relative_to(base).as_posix()
        if rel in (paths.INDEX, paths.LOG) or rel.startswith("."):
            continue
        meta, body = frontmatter.parse(p.read_text(encoding="utf-8"))
        out.append(Page(rel=rel, meta=meta, body=body))
    return out


def exists(rel: str, root: Path | None = None) -> bool:
    return paths.page_path(rel, root).is_file()


def titles_of(pages: list[Page]) -> set[str]:
    """页面标题与文件名的集合，[[X]] 的解析目标。

    引用写中文标题（`[[意图路由]]`）也允许写文件名（`[[意图路由]]` 同名时无所谓），
    两个都收进来，少一类「明明有这页却报死链」的假警报。
    """
    out: set[str] = set()
    for p in pages:
        t = str(p.meta.get("title", "")).strip()
        if t:
            out.add(t)
        out.add(Path(p.rel).stem)
    return out


# ─── 写（两阶段提交）─────────────────────────────────
def commit(
    writes: list[Page],
    *,
    root: Path | None = None,
    allow_dangling: bool = False,
) -> tuple[list[Page], list[str]]:
    """阶段一全量校验 + 死链降级，阶段二统一落盘。

    → (实际写入的页面, 警告)。errors 非空抛 CommitError，磁盘保持不变。
    """
    base = root or paths.wiki_dir()
    existing = read_all(base)
    known = titles_of(existing + writes)

    warns: list[str] = []
    errors: list[str] = []
    for p in writes:
        errs, ws = schema.validate(p.rel, p.meta, p.body)
        errors += errs
        warns += ws

        dead = {t for t in frontmatter.links(p.body) if t not in known}
        if dead and not allow_dangling:
            p.body, hit = frontmatter.degrade(p.body, dead)
            p.dead = hit
            warns.append(f"{p.rel}：死链降级 {'、'.join(hit)}")
        elif dead:
            warns.append(f"{p.rel}：保留悬空引用 {'、'.join(sorted(dead))}")

    if errors:
        raise CommitError(errors)

    for p in writes:                    # 阶段二：到这里已经不可能失败了
        _write(p, base)

    write_index(read_all(base), base)
    return writes, warns


def _write(p: Page, base: Path) -> None:
    path = base / p.rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(frontmatter.dump(p.meta, p.body), encoding="utf-8")


# ─── 索引与日志（代码渲染）───────────────────────────
def render_index(pages: list[Page]) -> str:
    by_type: dict[str, list[Page]] = {}
    for p in pages:
        by_type.setdefault(str(p.meta.get("type", "")).strip(), []).append(p)

    out = [INDEX_HEADER.rstrip(), ""]
    for t, label in GROUP_ORDER:
        group = by_type.get(t, [])
        if not group:
            continue
        out += [f"## {label}", "", "| 页面 | 摘要 | 标签 |", "| --- | --- | --- |"]
        for p in sorted(group, key=lambda x: x.rel):
            title = str(p.meta.get("title", "")).strip() or Path(p.rel).stem
            summary = str(p.meta.get("summary", "")).replace("|", "\\|")
            tags = frontmatter.as_list(p.meta.get("tags"))
            out.append(f"| [{title}]({p.rel}) | {summary} | {', '.join(tags)} |")
        out.append("")

    if len(out) == 2:
        out += ["*（还没有页面）*", ""]
    return "\n".join(out).rstrip() + "\n"


def write_index(pages: list[Page], root: Path | None = None) -> None:
    base = root or paths.wiki_dir()
    base.mkdir(parents=True, exist_ok=True)
    (base / paths.INDEX).write_text(render_index(pages), encoding="utf-8")


def append_log(section: str, root: Path | None = None) -> None:
    base = root or paths.wiki_dir()
    base.mkdir(parents=True, exist_ok=True)
    path = base / paths.LOG
    first = not path.is_file()
    with path.open("a", encoding="utf-8", newline="\n") as f:
        if first:
            f.write(LOG_HEADER + "\n")
        f.write(section.rstrip() + "\n\n")
