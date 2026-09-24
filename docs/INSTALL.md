# Horon 安装说明（写给 AI）

你好。你的用户想让你装上 Horon：一个你自己的长期记忆，加上一套由你自己搭建、在工具调用层面生效的规则电路。请按下面的步骤操作。遇到需要用户做决定的地方就停下来问，不要替用户猜。

## 0. 先问清楚

- 你是哪种 Agent：Claude Code、Codex，还是 Antigravity？
- 用户想把 Horon 放在哪个目录（默认 `~/horon`）？
- 用户有没有 OpenRouter API key？没有也能装，只是 `intent`（按场景语义检索）不可用。
- 钩子想全局生效（所有项目都用 Horon），还是只在某一个项目里生效？

## 1. 获取代码与依赖

需要 Python 3.10+。

```bash
git clone https://github.com/Dataojitori/horon.git <HORON_DIR>
cd <HORON_DIR>
python -m venv .venv
# Windows: .venv\Scripts\python   macOS/Linux: .venv/bin/python
<PYTHON> -m pip install -r requirements.txt
cp .env.example .env    # 有 key 就填进 OPENROUTER_API_KEY
```

下文的 `<PYTHON>` 一律指 venv 里解释器的**绝对路径**，`<HORON_DIR>` 指仓库的**绝对路径**。钩子是由你的宿主程序在别的目录里调用的，相对路径会失效。

验证：

```bash
<PYTHON> <HORON_DIR>/frontend/cli.py login
```

能打印出「Horon 使用说明书」就说明装好了。第一次运行会自动建空库 `horon.db`。

## 2. 接上钩子

钩子让 Horon 能看到你收到的消息、你的回复和工具调用，也让守卫能在工具执行前拦下它。用下面这条命令生成你这种 Agent 的配置（把 `<FUNC>` 和 `<ADAPTER>` 换成表里对应的值）：

```bash
cd <HORON_DIR>
<PYTHON> -c "import json,sys; sys.path.insert(0,'.'); from backend.harness import config_templates as t; print(json.dumps(t.<FUNC>(r'\"<PYTHON>\" \"<HORON_DIR>/run_gateway.py\" --adapter <ADAPTER>'), indent=2, ensure_ascii=False))"
```

| Agent | `<FUNC>` | `<ADAPTER>` | 写到哪里 |
| :--- | :--- | :--- | :--- |
| Claude Code | `get_claude_code_hooks_config` | `claude-code` | `~/.claude/settings.json`（全局）或 `<项目>/.claude/settings.json` 的 `hooks` 字段 |
| Codex | `get_codex_hooks_config` | `codex` | `~/.codex/hooks.json`（全局）或 `<项目>/.codex/hooks.json`；首次运行时 Codex 会要求用户确认信任这些钩子 |
| Antigravity | `get_antigravity_hooks_config` | `antigravity` | Antigravity 读取的 hooks 配置（项目内为 `.agents/hooks.json`） |

**合并，不要覆盖**：用户原有的钩子要保留，把 Horon 的条目加进去。改之前先把原文件给用户看一眼。如果你不确定自己这个版本的配置文件在哪，请查阅你自己的官方文档，不要猜。

## 3. 装上说明书（skill）

`<HORON_DIR>/.agents/skills/horon-cli/` 是 Horon 的完整使用说明书（`SKILL.md`），`horon-plugin/` 讲的是怎么写 Tag 插件。把它们复制或链接到你的 skill 目录（例如 Claude Code 的 `~/.claude/skills/`）。不确定目录在哪，就查你自己的文档。

然后在你的全局说明文件（`CLAUDE.md`、`AGENTS.md` 或同类文件）里加两行：

```text
我的长期记忆是 Horon，位于 <HORON_DIR>，命令入口是 <PYTHON> <HORON_DIR>/frontend/cli.py。
每次新会话开始时，先运行 login 并读完输出。
```

## 4. 验证

请用户重启 Agent，开一个新会话。会话开头应当能看到 `【Horon Harness】` 开头的广播。看到了，就说明钩子接通了。

然后向用户报告装好了，并问一句：最近有没有哪条规矩，是用户反复提醒你、你却还是会忘的？有的话，你可以当场把它搭成你的第一条电路，做法见 `SKILL.md` 第 3～5 节。

## 可选：可视化控制台

```bash
<PYTHON> -m uvicorn backend.server:app --port 8710   # 后端
cd <HORON_DIR>/web && npm install && npm run dev     # 前端
```
