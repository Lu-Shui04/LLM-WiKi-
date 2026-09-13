"""中文 slug 规范化：标题 → 能安全落盘的 Windows 文件名。

全部是确定性变换，没有 LLM、没有网络。规则见 SCHEMA.md 第五节。

为什么不用 python-slugify 那类库：它们默认把非 ASCII 全丢掉，
「意图路由」会变成空字符串。这里的中文必须原样保留——产物是给人翻的。
"""
import hashlib
import re
import unicodedata
from collections.abc import Iterable

# Windows 文件名非法字符 + ASCII 控制字符
_ILLEGAL = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_SPACE = re.compile(r"[\s　]+")     # 含全角空格
_DASH = re.compile(r"-{2,}")
_MAX = 60


def slugify(title: str) -> str:
    """标题 → slug。规范化后为空时兜底成 page-<hash8>（lint 会提示人工指定）。"""
    s = unicodedata.normalize("NFC", title or "")
    s = _ILLEGAL.sub("", s)
    s = _SPACE.sub("-", s)
    s = _DASH.sub("-", s)
    s = s.strip("-.")                   # 首尾的 - 和 . 都不要（Windows 不允许 . 结尾）
    if len(s) > _MAX:
        s = s[:_MAX].strip("-.")        # 截断后可能又露出一个 - 或 .
    if not s:
        s = "page-" + hashlib.sha256((title or "").encode("utf-8")).hexdigest()[:8]
    return s


def unique_slug(slug: str, taken: Iterable[str]) -> str:
    """判重必须**大小写不敏感**——NTFS 不区分大小写，
    `RAG切块.md` 和 `rag切块.md` 在磁盘上是同一个文件，撞了会静默覆盖。

    taken 传已占用的 slug（不含扩展名）。撞了就加 -2、-3……
    """
    used = {t.lower() for t in taken}
    if slug.lower() not in used:
        return slug
    n = 2
    while f"{slug}-{n}".lower() in used:
        n += 1
    return f"{slug}-{n}"
