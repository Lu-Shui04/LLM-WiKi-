"""lint：wiki 产物的静态检查。全是确定性代码，不调 LLM、不联网。

    python -m app.wiki lint

六项检查见 SCHEMA.md 第十节。有 error 时退出码 1。

放在 ingest 之前写，是为了能用它校验 ingest 的产物——
没有检查器的时候，模型编出来的页面只能靠肉眼翻，翻三页就翻不动了。
"""
from collections import defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from app.wiki import frontmatter, paths, schema, store

SIMILARITY = 0.7
# 摘要页和总览本来就不太会被人链，不按孤立页算
LINK_EXEMPT = ("summaries/",)


@dataclass
class Issue:
    level: str          # "error" | "warn"
    rel: str
    kind: str
    message: str

    def __str__(self) -> str:
        return f"[{self.level}] {self.rel or 'wiki'}  {self.message}"


def run(root: Path | None = None) -> list[Issue]:
    base = root or paths.wiki_dir()
    pages = store.read_all(base)
    if not pages:
        return [Issue("warn", "", "empty", "wiki/ 里还没有页面，先跑 ingest")]

    # [[X]] → rel。标题和文件名都收，少一类「明明有这页却报死链」的假警报
    tmap: dict[str, str] = {}
    for p in pages:
        t = str(p.meta.get("title", "")).strip()
        if t:
            tmap.setdefault(t, p.rel)
        tmap.setdefault(Path(p.rel).stem, p.rel)

    issues: list[Issue] = []
    inbound: dict[str, int] = defaultdict(int)

    for p in pages:
        # ① frontmatter 与结构（复用落盘前那套校验）
        errs, warns = schema.validate(p.rel, p.meta, p.body)
        issues += [Issue("error", p.rel, "schema", e) for e in errs]
        issues += [Issue("warn", p.rel, "schema", w) for w in warns]

        # ② 死链
        for t in frontmatter.links(p.body):
            target = tmap.get(t)
            if target is None:
                issues.append(Issue("error", p.rel, "dead-link", f"[[{t}]] 没有对应页面"))
            else:
                inbound[target] += 1

        # ③ 无来源断言：有「要点」却不标来源，等于说了话不认账
        if "## 要点" in p.body and not frontmatter.as_list(p.meta.get("sources")):
            issues.append(
                Issue("error", p.rel, "no-source", "有「要点」小节，但 frontmatter 没有 sources")
            )

        # ④ 未处理的矛盾
        c = str(p.meta.get("contradictions", "none")).strip() or "none"
        if c == "open":
            issues.append(
                Issue("error", p.rel, "contradiction", "存在未处理的矛盾（contradictions: open）")
            )
        elif "> [!WARNING] 矛盾" in p.body and c == "none":
            issues.append(
                Issue("error", p.rel, "contradiction", "正文有矛盾块，frontmatter 却写着 none")
            )

    # ⑤ 孤立页面
    for p in pages:
        if p.rel.startswith(LINK_EXEMPT) or p.rel == paths.OVERVIEW:
            continue
        if not inbound.get(p.rel):
            issues.append(Issue("warn", p.rel, "orphan", "没有任何页面链向它"))

    issues += _duplicates(pages)
    return issues


def _duplicates(pages: list) -> list[Issue]:
    """同目录内标题高度相似 → 提示可能是同一个页被写了两遍。

    中文标题短，阈值定 0.7 是保守的：「RAG切块」vs「RAG切块策略」会报（0.71），
    「意图路由」vs「订单Agent」不会（0）。
    """
    out: list[Issue] = []
    by_dir: dict[str, list] = defaultdict(list)
    for p in pages:
        by_dir[Path(p.rel).parent.as_posix()].append(p)

    for group in by_dir.values():
        for i in range(len(group)):
            for j in range(i + 1, len(group)):
                a, b = group[i], group[j]
                ta = str(a.meta.get("title", "")).strip() or Path(a.rel).stem
                tb = str(b.meta.get("title", "")).strip() or Path(b.rel).stem
                ratio = SequenceMatcher(None, ta, tb).ratio()
                if ratio >= SIMILARITY:
                    out.append(
                        Issue("warn", a.rel, "duplicate",
                              f"标题与 {b.rel}（{tb}）高度相似（{ratio:.2f}），可能是同一个页")
                    )
    return out


def sort_issues(issues: list[Issue]) -> list[Issue]:
    return sorted(issues, key=lambda i: (i.level != "error", i.rel, i.kind))


def report(issues: list[Issue]) -> int:
    """打印报告，返回退出码。"""
    if not issues:
        print("lint 通过：没有发现问题")
        return 0

    for it in sort_issues(issues):
        print(it)

    n_err = sum(1 for i in issues if i.level == "error")
    n_warn = sum(1 for i in issues if i.level == "warn")
    print(f"\n共 {n_err} 个 error、{n_warn} 个 warn")
    return 1 if n_err else 0
