"""对话模型工厂。

实例做了缓存：ChatDeepSeek 内部持有 httpx 连接池，每次新建都要重做一轮 TLS 握手，
首字能差 0.2s 左右。进程内复用同一个实例即可。

注意 deepseek-flash 是推理模型：先流 `additional_kwargs.reasoning_content`，
再流 `content`。只转发 content 的话，用户在整个思考阶段看不到任何东西。
"""
from functools import lru_cache

from langchain_deepseek import ChatDeepSeek

from app.config import settings

# **必须显式设超时。** 不设的话 httpx 是「等到天荒地老」，
# 而上游连接卡住是真实会发生的：表现是前端点完发送什么都不显示、
# 状态停在「检索资料…」不动，也不报错——因为它确实还在等，
# 只是永远等不到。这种「什么都不发生」比报错难查得多。
CHAT_TIMEOUT = 120          # 正常一轮回答 7-15 秒，120 秒是给长回答留的余量
REWRITE_TIMEOUT = 30        # 改写只输出十几个词，超过 30 秒必然是卡住了


@lru_cache(maxsize=4)
def get_chat_model(streaming: bool = True, timeout: int = CHAT_TIMEOUT) -> ChatDeepSeek:
    settings.require("deepseek_api_key")
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        streaming=streaming,
        # 流式也要账单：不加这个，最后那块 usage 根本不会回来，
        # 前端只能拿字数凑一个估计值。加上它多出来的那一块没有正文，只带用量。
        stream_usage=True,
        temperature=0.3,
        timeout=timeout,
        # 不重试：这是交互式请求，用户就在屏幕前等。
        # 卡住时宁可 120 秒后给一条明确的错误，也不要静默重试到 4 分钟。
        max_retries=0,
    )
