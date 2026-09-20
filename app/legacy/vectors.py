"""知识库向量表。切块和向量一起存 SQLite，检索在内存里算 numpy 余弦。

不上 pgvector：单机个人知识库几百到几万块，内存点积是亚毫秒级，省掉一整套容器和连接。
真撑不住再换，换的时候只动 search()，上层不用知道。

向量存 BLOB（float32 裸字节），行读出来一次性堆成矩阵，查询就是一次矩阵乘法。
"""
import asyncio
import json
from contextlib import closing
from datetime import datetime, timezone

import numpy as np

from app.db.sqlite import connect

PRIORITY_BONUS = 0.03   # 标了 priority 的块加这一点分，只用来打破平局，不改变量级

# 改了 SCHEMA 或 app/legacy/chunk.py 的切块逻辑就 +1。切块是从 knowledge/ 重算出来的派生物，
# 与其写迁移，不如整个重建——省得再踩一次「CREATE TABLE IF NOT EXISTS 不改已存在的表」。
# 也顺手解决了「切块代码变了但 md 没变，ingest 按 sha 判定跳过」的问题。
SCHEMA_VERSION = 2

SCHEMA = """
CREATE TABLE IF NOT EXISTS kb_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS chunks (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_path   TEXT NOT NULL,
    doc_type   TEXT NOT NULL,
    parent_key TEXT,
    ord        INTEGER NOT NULL,
    title      TEXT NOT NULL,
    text       TEXT NOT NULL,          -- 实际送去 embedding 的
    full_text  TEXT NOT NULL,          -- 命中后真正喂给模型的（子块这里是父块全文）
    priority   INTEGER NOT NULL DEFAULT 0,
    meta       TEXT NOT NULL,
    vec        BLOB NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_path);

CREATE TABLE IF NOT EXISTS docs (
    path       TEXT PRIMARY KEY,
    sha        TEXT NOT NULL,
    chunks     INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);
"""

_cache: dict = {"mat": None, "rows": None}


def _unit(a: np.ndarray) -> np.ndarray:
    return a / (np.linalg.norm(a, axis=-1, keepdims=True) + 1e-9)


# ─── 同步实现 ────────────────────────────────────────
def _init() -> None:
    with closing(connect()) as conn:
        conn.executescript("CREATE TABLE IF NOT EXISTS kb_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);")
        row = conn.execute("SELECT value FROM kb_meta WHERE key = 'schema_version'").fetchone()
        cur = row["value"] if row else ""
        has_chunks = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'chunks'"
        ).fetchone()
        # 没有版本号却有 chunks 表 = 加版本号之前建的，schema 未知，当过期处理
        if cur != str(SCHEMA_VERSION) and (cur or has_chunks):
            print(f"[知识库] 向量表 schema {cur or '未知'} → {SCHEMA_VERSION}，重建")
            conn.executescript("DROP TABLE IF EXISTS chunks; DROP TABLE IF EXISTS docs;")
        with conn:
            conn.executescript(SCHEMA)
            conn.execute(
                "INSERT INTO kb_meta (key, value) VALUES ('schema_version', ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(SCHEMA_VERSION),),
            )


def _doc_sha(path: str) -> str | None:
    with closing(connect()) as conn:
        row = conn.execute("SELECT sha FROM docs WHERE path = ?", (path,)).fetchone()
    return row["sha"] if row else None


def _replace_doc(path: str, sha: str, rows: list[dict]) -> None:
    """整篇覆盖：先删旧块再写。改文档重跑就够，不用清库。"""
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM chunks WHERE doc_path = ?", (path,))
        conn.executemany(
            "INSERT INTO chunks (doc_path, doc_type, parent_key, ord, title, text,"
            " full_text, priority, meta, vec) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    path, r["doc_type"], r["parent_key"], r["ord"], r["title"],
                    r["text"], r["full_text"], r["priority"],
                    json.dumps(r["meta"], ensure_ascii=False),
                    np.asarray(r["vec"], dtype=np.float32).tobytes(),
                )
                for r in rows
            ],
        )
        conn.execute(
            "INSERT INTO docs (path, sha, chunks, updated_at) VALUES (?, ?, ?, ?)"
            " ON CONFLICT(path) DO UPDATE SET sha = excluded.sha,"
            " chunks = excluded.chunks, updated_at = excluded.updated_at",
            (path, sha, len(rows), now),
        )
    _cache["mat"] = None            # 库变了，缓存作废


def _load() -> tuple[np.ndarray, list[dict]]:
    if _cache["mat"] is None:
        with closing(connect()) as conn:
            rows = [
                dict(r)
                for r in conn.execute(
                    "SELECT id, doc_path, doc_type, title, parent_key, text, full_text,"
                    " priority, meta, vec FROM chunks ORDER BY id"
                )
            ]
        mat = (
            _unit(np.stack([np.frombuffer(r["vec"], dtype=np.float32) for r in rows]))
            if rows
            else np.zeros((0, 1), dtype=np.float32)
        )
        _cache["mat"], _cache["rows"] = mat, rows
    return _cache["mat"], _cache["rows"]


def _search(
    vec: list[float], top_k: int, min_score: float, docs: list[str] | None = None
) -> list[dict]:
    """docs 限定只在哪几个文档里找。意图识别判出「只问简历」时，能挡住常见问答混进来。"""
    mat, rows = _load()
    if not rows:
        return []

    prios = np.array([r["priority"] for r in rows], dtype=np.float32)
    scores = mat @ _unit(np.asarray(vec, dtype=np.float32)) + PRIORITY_BONUS * prios

    hits: list[dict] = []
    seen: set[str] = set()
    for i in np.argsort(-scores):
        if scores[i] < min_score:
            break                                   # 已按分数降序，后面的只会更低
        r = rows[i]
        if docs and not any(d in r["doc_path"] for d in docs):
            continue
        key = r["full_text"][:80]                   # 同一个父块只留一条，别把整个项目塞进上下文三遍
        if key in seen:
            continue
        seen.add(key)
        hits.append(
            {
                "doc": r["doc_path"],
                "doc_type": r["doc_type"],
                # 喂的是父块全文时，标题就得是父块的名字。否则点开来源一看，
                # 标签写着「一段可直接背诵的完整表达」、摊开是整个项目，谁看都懵
                "title": (
                    (r["parent_key"] or r["title"])
                    if len(r["full_text"]) > len(r["text"]) * 1.5
                    else r["title"]
                ),
                "text": r["full_text"],
                "score": round(float(scores[i]), 4),
                "priority": r["priority"],
                "meta": json.loads(r["meta"]),
            }
        )
        if len(hits) >= top_k:
            break
    return hits


def _stats() -> dict:
    with closing(connect()) as conn:
        row = conn.execute("SELECT COUNT(*) AS n FROM chunks").fetchone()
    return {"chunks": row["n"] or 0}


def _by_title(names: list[str], own_text: bool = False) -> list[dict]:
    """按标题路径取块，给显式路由用——不走向量检索，也就没有分数可推断错。

    顺序按 names 给，不是按 id；同一段能被多个名字命中时只取第一次。

    own_text=True 返回子块自己那一段，而不是它所属父块的全文。默认关着，
    因为大多数栏目（基本信息、荣誉证书）父子内容本来就一样，给全文更完整；
    但项目那种 3412 字的父块得开——整篇塞进上下文既占地方，
    标题写着「项目背景」内容却是全篇，点开也对不上。
    """
    with closing(connect()) as conn:
        rows = [dict(r) for r in conn.execute("SELECT * FROM chunks")]
    out, used = [], set()
    for name in names:
        for r in rows:
            if r["id"] in used or name not in r["title"]:
                continue
            used.add(r["id"])
            out.append(
                {
                    "doc": r["doc_path"],
                    "doc_type": r["doc_type"],
                    "title": r["title"],
                    "text": r["text"] if own_text else r["full_text"],
                    "score": 1.0,          # 路由命中没有相似度可言，给满分只为了前端排序稳定
                    "priority": r["priority"],
                    "meta": json.loads(r["meta"]),
                }
            )
    return out


# 联系方式单独存一行。不进 chunks、不进向量、不进模型上下文，
# 问到的时候由 chat.py 直接读这里返回——真值只在这条路径上出现。
def _set_contact(contact: dict) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO kb_meta (key, value) VALUES ('contact', ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (json.dumps(contact, ensure_ascii=False),),
        )


def _contact() -> dict:
    with closing(connect()) as conn:
        row = conn.execute("SELECT value FROM kb_meta WHERE key = 'contact'").fetchone()
    return json.loads(row["value"]) if row else {}


# ─── 异步包装 ────────────────────────────────────────
async def init() -> None:
    await asyncio.to_thread(_init)


async def doc_sha(path: str) -> str | None:
    return await asyncio.to_thread(_doc_sha, path)


async def replace_doc(path: str, sha: str, rows: list[dict]) -> None:
    await asyncio.to_thread(_replace_doc, path, sha, rows)


async def search(
    vec: list[float], top_k: int = 4, min_score: float = 0.35, docs: list[str] | None = None
) -> list[dict]:
    return await asyncio.to_thread(_search, vec, top_k, min_score, docs)


async def stats() -> dict:
    return await asyncio.to_thread(_stats)


async def set_contact(contact: dict) -> None:
    await asyncio.to_thread(_set_contact, contact)


async def contact() -> dict:
    return await asyncio.to_thread(_contact)


async def by_title(names: list[str], own_text: bool = False) -> list[dict]:
    return await asyncio.to_thread(_by_title, names, own_text)
