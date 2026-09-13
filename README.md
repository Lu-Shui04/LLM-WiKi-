# 个人知识库 Agent（server-py）

面试练习用的个人知识库：把简历和常见问答**编译**成结构化 Wiki，
提问时直接读 Wiki 页面回答，并给出可追溯的引用。

---

## 一、两套检索并存

| | 旧：向量检索 RAG | 新：LLM Wiki |
|---|---|---|
| 相关性裁决的时机 | **查询时**用余弦相似度猜 | **编译时**用 LLM 读全文定 |
| 入口 | Web（`app/api/chat.py`） | CLI（`python -m app.wiki`） |
| 本轮状态 | 未改动，仍可用 | 新增 |

**为什么弃用向量检索**：实测数据留在 `app/api/chat.py` 的注释里——
正样本最低分 0.509（「你平时怎么学习的」）**低于**负样本最高分 0.589
（「帮我写一段 Python 快排」，代码题天然贴近技术语料）。
正负样本在分数上重叠，**没有阈值能把它们分开**。
为此已经在 `chat.py` 里堆了 9 条显式路由 + 意图分类 + 寒暄拦截来绕开检索——
等于用规则手工重建了一个本该由知识组织方式解决的问题。

LLM Wiki 换的是思路：把「查询时用向量猜哪些块相关」换成
「摄入时用 LLM 把文档编译成结构化页面，查询时直接读页面」。
相关性裁决交给一次有全文、有上下文的阅读理解，而不是每次查询时的余弦分数。

原料只有两份文档，编译后页面数远小于上下文窗口——**"检索"这个问题直接消失了**。

---

## 二、安装

```powershell
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m pip install -r requirements-dev.txt   # 跑测试才需要
```

复制 `.env.example` 为 `.env` 并填 key：

```powershell
copy .env.example .env
.venv\Scripts\python.exe -m app.config      # 自检，确认配置读到了
```

> **所有命令都必须用 `.venv\Scripts\python.exe`。**
> PATH 上的裸 `python` 指向另一个解释器（hermes 的 venv），依赖完全不对。

---

## 三、三个命令

### `ingest` — 摄入一份原始文档

```powershell
.venv\Scripts\python.exe -m app.wiki ingest knowledge/我的简历.md
```

流程：读源文档 → LLM 分析该建哪些页 → 分三组生成 → 代码校验 → 落盘 → 渲染 index、追加 log。

常用参数：

| 参数 | 作用 |
|---|---|
| `--dry` | 只打印增量判定与 prompt 体量估算，**不调用模型、不花钱** |
| `--force` | 忽略增量判定，**全部页面整页重编**（不只是绕过文档级跳过） |
| `--no-merge` | 只写摘要页，不动实体/概念/总览（假矛盾误报时的保底开关） |
| `--allow-dangling` | 保留悬空 `[[ ]]` 引用，不降级为纯文本 |
| `--no-reasoning` | 不把编译推理写进 `log.md` |

首次编译一份上万字的文档大约 **3 分钟、4.7 万 token**（`deepseek-v4-pro`）。

**增量跳过**：摘要页的 frontmatter 里记着 `source_sha`（源文档内容哈希）
和 `compiler_fp`（编译规则指纹）。两者都没变时整份文档跳过，**零 token、1 秒内返回**。

**规则变更时 `op: skip` 由代码升级为 `update`**：模型判「这页没有新事实」问的是
**来源**维度，它没有「规则版本」这个概念。规则变了却听它 skip，摘要页的
`compiler_fp` 就永远追不上当前值——于是每次 ingest 都判「规则变了」→ 重编 → 又被 skip，
**永久空转**，页面还一直停在旧规则的产物上。所以这个判断在 `compiler.py` 里，
不在模型手里（`--force` 走同一条路径）。

### `query` — 基于 Wiki 提问

```powershell
.venv\Scripts\python.exe -m app.wiki query "讲一下你的项目"
.venv\Scripts\python.exe -m app.wiki query "…" --show-pages     # 只看会读哪几页
```

**默认把全部页面全文喂进上下文，不做 LLM 选页。**
选页恰恰是又一次「判断准不准」的问题，这套知识库原来就栽在这上面。
页面数超过 `wiki_inline_max_pages`（默认 15）时才退化成代码打分预筛（字符 n-gram），
仍然是确定性的，**不碰向量检索**。`--pages` 可手工指定兜底。

问答话术在 `prompts/wiki_qa.md`，**改完存盘即生效**，不用重启。

### `lint` — 静态检查

```powershell
.venv\Scripts\python.exe -m app.wiki lint
```

六项检查：孤立页面、死链、重复实体/概念、无来源断言、未处理矛盾、frontmatter 缺字段。
有 error 时退出码 1。

---

## 四、目录

```
knowledge/            原始素材层。只读、不可变。一切事实的最终溯源地。
SCHEMA.md             页面契约：类型、frontmatter、命名、矛盾判定、op 规则
wiki/                 编译产物层（纯派生物，删掉重跑即可重建）
  index.md            全局索引（代码渲染，不由 LLM 写）
  log.md              追加式日志（代码追加）
  overview.md         知识库总览
  summaries/          来源摘要页
  entities/           实体页
  concepts/           概念页
  comparisons/        对比页（按需）
  queries/            问答归档（按需）
  .cache/             分析缓存与 LLM 原始输出（不进版本库）
app/wiki/             代码
  __main__.py         CLI 单入口
  compiler.py         两阶段编译
  query.py            选页 + 生成
  lint.py             静态检查
  llm.py              编译模型工厂（v4-pro）
  prompts/            编译提示词（进指纹，不热加载）
  slug / paths / frontmatter / protocol / schema / store    确定性内核
prompts/wiki_qa.md    问答话术（mtime 热加载）
tests/                单测，全部离线
```

**`wiki/` 全是派生物**（`SCHEMA.md` 不在 `wiki/` 里，它在项目根目录，是**源**不是产物）。
删掉 `wiki/` 下除 `.cache/` 之外的一切再重跑 ingest，能得到等价结果——
所以 wiki 坏了不用修，删掉重跑。真相只有一个来源：`knowledge/` + `SCHEMA.md`。

---

## 五、SCHEMA.md 的副作用（重要）

`compiler_fp` = 编译提示词 + `compiler.py` + `SCHEMA.md` 三者的指纹。

**改其中任何一个，下一次 ingest 会全量重编**（约 3 分钟、4.7 万 token）。
包括所有本来就判定为 `skip` 的页面——它们会被升级成 `update` 整页重出，
否则产物会新旧规则混在一起，比「多花一次钱」糟得多。

这是**期望行为**：规范变了，产物就该跟着变。不是 bug。
想避免重编就不要在没有内容变更需求时动这三个地方。

---

## 六、与旧 RAG 的关系

本轮**完全没碰** `app/api/chat.py`，Web 端仍走原来的向量检索。
CLI 走新 Wiki，两者并存、互不影响。

下一轮才做的事：

- 把 `chat.py` 的检索决策搬进 `app/legacy/rag_chat.py`
- 加 `chat_backend` 分派开关（配置项已存在，只是还没接线）
- 废弃旧向量层

已经探明的两个 Web 兼容性硬约束，留给下一轮：

- `web/index.html:466` 是 `chip.textContent = \`${it.doc}【${it.n}】\``，
  wiki 模式下 `doc` 要填短名（`concepts/意图路由`）而不是全路径，否则 chip 会被撑长
- `_StripMarks` 引用编号清洗、`reasoning_content` → `thinking` 转发、`session.lock`
  这三样绝不能在切换时误砍

---

## 七、测试

```powershell
.venv\Scripts\python.exe -m pytest -v
```

只测确定性内核（slug / frontmatter / protocol / store / lint / query 的选页逻辑 /
compiler 里不碰模型的那几个纯函数）。**全部离线**，不联网、不打 API、不需要 key。

其中 `tests/test_compiler.py` 盯的是两条**静默失效**的规则：摘要页路径必须由代码定死、
`op: skip` 的页面不能进生成环节。它们出错时不会报错，只会让每次 ingest 都重编——
这种问题靠肉眼看产物发现不了，只能靠测试钉住。

编译和问答要打 API，不做自动化测试，靠 CLI 手动验证。

---

## 八、常见问题

**Q：编译出来页面只有两三个，而且摘要页特别长？**
说明分析阶段把建页门槛设太高了。看 `wiki/.cache/last-raw/00__plan.json`
就能看到它当时规划了哪些页——那是诊断这类问题的第一现场。

**Q：`[[某个页面]]` 变成了纯文本？**
那是**死链降级**（见 SCHEMA.md 第十二节）：链接指向的页面不存在，
留着点不开反而是噪声。`log.md` 里有记录。确实需要保留悬空引用时加 `--allow-dangling`。

**Q：页面上有 `> [!WARNING] 矛盾`，但我觉得那两个说法并不冲突？**
看 `wiki/.cache/last-raw/_reasoning.txt`，里面有模型当时的推理过程——
那是唯一能看出它"为什么把这条判成矛盾"的地方。如果确实是误报，
改 `SCHEMA.md` 第六节的判定条款（改了会触发全量重编，这是应该的）。

**Q：联系方式会不会进模型？**
会。这是明确确认过的选择——**不做隐私剥离**，电话和邮箱以明文写进 wiki 页面，
每次相关提问都会重新进入模型上下文。打包或截图给别人看时请自行留意。
