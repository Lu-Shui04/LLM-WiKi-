"""会话日志：给一轮问答发一个 trace id，全链路都带着它。

为什么要有这个东西：一轮问答会依次经过

    检索词改写 → FTS5 召回 → 取证据 → 拼提示词 → 调模型 → 解引用 → 落盘

而在这之前，服务端**一行日志都没有**。出问题时只能看到 uvicorn 那句
「POST /api/chat 200 OK」——它连请求是死是活都分不清。真出过一次：
用户说「发了消息没反应」，我翻遍日志只知道请求发出去了，不知道卡在哪一步、
卡了多久。查这种问题比修它慢十倍。

所以这里做的只有一件事：**每一步都记一行，每行都带同一串 trace id**。
出了问题把那串 id 贴出来，或者直接 grep 日志文件，就能看到这一轮走到哪、
每一步花了多久、哪一步返回的是空的。

用起来是这样：

    from app.core import log

    log.setup()
    with log.trace_scope() as trace:
        t = log.Timer()
        ...
        log.step("kb.search", 单元=8, 用时=t.ms)
        log.step("model.done", 输入=2095, 输出=687, 用时=8100)

输出长这样（trace 那一列是同一轮的全部日志）：

    14:22:26 INFO  [a3f9c21b] agent         chat.start      长度=9
    14:22:26 INFO  [a3f9c21b] agent         kb.search       FTS=8 LIKE=0 用时=12ms
    14:22:34 INFO  [a3f9c21b] agent         model.done      输入=2095 输出=687
    14:22:34 INFO  [a3f9c21b] agent         chat.done       总用时=8.4s
"""
import logging
import logging.handlers
import sys
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from app.config import settings

# trace 走 ContextVar 而不是参数透传：一轮问答会跨好几个模块（api → kb → wiki），
# 一路把它当参数传下去，每个函数签名都要多一个没人关心的形参。
# ContextVar 是按**协程**隔离的——同一个进程里两个用户同时提问，各自的
# trace 不会串到对方那边去，这一点是靠传全局变量做不到的。
_trace: ContextVar[str] = ContextVar("trace", default="-")

_FORMAT = "%(asctime)s %(levelname)-5s [%(trace)s] %(name)-9s %(message)s"
_DATEFMT = "%H:%M:%S"
_configured = False

# 环境变量里写 "debug" / "info" / "warning" 都认
LEVELS = {"DEBUG": logging.DEBUG, "INFO": logging.INFO,
          "WARNING": logging.WARNING, "ERROR": logging.ERROR}

log = logging.getLogger("agent")


class _TraceFilter(logging.Filter):
    """把当前 trace 塞进每条记录。格式化串里的 %(trace)s 就是它。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.trace = _trace.get()
        return True


def new_trace() -> str:
    """一轮问答的编号。8 位十六进制：短到能口头念，也够不容易撞。"""
    return uuid.uuid4().hex[:8]


def current_trace() -> str:
    return _trace.get()


@contextmanager
def trace_scope(trace: str = ""):
    """之后所有日志都带上这个编号。可以嵌套，内层覆盖外层。"""
    token = _trace.set(trace or new_trace())
    try:
        yield _trace.get()
    finally:
        _trace.reset(token)


def setup(level: int | None = None, to_file: bool = True) -> None:
    """装一次就够，重复调用是空操作（uvicorn --reload 会重跑模块级代码）。"""
    global _configured
    if _configured:
        return
    _configured = True

    # Windows 控制台默认是 GBK，中文日志会显示成乱码——而「乱码的日志」
    # 和「没有日志」在排查时是一回事。文件那一路本来就用 utf-8，这里只修控制台。
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass        # 被重定向到不支持重配置的对象上时就算了，不影响写文件

    root = logging.getLogger()
    root.setLevel(level or LEVELS.get(settings.log_level.upper(), logging.INFO))

    formatter = logging.Formatter(_FORMAT, datefmt=_DATEFMT)
    trace_filter = _TraceFilter()

    console = logging.StreamHandler()
    console.setFormatter(formatter)
    console.addFilter(trace_filter)
    root.addHandler(console)

    if to_file:
        try:
            settings.log_dir.mkdir(parents=True, exist_ok=True)
            # 轮转：单个文件 5MB，留 3 份。不轮转的话跑久了日志会大到打不开——
            # 而这个文件恰恰是出问题时要打开的那个，打不开就等于没有。
            handler = logging.handlers.RotatingFileHandler(
                settings.log_dir / "session.log",
                maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
            )
            handler.setFormatter(formatter)
            handler.addFilter(trace_filter)
            root.addHandler(handler)
        except OSError as exc:
            # 写不了文件不该让服务起不来，控制台照常输出
            root.warning("日志文件打不开，只输出到控制台：%s", exc)

    # uvicorn 自己的访问日志太吵，而且它没有 trace，留着只会稀释真正有用的行
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)


class Timer:
    """记一步花了多久。用法：

        t = Timer()
        ...
        log.step("kb.search", 用时=t.ms)
    """

    __slots__ = ("_t0",)

    def __init__(self) -> None:
        self._t0 = time.perf_counter()

    @property
    def ms(self) -> str:
        return f"{self.elapsed_ms:.0f}ms"

    @property
    def elapsed_ms(self) -> float:
        return (time.perf_counter() - self._t0) * 1000


def _render(fields: dict) -> str:
    parts = []
    for key, value in fields.items():
        if isinstance(value, float):
            value = round(value, 1)
        elif isinstance(value, (list, tuple, set)):
            value = "、".join(str(v) for v in value) or "（空）"
        elif isinstance(value, str) and len(value) > 60:
            value = value[:57] + "..."
        parts.append(f"{key}={value}")
    return " ".join(parts)


def step(name: str, **fields) -> None:
    """记一步。字段名用中文——这些行是给人读的，不是给解析器读的。"""
    log.info("%-15s %s", name, _render(fields))


def warn(name: str, **fields) -> None:
    log.warning("%-15s %s", name, _render(fields))
