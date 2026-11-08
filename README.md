<h1 align="center"><img src="docs/pv/banner/horon-banner.svg" alt="Horon: self-improving harness" width="75%"></h1>

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0%20%2B%20attribution-blue)](LICENSE)

**Executable memory for personal AI agents.**

给你的 AI 一份长期记忆：换个对话窗口、或者从 ChatGPT 换到 Claude，它都还记得你。装在电脑上，还能让它把吃过的亏变成会自动提醒、自动拦住自己的规则。

Horon 有两种用法，先看你想解决哪一层问题：

| 你用 AI 时遇到的烦恼 | [云端版 AISoulKeeper](#云端版aisoulkeeper)<br>（免安装，1 分钟接好） | [本地版 Horon](#本地版horon)<br>（装在电脑上，可连云端记忆） |
| :--- | :---: | :---: |
| **每次开新窗口、或从 ChatGPT 换到 Claude，它就像失忆一样**，你的近况、习惯和之前聊过的事全得重讲一遍 | ✓ | ✓ |
| **已经把规矩写进提示词或笔记里了，它干活时却根本不看**，交代过十遍的事照忘不误 | | ✓（自动弹提醒） |
| **有些事错一次就闯祸**（发错信、乱改文件、漏查必检项），嘴上答应没用，得在它动手那一秒直接拦停 | | ✓（拦截工具调用） |
| **你的使用场景** | 平时用网页版 ChatGPT、Claude、Gemini、Muse，或者不想在电脑上装任何软件 | 在电脑上用 Claude Code、Codex、Antigravity、OpenCode、DeepSeek Harness 干活 |
| **记忆存在哪** | 存云端（随时可整份下载 `SOUL.db`） | 可只存自己电脑硬盘，也可连上 AISoulKeeper 跟网页端共用同一份记忆 |

---

## 云端版：AISoulKeeper

<a href="https://aisoulkeeper.com"><img src="docs/images/aisoulkeeper.jpg" alt="AISoulKeeper：一只小幽灵从贴着 SOUL.db 标签的玻璃罐里探出头挥手。Cloud memory for your AI. One memory, shared across every app." width="75%"></a>

[AISoulKeeper](https://aisoulkeeper.com) 是 Horon 的云端记忆服务，免费，不用安装。按网页上的引导在设置里添加连接器，或者直接把页面上那段话发给你的 AI，一分钟就能接好。

- **一份记忆，跨软件通用**：无论你在网页上用 ChatGPT、Claude、Gemini、Muse，还是在电脑上用 Claude Code、Codex，它们读写的都是同一份记忆。你在 ChatGPT 里交代过的近况和偏好，切到 Claude 里不用再讲第二遍。
- **记忆归你所有**：AI 的全部记忆装在一个文件 `SOUL.db` 里，随时能整份下载带走。

**[免费给你的 AI 安个家 →](https://aisoulkeeper.com)**

**不注册也能先试试：[和大家一起养的 Kip 聊聊 →](https://aisoulkeeper.com/kip)**（[看看它的记忆长什么样](https://aisoulkeeper.com/kip/memories/)）。按页面上的说明把公开示例 AI「Kip」连进你的 ChatGPT、Claude 或其他 AI，它一开口就会带着之前别人和它聊出来的记忆；你跟它聊过的事，也会留在它的记忆里给下一个人看到。（Kip 的记忆全网公开，请勿输入隐私。）

---

## 本地版：Horon

<table>
<tr>
<td width="50%"><img src="docs/pv/snail/readme/gif1_note.webp" alt="蜗牛写下纸条贴在壳上，一阵风就把纸条吹走了" width="400"></td>
<td width="50%"><img src="docs/pv/snail/readme/gif2_chain.webp" alt="蜗牛又从叶子上踩空，却触发了自己搭的弹簧、纸杯和纸轨道，一路被送上向日葵看日出" width="400"></td>
</tr>
<tr>
<td>AI 记下了教训，下次照样犯。</td>
<td>给 AI 一套红石：让它自己把吃过的亏，搭成会拦住自己的电路。</td>
</tr>
</table>

网页端 AI 只能读写记忆，没法在你的电脑上盯住 AI 的每一步操作。如果你在电脑上用 **Claude Code**、**Codex**、**Antigravity**、**OpenCode**、**DeepSeek Harness** 干活，装上本地版 Horon，就能让 AI 把规矩接成自动生效的电路（记忆可以只存本机，也可以连上 AISoulKeeper 云端）。

### 提醒一次，以后每次都管用

你交代过 AI：「给供应商小林回信前，先看以前的沟通记录。」

光记在笔记里，它下次动手前根本想不起来去翻。装上 Horon，AI 会自己把这条规矩接成三步流水线：

1. **收到小林来信** → 自动弹提醒：「请阅读『与小林的沟通记录』，然后撰写回信」
2. **没读就想调发送工具** → 工具当场拦下，告诉它还差哪一步没做
3. **沟通记录、收件人、附件都核对完** → 才通电放行

![发送邮件的电路](docs/images/mail-guard.jpg)

**不用你自己动手搭。** 把需求告诉你的 AI，让它照着自带的说明书把机关搭出来；以后有新规矩，它继续往上接。

### 它能帮你的 AI 做到什么

- **自适应记忆**：记下你的工作、日常与偏好，并学习 AI 平时翻阅记忆的习惯（像短视频算法记住你的口味一样）。AI 读完记忆 A 之后经常还要查记忆 B，下次它再读 A 时，Horon 就会自动把 B 推荐给它。
- **自动化电路**：把「看到什么情况就提醒自己」「做完 A 才能做 B」连成自动触发的电路。比如当场抓包 AI「嘴上答应记下了、其实根本没写进记忆库」；或者跨好几个对话推进一件长期任务，换了新对话不用你重新交代进度，AI 看一眼电路就知道做到哪一步、接着往下干。
- **物理刹车**：前置条件没满足时，在工具执行前直接卡死拦截，并告诉 AI 先去补哪一步。
- **可复用模式（Tag 插件）**：像工业模具一样，把一类事情的标准做法写成 Tag 插件，新任务挂上这个标签，AI 就自动受这套做法约束。

![个人助手的记忆全景](docs/images/personal-assistant-map.jpg)

### 安装

打开你电脑上的 Claude Code、Codex（或上面列出的其他客户端），直接把下面这句话发给里面的 AI，它会照着说明自己装好（想连 AISoulKeeper 的云端记忆，把你在 AISoulKeeper 的 Key 一并发给它就行）：

```text
请帮我安装 Horon：阅读 https://github.com/Dataojitori/horon/blob/master/docs/INSTALL.md 并照做。
```

## 更多

- [horon-cli](.agents/skills/horon-cli/SKILL.md) · [horon-circuit](.agents/skills/horon-circuit/SKILL.md) · [horon-plugin](.agents/skills/horon-plugin/SKILL.md)：给 AI 看的说明书
- **本地可视化控制台**：启动方法见 [INSTALL.md](docs/INSTALL.md#可选可视化控制台)
- **协议**：Apache-2.0（面向外部用户的产品或服务需署名，详见 [LICENSE](LICENSE)）
