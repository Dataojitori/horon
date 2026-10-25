"""
Horon 远程执行引擎：把「一条 CLI 命令」安全地跑在指定的库上。

    from backend.remote_cli import create_database, delete_database, run_remote_cli
    create_database(db_path)                      # 新建 AI 时调一次
    delete_database(db_path)                      # 删 AI 时调一次
    result = run_remote_cli(db_path, ["read_concept", "951"])
    result = run_remote_cli(db_path, ["batch"], batch_text="read_concept 951\nrecent 5")

  create_database  在 db_path 建一个空库（建表、迁移记录、系统 Tag）。路径上已有库就抛
                   FileExistsError；父目录不存在会先建。定义在 backend/db.py，这里转出。
  delete_database  删掉 db_path 这个库和 Horon 为它留下的所有文件。
  db_path     要打开的 SQLite 库文件，必须已经由 create_database 建好；不存在就返回 404，
              不会顺手建一个。每次调用只碰这一个库。
  args        就是 `python frontend/cli.py` 后面那串参数，一项一个，不要拼成一个字符串。
  batch_text  只在 args[0] == "batch" 时使用：每行一条命令的文本（格式同 batch 文件，省略
              `python frontend/cli.py` 前缀）。batch 在这里不读文件、不读 stdin。

返回 RemoteCliResult：status（建议的 HTTP 状态）、ok、output、error，以及这次调用的付费开销
cost_usd 和 usage 明细（嵌入、suggest_name、audit 里 Jev 判断等花钱的调用，由 Horon 自己算好，
见 backend/llm.py；调用方不需要知道 Horon 用了哪些模型、多少钱）。
  status：200 命令真的跑了（成不成功看 ok）；403 命令不在白名单或用了读写服务器文件的参数；
  404 库文件不存在；422 参数格式不对；500 打开库失败。

这里不管账号、鉴权和 key：谁能调用、调用哪个库，由调用方（如 aisoulkeeper）决定。
想把它暴露成 HTTP 接口，调用方自己写路由，把请求里的 args / batch_text 交给 run_remote_cli。

和本地 CLI 的区别：
    - 直接调 frontend/cli.py 的 _build_parser() 和 _audited_dispatch()，不起子进程；
      每次调用开一个 HoronDB 连接，用完关闭。
    - 会话固定为 remote-api（审计日志、读取记录和本地的 devonly / 宿主会话分开）。
    - 不加载任何 Tag 插件（HoronDB(plugins_enabled=False)）。写入时不过插件检查，audit 不跑插件。
    - 不调用 hook 的 sense()，所以传感器、Jev 复核都不会因为这里的命令被求值
      （详见 set 配了电路属性后的行为：只落库，不求值正则、不问 Jev）。
    - 不走钩子。本地开新会话时做的完整性检查（缺向量后台补、电路组合违规提示）改在 login 里做，
      违规提示附在 login 输出末尾；补向量的后台进程只补这次打开的那个库。
    - 只认白名单里的子命令，且不允许任何会读写服务器文件的参数。
    - 不加锁：并发由 SQLite 自己排队，并发 update 不丢更新靠 update 自身的比较写入。
      库被删掉后迟到的命令打不开库（HoronDB 不建库），返回 404。
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .llm import CostMeter
from .db import HoronDB, create_database  # noqa: F401  create_database 是给调用方的建库入口
from .integrity import backfill_lock_path, check_at_session_start
from frontend import cli as _cli

logger = logging.getLogger("horon.remote_cli")

REMOTE_SESSION_ID = "remote-api"
REMOTE_ADAPTER = "remote-api"

# 子命令白名单：名单以外一律拒绝（reset、read_memory 等都不在里面）。
ALLOWED_COMMANDS = frozenset({
    "login", "read_concept", "search_keyword", "search_semantic", "list_concepts", "recent",
    "create_concept", "update", "set", "add", "delete", "place", "cut",
    "list_tags", "create_tag", "delete_tag", "remind", "inbox", "compile",
    "suggest_name", "audit", "batch", "read_hook",
})

# argparse 解析后的 dest → 命令行上的写法。这些参数会让服务器读写文件，一律拒绝。
# 按解析结果（Namespace）检查，不按字符串匹配：--content_file、--content-file=x 这些写法
# 都落到同一个 dest 上；而 `--append "--file"` 这种把字面文字当内容的合法请求也不会被误伤。
FILE_ARG_DESTS = {
    "content_file": "--content-file",
    "append_file": "--append-file",
    "old_file": "--old-file",
    "new_file": "--new-file",
    "file": "--file",
    "out": "--out",
}

MAX_ARGS = 200


@dataclass
class RemoteCliResult:
    """一次 run_remote_cli 的结果。usage 是 CostMeter.summary()：cost_usd、input_tokens、
    output_tokens、unpriced_calls（响应里没给价钱的调用数）和逐次调用明细 calls。"""
    status: int
    ok: bool
    output: str = ""
    error: str = ""
    usage: dict[str, Any] = field(default_factory=lambda: CostMeter().summary())

    @property
    def cost_usd(self) -> float:
        return self.usage["cost_usd"]

    def payload(self) -> dict[str, Any]:
        """可以直接序列化成 JSON 返回给最终调用者的字典。"""
        return {"ok": self.ok, "output": self.output, "error": self.error,
                "cost_usd": self.cost_usd, "usage": self.usage}


class _RemoteError(Exception):
    status = 400


class _Rejected(_RemoteError):
    """策略拒绝：命令不在白名单、用了文件参数。"""
    status = 403


class _BadRequest(_RemoteError):
    """请求本身不合格（如 batch 没带 batch_text）。"""
    status = 422


# ── 白名单 ───────────────────────────────────────────────────────────────────

def _check_command_name(tokens: list[str]) -> None:
    """解析之前先查：第一项必须是白名单里的子命令（不接受放在子命令前面的任何选项）。"""
    if not tokens:
        raise _Rejected("args 不能为空：第一项是子命令名。")
    if tokens[0] not in ALLOWED_COMMANDS:
        raise _Rejected(f"命令 '{tokens[0]}' 不在白名单内。允许的子命令：{', '.join(sorted(ALLOWED_COMMANDS))}。")


def _check_no_file_args(ns) -> None:
    """解析之后查：任何会读写服务器文件的参数都拒绝。"""
    for dest, flag in FILE_ARG_DESTS.items():
        if getattr(ns, dest, None) is not None:
            raise _Rejected(
                f"远程接口不允许使用 {flag}（会读写服务器上的文件）。内容请直接写在请求里；"
                f"batch 的命令文本放在 batch_text 里。")


def _check_parsed(ns) -> None:
    """已解析命令的完整策略检查，单条命令和 batch 的每一行都过这里。"""
    if ns.command not in ALLOWED_COMMANDS:  # 防御：上面按名字查过一遍，这里按解析结果再查一遍
        raise _Rejected(f"命令 '{ns.command}' 不在白名单内。")
    _check_no_file_args(ns)


class _Usage(Exception):
    """argparse 报了用法错误，或者打印了 --help。text 是它要输出的文字，code 是退出码。"""

    def __init__(self, code, text: str):
        self.code = code
        self.text = text


def _parse(parser, tokens: list[str]):
    """用 CLI 的 parser 解析一条命令；argparse 的输出收进缓冲区而不是写 stderr/stdout。"""
    buf = io.StringIO()
    reset_token = _cli._PARSER_OUTPUT.set(buf)
    try:
        return parser.parse_args(tokens)
    except SystemExit as e:
        raise _Usage(e.code, buf.getvalue()) from None
    finally:
        _cli._PARSER_OUTPUT.reset(reset_token)


def _usage_hint(text: str) -> str:
    if "unrecognized arguments" in text or "expected one argument" in text:
        text += ("\n[提示] args 是 CLI 参数数组，一个参数一项（不要把整条命令拼成一个字符串）。"
                 "值以 - 开头的内容请写成 --选项=值 的形式。")
    return text


# ── 执行 ─────────────────────────────────────────────────────────────────────

def _validate_batch(parser, batch_text: str) -> None:
    """batch_text 里每一行都先过一遍白名单和文件参数检查；任何一行不合格，整段都不执行。"""
    try:
        commands = _cli._parse_batch_commands(batch_text)
    except Exception as e:
        raise _BadRequest(f"batch_text 语法错误：{e}") from None
    if not commands:
        raise _BadRequest("batch_text 里没有命令。")
    for start_line, tokens, _raw in commands:
        try:
            _check_command_name(tokens)
            if tokens[0] == "batch":
                raise _Rejected("batch 里不能再嵌套 batch。")
            _check_parsed(_parse(parser, tokens))
        except _Usage as u:
            raise _BadRequest(f"batch_text 第 {start_line} 行参数错误：{_usage_hint(u.text).strip()}") from None
        except _Rejected as r:
            raise _Rejected(f"batch_text 第 {start_line} 行被拒绝：{r}") from None



def delete_database(db_path: str | Path) -> None:
    """删掉 db_path 这个库，连同 SQLite 的旁路文件（-wal、-shm、-journal）和补向量锁。
    不存在的文件跳过。正在跑的命令不等：它们之后打不开库，得到 404。"""
    db_path = Path(db_path).resolve()
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(str(db_path) + suffix).unlink(missing_ok=True)
    backfill_lock_path(db_path).unlink(missing_ok=True)


def _session_start_check(db: HoronDB) -> str:
    """login 相当于本地开新会话：跑同一个完整性检查（缺向量就在后台起补向量进程，只补 db 这个库）。
    本地的电路违规提示走钩子广播，这里没有钩子，所以附在 login 输出末尾。出错只记日志。"""
    try:
        problems = check_at_session_start(db)
    except Exception:
        logger.exception("remote cli: session-start integrity check failed")
        return ""
    return f"\n【完整性检查】{problems}\n" if problems else ""


def _run(db_path: Path, ns, parser, args: list[str], batch_text: str | None) -> tuple[int, bool, str, str]:
    """已通过策略检查的命令在这里真正执行。返回 (HTTP 状态, ok, output, error)。"""
    try:
        db = HoronDB(
            db_path=db_path,
            snapshot_mode=True,
            session_id=REMOTE_SESSION_ID,
            plugins_enabled=False,
        )
    except FileNotFoundError:
        return 404, False, "", "这个记忆库不存在（可能已被删除）。"
    except Exception as e:
        logger.exception("remote cli: failed to open database")
        return 500, False, "", f"打开数据库失败：{type(e).__name__}: {e}"
    try:
        db.touch_session(REMOTE_ADAPTER)  # 登记 remote-api 会话；已登记的只刷新最后活跃时间
        if ns.command == "batch":
            buf = io.StringIO()
            reset_token = _cli._PARSER_OUTPUT.set(buf)
            try:
                out, err = _cli._run_batch(parser, db, batch_text, ns.all, check=_check_parsed)
            finally:
                _cli._PARSER_OUTPUT.reset(reset_token)
            error = (err or "").strip()
            if buf.getvalue():
                error = (error + "\n" + buf.getvalue().strip()).strip()
            return 200, err is None, out, error
        result = _cli._audited_dispatch(ns, db)
        output = _cli._render(result)
        if ns.command == "login":
            output += _session_start_check(db)
        return 200, True, output, ""
    except ValueError as e:  # CLI 里“正常的失败”（找不到节点、参数不合法、拒绝写入等）
        return 200, False, "", str(e)
    except SystemExit as e:
        return 200, False, "", f"命令异常退出（code={e.code}）"
    except Exception as e:
        logger.exception("remote cli: unexpected error running %s", args[0])
        return 200, False, "", f"内部错误：{type(e).__name__}: {e}"
    finally:
        db.close()


def run_remote_cli(db_path: str | Path, args: list[str], batch_text: str | None = None) -> RemoteCliResult:
    """在 db_path 这个库上安全地执行一条 CLI 命令（或一段 batch），返回结果和这次的付费开销。

    同步函数；异步框架里放进线程池调用。策略检查（白名单、文件参数、argparse）不碰库，
    不合格直接返回 403/422，开销为 0。
    """
    db_path = Path(db_path).resolve()
    args = list(args)
    if len(args) > MAX_ARGS:
        return RemoteCliResult(422, False, error=f"args 最多 {MAX_ARGS} 项。")
    try:
        _check_command_name(args)
        if batch_text is not None and args[0] != "batch":
            raise _BadRequest("batch_text 只能和 batch 命令一起使用。")
        parser = _cli._build_parser()
        try:
            ns = _parse(parser, args)
        except _Usage as u:
            if u.code in (0, None):  # --help
                return RemoteCliResult(200, True, output=u.text)
            return RemoteCliResult(200, False, error=_usage_hint(u.text).strip())
        _check_parsed(ns)
        if ns.command == "batch":
            if batch_text is None:
                raise _BadRequest("batch 需要 batch_text（每行一条命令的文本）；这里不读文件也不读 stdin。")
            _validate_batch(parser, batch_text)
    except _RemoteError as e:
        return RemoteCliResult(e.status, False, error=str(e))

    with CostMeter() as meter:
        status, ok, output, error = _run(db_path, ns, parser, args, batch_text)
    return RemoteCliResult(status, ok, output, error, meter.summary())
