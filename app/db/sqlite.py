"""SQLite 存储层：用户 / 会话 token / 对话历史

选 SQLite 不选 MySQL：单机个人 Agent，没有并发写入压力，零部署零运维，
库文件跟项目走。真要上规模是换 Postgres（向量库本来就在 PG），不是 MySQL。

库文件在 data/ 下，和代码分开。
同步实现 + asyncio.to_thread 包一层，不阻塞事件循环，也就不用引 aiosqlite。

密码用标准库的 pbkdf2-hmac-sha256，不加依赖。200k 轮约 100ms，
正好是 to_thread 派上用场的地方 —— 别在事件循环里算。
"""
import asyncio
import hashlib
import hmac
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "agent.db"
TOKEN_DAYS = 7
PBKDF2_ROUNDS = 200_000

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    username      TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sessions (
    token      TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role       TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_user ON messages(user_id, id);
"""


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _digest(password: str, salt: bytes) -> str:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS).hex()


def _pack(password: str) -> str:
    salt = os.urandom(16)
    return f"{salt.hex()}${_digest(password, salt)}"


def _verify(password: str, packed: str) -> bool:
    salt_hex, hash_hex = packed.split("$", 1)
    return hmac.compare_digest(_digest(password, bytes.fromhex(salt_hex)), hash_hex)


# ─── 同步实现 ────────────────────────────────────────
def _init_db() -> None:
    with closing(connect()) as conn, conn:
        conn.executescript(SCHEMA)


def _login(username: str, password: str) -> dict:
    """用户名不存在就注册，存在就校验密码。

    哈希在事务外算：pbkdf2 要 100ms 左右，攥着写锁算会把并发写入全堵死。
    """
    with closing(connect()) as conn:
        row = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if row is None:
            packed, is_new = _pack(password), True
        else:
            if not _verify(password, row["password_hash"]):
                return {"error": "bad_password"}
            packed, is_new = None, False

        with conn:
            if is_new:
                cur = conn.execute(
                    "INSERT INTO users (username, password_hash, created_at) VALUES (?, ?, ?)",
                    (username, packed, _now()),
                )
                user_id = cur.lastrowid
            else:
                user_id = row["id"]

            token = uuid.uuid4().hex
            expires = (datetime.now(timezone.utc) + timedelta(days=TOKEN_DAYS)).isoformat(timespec="seconds")
            conn.execute(
                "INSERT INTO sessions (token, user_id, expires_at, created_at) VALUES (?, ?, ?, ?)",
                (token, user_id, expires, _now()),
            )

    return {
        "user_id": user_id,
        "username": username,
        "session_token": token,
        "expires_at": expires,
        "is_new_user": is_new,
    }


def _user_by_token(token: str) -> dict | None:
    if not token:
        return None
    with closing(connect()) as conn, conn:
        row = conn.execute(
            "SELECT u.id, u.username, s.expires_at FROM sessions s "
            "JOIN users u ON u.id = s.user_id WHERE s.token = ?",
            (token,),
        ).fetchone()
        if row is None:
            return None
        # 两边都是 UTC +00:00 秒级格式，字符串比较即时间先后
        if row["expires_at"] < _now():
            conn.execute("DELETE FROM sessions WHERE token = ?", (token,))
            return None
        return {"id": row["id"], "username": row["username"]}


def _delete_session(token: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM sessions WHERE token = ?", (token,))


def _append_message(user_id: int, role: str, content: str) -> None:
    with closing(connect()) as conn, conn:
        conn.execute(
            "INSERT INTO messages (user_id, role, content, created_at) VALUES (?, ?, ?, ?)",
            (user_id, role, content, _now()),
        )


def _history(user_id: int, limit: int = 500) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            "SELECT role, content, created_at FROM messages "
            "WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    return [dict(r) for r in reversed(rows)]   # 倒序取最近 N 条，再翻回正序


def _clear_history(user_id: int) -> None:
    with closing(connect()) as conn, conn:
        conn.execute("DELETE FROM messages WHERE user_id = ?", (user_id,))


# ─── 异步包装 ────────────────────────────────────────
async def init_db() -> None:
    await asyncio.to_thread(_init_db)


async def login(username: str, password: str) -> dict:
    return await asyncio.to_thread(_login, username, password)


async def user_by_token(token: str) -> dict | None:
    return await asyncio.to_thread(_user_by_token, token)


async def delete_session(token: str) -> None:
    await asyncio.to_thread(_delete_session, token)


async def append_message(user_id: int, role: str, content: str) -> None:
    await asyncio.to_thread(_append_message, user_id, role, content)


async def history(user_id: int, limit: int = 500) -> list[dict]:
    return await asyncio.to_thread(_history, user_id, limit)


async def clear_history(user_id: int) -> None:
    await asyncio.to_thread(_clear_history, user_id)
