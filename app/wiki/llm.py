"""编译模型工厂。

和问答分开配置：编译是一次性深度加工，产物质量决定之后所有问答的质量，
所以用 v4-pro、temperature 压到 0.2（问答是 flash + 0.3）。
**编译要稳，不要发挥。**

实例缓存，理由同 app/core/llm.py：ChatDeepSeek 内部持有 httpx 连接池。

v4-pro 也是推理模型：先流 `additional_kwargs.reasoning_content`，再流 `content`。
"""
from functools import lru_cache

from langchain_deepseek import ChatDeepSeek

from app.config import settings


@lru_cache(maxsize=2)
def get_ingest_model(streaming: bool = True) -> ChatDeepSeek:
    settings.require("deepseek_api_key")
    return ChatDeepSeek(
        model=settings.ingest_model,
        api_key=settings.deepseek_api_key,
        api_base=settings.deepseek_base_url,
        streaming=streaming,
        temperature=0.2,
        # v4-pro 是推理模型，reasoning_content 和 content **共享**这个额度。
        # 分析一步动辄推理一两万字，给 8192 会让 JSON 根本没机会输出——
        # 表现是「返回了几百字、但不是 JSON」，很难往截断上想。实测 65536 可用。
        max_tokens=32768,
        # 一次编译调用可能跑好几分钟，别用库里的默认超时
        timeout=900,
        max_retries=2,
    )
