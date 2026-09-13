"""多文件输出协议：`===FILE: <路径>===` 分隔符。

为什么正文不用 JSON：中文 Markdown 嵌进 JSON，换行和引号必须转义，
模型漏一处，`json.loads` 是**全有或全无**——一次编译全废。
分隔符协议是行级容错的，坏一段只坏一段，还能原样落盘，少一层「解析→再序列化」。

宽松到什么程度（SCHEMA.md 第十节）：
  - 大小写不敏感、两边随意留空格
  - 被 ``` 围栏包住也认（那是模型的书写习惯，不是内容）
  - 最后一段缺结尾 === 也算数

严到什么程度：
  - 路径必须落在白名单目录里、必须 .md、拒绝绝对路径和 `..`
    （这条不能宽——路径穿越会写出 wiki/ 之外）
"""
import re
from pathlib import Path

from app.wiki import paths

_FILE = re.compile(r"^===\s*FILE\s*:\s*(.+?)\s*===\s*$", re.IGNORECASE)
_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*$")

# Windows 不允许出现在文件名里的字符。`: ` 尤其阴——NTFS 把它当数据流（ADS），
# 写 "chunkSize: 500.md" 会在磁盘上留下一个名为 chunkSize 的空文件，正文进 ADS，
# 之后 read_all 永远读不到这一页。这些都必须挡在写盘之前。
_BAD_NAME = re.compile(r'[\\/:*?"<>|\x00-\x1f]')

# Windows 保留设备名：即使带扩展名（"nul.md"）也不能用，
# 写它 write_text 会**成功返回**而磁盘上什么都没有——页面静默消失。
_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


class ProtocolError(ValueError):
    """路径不合法，或整段输出里一个文件标记都没有。"""


def parse_files(text: str) -> list[tuple[str, str]]:
    """→ [(相对路径, 正文)]。路径已规范化并校验过。"""
    out: list[tuple[str, str]] = []
    cur: str | None = None
    buf: list[str] = []

    for line in (text or "").splitlines():
        m = _FILE.match(line.strip())
        if m:
            if cur is not None:
                out.append((cur, "\n".join(buf).strip()))
            cur = check_rel(m.group(1))
            buf = []
            continue
        if _FENCE.match(line):
            continue                    # 围栏是书写习惯，丢掉
        if cur is not None:
            buf.append(line)

    if cur is not None:                 # 末段缺结尾 === 也认
        out.append((cur, "\n".join(buf).strip()))

    if not out:
        raise ProtocolError("输出里没有任何 ===FILE: <路径>=== 标记")
    return out


def check_rel(raw: str) -> str:
    """校验并规范化一个输出路径。不合规直接抛，不静默丢弃——
    路径写错的页面悄悄消失，比整次编译失败更难查。"""
    rel = (raw or "").strip().strip("`").strip().replace("\\", "/")
    if not rel:
        raise ProtocolError("空路径")
    if rel.startswith("/") or re.match(r"^[a-zA-Z]:", rel):
        raise ProtocolError(f"不接受绝对路径：{raw}")

    parts = [p for p in rel.split("/") if p not in ("", ".")]
    if any(p == ".." for p in parts):
        raise ProtocolError(f"不接受上级目录：{raw}")
    # 大小写不敏感地认总览页：分隔符协议本身就是大小写不敏感的
    # （_FILE 带 IGNORECASE），模型写 Overview.md 不该废掉整次编译。
    if len(parts) == 1 and parts[0].casefold() == paths.OVERVIEW.casefold():
        return paths.OVERVIEW           # 总览页在根目录，是唯一的例外

    if len(parts) != 2:
        raise ProtocolError(f"必须是 <目录>/<文件名>.md：{raw}")
    d, name = parts
    if d not in paths.ALLOWED_DIRS:
        raise ProtocolError(
            f"目录必须是 {list(paths.ALLOWED_DIRS)} 之一：{raw}"
        )
    if not name.endswith(".md") or name == ".md":
        raise ProtocolError(f"只接受 .md 文件：{raw}")

    # 文件名字符集也要挡。这里**拒绝而不是自动改名**：改名之后 rel 就和模型写的
    # ===FILE: 标记对不上了，页面会掉进「计划外页面」分支——对摘要页来说，
    # 那意味着 source_sha/compiler_fp 落不到该落的页上，增量跳过永久失效。
    # 宁可整次编译硬失败（一个字都没写），也不要悄悄换个名字。
    if _BAD_NAME.search(name):
        raise ProtocolError(
            f"文件名里有 Windows 不允许的字符（写盘时会静默失败或变成数据流）：{raw}"
        )
    stem = Path(name).stem
    if stem.upper() in _RESERVED:
        raise ProtocolError(
            f"{stem} 是 Windows 保留设备名，写它不会在磁盘上产生文件：{raw}"
        )
    if name != name.rstrip(". "):
        raise ProtocolError(f"文件名不能以点或空格结尾：{raw}")
    return f"{d}/{name}"


def stem_of(rel: str) -> str:
    """'concepts/意图路由.md' → '意图路由'"""
    return Path(rel).stem
