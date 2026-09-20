"""建库：扫 knowledge/ 下的 md → 按结构切块 → 算 embedding → 写进 SQLite

    python -m app.legacy.ingest           增量，文件内容没变就跳过（不重复花 embedding 的钱）
    python -m app.legacy.ingest --force   全部重切重算
    python -m app.legacy.ingest --dry     只切不算，看看切出来什么模样

文件按路径整篇覆盖，改完文档重跑就行，不用清库。
"""
import asyncio
import hashlib
import inspect
import sys

from app.config import settings
from app.legacy import chunk, vectors
from app.legacy.embed import embed

SKIP = {"README.md"}   # 目录说明不算知识

# 切块代码的指纹。改了 CUES、切分规则这些，md 本身没变，
# 只按内容 sha 判断会误判"未变"而跳过——把指纹掺进 sha 里就自动重建了。
CHUNKER_FP = hashlib.sha256(inspect.getsource(chunk).encode("utf-8")).hexdigest()[:8]


async def build(force: bool = False, dry: bool = False) -> None:
    await vectors.init()
    root = settings.knowledge_dir
    files = [p for p in sorted(root.rglob("*.md")) if p.name not in SKIP]
    if not files:
        print(f"{root} 下没有 .md 文件")
        return

    total_new, contact = 0, {}
    for path in files:
        rel = path.relative_to(root).as_posix()
        md = path.read_text(encoding="utf-8")
        sha = hashlib.sha256(f"{CHUNKER_FP}:{md}".encode("utf-8")).hexdigest()[:16]
        rows = chunk.split(md)

        # 切分时已把隐私串从正文剥离到 meta，这里汇总成一份。
        # 跳过的文件也要收：否则增量跑一次（全跳过）会把联系方式清空
        for r in rows:
            for k in ("phone", "email"):
                if k in r["meta"]:
                    contact[k] = r["meta"][k]

        if dry:
            print(f"\n── {rel}  [{rows[0]['doc_type'] if rows else '?'}]  共 {len(rows)} 块")
            for r in rows:
                parent = f"  ← 父块 {len(r['full_text'])} 字" if len(r["full_text"]) > len(r["text"]) else ""
                print(f"   {'★' if r['priority'] else '·'} {r['title'][:60]}  ({len(r['text'])} 字){parent}")
            continue

        if not force and await vectors.doc_sha(rel) == sha:
            print(f"跳过 {rel} 内容未变")
            continue

        vecs = await embed([r["text"] for r in rows])
        for r, v in zip(rows, vecs):
            r["vec"] = v
        await vectors.replace_doc(rel, sha, rows)
        total_new += len(rows)
        print(f"入库 {rel}  [{rows[0]['doc_type']}]  {len(rows)} 块  嵌入 {len(vecs)} 条")

    if not dry:
        await vectors.set_contact(contact)
        s = await vectors.stats()
        held = "、".join(f"{k}={v}" for k, v in contact.items()) or "无"
        print(f"\n知识库共 {s['chunks']} 块，本次新增/更新嵌入 {total_new} 条")
        print(f"联系方式已剥离到元数据（{held}），正文不含原文")


if __name__ == "__main__":
    asyncio.run(
        build(force="--force" in sys.argv, dry="--dry" in sys.argv)
    )
