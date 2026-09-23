"""知识库检索层的数据表。SQLite + FTS5(trigram)，同一个 agent.db。

表结构照 FF-LLM-Wiki知识库 的思路，但少一层：它要维护 source / source_version /
snapshot 的多版本链条（因为它有审核发布），这里源文档就是磁盘上的文件、
产物就是 wiki/ 里的页面，所以只需要四张实体表加一张索引表：

    kb_sources     源文档全文（**存全文**，因为点开引用要按字符区间取那一段原文）
    kb_evidence    证据片段：路径 + char_start/char_end + quote
    kb_units       知识单元：页面路径 + 小节 + 正文
    kb_links       单元 → 证据（多对多，带顺序）
    kb_page_links  页面 → 页面（从「相关」那一节来，召回不够时扩一跳用）
    kb_fts         FTS5 虚表，trigram 分词，bm25 权重 title8 / summary3 / body1

**整张索引都是派生物**，从 knowledge/ 和 wiki/ 算出来，所以有一个
replace() 做整体重建：先在一个事务里清空再灌，失败就整体回滚，
不会留下「一半是新索引、一半是旧的」这种查不出问题也修不好的状态。

**为什么 FTS5 不自己算 BM25**：stdlib 的 sqlite3 编进了 FTS5，bm25() 是
内置函数，一行 SQL 就能按列加权排序。旧链路 app/wiki/scoring.py 是在内存里
把全部页面读进来现算——它自己在注释里写了这笔债「等页面到几千个再换 FTS5」。
"""
import asyncio
import re
import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone

from app.db.sqlite import connect

KB_SCHEMA = 1           # 改表结构就 +1，启动时会自动重建

_TICK = chr(96)

DDL = """
CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_sources (
    name       TEXT PRIMARY KEY,
    sha        TEXT NOT NULL,
    chars      INTEGER NOT NULL,
    text       TEXT NOT NULL,
    indexed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kb_evidence (
    eid        TEXT PRIMARY KEY,
    source     TEXT NOT NULL,
    path       TEXT NOT NULL,
    char_start INTEGER NOT NULL,
    char_end   INTEGER NOT NULL,
    quote      TEXT NOT NULL,
    seq        INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_kb_evidence_source ON kb_evidence(source, seq);
CREATE TABLE IF NOT EXISTS kb_units (
    kid          TEXT PRIMARY KEY,
    page_rel     TEXT NOT NULL,
    page_title   TEXT NOT NULL,
    page_type    TEXT NOT NULL,
    page_summary TEXT NOT NULL DEFAULT '',
    heading      TEXT NOT NULL,
    trail        TEXT NOT NULL,
    text         TEXT NOT NULL,
    source_hint  TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS ix_kb_units_page ON kb_units(page_rel);
CREATE TABLE IF NOT EXISTS kb_links (
    kid  TEXT NOT NULL,
    eid  TEXT NOT NULL,
    rank INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (kid, eid)
);
CREATE INDEX IF NOT EXISTS ix_kb_links_eid ON kb_links(eid);
CREATE TABLE IF NOT EXISTS kb_page_links (
    page_rel   TEXT NOT NULL,
    target_rel TEXT NOT NULL,
    target     TEXT NOT NULL,
    PRIMARY KEY (page_rel, target)
);
""" + "CREATE VIRTUAL TABLE IF NOT EXISTS kb_fts USING fts5(" \
    + "kid UNINDEXED, title, summary, body, tokenize='trigram');"

_FTS_COLS = "bm25(kb_fts, 0.0, 8.0, 3.0, 1.0)"     # kid / title / summary / body
_QUERY_TERM = re.compile(r"[\u3400-\u9fff]+|[A-Za-z0-9_+#.\-]+")
_STRIP = re.compile(r"[^\w\u3400-\u9fff]")


@dataclass(frozen=True)
class Evidence:
    eid: str
    source: str
    path: str
    char_start: int
    char_end: int
    quote: str
    seq: int


@dataclass(frozen=True)
class UnitRow:
    kid: str
    page_rel: str
    page_title: str
    page_type: str
    page_summary: str
    heading: str
    trail: str
    text: str
    source_hint: str


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ─── 建表 ────────────────────────────────────────────
def _ensure_schema(conn: sqlite3.Connection) -> bool:
    """建表；版本对不上就把整个 kb 层丢掉重建。返回是否发生了重建。"""
    have = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='kb_meta'"
    ).fetchone()
    version = ""
    if have:
        row = conn.execute("SELECT value FROM kb_meta WHERE key='schema'").fetchone()
        version = row[0] if row else ""
    if version == str(KB_SCHEMA):
        return False

    # 版本变了就整体重建。不写迁移脚本：这一层全是派生物，
    # 重建的代价只是几秒重算，比维护迁移便宜也更不容易出错。
    conn.execute("DROP TABLE IF EXISTS kb_fts")
    for table in ("kb_meta", "kb_sources", "kb_evidence", "kb_units",
                  "kb_links", "kb_page_links"):
        conn.execute(f"DROP TABLE IF EXISTS {table}")
    conn.executescript(DDL)
    conn.execute("INSERT OR REPLACE INTO kb_meta (key, value) VALUES ('schema', ?)",
                 (str(KB_SCHEMA),))
    return True


def ensure_schema() -> None:
    with closing(connect()) as conn, conn:
        _ensure_schema(conn)


# ─── 重建 ────────────────────────────────────────────
def _replace(sources, evidence, units, links, page_links) -> None:
    """整体替换。一个事务，要么全成要么全不成。"""
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM kb_fts")
        for table in ("kb_sources", "kb_evidence", "kb_units", "kb_links", "kb_page_links"):
            conn.execute(f"DELETE FROM {table}")

        conn.executemany(
            "INSERT INTO kb_sources (name, sha, chars, text, indexed_at) VALUES (?,?,?,?,?)",
            [(s["name"], s["sha"], len(s["text"]), s["text"], _now()) for s in sources],
        )
        conn.executemany(
            "INSERT INTO kb_evidence (eid, source, path, char_start, char_end, quote, seq)"
            " VALUES (?,?,?,?,?,?,?)",
            [(e.eid, e.source, e.path, e.char_start, e.char_end, e.quote, e.seq) for e in evidence],
        )
        rows = [(u.kid, u.page_rel, u.page_title, u.page_type, u.page_summary,
                 u.heading, u.trail, u.text, u.source_hint) for u in units]
        conn.executemany(
            "INSERT INTO kb_units (kid, page_rel, page_title, page_type, page_summary,"
            " heading, trail, text, source_hint) VALUES (?,?,?,?,?,?,?,?,?)", rows)
        conn.executemany(
            "INSERT OR REPLACE INTO kb_links (kid, eid, rank) VALUES (?,?,?)", links)
        conn.executemany(
            "INSERT OR REPLACE INTO kb_page_links (page_rel, target_rel, target) VALUES (?,?,?)",
            page_links)
        conn.executemany(
            "INSERT INTO kb_fts (kid, title, summary, body) VALUES (?,?,?,?)",
            [(u.kid, u.fts_title, u.page_summary, u.text) for u in units],
        )


def replace(sources, evidence, units, links, page_links) -> None:
    ensure_schema()
    _replace(sources, evidence, units, links, page_links)


# ─── 指纹 ────────────────────────────────────────────
def get_meta(key: str) -> str:
    with closing(connect()) as conn:
        row = conn.execute("SELECT value FROM kb_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else ""


def set_meta(key: str, value: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("INSERT OR REPLACE INTO kb_meta (key, value) VALUES (?, ?)", (key, value))


def stats() -> dict:
    ensure_schema()
    with closing(connect()) as conn:
        one = lambda sql: conn.execute(sql).fetchone()[0]      # noqa: E731
        return {
            "sources": one("SELECT COUNT(*) FROM kb_sources"),
            "evidence": one("SELECT COUNT(*) FROM kb_evidence"),
            "units": one("SELECT COUNT(*) FROM kb_units"),
            "links": one("SELECT COUNT(*) FROM kb_links"),
            "page_links": one("SELECT COUNT(*) FROM kb_page_links"),
            "fingerprint": get_meta("fingerprint"),
        }


# ─── 读 ──────────────────────────────────────────────
def _unit_row(row: sqlite3.Row) -> UnitRow:
    return UnitRow(kid=row["kid"], page_rel=row["page_rel"], page_title=row["page_title"],
                   page_type=row["page_type"], page_summary=row["page_summary"],
                   heading=row["heading"], trail=row["trail"], text=row["text"],
                   source_hint=row["source_hint"])


def _evidence_row(row: sqlite3.Row) -> Evidence:
    return Evidence(eid=row["eid"], source=row["source"], path=row["path"],
                    char_start=row["char_start"], char_end=row["char_end"],
                    quote=row["quote"], seq=row["seq"])


def units_by_ids(kids: list[str]) -> list[UnitRow]:
    if not kids:
        return []
    marks = ",".join("?" * len(kids))
    with closing(connect()) as conn:
        rows = conn.execute(f"SELECT * FROM kb_units WHERE kid IN ({marks})", kids).fetchall()
    by_id = {row["kid"]: _unit_row(row) for row in rows}
    return [by_id[k] for k in kids if k in by_id]      # 保持调用方给的顺序（就是打分顺序）


def unit_by_id(kid: str) -> UnitRow | None:
    found = units_by_ids([kid])
    return found[0] if found else None


def evidence_by_ids(eids: list[str]) -> list[Evidence]:
    if not eids:
        return []
    marks = ",".join("?" * len(eids))
    with closing(connect()) as conn:
        rows = conn.execute(f"SELECT * FROM kb_evidence WHERE eid IN ({marks})", eids).fetchall()
    by_id = {row["eid"]: _evidence_row(row) for row in rows}
    return [by_id[e] for e in eids if e in by_id]


def evidence_for(kid: str) -> list[Evidence]:
    """一个单元绑定的证据，按在单元里出现的先后。"""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT e.* FROM kb_links l JOIN kb_evidence e ON e.eid = l.eid"
            " WHERE l.kid = ? ORDER BY l.rank", (kid,)).fetchall()
    return [_evidence_row(row) for row in rows]


def source_text(name: str) -> str:
    with closing(connect()) as conn:
        row = conn.execute("SELECT text FROM kb_sources WHERE name = ?", (name,)).fetchone()
    return row[0] if row else ""


def neighbor_pages(page_rel: str, limit: int = 6) -> list[str]:
    """沿「相关」那一节扩一跳。只收解析得到真实页面的边。"""
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT target_rel FROM kb_page_links"
            " WHERE page_rel = ? AND target_rel <> '' LIMIT ?", (page_rel, limit)).fetchall()
    return [row[0] for row in rows]


# ─── 检索 ────────────────────────────────────────────
def fts_expression(query: str, max_terms: int = 24) -> str | None:
    """问题 → FTS5 的 MATCH 表达式。

    中文切成**三元组**：trigram 分词器只索引三连字，所以两字词一个都匹配不到
    （实测：库里写着「重试三次」，「重试」匹配为空）。这正是 FF-LLM-Wiki知识库
    那个正则写成 {3,} 的原因，也是下面必须有 LIKE 兜底的原因。

    这里对 >=3 字的中文串切三元组，英文数字整词收下；短于 3 字的整串交给
    like_fallback。词与词之间是 OR——召回优先，排序交给 bm25。
    """
    terms: list[str] = []
    for piece in _QUERY_TERM.findall(query or ""):
        if piece.isascii():
            if len(piece) >= 3:
                terms.append(piece.lower())
        elif len(piece) >= 3:
            terms.extend(piece[i:i + 3] for i in range(len(piece) - 2))
    unique = list(dict.fromkeys(t for t in terms if len(t) >= 3))[:max_terms]
    if not unique:
        return None
    return " OR ".join('"' + t.replace('"', "") + '"' for t in unique)


def fts_search(expression: str, limit: int = 8) -> list[str]:
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT kid, {_FTS_COLS} AS score FROM kb_fts"
            " WHERE kb_fts MATCH ? ORDER BY score LIMIT ?", (expression, limit)).fetchall()
    return [row[0] for row in rows]


def like_fragments(query: str, cap: int = 16) -> list[str]:
    """查询 → 用来做 LIKE 的短片段。

    **按空格分词之后再切，不是把整句压成一个串再滑窗。** 上一版是后者，
    于是同义词桥等于白加：同义词是拼在问题**后面**的，而滑窗取前 8 个片段时
    它们正好在窗口之外被截掉——「联系方式」展开出来的「电话 / 邮箱」一个都没进
    LIKE。这个 bug 不报错，只让召回悄悄少一截。

    两字词整词收下（trigram 索引查不到它们，只能靠 LIKE）；
    更长的按二元组切。同义词一般是短词，所以它们现在能稳定进窗口。
    """
    out: list[str] = []
    for token in (query or "").split():
        compact = _STRIP.sub("", token)
        if len(compact) < 2:
            continue
        if len(compact) == 2:
            out.append(compact)
        else:
            out.extend(compact[i:i + 2] for i in range(len(compact) - 1))
    return [f for f in dict.fromkeys(out) if len(f) >= 2][:cap]


def like_fallback(query: str, limit: int = 8) -> list[str]:
    """两字词和口语问法走这里。

    拿整句的 2-gram 去 LIKE 标题/摘要/正文，命中一条就算候选。
    精度不高，但它的位置是「FTS5 一个都没召回到」之后的兜底——
    这时候空手而归比给几条弱候选更糟：模型会直接说「资料里没有」。
    """
    fragments = like_fragments(query)
    if not fragments:
        return []
    # 必须**打分**，不能只 WHERE。上一版是 WHERE ... LIMIT 8，没有 ORDER BY，
    # SQLite 就按 rowid 给——等于随机取前 8 个。实测「你的联系方式是什么」
    # 因此把「前端可观测性 › 细节」排到第一位，而库里真正相关的是简历那一页。
    #
    # 权重方向：标题命中比正文命中值钱得多（和 FTS 那边的 title 8 / body 1 同理）。
    # 列名用 kb_units 的真名（page_title / page_summary），不是 FTS 虚表那套。
    score = " + ".join(
        "(CASE WHEN page_title LIKE ? THEN 3 ELSE 0 END"
        " + CASE WHEN heading LIKE ? THEN 2 ELSE 0 END"
        " + CASE WHEN page_summary LIKE ? THEN 2 ELSE 0 END"
        " + CASE WHEN text LIKE ? THEN 1 ELSE 0 END)"
        for _ in fragments)
    params: list[str] = []
    for fragment in fragments:
        like = f"%{fragment}%"
        params += [like, like, like, like]
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT kid FROM kb_units WHERE ({score}) > 0"
            f" ORDER BY ({score}) DESC, kid LIMIT ?",
            (*params, *params, limit)).fetchall()
    return [row[0] for row in rows]


def units_of_pages(page_rels: list[str], exclude: set[str]) -> list[str]:
    """按页面取单元，按页面内的顺序。扩一跳时用。"""
    if not page_rels:
        return []
    marks = ",".join("?" * len(page_rels))
    with closing(connect()) as conn:
        rows = conn.execute(
            f"SELECT kid FROM kb_units WHERE page_rel IN ({marks}) ORDER BY rowid",
            page_rels).fetchall()
    return [row[0] for row in rows if row[0] not in exclude]


def page_title(page_rel: str) -> str:
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT page_title FROM kb_units WHERE page_rel = ? LIMIT 1", (page_rel,)).fetchone()
    return row[0] if row else ""


# ─── 异步包装（API 层用）──────────────────────────────
async def a_units_by_ids(kids):
    return await asyncio.to_thread(units_by_ids, kids)


async def a_evidence_by_ids(eids):
    return await asyncio.to_thread(evidence_by_ids, eids)


async def a_stats():
    return await asyncio.to_thread(stats)
