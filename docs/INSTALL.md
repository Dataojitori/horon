# Horon 安装说明（写给 AI）

你好。你的用户想让你装上 Horon：一个你自己的长期记忆，加上一套由你自己搭建、在工具调用层面生效的规则电路。请按下面的步骤操作。遇到需要用户做决定的地方就停下来问，不要替用户猜。

## 0. 先问清楚

- 你是哪种 Agent：Claude Code、Codex、Antigravity、OpenCode，还是 DeepSeek Harness（dsh）？是 dsh 的话（系统提示词开头写着 powered by DeepSeek Harness，或你的工具列表里有 `plugin_manager`），直接跳到下面「DeepSeek Harness（dsh）」一节，只做那一节。
- 用户想把 Horon 放在哪个目录（默认 `~/horon`）？
- 用户有没有 OpenRouter API key？没有也能装，只是 `search_semantic`（按场景语义检索）不可用。
- 钩子想全局生效（所有项目都用 Horon），还是只在某一个项目里生效？

## DeepSeek Harness（dsh）

Horon 作为 dsh 插件发布在 npm 上，插件自己准备 Python 环境。dsh 用户不用下载代码、不用装 Python、不用改配置文件，第 0 节剩下的三个问题也不用问。（用户说他已经下载了 Horon 仓库、想用自己的 Python 的话，按第 1 节末尾的「dsh 用户用自己的仓库」做，不做本节。）

1. 装插件：用你的 `plugin_manager` 工具的 `install_bundle` 安装包名 `horon-harness`，装进你当前所在的 profile。标准模式里有这个工具；你没有它的话（比如在极简模式），请用户在 dsh 的「插件」页点「添加插件」，输入 `horon-harness`，装完点「立即启用」（直接关掉对话框的话插件装上了但是关着的）。不要在终端里跑 `npm`、`pnpm` 或 `dsh plugin`：你的终端被沙箱限制在工作目录里。
2. 安装报找不到 pnpm 时（用 npm 装的 dsh 命令行版和网页版需要 pnpm，桌面版自带）：请用户在他自己电脑的终端里运行 `npm i -g pnpm`，再重启 dsh，回到这个会话让你重做第 1 步。
3. 装好后，用一条消息把剩下的事一次交代给用户，因为他重启以后可能不会回到这个会话：
   - 重启 dsh。第一次启动时，插件会在后台下载 uv、Python 3.12 和依赖，放在 `~/.horon/runtime`，需要联网，通常半分钟以内。记忆库默认是 `~/.horon/horon.db`。
   - 验证：开一个新会话，随便发一句话（比如「你好」）。AI 回复之前会先调用 `horon` 工具执行 login，对话里能看到这次工具调用、并且它读到了 Horon 的说明书，就说明装好了。插件还在初始化时，这次调用会返回当前进度，等一会儿再发一句。
   - 设置（都可选，改了不用重启）：「插件」页 → Horon → 配置。数据目录；OpenRouter API key（不填则按场景语义检索不可用）；AISoulKeeper 云端 key（填了就用云端记忆，不填用本地库）；新会话提醒（默认开，关掉后 AI 开局不会自动读记忆）；子代理是否接入 Horon（默认关，子代理只干交代的活、不碰记忆）。key 存在 dsh 的凭据库里，你写不进去；用户在对话里把 key 发给你的话，也请他自己去配置页填。

装好以后，dsh 里的 AI 每个新会话都会收到插件的提醒，告诉它先 login、读说明书、用 `horon` 工具读写记忆，不用你另外写进任何说明文件。

Windows 和 Linux 已实测，macOS 尚未实测。

## 1. 获取代码与依赖

需要 Python 3.10+，依赖列在 `requirements.txt` 里。装进哪个 Python 环境随用户习惯：系统的、venv、conda 都行。用户平时怎么管 Python 环境、要不要单独隔开一个，拿不准就问。

```bash
git clone https://github.com/Dataojitori/horon.git <HORON_DIR>
cd <HORON_DIR>
<PYTHON> -m pip install -r requirements.txt
cp .env.example .env    # 有 key 就填进 OPENROUTER_API_KEY
```

下文的 `<PYTHON>` 指装了这些依赖的那个 Python 解释器的**绝对路径**，`<HORON_DIR>` 指仓库的**绝对路径**。钩子是由你的宿主程序在别的目录里调用的，相对路径会失效。

验证：

```bash
<PYTHON> <HORON_DIR>/frontend/cli.py login
```

能打印出说明书路径和「启动节点」一段就说明装好了。第一次运行会自动建空库 `horon.db`。

**dsh 用户用自己的仓库**：做完上面的步骤后，请用户在他自己的终端里运行 `dsh plugin --profile <profile> add <HORON_DIR>`（`<profile>` 是他平时用的 profile，一般是 `web`，都在 `~/.dsh/profiles/` 下；已经装过 npm 版 `horon-harness` 的，先在「插件」页卸载它），并把用户级环境变量 `HORON_PYTHON` 设成 `<PYTHON>`，然后重启 dsh。这样插件直接用这份仓库和这个 Python，`git pull` 后重启 dsh 生效。第 2、3 节不用做，验证照「DeepSeek Harness（dsh）」一节第 3 步。

## 2. 接上钩子

钩子让 Horon 能看到你收到的消息、你的回复和工具调用，也让守卫能在工具执行前拦下它。用下面这条命令生成你这种 Agent 的配置（把 `<FUNC>` 和 `<ADAPTER>` 换成表里对应的值）：

```bash
cd <HORON_DIR>
<PYTHON> -c "import json,sys; sys.path.insert(0,'.'); from backend.harness import config_templates as t; print(json.dumps(t.<FUNC>(r'\"<PYTHON>\" \"<HORON_DIR>/run_gateway.py\" --adapter <ADAPTER>'), indent=2, ensure_ascii=False))"
```

| Agent | `<FUNC>` | `<ADAPTER>` | 写到哪里 |
| :--- | :--- | :--- | :--- |
| Claude Code | `get_claude_code_hooks_config` | `claude-code` | `~/.claude/settings.json`（全局）或 `<项目>/.claude/settings.json` 的 `hooks` 字段 |
| Codex | `get_codex_hooks_config` | `codex` | `~/.codex/hooks.json`（全局）或 `<项目>/.codex/hooks.json`。钩子写好后要经用户信任才会生效：命令行版 Codex 首次运行时会弹出确认；ChatGPT 桌面版不会自动弹出，需要提醒用户去「设置」的钩子页面里手动信任 Horon 的钩子。写在项目里的 `.codex/hooks.json`，要这个项目本身已被信任才会加载；以后重新生成配置，钩子内容一变就要重新信任 |
| Antigravity | `get_antigravity_hooks_config` | `antigravity` | `~/.gemini/config/hooks.json`（全局，App/IDE 与 CLI 都读）或 `<项目>/.agents/hooks.json`（项目需被信任）。命令在 hooks.json 所在目录下执行，所以要写绝对路径；全局和项目两份不要同时留，否则在该项目里每个事件会触发两次 |

**合并，不要覆盖**：用户原有的钩子要保留，把 Horon 的条目加进去。改之前先把原文件给用户看一眼。如果你不确定自己这个版本的配置文件在哪，请查阅你自己的官方文档，不要猜。

### OpenCode：装插件

OpenCode 不读上面的钩子配置，而是加载 JS 插件，插件随仓库提供，跳过上面那条生成命令。

**OpenCode**：

1. 新建 `~/.config/opencode/plugin/horon-harness.js`，内容只有一行（路径用正斜杠）：

   ```js
   export { HoronHarness } from "<HORON_DIR>/backend/harness/opencode-plugin.js"
   ```

2. 插件默认用 `PATH` 里的 `python` 跑 Horon。`<PYTHON>` 不是 `PATH` 里那个的话，请用户把用户级环境变量 `HORON_PYTHON` 设成 `<PYTHON>`，否则钩子找不到依赖会报错。
3. 重启 OpenCode。开着 `opencode serve` 或网页版的，也要一起重启。

## 3. 装上说明书（skill）

`<HORON_DIR>/.agents/skills/horon-cli/` 是 Horon 的日常使用说明书（`SKILL.md`），`horon-circuit/` 讲的是怎么搭传感器、电路与工具守卫，`horon-plugin/` 讲的是怎么写 Tag 插件。把它们复制或链接到你的 skill 目录（例如 Claude Code 的 `~/.claude/skills/`）。不确定目录在哪，就查你自己的文档。

然后在你的全局说明文件（`CLAUDE.md`、`AGENTS.md` 或同类文件）里加两行：

```text
我的长期记忆是 Horon，位于 <HORON_DIR>，命令入口是 <PYTHON> <HORON_DIR>/frontend/cli.py。
每次新会话开始时，先运行 login 并读完输出，再读 horon-cli 说明书（login 只给路径，不输出正文）。
```

## 4. 验证

请用户重启 Agent，开一个新会话。会话开头应当能看到 `【Horon Harness】` 开头的广播（OpenCode 和 dsh 里，它会跟着用户的第一条消息出现）。看到了，就说明钩子接通了。

然后向用户报告装好了，并问一句：最近有没有哪条规矩，是用户反复提醒你、你却还是会忘的？有的话，你可以当场照着 `horon-circuit` 说明书把它搭成你的第一条电路。

## 可选：可视化控制台

```bash
<PYTHON> -m uvicorn backend.server:app --port 8710   # 后端
cd <HORON_DIR>/web && npm install && npm run dev     # 前端
```
