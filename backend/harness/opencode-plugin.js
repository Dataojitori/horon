// Horon Harness plugin for opencode.
//
// opencode has no command hooks (hooks.json); it only loads JS plugins. This plugin turns
// opencode's plugin hooks into the same lifecycle events the other hosts use and hands each
// one, as JSON on stdin, to `python run_gateway.py <Event> --adapter opencode`. All Horon logic
// stays in Python (backend/harness/adapters/opencode.py documents the payloads and replies).
//
// Install (global, every opencode session): ~/.config/opencode/plugin/horon-harness.js containing
//   export { HoronHarness } from "<this file's absolute path>"
// Restart opencode (including `opencode serve` / web on 4096) for it to load.
//
// Mapping:
//   chat.message          -> UserPromptSubmit  (banner appended to the end of her last text part)
//   experimental.session.compacting / event session.compacted / experimental.chat.messages.transform
//                         -> marks session compacted; next user message (UserPromptSubmit with reset)
//                            or auto-continue turn (SessionStart with source="compact") resets Horon
//                            session state and attaches the compaction banner
//   tool.execute.before   -> PreToolUse        (deny = throw, opencode returns the error to the model)
//   tool.execute.after    -> PostToolUse       (banner appended to the tool output)
//   event session.idle    -> Stop              (block = send the reason back into the session once)
//   event session.error   -> Interrupt         (only for MessageAbortedError, i.e. the user pressed stop)
//   (built-in tool names and args reach Horon as opencode has them -- bash, filePath -- so only rules written
//    for those names apply; MCP tools <server>_<tool> become mcp__<server>__<tool> in Python, the name
//    Claude Code and dsh use, so rules on an MCP tool apply in every host)
//   shell.env             -> sets HORON_OPENCODE_SESSION_ID so frontend/cli.py run from the bash tool
//                            knows which session it belongs to.
//
// Turn it off for one process: HORON_HOOKS=off (e.g. when asking a model for a clean cold read).
// Agents in SKIP_AGENTS never touch Horon.

import { spawn } from "node:child_process"
import path from "node:path"
import { fileURLToPath } from "node:url"

const HORON_ROOT = process.env.HORON_ROOT || path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..", "..")
const GATEWAY = path.join(HORON_ROOT, "run_gateway.py")
const PYTHON = process.env.HORON_PYTHON || "python"
const DISABLED = /^(0|off|false|no)$/i.test(process.env.HORON_HOOKS ?? "")
const SKIP_AGENTS = new Set(["horon-auditor"])
const GATEWAY_TIMEOUT_MS = 45_000 // Jev alone may take 20 s

function callGateway(event, payload, log) {
  return new Promise((resolve) => {
    let out = ""
    let err = ""
    let done = false
    const finish = (value) => {
      if (done) return
      done = true
      clearTimeout(timer)
      resolve(value)
    }
    let child
    try {
      child = spawn(PYTHON, [GATEWAY, event, "--adapter", "opencode"], {
        cwd: HORON_ROOT,
        env: { ...process.env, PYTHONUTF8: "1", PYTHONIOENCODING: "utf-8" },
        windowsHide: true,
      })
    } catch (e) {
      log(`spawn failed for ${event}: ${e}`)
      return finish(null)
    }
    const timer = setTimeout(() => {
      log(`${event} timed out after ${GATEWAY_TIMEOUT_MS} ms`)
      child.kill()
      finish(null)
    }, GATEWAY_TIMEOUT_MS)
    child.stdout.setEncoding("utf8")
    child.stderr.setEncoding("utf8")
    child.stdout.on("data", (d) => (out += d))
    child.stderr.on("data", (d) => (err += d))
    child.on("error", (e) => {
      log(`${event} process error: ${e}`)
      finish(null)
    })
    child.on("close", (code) => {
      if (code !== 0) log(`${event} exited ${code}: ${err.trim()}`)
      const text = out.trim()
      if (!text) return finish(null)
      try {
        finish(JSON.parse(text))
      } catch {
        log(`${event} returned non-JSON: ${text.slice(0, 500)}`)
        finish(null)
      }
    })
    child.stdin.on("error", () => {})
    child.stdin.end(JSON.stringify(payload))
  })
}

// tool.execute.after gets two result shapes. Built-in tools: {title, output: string, metadata}.
// MCP tools: the raw MCP result {content: [...], ...}; opencode builds the text output from it
// *after* the hook (text items and resource.text joined by blank lines; images and blobs become
// attachments), so the hook must read and extend `content`, not `output`.
function toolResultText(result) {
  if (typeof result?.output === "string") return result.output
  if (Array.isArray(result?.content)) {
    const texts = []
    for (const item of result.content) {
      if (item?.type === "text" && typeof item.text === "string") texts.push(item.text)
      else if (item?.type === "resource" && typeof item.resource?.text === "string") texts.push(item.resource.text)
    }
    return texts.join("\n\n")
  }
  return ""
}

// Returns false when the shape is unknown and nothing was attached.
function appendToToolResult(result, notice) {
  if (typeof result?.output === "string") {
    result.output = result.output ? `${result.output}\n\n${notice}` : notice
    return true
  }
  if (Array.isArray(result?.content)) {
    result.content.push({ type: "text", text: notice }) // non-text items stay where they are
    return true
  }
  return false
}

// opencode IDs carry a type prefix ("prt" for parts, "msg" for messages) and must sort after
// what already exists (opencode's own scheme: 12 hex digits of ms*4096+counter, then 14 random base62 chars).
// The banner is appended to her text after this separator; every banner starts with 【Horon Harness】.
const BANNER_SEP = "\n\n"
const BANNER_HEAD = "【Horon Harness】"
// What she typed in a text part: without a banner we appended earlier (forked or edited messages carry it).
function ownText(text) {
  if (text.startsWith(BANNER_HEAD)) return ""
  const i = text.indexOf(BANNER_SEP + BANNER_HEAD)
  return i < 0 ? text : text.slice(0, i)
}

let idCounter = 0
const BASE62 = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
function ascendingID(prefix) {
  idCounter = (idCounter + 1) % 4096
  const hex = (BigInt(Date.now()) * 4096n + BigInt(idCounter)).toString(16).padStart(12, "0").slice(-12)
  let rand = ""
  for (let i = 0; i < 14; i++) rand += BASE62[Math.floor(Math.random() * 62)]
  return `${prefix}_${hex}${rand}`
}

export const HoronHarness = async ({ client }) => {
  if (DISABLED) return {}

  const log = (message) => {
    client.app
      .log({ body: { service: "horon-harness", level: "warn", message } })
      .catch(() => {})
  }

  const agentOf = new Map() // sessionID -> agent name of the latest user message
  const modelOf = new Map() // sessionID -> {providerID, modelID}
  const lastText = new Map() // sessionID -> {messageID, text} of the latest assistant message
  const continuing = new Set() // sessions whose current turn is our Stop continuation (its Stop must not block again)
  const ownMessages = new Set() // message IDs of the continuations we sent; only these skip UserPromptSubmit
  const children = new Set() // subagent sessions: never block their Stop
  const aborted = new Set() // sessions the user just interrupted
  const compacting = new Map() // sessionID -> "running" | "consumed" during compaction lifecycle
  const compacted = new Set() // sessions whose context was compacted and await state reset + banner injection
  const injectedBanners = new Map() // sessionID -> {messageID, context} for auto-continue messages bypassing chat.message
  let mcpServers = [] // MCP server names from the config, so Python can map <server>_<tool> to mcp__<server>__<tool>
  const turnOf = new Map() // sessionID -> counter bumped by every real user message (a new user turn)
  const queues = new Map() // sessionID -> tail of that session's gateway calls

  const skipped = (sessionID) => !sessionID || SKIP_AGENTS.has(agentOf.get(sessionID))

  const consumeCompacted = (sid) => {
    if (!compacted.delete(sid)) return false
    if (compacting.get(sid) === "running") compacting.set(sid, "consumed")
    else compacting.delete(sid)
    return true
  }

  // opencode does not await `event` callbacks, so a Stop can still be in the gateway when the next user
  // message arrives. Gateway calls of one session therefore run one at a time, in arrival order: the new
  // turn's UserPromptSubmit (end_turn + sensing) cannot interleave with the old turn's Stop in the database.
  const inOrder = (sid, fn) => {
    const run = (queues.get(sid) ?? Promise.resolve()).then(fn)
    queues.set(sid, run.catch(() => {}))
    return run
  }

  return {
    config: async (config) => {
      mcpServers = Object.keys(config?.mcp ?? {})
    },

    "chat.message": async (input, output) => {
      const sid = input.sessionID
      if (input.agent) agentOf.set(sid, input.agent)
      if (input.model) modelOf.set(sid, input.model)
      aborted.delete(sid)
      if (skipped(sid)) return
      const mid = input.messageID ?? output.message?.id
      if (mid && ownMessages.delete(mid)) return // our own Stop continuation, not a user turn
      // A real user message, even one sent while a continuation is running, starts a new user turn.
      // Bump the turn before waiting in the queue, so a Stop still in flight sees it is stale.
      turnOf.set(sid, (turnOf.get(sid) ?? 0) + 1)
      continuing.delete(sid)

      // Her own text parts (not synthetic, not an old separate banner part).
      const hers = (output.parts || []).filter(
        (p) => p.type === "text" && !p.synthetic && !p.metadata?.horon && typeof p.text === "string",
      )
      const prompt = hers
        .map((p) => ownText(p.text)) // a forked or edited message still carries the banner we appended
        .filter(Boolean)
        .join("\n")
      const reset = consumeCompacted(sid)
      const res = await inOrder(sid, () =>
        callGateway("UserPromptSubmit", { session_id: sid, prompt, ...(reset ? { reset: true } : {}) }, log),
      )
      if (res?.context) {
        // The banner goes at the end of her last text part, not into a part of its own. Not synthetic on
        // purpose: opencode's UI hides synthetic parts, and she wants to see these to debug. A part of its own
        // showed up in front of her text: opencode gives a plugin's part a fresh server-clock ID (ignoring ours),
        // the desktop app's IDs for her parts come from its own clock, which can be ahead, parts are ordered by
        // ID, and the app shows only the first text part, so she saw the banner instead of what she typed.
        // Side effect: copy/fork/edit of her message include the banner (ownText cuts it off again).
        const last = hers[hers.length - 1]
        if (last) last.text = `${last.text}${BANNER_SEP}${res.context}`
        else
          output.parts.push({
            id: ascendingID("prt"),
            sessionID: sid,
            messageID: output.message.id,
            type: "text",
            text: res.context,
            metadata: { horon: true },
          })
      }
    },

    "tool.execute.before": async (input, output) => {
      const sid = input.sessionID
      if (skipped(sid)) return
      const reset = consumeCompacted(sid)
      const res = await inOrder(sid, () =>
        callGateway(
          "PreToolUse",
          {
            session_id: sid,
            tool_name: input.tool,
            tool_input: output.args ?? {},
            mcp_servers: mcpServers,
            ...(reset ? { reset: true } : {}),
          },
          log,
        ),
      )
      if (res?.decision === "deny") throw new Error(res.reason || "Blocked by Horon guard policy.")
    },

    "tool.execute.after": async (input, output) => {
      const sid = input.sessionID
      if (skipped(sid)) return
      await inOrder(sid, async () => {
        const res = await callGateway(
          "PostToolUse",
          { session_id: sid, tool_name: input.tool, tool_output: toolResultText(output), mcp_servers: mcpServers },
          log,
        )
        if (!res?.context) return
        if (!appendToToolResult(output, res.context)) {
          // Unknown result shape: the gateway already dequeued these, so put them back for the next drain.
          log(`tool.execute.after: cannot attach notice to ${input.tool} result; requeued`)
          await callGateway("Requeue", { session_id: sid, context: res.context }, log)
        }
      })
    },

    "experimental.session.compacting": async (input) => {
      const sid = input.sessionID
      if (!sid) return
      compacting.set(sid, "running")
      compacted.delete(sid)
      lastText.delete(sid)
    },

    "experimental.compaction.autocontinue": async (input) => {
      const sid = input.sessionID
      if (!sid || skipped(sid)) return
      if (compacting.get(sid) !== "consumed") compacted.add(sid)
    },

    "experimental.chat.messages.transform": async (_input, output) => {
      const msgs = output?.messages
      if (!Array.isArray(msgs) || msgs.length === 0) return
      let lastUser = null
      for (let i = msgs.length - 1; i >= 0; i--) {
        if (msgs[i]?.info?.role === "user") {
          lastUser = msgs[i]
          break
        }
      }
      if (!lastUser?.info?.sessionID) return
      const sid = lastUser.info.sessionID
      if (skipped(sid)) return

      if (compacting.get(sid) === "running" && msgs.some((m) => m?.info?.role === "assistant" && m?.info?.summary)) {
        compacted.add(sid)
      }

      // Auto-compaction inserts a synthetic user continuation message directly into storage without
      // triggering `chat.message`. Reset the session and attach the compaction banner here before the
      // model sees the compacted context.
      if (consumeCompacted(sid)) {
        const res = await inOrder(sid, () =>
          callGateway("SessionStart", { session_id: sid, source: "compact" }, log),
        )
        if (res?.context) {
          injectedBanners.set(sid, { messageID: lastUser.info.id, context: res.context })
        }
      }

      const inj = injectedBanners.get(sid)
      if (!inj) return
      const target = msgs.find((m) => m?.info?.id === inj.messageID)
      if (!target || !Array.isArray(target.parts)) return
      const textParts = target.parts.filter((p) => p?.type === "text" && typeof p.text === "string")
      const lastPart = textParts[textParts.length - 1]
      if (lastPart) {
        if (!lastPart.text.endsWith(inj.context)) {
          lastPart.text = `${lastPart.text}${BANNER_SEP}${inj.context}`
        }
      } else {
        target.parts.push({
          id: ascendingID("prt"),
          sessionID: sid,
          messageID: target.info.id,
          type: "text",
          text: inj.context,
          metadata: { horon: true },
        })
      }
    },

    "experimental.text.complete": async (input, output) => {
      if (compacting.get(input.sessionID) === "running") return
      const prev = lastText.get(input.sessionID)
      const text = output.text ?? ""
      if (prev && prev.messageID === input.messageID) prev.text = prev.text ? `${prev.text}\n${text}` : text
      else lastText.set(input.sessionID, { messageID: input.messageID, text })
    },

    "shell.env": async (input, output) => {
      if (input.sessionID) output.env.HORON_OPENCODE_SESSION_ID = input.sessionID
    },

    event: async ({ event }) => {
      const props = event.properties || {}
      if (event.type === "session.created") {
        if (props.info?.parentID) children.add(props.info.id)
        return
      }
      if (event.type === "session.deleted") {
        const id = props.info?.id
        for (const m of [agentOf, modelOf, lastText, turnOf, queues, compacting, injectedBanners]) m.delete(id)
        for (const s of [continuing, children, aborted, compacted]) s.delete(id)
        return
      }
      if (event.type === "session.compacted") {
        const sid = props.sessionID
        if (!sid) return
        const state = compacting.get(sid)
        compacting.delete(sid)
        lastText.delete(sid)
        if (state !== "consumed" && !skipped(sid)) compacted.add(sid)
        return
      }
      if (event.type === "session.error") {
        const sid = props.sessionID
        if (sid && compacting.get(sid) === "running") {
          compacting.delete(sid)
          compacted.delete(sid)
        }
        if (sid && props.error?.name === "MessageAbortedError") {
          aborted.add(sid)
          continuing.delete(sid)
          lastText.delete(sid)
          if (!skipped(sid)) await inOrder(sid, () => callGateway("Interrupt", { session_id: sid }, log))
        }
        return
      }
      if (event.type !== "session.idle") return

      const sid = props.sessionID
      if (skipped(sid)) return
      if (compacting.get(sid) === "running" || compacted.has(sid)) {
        // Manual `/compact` goes idle immediately after writing the summary; do not run Stop on the summary.
        compacting.delete(sid)
        return
      }
      if (aborted.has(sid)) {
        aborted.delete(sid) // Interrupt already ended the turn
        return
      }
      const turn = turnOf.get(sid) ?? 0
      const wasContinuing = continuing.delete(sid)
      const last = lastText.get(sid)?.text ?? ""
      lastText.delete(sid)
      await inOrder(sid, async () => {
        // A new user turn that arrived while this Stop waited in the queue has its UserPromptSubmit queued
        // behind us, so ending the old turn here is still in order. It only must not block: with
        // stop_hook_active the gateway senses the old reply, ends the old turn and leaves the notices in the
        // queue, and the new turn's UserPromptSubmit (next in line) delivers them.
        const stale = (turnOf.get(sid) ?? 0) !== turn
        const res = await callGateway(
          "Stop",
          {
            session_id: sid,
            last_assistant_message: last,
            stop_hook_active: wasContinuing || children.has(sid) || stale,
          },
          log,
        )
        if (res?.decision !== "block" || !res.reason) return
        if ((turnOf.get(sid) ?? 0) !== turn) {
          // The user moved on while the gateway was deciding. Do not continue the old turn or mark the new
          // one as a continuation; hand the dequeued notices back so the new turn's first drain delivers them.
          // (Still inside the queue, so this lands before the new turn's UserPromptSubmit.)
          await callGateway("Requeue", { session_id: sid, context: res.reason }, log)
          return
        }
        // No await between the check above and these lines: nothing can slip in between.
        continuing.add(sid)
        const messageID = ascendingID("msg")
        ownMessages.add(messageID)
        const body = { messageID, parts: [{ type: "text", text: res.reason }] }
        if (agentOf.get(sid)) body.agent = agentOf.get(sid)
        if (modelOf.get(sid)) body.model = modelOf.get(sid)
        const r = await client.session.promptAsync({ path: { id: sid }, body }).catch((e) => ({ error: e }))
        if (r?.error) {
          continuing.delete(sid)
          ownMessages.delete(messageID)
          log(`Stop continuation failed: ${JSON.stringify(r.error).slice(0, 300)}`)
          await callGateway("Requeue", { session_id: sid, context: res.reason }, log)
        }
      })
    },
  }
}
