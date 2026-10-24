"""
Horon 本地网页后台：把 backend/web_api.py 的前端接口挂在 /api 下，库用 .env 里 HORON_DB 指的那个。

    python -m uvicorn backend.server:app --port 8710

这里的接口没有鉴权，只适合本机开发。远程（多用户、按账号选库）的挂载在 aisoulkeeper。
"""
from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from ._db_common import OFFLINE_DEV_SESSION_ID
from .db import HoronDB
from .web_api import make_router

_PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 本地这一个库的路径，服务启动时（lifespan）从 .env 的 HORON_DB 读一次。
_db_path: Path | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _db_path
    load_dotenv(_PROJECT_ROOT / ".env")
    _db_path = _PROJECT_ROOT / os.environ["HORON_DB"]
    HoronDB(db_path=_db_path, session_id=OFFLINE_DEV_SESSION_ID).close()  # 库不在就让服务启动时直接报错
    yield


app = FastAPI(title="Horon API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(make_router(lambda _request: _db_path, remote=False), prefix="/api")
