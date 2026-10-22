// DeepSeek Harness (dsh) plugin for Horon: the `horon` tool plus the Horon Harness hooks.
//
// Tool: `horon` takes the CLI argument list and runs `python frontend/cli.py <args>` from the
// Horon repository root. Commands and flags are documented in the Horon CLI skill
// (.agents/skills/horon-cli/SKILL.md), not in this schema, so every host follows the same manual.
// The process is spawned by the dsh host, not through ctx.sandbox: Horon writes its database on
// every call (audit log, attention transitions), which a workspace-write shell sandbox would
// refuse when the database lives outside the session workspace.
//
// Hooks: dsh events are turned into the same lifecycle events the opencode plugin sends and
// handed, as JSON on stdin, to `python run_gateway.py <Event> --adapter dsh`. All Horon logic
// stays in Python (backend/harness/adapters/opencode.py documents the payloads and replies).
// Tool names and arguments reach Horon as dsh has them (pwsh, edit, horon {args: [...]}), so
// only rules written for those names apply here, not the ones written for Claude Code's tools.
//   agent/pre-step        -> UserPromptSubmit when the step admits a message she typed
//                            (source kind 'user'); SessionStart(source=compact) on the first
//                            step after a compaction that no user message starts. The banner
//                            enters the step as one more user message with source kind 'horon'.
//   tools/pre-execute     -> PreToolUse        (deny -> {kind:'deny', reason})
//   tools/post-execute    -> PostToolUse       (banner -> additionalContexts, after the result)
//   agent/turn-stopping   -> Stop              (block -> agent.steer(reason), the turn runs one
//                                               more step; the Stop after it does not block)
//   session/event compaction/end, agent/created source compact|clear
//                         -> marks the session compacted; the next UserPromptSubmit carries reset
//
// Mount: in the profile patch, add to the agent preset's plugins
//   - name: file:///C:/Users/.../horon/backend/harness/dsh-plugin/index.js   (a file:// URL)
//     config: { python: <python.exe>, hooks: true }
// config.hooks: false (or env HORON_HOOKS=off) keeps only the tool.

import { spawn } from 'node:child_process'
import { randomUUID } from 'node:crypto'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

export const name = 'horon'
export const inject = ['tools']

const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const GATEWAY_TIMEOUT_MS = 45_000 // Jev alone may take 20 s
const SOURCE_KIND = 'horon'
const UTF8_ENV = { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }

function settings(config) {
  const root = config.root ?? DEFAULT_ROOT
  return { root, python: config.python ?? 'python' }
}

// Run `python <script> <args>` in the Horon root, feeding `stdin`; resolve {code, out, err}.
function runPython(config, script, args, { stdin, env, signal, timeoutMs } = {}) {
  const { root, python } = settings(config)
  return new Promise((resolve, reject) => {
    const child = spawn(python, [path.join(root, script), ...args], {
      cwd: root,
      env: { ...process.env, ...UTF8_ENV, ...env },
      shell: false,
      windowsHide: true,
      signal,
    })
    const out = []
    const err = []
    const timer = timeoutMs ? setTimeout(() => child.kill(), timeoutMs) : undefined
    child.stdout.on('data', (chunk) => out.push(chunk))
    child.stderr.on('data', (chunk) => err.push(chunk))
    child.on('error', (e) => {
      clearTimeout(timer)
      reject(e)
    })
    child.on('close', (code) => {
      clearTimeout(timer)
      resolve({ code, out: Buffer.concat(out).toString('utf8'), err: Buffer.concat(err).toString('utf8') })
    })
    child.stdin.on('error', () => {})
    child.stdin.end(stdin ?? '')
  })
}

// One user-role message carrying a Horon banner, shaped like dsh's own createUserMessage.
function bannerMessage(text, summary) {
  return Object.freeze({
    id: randomUUID(),
    role: 'user',
    content: [{ type: 'text', text }],
    source: { kind: SOURCE_KIND, form: 'notice', summary },
  })
}

// Text blocks of a message content or a tool result content, joined.
function contentText(content) {
  if (typeof content === 'string') return content
  if (!Array.isArray(content)) return content == null ? '' : JSON.stringify(content)
  return content
    .map((block) => (block?.type === 'text' && typeof block.text === 'string' ? block.text : ''))
    .filter(Boolean)
    .join('\n')
}

function registerTool(ctx, config) {
  const skill = path.join(settings(config).root, '.agents', 'skills', 'horon-cli', 'SKILL.md')
  ctx.tools.register({
    name: 'horon',
    description:
      `Run the Horon CLI: python frontend/cli.py <args>, from the Horon repository root. ` +
      `Pass each CLI argument as one array item; no shell quoting is applied. ` +
      `Read the manual before first use: ${skill}`,
    parameters: {
      type: 'object',
      properties: {
        args: {
          type: 'array',
          items: { type: 'string' },
          description: 'CLI arguments, e.g. ["read_concept", "42"].',
        },
      },
      required: ['args'],
      additionalProperties: false,
    },
    output: {
      schema: { type: 'string' },
      render: (_args, value) => [{ type: 'text', text: value }],
    },
    async execute(args, exec) {
      // The CLI records reads and audit entries under the dsh session the call came from.
      const sessionId = exec?.agent?.id
      const { code, out, err } = await runPython(config, path.join('frontend', 'cli.py'), args.args, {
        env: sessionId ? { HORON_DSH_SESSION_ID: String(sessionId) } : {},
        signal: exec?.signal,
      })
      const text = out + err
      if (code !== 0) throw new Error(`horon exited with code ${code}\n${text}`)
      return text
    },
  })
}

function registerHooks(ctx, config) {
  const log = (message) => ctx.logger?.warn?.(`horon: ${message}`)

  // `signal` is the hook's cancellation: stopping the turn kills the gateway child instead of waiting it out.
  const callGateway = async (event, payload, signal) => {
    if (signal?.aborted) return null
    try {
      const { code, out, err } = await runPython(config, 'run_gateway.py', [event, '--adapter', 'dsh'], {
        stdin: JSON.stringify(payload),
        timeoutMs: GATEWAY_TIMEOUT_MS,
        signal,
      })
      if (signal?.aborted) return null
      if (code !== 0) log(`${event} exited ${code}: ${err.trim().slice(0, 500)}`)
      const text = out.trim()
      if (!text) return null
      return JSON.parse(text)
    } catch (e) {
      if (!signal?.aborted) log(`${event} failed: ${e}`)
      return null
    }
  }

  const compacted = new Set() // sessions whose context was compacted or cleared, awaiting reset + banner
  const continuing = new Set() // sessions whose current step runs on our Stop steering
  const lastText = new Map() // sessionId -> text of the latest assistant message
  const queues = new Map() // sessionId -> tail of that session's gateway calls

  // Parallel tool calls of one step run their pre/post-execute concurrently; gateway calls of one
  // session run one at a time, in arrival order, so sensing and draining do not interleave.
  const inOrder = (sid, fn) => {
    const run = (queues.get(sid) ?? Promise.resolve()).then(fn)
    queues.set(sid, run.catch(() => {}))
    return run
  }
  const sidOf = (agent) => (agent?.id == null ? null : String(agent.id))
  const isChild = (agent) => Boolean(agent?.session?.header?.parentSession)

  ctx.on('agent/created', ({ agent, source }) => {
    if (source === 'compact' || source === 'clear') {
      const sid = sidOf(agent)
      if (sid) compacted.add(sid)
    }
  })

  ctx.on('session/event', (session, event) => {
    const sid = session?.id == null ? null : String(session.id)
    if (!sid || !event) return
    if (event.type === 'assistant/message') {
      const text = contentText(event.data?.message?.content)
      if (text.trim()) lastText.set(sid, text)
    } else if (event.type === 'compaction/end' && !event.data?.error) {
      compacted.add(sid)
      lastText.delete(sid)
    }
  })

  ctx.on('agent/disposed', ({ agent }) => {
    const sid = sidOf(agent)
    for (const m of [lastText, queues]) m.delete(sid)
    for (const s of [compacted, continuing]) s.delete(sid)
  })

  ctx.on('agent/pre-step', async ({ agent, messages, signal }, next) => {
    const decision = await next()
    const sid = sidOf(agent)
    if (!sid || decision?.kind !== 'enter') return decision
    const typed = (decision.messages ?? messages ?? []).filter((m) => m?.source?.kind === 'user')
    let res = null
    if (typed.length > 0) {
      // A message she typed starts a new user turn; our Stop continuation no longer applies.
      continuing.delete(sid)
      const prompt = typed.map((m) => contentText(m.content)).filter(Boolean).join('\n')
      const reset = compacted.delete(sid)
      res = await inOrder(sid, () =>
        callGateway('UserPromptSubmit', { session_id: sid, prompt, ...(reset ? { reset: true } : {}) }, signal),
      )
    } else if (compacted.delete(sid)) {
      res = await inOrder(sid, () => callGateway('SessionStart', { session_id: sid, source: 'compact' }, signal))
    }
    if (!res?.context) return decision
    return { ...decision, messages: [...decision.messages, bannerMessage(res.context, 'Horon Harness')] }
  })

  ctx.on('tools/pre-execute', async (exec, next) => {
    const sid = sidOf(exec?.agent)
    if (sid) {
      const reset = compacted.delete(sid)
      const res = await inOrder(sid, () =>
        callGateway('PreToolUse', {
          session_id: sid,
          tool_name: exec.name,
          tool_input: exec.arguments ?? {},
          ...(reset ? { reset: true } : {}),
        }, exec.signal),
      )
      if (res?.decision === 'deny') return { kind: 'deny', reason: res.reason || 'Blocked by Horon guard policy.' }
    }
    return next()
  }, { prepend: true }) // ahead of permission presets, which may answer without calling next()

  ctx.on('tools/post-execute', async (exec, result, next) => {
    const downstream = await next()
    const sid = sidOf(exec?.agent)
    if (!sid) return downstream
    // A downstream block replaces the content the model sees with its feedback.
    const shown = downstream?.kind === 'block' ? downstream.feedback : (downstream?.content ?? result?.content)
    const res = await inOrder(sid, () =>
      callGateway('PostToolUse', { session_id: sid, tool_name: exec.name, tool_output: contentText(shown) }, exec.signal),
    )
    if (!res?.context) return downstream
    return {
      ...downstream,
      additionalContexts: [bannerMessage(res.context, `Horon Harness · ${exec.name}`), ...(downstream.additionalContexts ?? [])],
    }
  })

  ctx.on('agent/turn-stopping', async ({ agent, signal }) => {
    const sid = sidOf(agent)
    if (!sid || signal?.aborted) return
    const wasContinuing = continuing.delete(sid)
    const last = lastText.get(sid) ?? ''
    lastText.delete(sid)
    const res = await inOrder(sid, () =>
      callGateway('Stop', {
        session_id: sid,
        last_assistant_message: last,
        stop_hook_active: wasContinuing || isChild(agent),
      }, signal),
    )
    if (res?.decision !== 'block' || !res.reason || signal?.aborted) return
    continuing.add(sid)
    try {
      agent.steer(bannerMessage(res.reason, 'Horon Harness · Stop'))
    } catch (e) {
      continuing.delete(sid)
      log(`Stop continuation failed: ${e}`)
      await inOrder(sid, () => callGateway('Requeue', { session_id: sid, context: res.reason }))
    }
  })
}

export function apply(ctx, config = {}) {
  registerTool(ctx, config)
  const off = config.hooks === false || /^(0|off|false|no)$/i.test(process.env.HORON_HOOKS ?? '')
  if (!off) registerHooks(ctx, config)
}
