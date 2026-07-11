"""Thin client that drives an opencode session for one external review (外审).

This layer is deliberately *content-agnostic*: it knows nothing about goals,
paths, or review rubrics. Its only jobs are transport + verdict capture:

  1. Open a BRAND-NEW opencode session (guarantees clean context per audit).
  2. Send caller-supplied text turns (`say`).
  3. On the designated turn, extract one JSON object from the reviewer's text
     and require it to pass the caller's rubric-specific validator
     (`collect_verdict`). Invalid replies are nudged and retried.
  4. Fail closed: if no valid verdict is captured, we return a NoVerdict — the
     caller (the confirm gate) must treat that as "do NOT confirm".

Prompt generation for each turn is a SEPARATE piece of work; it is injected by
the caller as plain text.

Transport: opencode's HTTP server (`opencode serve` / the running web port).
  - Model is pinned in the `horon-auditor` agent config (opencode.json), NOT
    sent per message — the client only references the agent by name.
  - Local default server needs no auth unless OPENCODE_SERVER_PASSWORD is set.

Run this module as a script (`python -m backend.audit.opencode_client --probe`)
against your live server to inspect the raw reply shape during integration.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

# --- Configuration (env-overridable) ----------------------------------------

# Point this at your running opencode server. You keep one on :4096 daily;
# consider a dedicated `opencode serve --port 4097` to isolate audit sessions
# from your personal ones.
SERVER_URL = os.environ.get("OPENCODE_SERVER_URL", "http://127.0.0.1:4096")

# Model lives in HORON's env — horon owns the choice and passes it per message,
# so nothing about the model has to be configured on the opencode side.
# Get the exact provider/model slug from `opencode models` (NOT the UI label).
# e.g. HORON_AUDITOR_MODEL="opencode/deepseek-v4-pro"
MODEL = os.environ.get("HORON_AUDITOR_MODEL")  # None => let server default

# opencode agent that runs the review. Defined project-locally in
# horon/opencode.json with the model pinned and bash locked to review_cli.py.
# (Since the model is pinned there, HORON_AUDITOR_MODEL can stay unset.)
AUDITOR_AGENT = os.environ.get("HORON_AUDITOR_AGENT", "horon-auditor")

# System prompt for the reviewer. horon OWNS the review instructions and sends
# them per message through the `system` field. The agent prompt in
# opencode.json supplies only the stable role framing. PLACEHOLDER for now; the
# real review rubric is written later.
SYSTEM_PROMPT = (
    "You are the Horon external reviewer. "
    "[PLACEHOLDER SYSTEM PROMPT — review rubric TBD]"
)

# Optional HTTP basic auth (only if OPENCODE_SERVER_PASSWORD is set on serve).
_SERVER_PASSWORD = os.environ.get("OPENCODE_SERVER_PASSWORD")
_SERVER_USER = os.environ.get("OPENCODE_SERVER_USER", "opencode")

# Sent when the reviewer's reply contained no JSON object accepted by the
# caller-supplied validator.
NUDGE_MESSAGE = (
    "Your previous reply did not contain a valid JSON verdict. Reply with "
    "ONLY a single JSON object — no prose, no markdown code fences, nothing "
    "before or after it."
)

_HTTP_TIMEOUT = int(os.environ.get("OPENCODE_HTTP_TIMEOUT", "300"))


# --- Exceptions & result types ----------------------------------------------


class OpencodeUnavailable(RuntimeError):
    """The opencode server could not be reached or returned a transport error.

    Distinct from a review result: this means we never got a verdict *because
    the machinery failed*, not because the reviewer declined. The gate should
    treat this as a hard error (surface it), not as a silent rejection.
    """


@dataclass
class Verdict:
    """A successfully captured structured verdict.

    Attributes:
        args: The validated JSON object extracted from the reviewer's reply.
              Its field schema is defined by the caller's rubric; this client
              does not interpret it.
        session_id: opencode session id, so the full transcript (incl. the
                    reviewer's free-form reasoning turns) can be recovered from
                    opencode's own store later.
    """

    args: dict[str, Any]
    session_id: str


@dataclass
class NoVerdict:
    """Fail-closed sentinel: no valid verdict was captured.

    The gate MUST NOT confirm on this. `reason` explains why (reviewer never
    called the tool after N nudges, or the call carried no usable args).
    """

    reason: str
    session_id: str
    last_text: str = ""


@dataclass
class Reply:
    """One assistant turn, normalized from the server's `parts` array.

    Attributes:
        text: All concatenated text parts of the reply.
        tool_calls: List of {"tool": name, "args": {...}} extracted from any
                    tool parts (defensive; may be empty).
        raw_parts: The untouched `parts` array, for debugging / calibration.
    """

    text: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    raw_parts: list = field(default_factory=list)


# --- HTTP plumbing (stdlib only, no extra deps) -----------------------------


def _request(method: str, path: str, payload: Optional[dict] = None) -> Any:
    """Send an HTTP request to the opencode server and return parsed JSON.

    Args:
        method: "GET" or "POST".
        path: Path beginning with "/", appended to SERVER_URL.
        payload: JSON body for POST (None for GET).

    Returns:
        Parsed JSON (dict/list), or None on an empty body.

    Raises:
        OpencodeUnavailable: connection refused, timeout, or non-2xx status.
    """
    url = f"{SERVER_URL}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"}
    if _SERVER_PASSWORD is not None:
        token = base64.b64encode(
            f"{_SERVER_USER}:{_SERVER_PASSWORD}".encode()).decode()
        headers["Authorization"] = f"Basic {token}"

    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            pass
        raise OpencodeUnavailable(
            f"opencode {method} {path} -> HTTP {e.code}: {detail[:500]}") from e
    except urllib.error.URLError as e:
        raise OpencodeUnavailable(
            f"cannot reach opencode server at {SERVER_URL} "
            f"({e.reason}). Is `opencode serve` running on that port?") from e

    return json.loads(body) if body else None


# --- Reply normalization ----------------------------------------------------


def _extract_text(parts: list) -> str:
    """Concatenate all text parts of a reply into one string."""
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("type") == "text" and p.get("text"):
            out.append(p["text"])
    return "\n".join(out)


def _extract_tool_calls(parts: list,
                        tool_name: Optional[str] = None) -> list[dict]:
    """Pull tool calls out of a reply's `parts`, defensively.

    The exact shape of a tool part is not pinned by opencode's public docs, so
    this scans for the likely representations rather than assuming one:
      - opencode ToolPart:  {"type":"tool", "tool":<name>, "state":{"input":{...}}}
      - AI-SDK-ish:         {"type":"tool-invocation", "toolName":..., "args":{...}}
      - flat fallbacks:     {"name":..., "input"/"args"/"arguments":{...}}

    Args:
        parts: The reply `parts` array.
        tool_name: If given, only return calls to this tool.

    Returns:
        List of {"tool": name, "args": {...}}. `args` may be {} if the shape
        was recognized as a tool call but no argument object could be located.
    """
    calls: list[dict] = []
    for p in parts:
        if not isinstance(p, dict):
            continue
        name = p.get("tool") or p.get("toolName") or p.get("name")
        looks_tool = p.get("type") in ("tool", "tool-invocation", "tool_call",
                                       "tool-call") or name is not None
        if not looks_tool or not name:
            continue

        args: Any = None
        state = p.get("state")
        if isinstance(state, dict):
            args = state.get("input") or state.get("args")
        if args is None:
            args = p.get("input") or p.get("args") or p.get("arguments")
        # Some encoders stringify the args JSON.
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except json.JSONDecodeError:
                pass
        if not isinstance(args, dict):
            args = {} if args is None else {"_raw": args}

        if tool_name is None or name == tool_name:
            calls.append({"tool": name, "args": args})
    return calls


def _extract_json_object(text: str) -> Optional[dict]:
    """Best-effort extract a single JSON object from an LLM reply.

    Tries json_repair first (a real parser — handles markdown ```json fences,
    trailing commas, prose around the object, and nested braces). Falls back to
    a string-aware brace-balancing scan + stdlib json if json_repair isn't
    installed.

    Args:
        text: The assistant reply text.

    Returns:
        The parsed dict, or None if no JSON object could be recovered.
    """
    if not text:
        return None
    try:
        from json_repair import repair_json
        obj = repair_json(text, return_objects=True)
        if isinstance(obj, dict) and obj:
            return obj
        if isinstance(obj, list):
            for item in obj:
                if isinstance(item, dict) and item:
                    return item
    except ImportError:
        pass
    except Exception:
        pass
    return _brace_scan_json(text)


def _brace_scan_json(text: str) -> Optional[dict]:
    """Locate the outermost {...} (string-aware) and json.loads it.

    Args:
        text: Text that may contain a JSON object amid other content.

    Returns:
        The first top-level object that parses as a dict, or None.
    """
    start = text.find("{")
    while start != -1:
        depth = 0
        in_str = False
        esc = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_str:
                if esc:
                    esc = False
                elif ch == "\\":
                    esc = True
                elif ch == '"':
                    in_str = False
            elif ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(text[start:i + 1])
                        if isinstance(obj, dict):
                            return obj
                    except json.JSONDecodeError:
                        pass
                    break
        start = text.find("{", start + 1)
    return None


def _normalize_reply(response: Any) -> Reply:
    """Turn a POST /message response into a Reply.

    The message endpoint returns {"info": Message, "parts": Part[]}. We read
    `parts` directly.
    """
    parts = []
    if isinstance(response, dict):
        parts = response.get("parts") or []
    return Reply(
        text=_extract_text(parts),
        tool_calls=_extract_tool_calls(parts),
        raw_parts=parts,
    )


# --- The session driver -----------------------------------------------------


class AuditSession:
    """A single external review: one fresh opencode session, used then dropped.

    Usage (prompts are the caller's job — passed in as plain text):

        with AuditSession(title="audit c#42") as s:
            s.say(turn1_prompt)              # e.g. open-ended "what's needed?"
            s.say(turn2_prompt)              # e.g. show the real candidate path
            result = s.collect_verdict(
                turn3_prompt,
                validator=validate_review_verdict,
            )
        if isinstance(result, Verdict):
            ...   # result.args holds the structured verdict
        else:
            ...   # NoVerdict -> gate must NOT confirm

    The session is intentionally not deleted on exit: opencode persists its own
    conversation log, so the transcript stays recoverable via `session_id`.
    """

    def __init__(self, title: Optional[str] = None,
                 agent: Optional[str] = AUDITOR_AGENT,
                 model: Optional[str] = MODEL,
                 system: Optional[str] = SYSTEM_PROMPT):
        """Create the handle (does not open the session yet).

        Args:
            title: Human-readable session title (shows in opencode's list).
            agent: opencode agent name (optional; omitted from the request when
                   None — horon drives everything per message instead).
            model: provider/model slug sent per message (optional; None lets
                   the server use its default). horon owns this via env.
            system: system prompt sent per message (optional). Prompt content is
                    the caller's job; this is just the plumbing to carry it.
        """
        self.title = title or "horon-audit"
        self.agent = agent
        self.model = model
        self.system = system
        self.session_id: Optional[str] = None

    def _message_body(self, text: str) -> dict:
        """Build a POST /message body, including only the fields that are set.

        Args:
            text: The user turn text.

        Returns:
            Request body dict with `parts` plus any of agent/model/system.
        """
        body: dict[str, Any] = {"parts": [{"type": "text", "text": text}]}
        if self.agent:
            body["agent"] = self.agent
        if self.model:
            # CONFIRMED via live server: the message `model` field must be an
            # object {"providerID","modelID"}, NOT a "provider/model" string.
            # (Usually left unset — the model is pinned in the agent config.)
            prov, _, mid = self.model.partition("/")
            body["model"] = {"providerID": prov, "modelID": mid}
        if self.system:
            body["system"] = self.system
        return body

    # -- lifecycle --

    def open(self) -> str:
        """Create a fresh opencode session and return its id.

        Returns:
            The new session id.

        Raises:
            OpencodeUnavailable: server unreachable, or no id in the response.
        """
        resp = _request("POST", "/session", {"title": self.title})
        sid = resp.get("id") if isinstance(resp, dict) else None
        if not sid:
            raise OpencodeUnavailable(
                f"POST /session returned no id: {str(resp)[:300]}")
        self.session_id = sid
        return sid

    def __enter__(self) -> "AuditSession":
        self.open()
        return self

    def __exit__(self, *exc) -> None:
        # Deliberately no delete; opencode keeps the transcript.
        return None

    # -- turns --

    def say(self, text: str) -> Reply:
        """Send one text turn and return the reviewer's reply.

        Args:
            text: The prompt for this turn (caller-generated).

        Returns:
            A normalized Reply (text + any tool_calls + raw parts).

        Raises:
            OpencodeUnavailable: server/transport failure.
            RuntimeError: called before `open()`.
        """
        if not self.session_id:
            raise RuntimeError("AuditSession.say called before open()")
        resp = _request("POST", f"/session/{self.session_id}/message",
                        self._message_body(text))
        return _normalize_reply(resp)

    def collect_verdict(
        self,
        text: str,
        *,
        validator: Callable[[dict[str, Any]], bool],
        max_nudges: int = 2,
    ):
        """Send a turn that must end in a single JSON verdict object.

        Sends `text`; parses the reply for one JSON object (json_repair handles
        fences / prose / trailing commas), then asks the caller-supplied
        validator to check the review rubric's schema. Missing or invalid JSON
        is nudged and retried up to `max_nudges` times.

        Args:
            text: The prompt asking for the verdict (caller-generated).
            validator: Rubric-specific schema validator. It must return True
                       only for a complete verdict that the confirm gate may
                       consume. Exceptions are treated as validation failures.
            max_nudges: Extra retry messages after the first attempt.

        Returns:
            Verdict on success; NoVerdict (fail-closed) if no valid JSON
            verdict was produced.

        Raises:
            OpencodeUnavailable: server/transport failure (NOT a rejection).
        """
        assert self.session_id  # for type-checkers; open() guarantees it
        reply = self.say(text)
        attempts = 0
        while True:
            obj = _extract_json_object(reply.text)
            valid = False
            if obj is not None:
                try:
                    valid = validator(obj) is True
                except Exception:
                    # A broken or overly strict rubric validator must fail
                    # closed just like malformed model output.
                    valid = False
            if valid:
                return Verdict(args=obj, session_id=self.session_id)
            if attempts >= max_nudges:
                return NoVerdict(
                    reason=(f"no valid JSON verdict after "
                            f"{attempts + 1} attempt(s)"),
                    session_id=self.session_id,
                    last_text=reply.text,
                )
            attempts += 1
            reply = self.say(NUDGE_MESSAGE)


# --- Calibration / smoke-test entrypoint ------------------------------------


def _probe() -> None:
    """Open a session, send one trivial turn, and dump the RAW reply.

    Use this once against your live server to confirm the message endpoint's
    response shape before wiring the client into the confirm gate.
    """
    print(f"server   : {SERVER_URL}")
    print(f"agent    : {AUDITOR_AGENT}")
    print(f"model    : {MODEL}")
    s = AuditSession(title="horon-audit probe")
    sid = s.open()
    print(f"session  : {sid}")
    resp = _request("POST", f"/session/{sid}/message",
                    s._message_body("Reply with the single word: pong."))
    print("--- RAW REPLY ---")
    print(json.dumps(resp, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    import sys

    if "--probe" in sys.argv:
        _probe()
    else:
        print(__doc__)
