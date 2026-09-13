"""wiki/ 的目录常量与路径换算。

目录结构见 SCHEMA.md 第二节、命名见第五节。

统一用 posix 风格的相对路径（`concepts/意图路由.md`）做内部标识：
跨平台比较不会因为反斜杠出岔子，写进 frontmatter 和日志也更好看。
"""
from pathlib import Path

from app.config import settings

# 页面类型 → 目录。这份映射必须和 SCHEMA.md 第一节的表格一致
TYPE_DIR = {
    "summary": "summaries",
    "entity": "entities",
    "concept": "concepts",
    "comparison": "comparisons",
    "query": "queries",
}
DIR_TYPE = {v: k for k, v in TYPE_DIR.items()}
ALLOWED_DIRS = tuple(TYPE_DIR.values())

INDEX = "index.md"
LOG = "log.md"
OVERVIEW = "overview.md"
CACHE_DIR = ".cache"

# 目录说明不算知识。和 app/core/ingest.py 的 SKIP 保持一致，
# 否则同一份 README 会在旧库进切块、在新库进编译，两边对不上。
SKIP_SOURCES = {"README.md"}


def wiki_dir() -> Path:
    return settings.wiki_dir


def page_path(rel: str, root: Path | None = None) -> Path:
    """相对路径 → 绝对路径。root 只在测试里传，正常走 settings。"""
    return (root or wiki_dir()) / rel


def rel_of(path: Path, root: Path | None = None) -> str:
    return path.relative_to(root or wiki_dir()).as_posix()


def summary_rel(source: str) -> str:
    """源文件名 → 摘要页相对路径。一份原始文档对应一个摘要页，必然存在。

    '我的简历.md' → 'summaries/我的简历.md'
    """
    return f"summaries/{Path(source).name}"


def source_files() -> list[Path]:
    """knowledge/ 下全部 .md，按路径排序。**只读，永不写入。**"""
    root = settings.knowledge_dir
    return [p for p in sorted(root.rglob("*.md")) if p.name not in SKIP_SOURCES]
