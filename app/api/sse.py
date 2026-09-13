"""SSE 事件的构造与流式清洗。对话流（chat.py）和 Wiki 对话流（wiki_chat.py）共用。

抽出来是因为两条链路要发**同一套**六类事件、都要摘引用编号、都要处理推理模型的
分段 content——各写一份的话，`StripMarks` 这种「细节极多、错了又很难发现」的逻辑
迟早会分叉，而分叉的后果是同一份历史在两个后端下被清洗成不同的样子。
"""
import json
import re


def sse(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def chunk_text(chunk) -> str:
    """推理模型的 content 可能是分段列表，统一压成字符串"""
    content = chunk.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            part.get("text", "") if isinstance(part, dict) else str(part) for part in content
        )
    return ""


class StripMarks:
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


def scour(text: str) -> str:
    """老会话里还躺着清洗器上线前留下的 [1]。读历史时一并摘掉——
    只摘新生成的不够，模型每轮都看得见历史里那些，照样跟着抄。"""
    return StripMarks.NUM.sub("", text)


def stage(key: str, text: str) -> dict:
    """阶段事件。前端拿它显示「识别意图…」这类进度，key 用于去重/排序"""
    return {"type": "stage", "key": key, "text": text}
