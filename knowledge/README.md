# 知识库目录

把要检索的资料丢这里，子目录随便分，比如：

```
knowledge/
  简历/简历-2026.md
  笔记/RISC-V memcpy 优化.md
  笔记/DeepSeek API 踩坑.md
```

规则：
- 只有 `.md` 会被读，其他后缀直接跳过。
- 建库命令会把这里扫一遍，切块 + 算 embedding 存进 `data/agent.db`。
- 改了文件重跑一次建库就行，会按文件路径覆盖旧块。
