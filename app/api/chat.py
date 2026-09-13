"""对话接口。SSE 流式返回，事件六类：

    start    连接建立
    sources  知识库检索结果（引用来源，先于正文发出）
    thinking 思考内容（推理模型先出这段，前端应立刻渲染，别让用户干等）
    token    正文
    done     结束
    error    异常也走 SSE，前端不会只看到断连

历史由服务端持有（内存 + SQLite），前端只发一句 text。
"""
import json
import re
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends
from fastapi.responses import StreamingResponse
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from app.api.auth import current_user
from app.config import settings
from app.core import prompt
from app.core.embed import embed
from app.core.llm import get_chat_model
from app.core.session import Session, manager
from app.db import sqlite, vectors

router = APIRouter(prefix="/api", tags=["chat"])

# 问到联系方式就走短路，绕开检索和模型。用正则而不是让模型自己判断——
# 提示词是软约束，模型可以不听话；这一层是硬保证
CONTACT_ASK = re.compile(r"联系方式|联系电话|手机号?|电话|邮箱|e-?mail|怎么联系|联系你|联系我")

# 简历里的固定栏目：问法有限、答案固定。这类问题向量检索的 top1 往往是对的，
# 但后面几条会被邻近栏目污染——「你想找什么工作」top1 是《求职意向》0.551，第 2、3 条
# 却是《前端那块你做了什么》和《荣誉证书》，中间直接断崖。砍 TOP_K 也救不了，因为
# 第 2 条本身就是噪声。所以和联系方式同一个路子：认出问法，直接取那一节。
# 只增不减——没覆盖到的问法照旧走向量检索，不会比现在差。
# 第四项是附加提示词（prompts/ 里的文件名），只有这个路由命中才拼上，别的回答看不到。
# 最后一个布尔是 own_text：只取子块自己那一段，而不是它所属父块的全文。
# 只有《项目经历》要开——那个父块 3412 字包着 10 个子块，命中哪个都返回整篇，
# 于是标题写「项目背景」、点开是全篇，对不上；塞进上下文也白白占地方。
FIXED_SECTIONS: list[tuple[str, re.Pattern, list[str], str | None, bool]] = [
    # 问助手能干什么 ≠ 问小米会什么。system.md 让他用本人身份说话，模型会把
    # 「你能做什么」当成「你的技能」去捞《核心技能》，答成一堆技术栈。所以单独拎出来，
    # 不检索、只给提示词。sections 留空 = 这轮压根不查知识库。
    ("助手能力",
     re.compile(r"你能做什么|你会做什么|你能干什么|你能干嘛|你能做啥|你能帮我做什么|"
                r"你有什么功能|你是干什么的|你能帮我干什么"),
     [], "assistant", False),
    # 「你是谁」和「你叫什么名字」是两件事：前者他自己在测工具，问的是这个助手；
    # 后者是模拟面试官问候选人。上一版合成一条，模型只能靠猜，答得又绕又长——
    # 先纠结半天"你这是问我还是问他"，再把简历念一遍。
    ("助手身份",
     re.compile(r"你是谁|你到底是谁|你是干嘛的|你是机器人|你是人还是|你是AI|你是ai|你是什么模型"),
     [], "identity", False),
    ("本人身份",
     re.compile(r"你叫什么|你的名字|哪个学校|什么学校|什么学历|什么专业|学什么专业|哪毕业"),
     ["基本信息", "教育经历"], None, False),
    # 自我介绍是宏观问题，走向量检索会捞进《核心技能》那种细节块（里面还写着 SSE 实现，
    # 对着面试官讲自我介绍时冒出来很脱节）。而《自我评价》这一节本身就包含了
    # 技能、代表作、意愿，正好是自我介绍要的四段——所以固定取这几节。
    # `(?!的)` 挡的是「介绍一下你自己的项目」——那是在问项目，不是自我介绍
    ("自我介绍",
     re.compile(r"自我介绍|介绍一下你自己(?!的)|介绍下你自己(?!的)|介绍一下自己(?!的)|"
                r"介绍下自己(?!的)|讲讲你自己|说说你自己|自我介绍一下"),
     ["基本信息", "教育经历", "自我评价", "荣誉证书", "求职意向"], "intro", False),
    # 项目类问题是被向量检索坑得最惨的一类：「你做过几个项目」实测第一名是《荣誉证书》
    # 0.548、项目切片 0.541，差 0.007，就是在瞎蒙（详见 vectors.py 里对检索边界的说明）。
    # 问法和答案都固定，直接规则接住。
    ("项目",
     re.compile(r"讲.{0,4}项目|聊.{0,2}项目|项目介绍|介绍.{0,4}项目|做过.{0,3}项目|"
                r"什么项目|项目经历|你做的项目|项目是做什么|项目有哪些"),
     ["项目背景", "项目职责", "项目成果"], "project", True),
    ("求职意向",
     re.compile(r"找什么.{0,2}工作|想做什么工作|意向岗位|什么岗位|意向城市|哪个城市|"
                r"想去哪|期望薪资|希望薪资|期望工资|希望工资|薪资要求|想要多少"),
     ["求职意向"], None, False),
    ("荣誉证书",
     re.compile(r"获过什么奖|拿过什么奖|有什么奖|什么奖项|什么荣誉|什么证书|哪些证书|获过哪些"),
     ["荣誉证书"], None, False),
    ("自我评价",
     re.compile(r"你的优点|你的优势|有什么优点|有什么优势|自我评价|评价一下自己|"
                r"你是什么样的人"),
     ["自我评价"], None, False),
]

# 纯寒暄整句：整句话只由问候/致谢词构成，没有任何实义内容。
# 和「相不相关」不同，问候词是封闭词表、穷举得完，所以这层是代码保证而非推断。
# 实测「你好」top1 能到 0.557（真问题「讲一下你的项目」才 0.568），阈值拦不住它，
# 只能认出句式直接不检索——命中就没有"筛不筛得掉"的问题了。
# 必须整句匹配：`你好，帮我讲讲项目` 这种带实义的不能算，尾部字符类只放语气词。
SMALLTALK = re.compile(
    r"^(?:"
    r"(?:你|您)?好|早上好|下午好|晚上好|哈喽|哈啰|嗨|hi|hello|"
    r"在吗|在么|在不在|谢谢|多谢|感谢|辛苦了|辛苦|好的|收到|ok|"
    r"再见|拜拜|哈哈+|嘿嘿|嗯+|哦+"
    r")[啊呀啦呢吧哈哟~～!！。.，,？?、\s]*$",
    re.IGNORECASE,
)

MAX_CONTEXT = 20    # 发给模型的历史条数上限；DB 里保留全部，只是不都塞进上下文
# 检索条数。14 个典型问题实测 top1 命中 12 个，但第 2 条就开始断崖——
# 「你获过什么奖」0.635 → 0.453 → 0.450、「你想找什么工作」0.551 → 0.476 → 0.470。
# 多给的不是"更多上下文"而是噪声：会渗进答案、也让来源那一行看着很脏。宁少勿多。
TOP_K = 3
# 余弦下限。别指望它做相关性裁决——实测正负样本重叠（正样本最低 0.509
# 「你平时怎么学习的」，负样本最高 0.589「帮我写一段 Python 快排」，它是代码题、
# 跟技术语料天然贴近）。试过的更严的线都会连带误杀真问题，所以只留个兜底值。
MIN_SCORE = 0.40

_ROLE_MAP = {"user": HumanMessage, "assistant": AIMessage, "system": SystemMessage}


class ChatRequest(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def _chunk_text(chunk) -> str:
    """推理模型的 content 可能是分段列表，统一压成字符串"""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return ""


class _StripMarks:
    """流式摘掉 [1]、【2】 这类引用编号。

    提示词里写了别写编号，但历史里只要躺着一轮旧回答带着它，模型就会跟着学——
    软约束挡不住这个，只能在这一层硬摘。难点是流式 token 会被切碎，`[1]` 可能
    分两次到（`[` + `1]`），所以在缓冲区里多留一小段，等看清楚了再决定吐不吐。
    """

    NUM = re.compile(r"[\[【]\s*\d{1,3}\s*[\]】]")
    HALF = re.compile(r"^[\[【]\s*\d{0,3}$")     # 像编号的前半截，还看不准

    def __init__(self) -> None:
        self.buf = ""

    def feed(self, piece: str) -> str:
        self.buf += piece
        out = ""
        while self.buf:
            if self.buf[0] in "[【":
                if self.NUM.match(self.buf):
                    self.buf = self.NUM.sub("", self.buf, count=1)
                    continue
                if len(self.buf) < 6 and self.HALF.match(self.buf):
                    break                       # 还可能是编号，攥着等下一块
            out += self.buf[0]
            self.buf = self.buf[1:]
        return out

    def flush(self) -> str:
        """流结束：残留的按原文吐出——真是编号的话，前面早就整个摘掉了"""
        tail, self.buf = self.buf, ""
        return tail


def _scour(text: str) -> str:
    """老会话里还躺着清洗器上线前留下的 [1]。读历史时一并摘掉——
    只摘新生成的不够，模型每轮都看得见历史里那些，照样跟着抄。"""
    return _StripMarks.NUM.sub("", text)


# 意图 → 限定在哪几个文档里检索。None = 不限制（已证明分不开时不硬分）
INTENT_DOCS: dict[str, list[str] | None] = {
    "resume": ["我的简历"],
    "faq": ["常见问题回答"],
    "both": None,
}


# 孤立的承接语：首轮就冒出一句「可以」，前面没有可接的话，字面上确实什么都没问。
# 这类词短、没实义，模型很容易被「别因为字面没内容就判 none」那句推着硬凑个类别，
# 于是给「可以」挂上三条不相关的 FAQ。所以首轮直接代码判掉，连模型都不用调。
FOLLOWUP_ONLY = re.compile(
    r"^(?:可以|行|好|好的|嗯+|是的|对|继续|然后呢?|展开|说说|为什么|那呢|还有呢|"
    r"接着|往下|详细点|再讲讲|ok)[啊呀吧呢么?？。！!，,\s]*$",
    re.IGNORECASE,
)


async def _classify(text: str, history: list[dict]) -> str:
    """规则没命中的开放问法，才让模型判该不该查、查哪份。

    这是开放判断（「该不该查知识库」穷举不完），所以只能交给模型——
    规则层兜住的是高频固定问法，模型层兜住长尾。分类失败一律回 both，
    也就是退回「全库检索」，不会比加这一层之前更差。

    必须带上最近两轮：像「可以」「那呢」「展开说说」这种承接语，单看几个字
    什么也判不出来——「可以」曾被判成 none 直接跳过知识库，模型就凭记忆
    讲了一整段项目。补上上文它才知道这是在点头同意上一轮的话题。
    """
    if not history and FOLLOWUP_ONLY.match(text.strip()):
        print(f"[意图] 首轮孤立承接语 {text!r} → 不检索")
        return "none"
    ctx = "\n".join(
        f"{'用户' if m['role'] == 'user' else '你'}：{m['content'].strip()[:160]}"
        for m in history[-4:]
    )
    ask = f"最近的对话：\n{ctx}\n\n现在用户说：{text}" if ctx else text
    try:
        reply = await get_chat_model(streaming=False).ainvoke(
            [SystemMessage(content=prompt.load("intent")), HumanMessage(content=ask)]
        )
        word = _chunk_text(reply).strip().lower()
    except Exception as exc:
        print(f"[意图] 分类失败，退回全库检索：{exc}")
        return "both"
    for key in ("none", "resume", "faq", "both"):   # none 优先：判成不查就别再往下试
        if key in word:
            return key
    print(f"[意图] 回了个看不懂的：{word[:40]!r} → 退回全库检索")
    return "both"


def _stage(key: str, text: str) -> dict:
    """阶段事件。前端拿它显示「识别意图…」这类进度，key 用于去重/排序"""
    return {"type": "stage", "key": key, "text": text}


async def _retrieve(text: str, history: list[dict]) -> AsyncIterator[dict]:
    """边干活边报进度：先 yield 若干 stage，最后 yield 一条 result。

    拆成生成器是因为算 embedding、调分类动辄一两秒，一次性 await 完再返回的话，
    这段时间前端完全不知道在干什么，只能干等。区分 searched 是因为寒暄、
    问助手能力这类压根没查知识库，那时不该跟模型说「知识库里没有相关内容」。
    """
    if SMALLTALK.match(text.strip()):
        print("[路由] 纯寒暄 → 不检索")
        yield {"type": "result", "hits": [], "searched": False, "extra": ""}
        return

    for name, pattern, sections, extra, own_text in FIXED_SECTIONS:
        if pattern.search(text):
            hits = await vectors.by_title(sections, own_text) if sections else []
            print(f"[路由] {name} → {'直接取 ' + str([h['title'] for h in hits]) if hits else '不检索'}")
            yield {"type": "result", "hits": hits, "searched": bool(sections), "extra": extra or ""}
            return

    yield _stage("analyze", "识别意图…")
    intent = await _classify(text, history)
    if intent == "none":
        print("[意图] none → 不检索")
        yield {"type": "result", "hits": [], "searched": False, "extra": ""}
        return

    yield _stage("search", "检索资料…")
    try:
        vec = (await embed([text]))[0]
    except Exception as exc:
        print(f"[检索] embedding 失败，本轮跳过知识库：{exc}")
        yield {"type": "result", "hits": [], "searched": True, "extra": ""}
        return
    hits = await vectors.search(vec, TOP_K, MIN_SCORE, INTENT_DOCS[intent])
    brief = "  ".join(f"{h['score']:.3f} {h['title'].split(' > ')[-1][:18]}" for h in hits)
    print(f"[意图] {intent} → [检索] {text[:24]!r} {len(hits)} 条  {brief}")
    yield {"type": "result", "hits": hits, "searched": True, "extra": ""}


def _system_prompt(hits: list[dict], searched: bool = True, extra: str = "") -> str:
    """话术本身在 prompts/ 里，这里只负责把检索结果拼上去"""
    base = prompt.load("system")
    if extra:
        base = f"{base}\n\n{prompt.load(extra)}"
    if hits:
        # 用标题当分隔，不给资料编号——编号摆在眼前，模型就会照着往正文里抄
        blocks = "\n\n".join(f"【{h['title']}】\n{h['text']}" for h in hits)
        return f"{base}\n\n以下是知识库检索到的资料：\n\n{blocks}"
    # 只有真去检索了又没捞到，才说「知识库没有」；寒暄不该被这么回
    return f"{base}\n\n{prompt.load('no_hits')}" if searched else base


async def _contact_stream(session: Session, text: str, contact: dict) -> AsyncIterator[str]:
    """确定性拼一行返回，整轮不碰 embedding、不碰模型——真值不进任何一次 API 请求"""
    pairs = [("电话", contact.get("phone")), ("邮箱", contact.get("email"))]
    answer = "我的联系方式：" + "；".join(f"{k} {v}" for k, v in pairs if v) + "。"
    async with session.lock:
        session.history.append({"role": "user", "content": text})
        await sqlite.append_message(session.user_id, "user", text)
        session.history.append({"role": "assistant", "content": answer})
        await sqlite.append_message(session.user_id, "assistant", answer)
    print("[短路] 问联系方式，直接读元数据返回，未检索未调模型")
    yield _sse({"type": "token", "text": answer})
    yield _sse({"type": "done"})


async def _stream(session: Session, text: str) -> AsyncIterator[str]:
    yield _sse({"type": "start", "model": settings.deepseek_model})

    if CONTACT_ASK.search(text) and (contact := await vectors.contact()):
        async for event in _contact_stream(session, text, contact):
            yield event
        return

    # 检索放在锁外面：算 embedding 要几百毫秒，没必要占着这把锁
    hits, searched, extra = [], True, ""
    async for ev in _retrieve(text, session.history):
        if ev["type"] == "result":
            hits, searched, extra = ev["hits"], ev["searched"], ev["extra"]
        else:
            yield _sse(ev)                  # stage 事件原样转发，前端拿它显示进度

    yield _sse(
        {
            "type": "sources",
            # 前端靠它区分「查了但没有」和「压根没查」。寒暄、问助手能力这些
            # 本来就没碰知识库，界面上不该挂一句「知识库里没有相关内容」
            "searched": searched,
            "items": [
                {
                    "n": i,
                    "title": h["title"],
                    "doc": h["doc"],
                    "score": h["score"],
                    "text": h["text"],      # 前端点击来源时展开看切片原文
                }
                for i, h in enumerate(hits, 1)
            ],
        }
    )

    answer, error = "", None
    marks = _StripMarks()
    yield _sse(_stage("compose", "组织回答…"))
    try:
        # 整段生成期间持锁：同一用户同时来两个请求会排队，不会读到同一份历史各自追加
        async with session.lock:
            session.history.append({"role": "user", "content": text})
            await sqlite.append_message(session.user_id, "user", text)

            messages = [SystemMessage(content=_system_prompt(hits, searched, extra))] + [
                _ROLE_MAP.get(m["role"], HumanMessage)(content=_scour(m["content"]))
                for m in session.history[-MAX_CONTEXT:]
            ]
            async for chunk in get_chat_model(streaming=True).astream(messages):
                reason = chunk.additional_kwargs.get("reasoning_content")
                if reason:
                    yield _sse({"type": "thinking", "text": reason})
                piece = _chunk_text(chunk)
                if piece:
                    # 落盘存的是清洗后的文本。存原文的话历史里永远留着编号，
                    # 下一轮模型又能照着抄回来，就永远摘不干净了
                    visible = marks.feed(piece)
                    answer += visible
                    if visible:
                        yield _sse({"type": "token", "text": visible})
            if tail := marks.flush():
                answer += tail
                yield _sse({"type": "token", "text": tail})
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    # 断开连接时生成器在 yield 处被关掉，走不到这里；正常情况下把回答落盘
    if answer:
        session.history.append({"role": "assistant", "content": answer})
        await sqlite.append_message(session.user_id, "assistant", answer)

    yield _sse({"type": "error", "message": error} if error else {"type": "done"})


@router.post("/chat")
async def chat(req: ChatRequest, user: dict = Depends(current_user)) -> StreamingResponse:
    session = await manager.get_session(user["id"], user["username"])
    return StreamingResponse(
        _stream(session, req.text.strip()),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 别让反向代理把流缓冲成一次性返回
        },
    )
