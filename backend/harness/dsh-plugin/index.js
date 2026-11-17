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
// Mount: the repository root is a dsh bundle (package.json `dsh.bundle`), whose patch
// (cordis.patch.yml next to this file) inserts this plugin at the profile root, so every agent
// preset inherits the tool and the hooks see every agent. Install with
//   dsh plugin --profile <profile> add <Horon checkout directory>
// Settings, each read from the row's config first, then the environment:
//   root   (HORON_ROOT)    the Horon checkout. Defaults to the checkout this file sits in, which
//                          only exists when the bundle was installed from a checkout directory.
//   python (HORON_PYTHON)  the Python that has Horon's requirements. When set, it is used as is.
//                          When not, the plugin prepares its own under HORON_HOME/runtime with uv
//                          (see "Managed Python" below), so `dsh plugin add horon` is all it takes.
//   hooks  (HORON_HOOKS)   false / off keeps only the tool.
// Volatile settings (edited on the settings page, no restart): dataDir, startupReminder, subagents
// and the two credential reference names. They are read with .get() at each spawn.

import { spawn } from 'node:child_process'
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, mkdirSync, readFileSync, readdirSync, statSync } from 'node:fs'
import { mkdir, open, readFile, rename, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import z from '@deepseek-ai/schemastery'

export const name = 'horon'
export const inject = ['tools']

// Row settings. root / python / hooks are the long-standing ordinary fields (restart on change).
// The rest are volatile: edited from the settings page (client.js) and applied without a restart.
// The two *KeyEnv fields only name a credential reference; the key itself is written through the
// credentials domain and never lands in cordis.patch.yml.
export const Config = z.object({
  root: z.string(),
  python: z.string(),
  hooks: z.boolean(),
  dataDir: z.string().volatile(),
  startupReminder: z.boolean().default(true).volatile(),
  subagents: z.boolean().default(false).volatile(),
  openrouterKeyEnv: z.string().role('credential-ref').default('HORON_OPENROUTER_API_KEY').volatile(),
  remoteKeyEnv: z.string().role('credential-ref').default('HORON_REMOTE_KEY').volatile(),
})

const DEFAULT_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..')
const CLI = path.join('frontend', 'cli.py')
const GATEWAY_TIMEOUT_MS = 45_000 // Jev alone may take 20 s
const SOURCE_KIND = 'horon'
const UTF8_ENV = { PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8' }

// A volatile field is a reactive value read with .get(); ordinary fields are plain values.
function vol(config, key) {
  const v = config?.[key]
  return v && typeof v === 'object' && typeof v.get === 'function' ? v.get() : v
}

function settings(config) {
  const root = config.root ?? process.env.HORON_ROOT ?? DEFAULT_ROOT
  // Non-empty config.python or HORON_PYTHON: the user's own Python, used exactly as before.
  const python = config.python || process.env.HORON_PYTHON || null
  return { root, python }
}

// ---- Managed Python ---------------------------------------------------------------------------
// With no python configured, the plugin prepares one itself, in the background, under
// HORON_HOME/runtime: find or download uv, let uv fetch its own Python 3.12 into a venv, sync the
// locked requirements. Nothing is written outside HORON_HOME (no PATH or shell edits).

const UV_VERSION = '0.13.8'
const PYTHON_VERSION = '3.12'
const STEPS = 3
const LOCK_FILE = 'requirements.lock.txt'
const HASH_FILE = '.horon-lock-hash' // inside the venv: sha256 of the lock file last synced
const STALE_LOCK_MS = 30 * 60_000
const IS_WIN = process.platform === 'win32'

const horonHome = () => process.env.HORON_HOME || path.join(os.homedir(), '.horon')
const runtimeDir = () => path.join(horonHome(), 'runtime')
const venvDir = () => path.join(runtimeDir(), 'venv')
const venvPython = () => (IS_WIN ? path.join(venvDir(), 'Scripts', 'python.exe') : path.join(venvDir(), 'bin', 'python'))
const uvName = IS_WIN ? 'uv.exe' : 'uv'

function uvAsset() {
  const arch = process.arch === 'x64' ? 'x86_64' : process.arch === 'arm64' ? 'aarch64' : null
  const target = {
    win32: arch && `${arch}-pc-windows-msvc`,
    linux: arch && `${arch}-unknown-linux-gnu`,
    darwin: arch && `${arch}-apple-darwin`,
  }[process.platform]
  if (!target) throw new Error(`no uv build for ${process.platform}/${process.arch}; set \`python\` (or HORON_PYTHON) instead`)
  return `uv-${target}.${IS_WIN ? 'zip' : 'tar.gz'}`
}

// Process-wide init state, read by the tool and the hooks.
const init = { state: 'idle', step: 0, label: '', startedAt: 0, error: '', python: '', promise: null }

function setStep(step, label) {
  init.step = step
  init.label = label
}

function lockHash(root) {
  return createHash('sha256').update(readFileSync(path.join(root, LOCK_FILE))).digest('hex')
}

function runtimeReady(root) {
  try {
    return existsSync(venvPython()) && readFileSync(path.join(venvDir(), HASH_FILE), 'utf8').trim() === lockHash(root)
  } catch {
    return false
  }
}

function findUv() {
  const own = path.join(runtimeDir(), 'bin', uvName)
  if (existsSync(own)) return own
  for (const dir of (process.env.PATH ?? '').split(path.delimiter)) {
    if (!dir) continue
    const candidate = path.join(dir, uvName)
    try {
      if (statSync(candidate).isFile()) return candidate
    } catch {}
  }
  return null
}

// Run a command to completion; reject with the original stderr/stdout tail on failure.
function runTool(cmd, args, { env, cwd } = {}) {
  return new Promise((resolve, reject) => {
    const child = spawn(cmd, args, { cwd, env: { ...process.env, ...env }, shell: false, windowsHide: true })
    const chunks = []
    child.stdout.on('data', (c) => chunks.push(c))
    child.stderr.on('data', (c) => chunks.push(c))
    child.on('error', (e) => reject(new Error(`${cmd}: ${e.message}`)))
    child.on('close', (code) => {
      const text = Buffer.concat(chunks).toString('utf8').trim()
      if (code === 0) resolve(text)
      else reject(new Error(`${path.basename(cmd)} ${args.slice(0, 2).join(' ')} exited with code ${code}\n${text.slice(-1500)}`))
    })
  })
}

function findFile(dir, name, depth) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isFile() && entry.name === name) return full
    if (entry.isDirectory() && depth > 0) {
      const hit = findFile(full, name, depth - 1)
      if (hit) return hit
    }
  }
  return null
}

async function downloadUv() {
  const asset = uvAsset()
  const url = `https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/${asset}`
  const tmp = path.join(runtimeDir(), 'tmp')
  await rm(tmp, { recursive: true, force: true })
  await mkdir(tmp, { recursive: true })
  const res = await fetch(url, { redirect: 'follow' })
  if (!res.ok) throw new Error(`downloading ${url} failed: HTTP ${res.status}`)
  const archive = path.join(tmp, asset)
  await writeFile(archive, Buffer.from(await res.arrayBuffer()))
  // The release publishes `<asset>.sha256` next to each archive; refuse an archive that does not match.
  const sumRes = await fetch(`${url}.sha256`, { redirect: 'follow' })
  if (!sumRes.ok) {
    await rm(tmp, { recursive: true, force: true })
    throw new Error(`uv 下载校验失败：无法获取 ${url}.sha256（HTTP ${sumRes.status}）`)
  }
  const expected = ((await sumRes.text()).trim().split(/\s+/)[0] ?? '').toLowerCase()
  const actual = createHash('sha256').update(await readFile(archive)).digest('hex')
  if (actual !== expected) {
    await rm(tmp, { recursive: true, force: true })
    throw new Error(`uv 下载校验失败：${asset} 的 sha256 不一致
期望 ${expected}
实际 ${actual}`)
  }
  const out = path.join(tmp, 'x')
  await mkdir(out, { recursive: true })
  // Windows: the bsdtar that ships with the system (a `tar` earlier on PATH may be GNU tar, which cannot read zip).
  const tar = IS_WIN ? path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'tar.exe') : 'tar'
  await runTool(tar, ['-xf', archive, '-C', out])
  const found = findFile(out, uvName, 2)
  if (!found) throw new Error(`${uvName} not found in ${asset}`)
  const bin = path.join(runtimeDir(), 'bin')
  await mkdir(bin, { recursive: true })
  const dest = path.join(bin, uvName)
  await rm(dest, { force: true })
  await rename(found, dest)
  await rm(tmp, { recursive: true, force: true })
  return dest
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms))

function pidAlive(pid) {
  try {
    process.kill(pid, 0)
    return true
  } catch (e) {
    return e.code === 'EPERM'
  }
}

// Cross-process mutex: an exclusive-create lock file under HORON_HOME/runtime. Returns a release function.
async function acquireLock() {
  const file = path.join(runtimeDir(), 'init.lock')
  await mkdir(runtimeDir(), { recursive: true })
  for (;;) {
    try {
      const fh = await open(file, 'wx')
      await fh.writeFile(String(process.pid))
      await fh.close()
      return () => rm(file, { force: true })
    } catch (e) {
      if (e.code !== 'EEXIST' && e.code !== 'EPERM' && e.code !== 'EBUSY') throw e
    }
    try {
      const holder = Number.parseInt(await readFile(file, 'utf8'), 10)
      const age = Date.now() - statSync(file).mtimeMs
      if (age > STALE_LOCK_MS || (Number.isInteger(holder) && !pidAlive(holder))) {
        await rm(file, { force: true })
        continue
      }
    } catch {}
    setStep(init.step || 1, '等待另一个 dsh 进程完成初始化')
    await sleep(2000)
  }
}

async function prepareRuntime(root) {
  const lockPath = path.join(root, LOCK_FILE)
  if (!existsSync(lockPath)) throw new Error(`${LOCK_FILE} not found in ${root}`)
  setStep(1, '准备 uv')
  const release = await acquireLock()
  try {
    if (runtimeReady(root)) return // another process finished while we waited
    const rt = runtimeDir()
    const uvEnv = {
      UV_PYTHON_INSTALL_DIR: path.join(rt, 'python'),
      UV_CACHE_DIR: path.join(rt, 'uv-cache'),
      UV_NO_CONFIG: '1',
    }
    let uv = findUv()
    if (!uv) {
      setStep(1, `下载 uv ${UV_VERSION}`)
      uv = await downloadUv()
    }
    const py = venvPython()
    if (!existsSync(py)) {
      setStep(2, `下载 Python ${PYTHON_VERSION} 并创建环境`)
      await runTool(uv, ['venv', venvDir(), '--python', PYTHON_VERSION, '--managed-python', '--clear'], { env: uvEnv })
    }
    setStep(3, '安装依赖')
    await runTool(uv, ['pip', 'sync', lockPath, '--python', py], { env: uvEnv })
    await writeFile(path.join(venvDir(), HASH_FILE), lockHash(root))
  } finally {
    await release()
  }
}

// Start (once per process) the background preparation of the managed Python. Never rejects.
function ensureRuntime(config, warn) {
  if (init.promise) return init.promise
  const { root } = settings(config)
  init.startedAt = Date.now()
  if (runtimeReady(root)) {
    init.state = 'ready'
    init.python = venvPython()
    init.promise = Promise.resolve()
    return init.promise
  }
  init.state = 'running'
  init.promise = prepareRuntime(root).then(
    () => {
      init.state = 'ready'
      init.python = venvPython()
    },
    (e) => {
      init.state = 'failed'
      init.error = String(e?.message ?? e)
      warn?.(`horon: Python setup failed at step ${init.step}/${STEPS}: ${init.error}`)
    },
  )
  return init.promise
}

// The Python to spawn, or a Chinese sentence saying why there is none yet.
function pythonFor(config) {
  const { python } = settings(config)
  if (python) return { python, managed: false }
  if (init.state === 'ready') return { python: init.python, managed: true }
  if (init.state === 'failed') return { notReady: `Horon 初始化失败（第 ${init.step}/${STEPS} 步）：${init.error}\n重启 dsh 会重试。` }
  const secs = Math.round((Date.now() - (init.startedAt || Date.now())) / 1000)
  return { notReady: `Horon 正在初始化（第 ${init.step || 1}/${STEPS} 步：${init.label || '准备'}，已用 ${secs} 秒），请稍后再试` }
}

// Environment for one spawn: the database, the two keys, the pycache location.
async function spawnEnv(ctx, config, root, managed) {
  const env = {}
  if (managed) env.PYTHONPYCACHEPREFIX = path.join(runtimeDir(), 'pycache')
  // Not a git checkout (an npm install): keep the cloud rule cache and its unsent audit rows out
  // of the package folder, which every plugin update replaces.
  if (!existsSync(path.join(root, '.git'))) env.HORON_LOCAL_DIR = path.join(horonHome(), 'local')
  const dataDir = String(vol(config, 'dataDir') ?? '').trim()
  if (dataDir) {
    // `~` is the home directory; a relative path is under HORON_HOME, never under root, which for
    // an npm install is the package folder that the next plugin update replaces.
    const expanded = dataDir === '~' || /^~[\\/]/.test(dataDir) ? path.join(os.homedir(), dataDir.slice(1)) : dataDir
    const dir = path.resolve(horonHome(), expanded)
    mkdirSync(dir, { recursive: true })
    env.HORON_DB = path.join(dir, 'horon.db')
  } else if (process.env.HORON_DB) {
    // inherited as is
  } else if (existsSync(path.join(root, '.env'))) {
    // Python reads HORON_DB from .env
  } else {
    mkdirSync(horonHome(), { recursive: true })
    env.HORON_DB = path.join(horonHome(), 'horon.db')
  }
  const credentials = ctx?.get?.('credentials')
  if (credentials) {
    for (const [field, fallback, target] of [
      ['openrouterKeyEnv', 'HORON_OPENROUTER_API_KEY', 'OPENROUTER_API_KEY'],
      ['remoteKeyEnv', 'HORON_REMOTE_KEY', 'HORON_REMOTE_KEY'],
    ]) {
      const ref = vol(config, field) || fallback
      try {
        const value = (await credentials.resolve(ref))?.value
        if (value) env[target] = value
      } catch {}
    }
  }
  return env
}

// Why the configured root is not a Horon checkout, or null when it is.
function rootProblem(config) {
  const { root } = settings(config)
  if (existsSync(path.join(root, CLI))) return null
  return (
    `Horon is not found at ${root} (no ${CLI} there). Set the Horon checkout directory as ` +
    `\`root\` in the horon row of the profile's cordis.patch.yml, or in the HORON_ROOT ` +
    `environment variable of the dsh process, then restart dsh.`
  )
}

// Run `python <script> <args>` in the Horon root, feeding `stdin`; resolve {code, out, err},
// or {notReady} (a sentence for the user) while the managed Python is still being prepared.
async function runPython(ctx, config, script, args, { stdin, env, signal, timeoutMs } = {}) {
  const { root } = settings(config)
  const target = pythonFor(config)
  if (target.notReady) return { notReady: target.notReady }
  const { python, managed } = target
  const spawnExtra = await spawnEnv(ctx, config, root, managed)
  return new Promise((resolve, reject) => {
    const child = spawn(python, [path.join(root, script), ...args], {
      cwd: root,
      env: { ...process.env, ...UTF8_ENV, ...spawnExtra, ...env },
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

// Sessions derived by dsh's subagent mechanism carry header.parentSession (set together with
// origin 'subagent' by dsh-subagent's childSessionMeta). With `subagents` off, such a session is
// muted: no reminder, no hooks, no horon tool. Decided once, when the agent is created, so a
// settings change applies to newly derived subagents only.
const isSubagent = (agent) => {
  const header = agent?.session?.header
  return Boolean(header?.parentSession) && header?.origin === 'subagent'
}

function registerSubagentGate(ctx, config, muted) {
  const restrictions = new Map() // sessionId -> disposer of the effect that hides the horon tool
  ctx.on('agent/created', ({ agent }) => {
    if (!isSubagent(agent) || vol(config, 'subagents') === true) return
    const sid = agent.id == null ? null : String(agent.id)
    if (!sid) return
    muted.add(sid)
    try {
      restrictions.set(sid, agent.ctx.effect(() => agent.ctx.tools.restrict({ deny: ['horon'] })))
    } catch (e) {
      ctx.logger?.warn?.(`horon: could not hide the horon tool from subagent ${sid}: ${e}`)
    }
  })
  ctx.on('agent/disposed', ({ agent }) => {
    const sid = agent?.id == null ? null : String(agent.id)
    if (!sid) return
    muted.delete(sid)
    const off = restrictions.get(sid)
    restrictions.delete(sid)
    try {
      off?.()
    } catch {}
  })
  // Unloading the plugin lifts every restriction it still holds.
  ctx.effect(() => () => {
    for (const off of restrictions.values()) {
      try {
        off()
      } catch {}
    }
    restrictions.clear()
    muted.clear()
  })
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
      const problem = rootProblem(config)
      if (problem) throw new Error(problem)
      // The CLI records reads and audit entries under the dsh session the call came from.
      const sessionId = exec?.agent?.id
      const { code, out, err, notReady } = await runPython(ctx, config, CLI, args.args, {
        env: sessionId ? { HORON_DSH_SESSION_ID: String(sessionId) } : {},
        signal: exec?.signal,
      })
      if (notReady) return notReady
      const text = out + err
      if (code !== 0) throw new Error(`horon exited with code ${code}\n${text}`)
      return text
    },
  })
}

function registerHooks(ctx, config, muted) {
  const log = (message) => ctx.logger?.warn?.(`horon: ${message}`)

  // `signal` is the hook's cancellation: stopping the turn kills the gateway child instead of waiting it out.
  const callGateway = async (event, payload, signal) => {
    if (signal?.aborted) return null
    try {
      const { code, out, err, notReady } = await runPython(ctx, config, 'run_gateway.py', [event, '--adapter', 'dsh'], {
        stdin: JSON.stringify(payload),
        timeoutMs: GATEWAY_TIMEOUT_MS,
        signal,
      })
      if (notReady || signal?.aborted) return null // Python not prepared yet: hooks skip
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
    if (!sid || muted.has(sid) || decision?.kind !== 'enter') return decision
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
    return { ...decision, messages: [...(decision.messages ?? []), bannerMessage(res.context, 'Horon Harness')] }
  })

  ctx.on('tools/pre-execute', async (exec, next) => {
    const sid = sidOf(exec?.agent)
    if (sid && !muted.has(sid)) {
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
    if (!sid || muted.has(sid)) return downstream
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
    if (!sid || muted.has(sid) || signal?.aborted) return
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

// Opening reminder: the first step of every session, and the first step after a compaction, gets
// one notice telling the model to run `horon login` and read the manual. It does not depend on the
// hooks switch, only on startupReminder (read on every step, so the settings page applies at once).
function registerReminder(ctx, config, muted) {
  const started = new Set() // sessions that already got their opening reminder
  const compacted = new Set() // sessions whose context was compacted or cleared since
  const sidOf = (agent) => (agent?.id == null ? null : String(agent.id))

  ctx.on('agent/created', ({ agent, source }) => {
    const sid = sidOf(agent)
    if (sid && (source === 'compact' || source === 'clear')) compacted.add(sid)
  })
  ctx.on('session/event', (session, event) => {
    const sid = session?.id == null ? null : String(session.id)
    if (sid && event?.type === 'compaction/end' && !event.data?.error) compacted.add(sid)
  })
  ctx.on('agent/disposed', ({ agent }) => {
    const sid = sidOf(agent)
    started.delete(sid)
    compacted.delete(sid)
  })
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    const decision = await next()
    const sid = sidOf(agent)
    if (!sid || muted.has(sid) || decision?.kind !== 'enter') return decision
    const due = !started.has(sid) || compacted.has(sid)
    if (!due) return decision
    started.add(sid)
    compacted.delete(sid)
    if (vol(config, 'startupReminder') === false) return decision
    const skill = path.join(settings(config).root, '.agents', 'skills', 'horon-cli', 'SKILL.md')
    const text =
      '【Horon】新会话开始（或上下文刚被压缩）。回复用户之前，必须先做两步，哪怕用户的问题很简单：' +
      '① 调用 horon 工具，args: ["login"]，读完输出；② 完整读一遍说明书：' +
      `${skill}。` +
      '之后读写记忆一律用 horon 工具：说明书里的 `python frontend/cli.py <命令>`，就是 horon 工具的 args = <命令> 后面那串，一项一个；' +
      '绝不要在终端里运行 python。'
    return { ...decision, messages: [...(decision.messages ?? []), bannerMessage(text, 'Horon 开局提醒')] }
  })
}

export function apply(ctx, config = {}) {
  // The row ships its own settings page (client.js), so Settings need not generate one.
  ctx.inject(['settings'], (c) => {
    c.effect(() => c.settings.configure({ auto: false }, ctx.fiber))
  })
  const muted = new Set() // subagent sessions that get no reminder, hooks or horon tool
  registerTool(ctx, config)
  registerSubagentGate(ctx, config, muted)
  registerReminder(ctx, config, muted)
  // Prepare the managed Python in the background; apply returns at once.
  if (!settings(config).python) void ensureRuntime(config, (m) => ctx.logger?.warn?.(m))
  // Without a checkout the tool still registers and explains the problem when called;
  // the hooks would only fail on every event, so they stay off until root is fixed.
  const problem = rootProblem(config)
  if (problem) {
    ctx.logger?.warn?.(`horon: hooks disabled. ${problem}`)
    return
  }
  const off = config.hooks === false || /^(0|off|false|no)$/i.test(process.env.HORON_HOOKS ?? '')
  if (!off) registerHooks(ctx, config, muted)
}
