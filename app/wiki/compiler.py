"""两阶段编译：把一份原始文档嚼碎，重组成结构化页面。

    python -m app.wiki ingest knowledge/我的简历.md
    python -m app.wiki ingest knowledge/我的简历.md --dry     只估算，不调模型
    python -m app.wiki ingest knowledge/我的简历.md --force   忽略跳过判定，重编

第一步·分析（1 次调用）：源文档全文 + SCHEMA + 现有 index + 各页大纲
    → 短结构 JSON，每个页面一条 {slug, type, op, title, summary}，外加矛盾与空白
第二步·生成（3 次调用）：摘要页 / 实体+概念页 / overview，用 ===FILE:=== 协议输出

为什么正文不用 JSON 输出：见 SCHEMA.md 第十节——`json.loads` 是全有或全无，
中文 Markdown 漏一处转义就整次编译作废。
为什么分 3 组：单次输出太长模型后半段会敷衍，且一处被 max_tokens 截断要整组重来。
"""
import asyncio
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from app.config import settings
from app.wiki import frontmatter, paths, protocol, store
from app.wiki.slug import slugify

PROMPTS = Path(__file__).parent / "prompts"

PAGE_TYPES = ("entity", "concept", "comparison")
REASONING_LOG_LIMIT = 8000      # log.md 里推理折叠块的截断长度，别让日志长到打不开


class CompileError(RuntimeError):
    pass


# ─── 指纹与哈希 ──────────────────────────────────────
def _digest(*chunks: bytes) -> str:
    h = hashlib.sha256()
    for c in chunks:
        h.update(c.replace(b"\r\n", b"\n"))   # 换行规范化，免得 CRLF/LF 影响指纹
    return h.hexdigest()[:8]


def compiler_fp() -> str:
    """编译提示词 + compiler.py + SCHEMA.md 的指纹。

    任何一个变了，都意味着「同样的输入现在会产出不同的结果」，
    那么增量跳过必须失效。这照抄 app/core/ingest.py 里 CHUNKER_FP 的既有模式。

    副作用（要写进 README）：改一次 SCHEMA.md 会让下次 ingest 全量重编。
    """
    parts = [Path(__file__).read_bytes()]
    for f in sorted(PROMPTS.glob("*.md")):
        parts.append(f.name.encode("utf-8"))
        parts.append(f.read_bytes())
    try:
        parts.append(settings.schema_file.read_bytes())
    except OSError as exc:
        raise CompileError(f"读不到 SCHEMA.md：{settings.schema_file}") from exc
    return _digest(*parts)


def source_sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:8]


# ─── 模型调用 ────────────────────────────────────────
@dataclass
class Reply:
    text: str
    reasoning: str
    usage: dict
    finish: str = ""        # "stop" 正常；"length" 表示被 max_tokens 截断


def _text_of(chunk) -> str:
    c = getattr(chunk, "content", "")
    if isinstance(c, str):
        return c
    if isinstance(c, list):                 # 少数情况下 content 是分段 list
        return "".join(
            p.get("text", "") if isinstance(p, dict) else str(p) for p in c
        )
    return ""


async def _ask(step_file: str, user: str, *, show: bool) -> Reply:
    # 惰性导入：langchain 一进来就是 4 秒。而增量跳过、--dry、lint 这些路径
    # 根本不调模型，没理由让它们替这个开销买单（实测跳过一次要 5.9s，其中
    # 4.1s 是 import，解释器本身只要 0.06s）。
    from langchain_core.messages import HumanMessage, SystemMessage

    from app.wiki.llm import get_ingest_model

    schema = settings.schema_file.read_text(encoding="utf-8")
    system = f"{schema}\n\n---\n\n{(PROMPTS / step_file).read_text(encoding='utf-8').strip()}"

    out: list[str] = []
    think: list[str] = []
    usage: dict = {}
    finish = ""
    n_reason = 0
    shown = 0

    async for chunk in get_ingest_model().astream(
        [SystemMessage(content=system), HumanMessage(content=user)]
    ):
        r = chunk.additional_kwargs.get("reasoning_content")
        if r:
            think.append(r)
            n_reason += len(r)
            if show and n_reason - shown >= 500:
                shown = n_reason
                print(f"    推理 {n_reason:,} 字…", flush=True)
        t = _text_of(chunk)
        if t:
            out.append(t)
        u = getattr(chunk, "usage_metadata", None)
        if u:
            usage = u                       # 只在最后一个 chunk 上报，覆盖取最终值
        fr = (getattr(chunk, "response_metadata", None) or {}).get("finish_reason")
        if fr:
            finish = fr

    if show and n_reason:
        print(f"    推理 {n_reason:,} 字")
    return Reply("".join(out).strip(), "".join(think).strip(), usage, finish)


def _guard(reply: Reply, step: str) -> None:
    """被 max_tokens 截断时明确报出来。

    不报的话，下游只会看到「输出不完整」——分析阶段表现为「不是 JSON」，
    生成阶段表现为「少了一个页面」，都很难联想到截断上去。
    """
    if reply.finish != "length":
        return
    _dump_raw([(f"{step}_截断输出", reply.text)], reply.reasoning)
    raise CompileError(
        f"{step}的输出被 max_tokens 截断（模型推理过长）。"
        f"原始输出已存到 wiki/.cache/last-raw/，可以去看它卡在哪一步。"
    )


def _add_usage(total: dict, u: dict) -> None:
    for k in ("input_tokens", "output_tokens", "total_tokens"):
        if k in u:
            total[k] = total.get(k, 0) + u[k]


# ─── 第一步的输出解析 ────────────────────────────────
def parse_json(text: str) -> dict:
    """宽松解析：先试整段，再退化成「第一个 { 到最后一个 }」。"""
    s = (text or "").strip()
    i, j = s.find("{"), s.rfind("}")
    for cand in (s, s[i:j + 1] if 0 <= i < j else ""):
        if not cand:
            continue
        try:
            got = json.loads(cand)
        except json.JSONDecodeError:
            continue
        if isinstance(got, dict):
            return got
    raise CompileError(f"分析阶段没有返回可解析的 JSON（收到 {len(s)} 字）")


@dataclass
class Item:
    type: str
    op: str
    title: str
    summary: str
    slug: str = ""

    @property
    def rel(self) -> str:
        if self.type == "overview":
            return paths.OVERVIEW
        d = paths.TYPE_DIR.get(self.type)
        if not d:
            raise CompileError(f"未知的页面类型：{self.type}")
        return f"{d}/{self.slug}.md"


def _items(plan: dict, source_name: str) -> list[Item]:
    raw = plan.get("pages")
    if not isinstance(raw, list) or not raw:
        raise CompileError("分析结果里没有 pages")

    out: list[Item] = []
    for r in raw:
        if not isinstance(r, dict):
            continue
        t = str(r.get("type", "")).strip().lower()
        slug = slugify(str(r.get("slug", "")).strip().removesuffix(".md"))
        op = str(r.get("op", "create")).strip().lower()
        if op not in ("create", "update", "skip"):
            op = "create"
        out.append(Item(
            type=t, op=op,
            title=str(r.get("title", "")).strip() or slug,
            summary=str(r.get("summary", "")).strip(),
            slug=slug,
        ))
    if not out:
        raise CompileError("分析结果里没有有效的页面条目")

    # 摘要页路径是确定的：summaries/<源文件名>。**不听模型的**——
    # 它把 slug 写成别的名字，source_sha/compiler_fp 就会落到另一个页面上，
    # 增量跳过随即静默失效（表现是每次 ingest 都重编，且永远追不上）。
    stem = Path(source_name).stem
    for i in out:
        if i.type == "summary":
            i.slug = stem
    # 同理，模型整条漏掉摘要页会造成完全一样的后果，这里补一个。
    if not any(i.type == "summary" for i in out):
        out.insert(0, Item(type="summary", op="create", title=stem, summary="", slug=stem))

    seen: set[str] = set()
    for i in out:
        if i.rel in seen:
            raise CompileError(f"计划里有两个页面撞到同一路径：{i.rel}")
        seen.add(i.rel)
    return out


# ─── 各步的输入拼装 ──────────────────────────────────
def _analyze_input(raw: str, name: str, pages_now: list) -> str:
    parts = [f"# 源文档：{name}", "", raw, "", "# 现有页面清单", ""]
    if not pages_now:
        parts.append("（还没有任何页面，这是首次编译）")
    else:
        for p in sorted(pages_now, key=lambda x: x.rel):
            title = str(p.meta.get("title", "")).strip() or Path(p.rel).stem
            parts.append(f"## {p.rel}   （{p.meta.get('type', '?')}）")
            parts.append(f"标题：{title}")
            if summ := str(p.meta.get("summary", "")).strip():
                parts.append(f"摘要：{summ}")
            if heads := frontmatter.outline(p.body):
                parts.append("小节：" + " / ".join(heads))
            parts.append("")
    parts += ["", "按你的职责输出 JSON。"]
    return "\n".join(parts)


def _known_list(known: set[str]) -> list[str]:
    return ["# 已知页面清单（[[ ]] 只能引用这些）", ""] + [f"- {t}" for t in sorted(known)]


def _summary_input(raw: str, name: str, known: set[str]) -> str:
    return "\n".join([
        f"# 源文档：{name}", "", raw, "",
        "# 要写的页面", f"summaries/{name}", "",
        *_known_list(known),
    ])


def _pages_input(raw: str, name: str, items: list[Item], pages_now: list, known: set[str]) -> str:
    parts = [f"# 源文档：{name}", "", raw, "", "# 要写的页面", ""]
    for i in items:
        parts += [f"## {i.rel}   op={i.op}", f"标题：{i.title}", f"摘要：{i.summary}", ""]

    olds = {p.rel: p for p in pages_now}
    rewrites = [i for i in items if i.op == "update" and i.rel in olds]
    if rewrites:
        parts += ["# 这些页面已有内容，整页重写时要保留其中不同来源的部分", ""]
        for i in rewrites:
            p = olds[i.rel]
            parts += [f"## {p.rel} 现有全文", "", p.body.strip(), ""]

    parts += ["", *_known_list(known)]
    return "\n".join(parts)


def _overview_input(raw: str, name: str, known: set[str]) -> str:
    parts = [
        f"# 本次新摄入：{name}", "",
        "# 知识库当前全部页面（含本次将新建的）", "",
        *[f"- {t}" for t in sorted(known)], "",
    ]
    cur = store.read_page(paths.OVERVIEW)
    if cur:
        parts += [f"# {paths.OVERVIEW} 现有全文，整页重写", "", cur.body.strip(), ""]
    parts += ["# 本次源文档（节选）", "", raw[:4000]]
    return "\n".join(parts)


# ─── 原始输出落盘（诊断用）───────────────────────────
def _dump_raw(texts: list[tuple[str, str]], reasoning: str, plan: dict | None = None) -> None:
    """把本次的原始输出落到 wiki/.cache/last-raw/。

    .cache/ 在 .gitignore 里。产物不合预期时，这里是唯一能看出
    「模型到底写了什么」和「它当时是怎么规划的」的地方。

    文件名前面带序号：同一个 rel 被模型输出两遍时，两份都留得下来
    （只按文件名存的话后一份会盖掉前一份，恰好把问题盖没了）。
    """
    try:
        d = settings.wiki_dir / paths.CACHE_DIR / "last-raw"
        d.mkdir(parents=True, exist_ok=True)
        for f in d.iterdir():
            if f.is_file():
                f.unlink()
        if plan is not None:
            (d / "00__plan.json").write_text(
                json.dumps(plan, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        for n, (name, text) in enumerate(texts, 1):
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)
            (d / f"{n:02d}__{safe}").write_text(text, encoding="utf-8")
        if reasoning:
            (d / "_reasoning.txt").write_text(reasoning, encoding="utf-8")
    except OSError as exc:
        print(f"    （原始输出落盘失败，不影响编译：{exc}）")


def _ad_hoc(rel: str) -> Item:
    """给「分析阶段没规划、但模型在生成阶段写出来了」的页面补一个条目。

    这种页面往往是好东西——生成时模型读的是全文，判断可能比分析阶段更全。
    类型从目录反推，title/summary 以模型 frontmatter 里写的为准。
    """
    t = "overview" if rel == paths.OVERVIEW else paths.DIR_TYPE.get(rel.split("/")[0], "concept")
    stem = Path(rel).stem
    return Item(type=t, op="create", title=stem, summary="", slug=stem)


# ─── 主流程 ──────────────────────────────────────────
@dataclass
class Result:
    source: str
    skipped: bool = False
    reason: str = ""
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    untouched: list[str] = field(default_factory=list)
    dead: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)
    n_contradictions: int = 0
    usage: dict = field(default_factory=dict)
    elapsed: float = 0.0
    reasoning: str = ""


def _resolve(source: str) -> Path:
    """接受 '我的简历.md' 或 'knowledge/我的简历.md'。"""
    p = Path(source)
    if p.parts and p.parts[0] == settings.knowledge_dir.name:
        p = Path(*p.parts[1:])
    return settings.knowledge_dir / p


async def ingest(
    source: str,
    *,
    force: bool = False,
    dry: bool = False,
    no_merge: bool = False,
    allow_dangling: bool = False,
    show_reasoning: bool = True,
) -> Result:
    t0 = time.time()
    src = _resolve(source)
    if not src.is_file():
        raise CompileError(f"找不到源文档：{src}")

    raw = src.read_text(encoding="utf-8")
    name = src.name
    sha, fp = source_sha(raw), compiler_fp()
    s_rel = paths.summary_rel(name)
    pages_now = store.read_all()
    old_summary = store.read_page(s_rel)

    if old_summary and not force:
        same_sha = str(old_summary.meta.get("source_sha", "")) == sha
        same_fp = str(old_summary.meta.get("compiler_fp", "")) == fp
        if same_sha and same_fp:
            return Result(source=name, skipped=True,
                          reason="源文档与编译规则都没变", elapsed=time.time() - t0)

    if dry:
        return _dry_report(name, raw, sha, fp, pages_now, old_summary, t0)

    today = date.today().isoformat()
    usage: dict = {}
    reasoning_all: list[str] = []

    print("[1/4] 分析…")
    reply = await _ask("analyze.md", _analyze_input(raw, name, pages_now), show=show_reasoning)
    _guard(reply, "分析阶段")
    _add_usage(usage, reply.usage)
    reasoning_all.append(reply.reasoning)
    try:
        plan = parse_json(reply.text)
    except CompileError:
        # 解析不出来时把原文留下来。不然只能对着「没有可解析的 JSON」干瞪眼，
        # 完全不知道模型到底吐了什么出来。
        _dump_raw([("analyze_raw", reply.text)], reply.reasoning)
        raise
    items = _items(plan, name)

    warns: list[str] = []
    # 规则变了（或 --force）时，skip 必须升级成 update。
    # 模型判「这页没有新事实」问的是**来源**维度，它没有「规则版本」这个概念；
    # 让它在这种时候 skip 的话，摘要页的 compiler_fp 永远追不上当前值——
    # 于是每次 ingest 都判「规则变了」→ 重编 → 又被 skip，**永久空转**，
    # 页面还永远停在旧规则的产物上。这个判断只能由代码做。
    rebuild = force or (
        old_summary is not None
        and str(old_summary.meta.get("compiler_fp", "")) != fp
    )
    for i in items:
        if i.op == "create" and store.exists(i.rel):
            # 挡住「每次造一个略不同的 slug 导致页面分裂」
            warns.append(f"{i.rel}：op 是 create 但页面已存在，降级为 update")
            i.op = "update"
        elif i.op == "skip" and rebuild:
            warns.append(f"{i.rel}：编译规则有变，skip 升级为 update（整页重编）")
            i.op = "update"

    known = store.titles_of(pages_now) | {i.title for i in items} | {i.slug for i in items}

    # skip 的页面不进生成环节：提示词里写着「skip 就一个字都不写」，
    # 还把它们列进「要写的页面」等于让模型自己跟自己打架，
    # 白花一次生成的钱（上一轮就是这么白花的）。
    live = [i for i in items if i.op != "skip"]
    summary_items = [i for i in live if i.type == "summary"]
    page_items = [i for i in live if i.type in PAGE_TYPES]
    overview_items = [i for i in live if i.type == "overview"]
    if no_merge:
        page_items, overview_items = [], []
        warns.append("--no-merge：只写摘要页，实体/概念/总览一律不动")

    texts: list[tuple[str, str]] = []
    if summary_items:
        print("[2/4] 生成来源摘要页…")
        r = await _ask("summary.md", _summary_input(raw, name, known), show=show_reasoning)
        _guard(r, "来源摘要页")
        _add_usage(usage, r.usage)
        reasoning_all.append(r.reasoning)
        texts += protocol.parse_files(r.text)
    if page_items:
        print(f"[3/4] 生成实体与概念页（{len(page_items)} 页）…")
        r = await _ask("pages.md", _pages_input(raw, name, page_items, pages_now, known),
                       show=show_reasoning)
        _guard(r, "实体与概念页")
        _add_usage(usage, r.usage)
        reasoning_all.append(r.reasoning)
        texts += protocol.parse_files(r.text)
    if overview_items:
        print("[4/4] 生成知识库总览…")
        r = await _ask("overview.md", _overview_input(raw, name, known), show=show_reasoning)
        _guard(r, "总览页")
        _add_usage(usage, r.usage)
        reasoning_all.append(r.reasoning)
        texts += protocol.parse_files(r.text)

    reasoning = "\n\n".join(x for x in reasoning_all if x)
    _dump_raw(texts, reasoning, plan)

    # ─── 组装页面（代码补 frontmatter，模型只提供内容字段）───
    by_rel = {i.rel: i for i in items}
    olds = {p.rel: p for p in pages_now}
    writes: list[store.Page] = []
    seen: set[str] = set()

    for rel, text in texts:
        item = by_rel.get(rel)
        if item is None:
            # 生成阶段模型自己多写的页面。它读的是全文，判断可能比分析阶段更全，
            # 丢掉可惜——收下，但要留痕，让人知道清单和产物曾经对不上。
            item = _ad_hoc(rel)
            warns.append(f"计划外页面，已收下：{rel}（分析阶段没规划到）")
        if item.op == "skip":
            warns.append(f"{rel}：op 是 skip，丢弃生成内容")
            continue
        if rel in seen:
            warns.append(f"{rel}：模型重复输出了同一个页面，只保留第一份")
            continue
        meta, body = frontmatter.parse(text)
        if not body.strip():
            warns.append(f"{rel}：生成内容为空，已忽略")
            continue
        seen.add(rel)
        writes.append(_finalize(rel, meta, body, item, name, today, sha, fp, olds.get(rel)))

    if not writes:
        warns.append("这一轮没有任何页面需要写盘")

    writes, commit_warns = store.commit(writes, allow_dangling=allow_dangling)
    warns += commit_warns

    created = [p.rel for p in writes if p.rel not in olds]
    updated = [p.rel for p in writes if p.rel in olds]
    untouched = [i.rel for i in items if i.op == "skip" and i.rel in olds]
    dead = [d for p in writes for d in p.dead]

    res = Result(
        source=name, created=created, updated=updated, untouched=untouched,
        dead=dead, warnings=warns,
        gaps=[str(g) for g in plan.get("gaps", []) if str(g).strip()],
        n_contradictions=len(plan.get("contradictions") or []),
        usage=usage, elapsed=time.time() - t0, reasoning=reasoning,
    )

    store.append_log(_log_section(res, show_reasoning))
    return res


def _finalize(rel, meta, body, item: Item, source_name: str, today: str,
              sha: str, fp: str, old: store.Page | None) -> store.Page:
    """把模型写的 frontmatter 和代码负责的字段合并。

    代码负责 created / updated / sources（还有摘要页的两个指纹）——
    模型写歪了也覆盖掉，这几样不能交给它。
    """
    old_meta = old.meta if old else {}
    meta["title"] = str(meta.get("title", "")).strip() or item.title
    meta["type"] = item.type
    meta["summary"] = str(meta.get("summary", "")).strip() or item.summary
    meta["contradictions"] = str(meta.get("contradictions", "none")).strip() or "none"
    meta["sources"] = frontmatter.merge_sources(old_meta.get("sources"), [source_name])
    meta["created"] = str(old_meta.get("created") or today)
    meta["updated"] = today
    if rel == paths.summary_rel(source_name):
        meta["source_sha"] = sha
        meta["compiler_fp"] = fp
    return store.Page(rel=rel, meta=meta, body=body)


def _log_section(res: Result, show_reasoning: bool) -> str:
    u = res.usage or {}
    fmt = lambda xs: "、".join(xs) if xs else "（无）"
    lines = [
        f"## {datetime.now().strftime('%Y-%m-%d %H:%M')} ingest {res.source}",
        "",
        f"- 模型：{settings.ingest_model}",
        f"- 用量：输入 {u.get('input_tokens', '未上报')} / 输出 {u.get('output_tokens', '未上报')} tokens",
        f"- 用时：{res.elapsed:.1f}s",
        f"- 新建：{fmt(res.created)}",
        f"- 更新：{fmt(res.updated)}",
        f"- 跳过：{fmt(res.untouched)}",
        f"- 死链降级：{fmt(res.dead)}",
        f"- 告警：{'；'.join(res.warnings) if res.warnings else '无'}",
    ]
    if res.gaps:
        lines.append(f"- 知识空白：{'；'.join(res.gaps)}")

    if show_reasoning and res.reasoning:
        text = res.reasoning
        if len(text) > REASONING_LOG_LIMIT:
            text = text[:REASONING_LOG_LIMIT] + f"\n\n…（共 {len(res.reasoning):,} 字，此处截断）"
        lines += ["", "<details><summary>编译推理（草稿，非页面内容）</summary>", "",
                  "```", text, "```", "", "</details>"]
    return "\n".join(lines)


def _dry_report(name, raw, sha, fp, pages_now, old_summary, t0) -> Result:
    print(f"源文档      {name}（{len(raw):,} 字）")
    print(f"source_sha  {sha}")
    print(f"compiler_fp {fp}")
    print(f"现有页面    {len(pages_now)} 个")
    if old_summary:
        prev_sha = old_summary.meta.get("source_sha", "?")
        prev_fp = old_summary.meta.get("compiler_fp", "?")
        print(f"上次编译    source_sha={prev_sha}  compiler_fp={prev_fp}")
        if str(prev_sha) == sha and str(prev_fp) == fp:
            print("判定        会跳过（源文档与编译规则都没变）")
        elif str(prev_sha) == sha:
            print("判定        会重编（编译规则变了：提示词或 SCHEMA.md 改过）")
        else:
            print("判定        会重编（源文档内容变了）")
    else:
        print("上次编译    无，这是首次")
    print(f"预计输入    首次约 {len(raw) + 6000:,} 字（源文档 + SCHEMA + 现有页面大纲）")
    print("（--dry 不调用模型，不花钱）")
    return Result(source=name, reason="dry", elapsed=time.time() - t0)


if __name__ == "__main__":        # 方便单跑：python -m app.wiki.compiler 我的简历.md
    import sys

    print(asyncio.run(ingest(sys.argv[1] if len(sys.argv) > 1 else "")))
