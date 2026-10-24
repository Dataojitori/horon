"""Horon 调用外部模型的唯一入口：用哪些模型、怎么调、每次花了多少钱。

模型（都写完整的 OpenRouter 模型 ID）：
    EMBEDDING_MODEL  书腰的语义向量（search_semantic、补向量）
    JEV_MODEL        Jev：传感器复核、audit 里的逐行跑题判断
    NAMER_MODEL      suggest_name 起名

调用：
    openrouter_post(url, payload, timeout)    嵌入（EMBEDDINGS_ENDPOINT）和 Jev（DECISIONS_ENDPOINT）
    complete_text(prompt, kind=...)           让 NAMER_MODEL 回一段文字；有 OPENCODE_GO_API_KEY
                                              走 OpenCode Go，没有就用 OPENROUTER_API_KEY 走 OpenRouter
两个函数收到响应后都自己调 record_usage 记账，调用方不用管。

记账：用 CostMeter 把一段代码包起来，就能拿到这段代码里所有模型调用的合计：

    with CostMeter() as meter:
        ...跑命令...
    meter.summary()   # {"cost_usd": ..., "input_tokens": ..., "output_tokens": ..., "calls": [...], ...}

美元数优先用提供方在响应里给的 usage.cost（OpenRouter 的嵌入、Jev、聊天都给）。响应里没有 cost 的
（OpenCode Go 是订阅，不给），按 PRICES_PER_TOKEN 里同一模型 ID 的单价折算；表里也没有这个模型，
cost_usd 记 None，合计里按 0 算，并在 summary 的 unpriced_calls 里数出来——换了模型忘了补单价，
会在这里露出来，不会悄悄套用别的模型的价格。

用 ContextVar 保存当前的 meter，所以每个线程、每个请求各记各的；没有 meter 时 record_usage 什么都不做。
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
import uuid
from contextvars import ContextVar
from typing import Any

# ── 模型与接口 ───────────────────────────────────────────────────────────────

EMBEDDING_MODEL = "voyageai/voyage-4-large"
EMBEDDING_DIMENSIONS = 1024
JEV_MODEL = "perplexity/pplx-decider-v1.2"
NAMER_MODEL = "openai/gpt-6-luna"

EMBEDDINGS_ENDPOINT = "https://openrouter.ai/api/v1/embeddings"
DECISIONS_ENDPOINT = "https://openrouter.ai/api/alpha/decisions"
CHAT_ENDPOINT = "https://openrouter.ai/api/v1/chat/completions"
GO_RESPONSES_ENDPOINT = "https://opencode.ai/zen/go/v1/responses"

# 响应里不带 cost 时用的单价（美元/token），键是完整模型 ID。
# 数字取自 https://openrouter.ai/api/v1/models 里该模型的 pricing.prompt / pricing.completion；
# 没算缓存折扣和超长输入加价，所以是近似。
PRICES_PER_TOKEN: dict[str, dict[str, float]] = {
    "openai/gpt-6-luna": {"input": 0.0000001, "output": 0.0000005},
}

# ── 记账 ─────────────────────────────────────────────────────────────────────

_CURRENT: ContextVar["CostMeter | None"] = ContextVar("horon_cost_meter", default=None)


class CostMeter:
    """收集 `with` 块内发生的模型调用。可以嵌套：内层记的调用也会算进外层。"""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self._token = None

    def __enter__(self) -> "CostMeter":
        self._outer = _CURRENT.get()
        self._token = _CURRENT.set(self)
        return self

    def __exit__(self, *exc) -> None:
        _CURRENT.reset(self._token)
        if self._outer is not None:
            self._outer.calls.extend(self.calls)

    def add(self, kind: str, model: str, input_tokens: int | None,
            output_tokens: int | None, cost_usd: float | None) -> None:
        self.calls.append({
            "kind": kind,
            "model": model,
            "input_tokens": input_tokens or 0,
            "output_tokens": output_tokens or 0,
            "cost_usd": cost_usd,
        })

    @property
    def cost_usd(self) -> float:
        return sum(c["cost_usd"] for c in self.calls if c["cost_usd"] is not None)

    def summary(self) -> dict[str, Any]:
        return {
            "cost_usd": self.cost_usd,
            "input_tokens": sum(c["input_tokens"] for c in self.calls),
            "output_tokens": sum(c["output_tokens"] for c in self.calls),
            "unpriced_calls": sum(1 for c in self.calls if c["cost_usd"] is None),
            "calls": list(self.calls),
        }


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def record_usage(kind: str, model: str, usage: Any) -> None:
    """记一次模型调用。model 是完整模型 ID；usage 是响应里的 usage 字段（可能缺失或字段名不同）。

    token 数兼容两种写法：prompt_tokens/completion_tokens（嵌入、聊天）、
    input_tokens/output_tokens（decisions、responses）。美元数取 usage.cost；没有就按
    PRICES_PER_TOKEN[model] 折算；单价表里也没有、或 token 数缺失，记 None。
    """
    meter = _CURRENT.get()
    if meter is None:
        return
    usage = usage if isinstance(usage, dict) else {}
    tokens_in = _number(usage.get("prompt_tokens", usage.get("input_tokens")))
    tokens_out = _number(usage.get("completion_tokens", usage.get("output_tokens")))
    cost = _number(usage.get("cost"))
    price = PRICES_PER_TOKEN.get(model)
    if cost is None and price is not None and tokens_in is not None and tokens_out is not None:
        cost = tokens_in * price["input"] + tokens_out * price["output"]
    meter.add(kind, model, tokens_in, tokens_out, cost)


# ── 调用 ─────────────────────────────────────────────────────────────────────

def openrouter_post(url: str, payload: dict, timeout: int, *, kind: str | None = None) -> dict:
    """POST a JSON payload to an OpenRouter endpoint, record its cost, and return the decoded reply.

    Input: the endpoint URL, the request payload (its "model" is the full model ID), and a
    timeout in seconds; `kind` labels the call in the cost record (default: "embedding" /
    "decision" / "chat" by endpoint). The API key is read from OPENROUTER_API_KEY (surrounding whitespace removed).
    Raises RuntimeError when the key is unset or blank, and when OpenRouter answers with an
    HTTP error; that message carries the status and OpenRouter's response body, which says
    why (bad key, no credit, unknown model, payload too large, ...).
    """
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key:
        raise RuntimeError(
            "OPENROUTER_API_KEY not set. "
            "Add it to .env or set the environment variable.")
    req = urllib.request.Request(
        url,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            reply = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"OpenRouter HTTP {e.code} from {url}: {body}") from e
    kind = kind or {EMBEDDINGS_ENDPOINT: "embedding", DECISIONS_ENDPOINT: "decision"}.get(url, "chat")
    record_usage(kind, str(payload.get("model", "")), reply.get("usage"))
    return reply


def complete_text(prompt: str, *, kind: str, model: str = NAMER_MODEL, timeout: int = 120) -> str:
    """Send one user prompt to `model` with reasoning turned off, record the cost, return the reply text.

    Input: the prompt; `kind` labels the call in the cost record (e.g. "suggest_name");
    `model` is the full OpenRouter model ID. With OPENCODE_GO_API_KEY set (non-blank) the call goes to
    OpenCode Go, which names the same model without the vendor prefix ("openai/gpt-6-luna" ->
    "gpt-6-luna"); otherwise it goes to OpenRouter with OPENROUTER_API_KEY.
    Output: the reply text ("" if the model returned none).
    Raises RuntimeError if no key is set, and on an HTTP error (the message carries the
    status and the response body); network errors propagate as raised.
    """
    if go_key := os.environ.get("OPENCODE_GO_API_KEY", "").strip():
        req = urllib.request.Request(
            GO_RESPONSES_ENDPOINT,
            data=json.dumps({"model": model.split("/", 1)[-1], "input": prompt,
                             "reasoning": {"effort": "none"}}).encode(),
            # OpenCode Go 要求自定 User-Agent 和每段对话一个 x-opencode-session
            headers={"Authorization": "Bearer " + go_key, "Content-Type": "application/json",
                     "User-Agent": "horon-namer/1.0", "x-opencode-session": str(uuid.uuid4())})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                data = json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"OpenCode Go HTTP {e.code} from {GO_RESPONSES_ENDPOINT}: {body}") from e
        record_usage(kind, model, data.get("usage"))
        return "".join(c.get("text", "") for o in data.get("output", []) if o.get("type") == "message"
                       for c in o.get("content", []))
    data = openrouter_post(CHAT_ENDPOINT, {
        "model": model, "reasoning": {"effort": "none"},
        "messages": [{"role": "user", "content": prompt}],
    }, timeout, kind=kind)
    return data["choices"][0]["message"]["content"] or ""
