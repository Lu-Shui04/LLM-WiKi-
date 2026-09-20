"""旧链路归档区。

这里的代码**默认不执行**：`settings.chat_backend` 是 "wiki" 时，一次请求都不会
走到这个包里。留着它的唯一价值是随时能切回去对照——尤其是那个
「向量检索在这个库上分不开正负样本」的实测结论（见 vectors.py 顶部的注释），
删掉就只剩一句没有证据的说法了。

    rag_chat.py   旧的向量检索对话流
    vectors.py    切块表 + 余弦检索
    embed.py      智谱 embedding（直接打 REST，不引 langchain-zhipuai）
    chunk.py      按 Markdown 结构切块
    ingest.py     建向量库的 CLI：python -m app.legacy.ingest
    postgres.py   pgvector 连接串（没接线，留作参考）

要彻底退役时：删掉整个包 + config.py 里的 zhipu_* / pg_* / embedding_* 配置项，
再把 chat_backend 开关一并去掉。
"""
