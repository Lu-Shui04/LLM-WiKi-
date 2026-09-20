"""智谱 embedding。直接打 REST，不引 langchain-zhipuai。

那个包对 langchain-core 1.x 支持不稳，而这里要的只是一个 POST，httpx 本来就在依赖里。
一次最多 64 条（接口上限），429 和 5xx 退避重试。
"""
import asyncio

import httpx

from app.config import settings

BATCH = 64
RETRY = 3


class EmbedError(RuntimeError):
    pass


async def embed(texts: list[str]) -> list[list[float]]:
    settings.require("zhipu_api_key")
    if not texts:
        return []

    url = f"{settings.zhipu_base_url.rstrip('/')}/embeddings"
    headers = {"Authorization": f"Bearer {settings.zhipu_api_key}"}
    out: list[list[float]] = []

    async with httpx.AsyncClient(timeout=60) as client:
        for start in range(0, len(texts), BATCH):
            payload = {
                "model": settings.embedding_model,
                "input": texts[start : start + BATCH],
                "dimensions": settings.embedding_dim,
            }
            for attempt in range(RETRY):
                resp = await client.post(url, headers=headers, json=payload)
                if resp.status_code == 200:
                    break
                # 限流和 5xx 值得重试，4xx 其余的是 key 或参数问题，重试也一样
                if resp.status_code != 429 and resp.status_code < 500:
                    raise EmbedError(f"智谱返回 {resp.status_code}：{resp.text[:200]}")
                if attempt == RETRY - 1:
                    raise EmbedError(f"智谱连续失败 {resp.status_code}：{resp.text[:200]}")
                await asyncio.sleep(2**attempt)

            data = resp.json()["data"]
            data.sort(key=lambda d: d["index"])   # 接口不保证顺序，按 index 排回来，否则向量和文本错位
            out.extend(d["embedding"] for d in data)

    return out
