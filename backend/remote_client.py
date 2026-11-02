"""
Horon 的远程模式：记忆库在服务器上，本机只放一个给钩子用的规则库。

在 .env 里设 HORON_REMOTE_KEY 就进入远程模式（HORON_REMOTE_URL 只有测试时才用，默认是线上地址）：
    - CLI 命令发给服务器的 POST /v1/cli 执行，一条命令一次请求；
    - 钩子照常在本机跑，读规则库，不走网络；
    - 每条命令的响应带服务器的规则版本，和规则库记的不同就拉一次 GET /v1/rules，写进规则库
      （backend/rule_sync.py）。

规则库在 .horon-local/remote/ 下，每个服务器地址和 key 一个文件，旁边的 .version 记着同步到的版本。
远程模式下 HORON_DB 不读也不写。
"""
from __future__ import annotations

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from backend.db import create_database, database_exists
from backend.rule_sync import apply_rules, rules_version

# 服务器上语义搜索、audit、suggest_name 要调模型，等得久一些
REQUEST_TIMEOUT_SECONDS = 180
DEFAULT_URL = "https://aisoulkeeper.com"
# Cloudflare 会挡 urllib 默认的 User-Agent（error 1010）
_USER_AGENT = "Horon-CLI"
RULES_DIR = Path(__file__).resolve().parent.parent / ".horon-local" / "remote"


class RemoteError(Exception):
    """服务器连不上，或者返回了读不懂的内容。"""


@dataclass(frozen=True)
class RemoteSettings:
    url: str
    key: str

    @property
    def rules_db(self) -> Path:
        """这个地址和 key 的规则库。换账号就换文件，不串。"""
        digest = hashlib.sha256(f"{self.url}\n{self.key}".encode("utf-8")).hexdigest()[:16]
        return RULES_DIR / f"rules-{digest}.db"

    @property
    def version_path(self) -> Path:
        return self.rules_db.with_suffix(".version")


@dataclass(frozen=True)
class RemoteResponse:
    ok: bool
    output: str
    error: str
    rules_version: str | None


def remote_settings() -> RemoteSettings | None:
    """从环境变量读远程配置（调用方先 load_dotenv）。没设 HORON_REMOTE_KEY 返回 None，就是本地模式。"""
    key = os.environ.get("HORON_REMOTE_KEY", "").strip()
    if not key:
        return None
    url = os.environ.get("HORON_REMOTE_URL", "").strip().rstrip("/") or DEFAULT_URL
    return RemoteSettings(url, key)


def ensure_rules_db(settings: RemoteSettings) -> Path:
    """返回规则库的路径，没有就建一个空的。"""
    path = settings.rules_db
    if not database_exists(path):
        create_database(path)
    return path


def _request(settings: RemoteSettings, path: str, body: dict | None = None) -> dict[str, Any]:
    """发一个请求，返回 JSON。HTTP 错误码的响应也是 JSON，照常返回，由调用方看 ok。"""
    request = urllib.request.Request(
        settings.url + path,
        data=None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {settings.key}", "Content-Type": "application/json",
                 "User-Agent": _USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = f"HTTP {e.code}"
    except OSError as e:  # URLError、超时都是 OSError
        raise RemoteError(f"Could not reach the memory server {settings.url}: {e}") from e
    else:
        status = "response"
    try:
        return json.loads(raw)
    except ValueError:
        raise RemoteError(f"The memory server sent something unreadable ({status}): {raw[:200]!r}") from None


def run_command(settings: RemoteSettings, args: list[str], batch_text: str | None, session_id: str) -> RemoteResponse:
    """在服务器上执行一条命令。session_id 让审计和共读记录归到本地这个会话。"""
    body: dict[str, Any] = {"args": args, "session_id": session_id, "include_rules_version": True}
    if batch_text is not None:
        body["batch_text"] = batch_text
    data = _request(settings, "/v1/cli", body)
    return RemoteResponse(bool(data.get("ok")), data.get("output") or "", data.get("error") or "",
                          data.get("rules_version"))


def local_version(settings: RemoteSettings) -> str | None:
    try:
        return settings.version_path.read_text(encoding="ascii").strip() or None
    except FileNotFoundError:
        return None


def refresh_rules(settings: RemoteSettings) -> None:
    """拉服务器的规则写进规则库。校验版本后才写，版本最后写：中途失败下次还会重拉。"""
    data = _request(settings, "/v1/rules")
    version, rules = data.get("version"), data.get("rules")
    if not version or not isinstance(rules, dict):
        raise RemoteError(f"Could not fetch the rules: {data.get('error') or 'the response has no rules'}")
    if rules_version(rules) != version:
        raise RemoteError("Could not fetch the rules: their content does not match their version.")
    apply_rules(ensure_rules_db(settings), rules)
    settings.version_path.write_text(version, encoding="ascii")
