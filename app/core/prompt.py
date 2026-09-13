"""系统提示词加载器。话术放 prompts/*.md，改完存盘即生效，不用重启后端。

按 mtime 缓存：文件没动就吃内存，动了下一条消息重读。
"""
from pathlib import Path

from app.config import settings

FILES = {
    "system": "system.md",      # 主提示词
    "no_hits": "no_hits.md",    # 没检索到资料时追加的说明
    "intro": "intro.md",        # 自我介绍专用：只在命中该路由时拼上，不污染其他回答
    "project": "project.md",    # 讲项目专用，同理
    "assistant": "assistant.md",  # 问到"你能做什么"时，提醒模型这是在问助手不是问用户
    "identity": "identity.md",  # 问到"你是谁"时同理——问的是助手，不是他本人
    "intent": "intent.md",      # 意图分类用的系统提示词，输出一个词
}

_cache: dict[str, tuple[float, str]] = {}   # name -> (mtime, 内容)


def load(name: str) -> str:
    path: Path = settings.prompts_dir / FILES[name]
    try:
        mtime = path.stat().st_mtime
    except OSError as exc:
        raise RuntimeError(f"提示词文件读不到：{path}（{exc}）") from exc

    hit = _cache.get(name)
    if hit and hit[0] == mtime:
        return hit[1]

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise RuntimeError(f"提示词文件是空的：{path}")
    _cache[name] = (mtime, text)
    print(f"[提示词] {'重' if hit else ''}加载 {path.name}（{len(text)} 字）")
    return text
