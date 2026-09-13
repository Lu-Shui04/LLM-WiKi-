"""会话管理：内存里的对话历史 + 每个用户一把异步锁

按 user_id 分桶而不是 session_token：token 每次登录都换新的，
而"退出后记忆不丢"要求历史跟着人走。

锁的作用：同一用户连发消息时（双击、多标签页），两个请求会同时
读到同一份历史再各自追加，上下文就串了。拿住锁串行处理即可。
"""
import asyncio
from dataclasses import dataclass, field

from app.db import sqlite


@dataclass
class Session:
    user_id: int
    username: str
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    history: list[dict] = field(default_factory=list)
    loaded: bool = False


class SessionManager:
    def __init__(self) -> None:
        self._sessions: dict[int, Session] = {}

    async def get_session(self, user_id: int, username: str) -> Session:
        """首次访问从 SQLite 把历史捞回内存，之后走内存"""
        session = self._sessions.get(user_id)
        if session is None:
            session = Session(user_id=user_id, username=username)
            self._sessions[user_id] = session
        if not session.loaded:
            session.history = await sqlite.history(user_id)
            session.loaded = True
            print(f"[session] 载入 user={user_id}({username}) 历史 {len(session.history)} 条")
        return session

    def drop(self, user_id: int) -> None:
        self._sessions.pop(user_id, None)

    @property
    def active(self) -> int:
        return len(self._sessions)


manager = SessionManager()
