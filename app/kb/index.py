"""索引编排：指纹 → 需要才重建。**整条路不打一次模型。**

索引是从 knowledge/ 和 wiki/ 算出来的派生物，所以它唯一的正确性问题就是
「什么时候该重算」。这里的判据是一个指纹：

    源文件（名字 + 内容哈希）  +  页面（路径 + 正文哈希）  +  切法版本

任何一项变了，指纹就变，下次启动（或下次提问前）自动重建。
什么都没变时只读文件算哈希，一个 SQL 写操作都不发。

切法版本（CHUNK_FP / UNITS_FP）必须手工维护：改了 chunk.py 的切分规则或
units.py 的拆块规则，就 +1。不这么做的话，改了代码但源文件和页面都没动，
指纹不变，索引会一直停在旧规则的产物上——而且它**看起来完全正常**，
是那种查不出问题也修不好的静默失效。
"""
import hashlib
from pathlib import Path

from app.config import settings
from app.kb import bind as binder
from app.kb import chunk, store, units as unit_mod
from app.wiki import paths, store as wiki_store

CHUNK_FP = 1        # 改 chunk.py 的切分规则就 +1
UNITS_FP = 1        # 改 units.py 的拆块规则就 +1


class IndexError_(RuntimeError):
    """建索引失败。名字带下划线是为了不和内置的 IndexError 撞。"""


def _digest(*parts: str) -> str:
    hasher = hashlib.sha256()
    for part in parts:
        hasher.update(part.encode("utf-8"))
        hasher.update(b"\x00")
    return hasher.hexdigest()[:16]


def evidence_id(source: str, start: int, end: int) -> str:
    """证据 ID 由「哪个文件的哪段区间」决定，所以重建后 ID 不变。

    不变很重要：引用是存进回答里的，重建一次就换一批 ID 的话，
    历史回答里的引用就全点不开了。
    """
    return "E-" + hashlib.sha1(f"{source}#{start}-{end}".encode("utf-8")).hexdigest()[:12]


def _load_sources() -> tuple[list[dict], list[store.Evidence]]:
    """knowledge/ 下的每一份资料 → (来源行, 证据片段)。"""
    sources: list[dict] = []
    evidence: list[store.Evidence] = []
    for path in paths.source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise IndexError_(f"读不了 {path.name}：{exc}") from exc
        sources.append({"name": path.name, "sha": chunk_digest(text), "text": text})
        for seq, fragment in enumerate(chunk.split(text)):
            evidence.append(store.Evidence(
                eid=evidence_id(path.name, fragment.char_start, fragment.char_end),
                source=path.name,
                path=fragment.path,
                char_start=fragment.char_start,
                char_end=fragment.char_end,
                quote=fragment.quote,
                seq=seq,
            ))
    return sources, evidence


def chunk_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _resolve_pages(links, pages) -> list[tuple[str, str, str]]:
    """「相关」里写的是页面标题，要解析成路径才能扩一跳。

    对不上的直接丢掉——加工过的页面里死链早就被降级成纯文本了，
    这里出现对不上的名字只可能是页面被删过，留着它扩一跳会扩到空。
    """
    by_name: dict[str, str] = {}
    for page in pages:
        title = str(page.meta.get("title", "")).strip()
        if title:
            by_name[title] = page.rel
        by_name.setdefault(Path(page.rel).stem, page.rel)
    out = []
    for link in links:
        target_rel = by_name.get(link.target, "")
        out.append((link.page_rel, target_rel, link.target))
    return out


def fingerprint() -> str:
    """当前磁盘状态 → 指纹。只读文件，不发 SQL。"""
    parts = [f"chunk={CHUNK_FP}", f"units={UNITS_FP}"]
    for path in paths.source_files():
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            parts.append(f"src:{path.name}:unreadable")
            continue
        parts.append(f"src:{path.name}:{chunk_digest(text)}")
    for page in wiki_store.read_all():
        parts.append(f"page:{page.rel}:{_digest(page.body)[:12]}")
    return _digest(*parts)


def rebuild() -> dict:
    """无条件重建。"""
    sources, evidence = _load_sources()
    pages = wiki_store.read_all()
    scan = unit_mod.scan(pages)
    links, bind_stats = binder.bind(scan.units, evidence)
    page_links = _resolve_pages(scan.links, pages)

    store.replace(sources, evidence, scan.units, links, page_links)
    print(f"[kb] 索引重建：{len(sources)} 份资料 / {len(evidence)} 条证据 / "
          f"{len(scan.units)} 个知识单元 / {len(links)} 条绑定边 / "
          f"{len(page_links)} 条页间关系")
    return {
        "sources": len(sources),
        "evidence": len(evidence),
        "units": len(scan.units),
        "links": len(links),
        "page_links": len(page_links),
        "bind": bind_stats,
    }


def ensure(force: bool = False) -> bool:
    """指纹没变就什么都不做。返回是否真的重建了。

    启动时、以及每次提问前都会调一次。文件不多时它只是读几十 KB 算个哈希，
    比一次 SQLite 查询还快；真到了几千份资料的规模，再把它改成
    按 mtime+size 判断——那时候这次优化才有意义。
    """
    store.ensure_schema()
    current = fingerprint()
    if not force and store.get_meta("fingerprint") == current:
        return False
    rebuild()
    store.set_meta("fingerprint", current)
    return True


def status() -> dict:
    current = fingerprint()
    info = store.stats()
    info["stale"] = info.get("fingerprint") != current
    info["chunk_fp"] = CHUNK_FP
    info["units_fp"] = UNITS_FP
    return info
