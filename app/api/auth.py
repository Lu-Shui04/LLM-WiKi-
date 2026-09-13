"""身份认证与会话历史接口

用户名 + 密码。首次输入的用户名密码即注册，之后填同样的即可回到自己的记忆。
日常不用反复输：token 存在浏览器里 7 天，只有换设备或清缓存才需要重来一次。
"""
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from app.core.session import manager
from app.db import sqlite

router = APIRouter(prefix="/api", tags=["auth"])

PASSWORD_MIN = 6


class LoginRequest(BaseModel):
    username: str = Field(max_length=32)
    password: str = Field(min_length=PASSWORD_MIN, max_length=64)


def _bearer(authorization: str) -> str:
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


async def current_user(authorization: str = Header(default="")) -> dict:
    """依赖注入用：从 Authorization 头解析 token，无效直接 401"""
    user = await sqlite.user_by_token(_bearer(authorization))
    if user is None:
        raise HTTPException(status_code=401, detail="登录已过期，请重新登录")
    return user


@router.post("/login")
async def login(req: LoginRequest) -> dict:
    username = req.username.strip()
    if not username:
        raise HTTPException(status_code=400, detail="用户名不能为空")

    result = await sqlite.login(username, req.password)
    if result.get("error") == "bad_password":
        raise HTTPException(
            status_code=401,
            detail=f"用户名「{username}」已存在，但密码不对。"
                   f"如果这个名字不是你注册的，换一个名字即可。",
        )

    await manager.get_session(result["user_id"], username)   # 登录即预热历史
    return {
        "session_token": result["session_token"],
        "is_new_user": result["is_new_user"],
        "expires_at": result["expires_at"],
        "username": result["username"],
    }


@router.get("/history")
async def history(user: dict = Depends(current_user)) -> dict:
    return {
        "username": user["username"],
        "messages": await sqlite.history(user["id"]),
    }


@router.delete("/history")
async def clear_history(user: dict = Depends(current_user)) -> dict:
    """真清：内存和 SQLite 一起删。

    前端那个「清空」原来只抹了浏览器里的 DOM，服务端一条没动——界面上看着
    空了，下一条消息模型还是带着全部旧上下文，旧回答里的引用编号也跟着传染回来。
    """
    session = await manager.get_session(user["id"], user["username"])
    async with session.lock:              # 别和正在生成的那一轮抢
        session.history.clear()
    await sqlite.clear_history(user["id"])
    print(f"[session] user={user['id']} 历史已清空")
    return {"ok": True}


@router.post("/logout")
async def logout(authorization: str = Header(default="")) -> dict:
    await sqlite.delete_session(_bearer(authorization))
    return {"ok": True}
