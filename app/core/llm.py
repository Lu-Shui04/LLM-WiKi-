"""对话模型工厂。

实例做了缓存：ChatDeepSeek 内部持有 httpx 连接池，每次新建都要重做一轮 TLS 握手，
首字能差 0.2s 左右。进程内复用同一个实例即可。

注意 deepseek-flash 是推理模型：先流 `additional_kwargs.reasoning_content`，
再流 `content`。只转发 content 的话，用户在整个思考阶段看不到任何东西。
"""
from functools import lru_cache

from langchain_deepseek import ChatDeepSeek

from app.config import settings


@lru_cache(maxsize=2)
def get_chat_model(streaming: bool = True) -> ChatDeepSeek:
    settings.require("deepseek_api_key")
    return ChatDeepSeek(
        model=settings.deepseek_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        streaming=streaming,
        temperature=0.3,
    )
