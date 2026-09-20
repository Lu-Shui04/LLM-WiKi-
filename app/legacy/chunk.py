"""切分策略。两份文档结构不同，用一套切法必然有一边是错的。

简历（叙事型）：`### 速购 AI 客服系统` 是父块，底下的 `####` 小节是子块。
子块单独拿去检索会丢上下文——问「RAG 怎么做的」命中的是 `#### 三、RAG…`，
但模型看到整个项目背景才知道这段话在说什么。所以父块的完整正文（含所有后代）
在切分时展开进子块的 full_text，检索命中子块就回吐父块全文。

答疑（问答型）：按 `## N. 问题` 切，一问一答不许拆开，拆碎等于没有。
问题文本抽进 metadata。

两种切法都只产出"可检索块"——父块不单独入库，它的正文已经在子块的 full_text 里了。
"""
import re

HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
FAQ_ASK = re.compile(r"^\*\*问：(.*?)\*\*\s*$", re.M)

# 隐私串。留在正文里的话，每次检索命中都会跟着进第三方模型 API——
# 问「你最大的体会是什么」也照样把手机号发出去，没必要。
# 抽出来放 meta，正文只留占位符；问到联系方式时由代码直接读 meta 返回，不进模型。
EMAIL = re.compile(r"[\w.\-+]+@[\w\-]+\.[A-Za-z]{2,}")
PHONE = re.compile(r"\b1[3-9]\d{9}\b|\b1[3-9]\d-\d{4}-\d{4}\b")


def scrub(text: str) -> tuple[str, dict]:
    """返回（清干净的正文, 抽出来的隐私字段）"""
    meta: dict[str, str] = {}

    def take(m: re.Match, key: str) -> str:
        meta[key] = f"{meta[key]} / {m.group()}" if key in meta else m.group()
        return "[见元数据]"

    # 先邮箱再电话：邮箱里常带手机号（13800000000@qq.com），那串号段本身也匹配电话正则，
    # 顺序反了就会把邮箱拆成两半
    text = EMAIL.sub(lambda m: take(m, "email"), text)
    text = PHONE.sub(lambda m: take(m, "phone"), text)
    return text, meta

# 简历里这些小节是面试现场真会被复述的，检索时加权
HIGH_PRIORITY = ("完整表达", "背诵", "取舍", "追问", "怎么答")

# 标题里出现左边这些词，就给嵌入文本补一句"人话"。
# 因为它们是写给人看的标签（"一段可直接背诵的完整表达"），本身不含内容语义，
# 问「讲一下你的项目」时跟查询一点都不像，光靠正文稀释后根本排不进前列。
# 实测：不补的话这个块在"讲一下你的项目"下连前八都进不去。
#
# 补的必须是**问句**，不能是名词堆。embedding 编的是整句语义，
# "姓名 名字 学校"这种标签串跟"你叫什么名字"相似度天然就低——实测前者 0.406，掉出前五。
# 另外：正文剥离隐私后已经没有电话邮箱了，cue 里别再提，否则是把清空的字段又招回来。
CUES = {
    "完整表达": "项目介绍 讲一下你的项目 介绍一下自己 项目总结 最大的体会 收获 感悟",
    "取舍": "技术取舍 为什么这么设计 权衡 方案对比 难点",
    "基本信息": "你是谁 你叫什么名字 你叫什么 哪个学校毕业的 学的什么专业 学历",
    "荣誉证书": "证书 荣誉 获奖 比赛",
    "自我评价": "做个自我介绍 介绍一下你自己 你是谁 说说你自己 个人评价 优势 性格",
    "求职意向": "想找什么工作 意向岗位 意向城市",
}


def sniff(md: str) -> str:
    """带 `**问：` 的按答疑切，其余按简历切"""
    return "faq" if re.search(r"^\*\*问：", md, re.M) else "resume"


def _cue(title: str) -> str:
    return " ".join(v for k, v in CUES.items() if k in title)


def _parse(md: str) -> tuple[str, list[dict]]:
    """按标题切成段。h1 只当文档标题，不进层级树——进了的话每段路径都被文档名污染。"""
    doc_title, raw, cur = "", [], None
    for line in md.splitlines():
        m = HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            if level == 1:
                doc_title, cur = title, None
                continue
            if cur:
                raw.append(cur)
            cur = {"level": level, "title": title, "body": []}
        elif cur is not None:
            cur["body"].append(line)
    if cur:
        raw.append(cur)
    return doc_title, raw


def _link(raw: list[dict]) -> None:
    """补标题路径、父子关系，以及父块的完整正文"""
    stack: list[int] = []
    for i, s in enumerate(raw):
        while stack and raw[stack[-1]]["level"] >= s["level"]:
            stack.pop()
        s["path"] = [raw[j]["title"] for j in stack] + [s["title"]]
        s["key"] = " > ".join(s["path"])
        s["ancestors"] = list(stack)
        stack.append(i)

    for i, s in enumerate(raw):
        nxt = raw[i + 1] if i + 1 < len(raw) else None
        s["is_parent"] = bool(nxt and nxt["level"] > s["level"])   # 后面还有更深的标题 = 父块

        # 父块的正文必须含全部后代：只取自己 body 的话，### 项目 底下那些 #### 全丢了
        end = len(raw)
        for j in range(i + 1, len(raw)):
            if raw[j]["level"] <= s["level"]:
                end = j
                break
        s["span"] = "\n\n".join(
            f"{'#' * x['level']} {x['title']}\n" + "\n".join(x["body"]).strip()
            for x in raw[i:end]
        ).strip()

    for i, s in enumerate(raw):
        parents = [j for j in s["ancestors"] if raw[j]["is_parent"]]
        s["parent_idx"] = parents[-1] if parents else None


def _resume(md: str) -> list[dict]:
    doc_title, raw = _parse(md)
    _link(raw)
    rows: list[dict] = []
    for i, s in enumerate(raw):
        if s["is_parent"]:
            continue                       # 父块不入库，正文已展开进子块
        pidx = s["parent_idx"]
        parent = raw[pidx] if pidx is not None else None
        own = f"{s['title']}\n" + "\n".join(s["body"]).strip()
        # 嵌入选文带上父块名，否则「项目成果」这种短小节没有上下文
        parts = [parent["title"] if parent else "", _cue(s["title"]), own]
        text = "\n".join(p for p in parts if p)
        # 两处都要清：full_text 是父块全文（含所有后代），只清 text 等于没清
        text, own_meta = scrub(text)
        full_text, span_meta = scrub(parent["span"] if parent else s["span"])
        rows.append(
            {
                "parent_key": parent["key"] if parent else None,
                "ord": i,
                "title": s["key"],
                "text": text,
                "full_text": full_text,
                "priority": int(any(k in s["title"] for k in HIGH_PRIORITY)),
                "meta": {
                    "doc_type": "resume",
                    "section": s["title"],
                    "doc_title": doc_title,
                    **span_meta,
                    **own_meta,
                },
            }
        )
    return rows


def _faq(md: str) -> list[dict]:
    m = re.match(r"^#\s+(.+)$", md, re.M)
    doc_title = m.group(1).strip() if m else ""
    rows: list[dict] = []
    # 第一个 ## 之前是文档标题和说明，丢掉
    for i, block in enumerate(re.split(r"^##\s+", md, flags=re.M)[1:]):
        lines = block.splitlines()
        title = lines[0].strip()
        body = "\n".join(lines[1:]).replace("---", "").strip()
        if not body:
            continue
        q = FAQ_ASK.search(body)
        body, own_meta = scrub(body)
        text = "\n".join(p for p in (doc_title, _cue(title), title, body) if p)
        full_text, _ = scrub(f"## {title}\n{body}")
        rows.append(
            {
                "parent_key": None,
                "ord": i,
                "title": title,
                "text": text,
                "full_text": full_text,
                "priority": 0,
                "meta": {
                    "doc_type": "faq",
                    "question": q.group(1).strip() if q else title,
                    "doc_title": doc_title,
                    **own_meta,
                },
            }
        )
    return rows


def split(md: str, doc_type: str | None = None) -> list[dict]:
    kind = doc_type or sniff(md)
    rows = _faq(md) if kind == "faq" else _resume(md)
    for r in rows:
        r["doc_type"] = kind
    return rows
