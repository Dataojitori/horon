<h1 align="center"><img src="docs/pv/banner/horon-banner.svg" alt="Horon: self-improving harness" width="75%"></h1>

[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License](https://img.shields.io/badge/license-Apache--2.0%20%2B%20attribution-blue)](LICENSE)

**Executable memory for personal AI agents.**

> **让 AI 把吃过的亏，搭成会拦住自己的红石电路。**

<table>
<tr>
<td width="50%"><img src="docs/pv/snail/readme/gif1_note.webp" alt="蜗牛写下纸条贴在壳上，一阵风就把纸条吹走了" width="400"></td>
<td width="50%"><img src="docs/pv/snail/readme/gif2_chain.webp" alt="蜗牛又从叶子上踩空，却触发了自己搭的弹簧、纸杯和纸轨道，一路被送上向日葵看日出" width="400"></td>
</tr>
<tr>
<td>AI 记下了教训，下次照样犯。</td>
<td>给 AI 一套红石：让它自己学会拼装记忆、思路和刹车。</td>
</tr>
</table>

**[给自己的 AI 安装](#安装)** · **[在线试试记忆](#在线试试记忆)**

---

## 提醒一次，以后每次都管用

你提醒过 AI：「给供应商小林回信前，先看以前的沟通记录。」

光记下来，它下次照样忘。用 Horon，让它自己把规矩接成电路：

1. **小林来信** → 自动提醒 AI 先读沟通记录
2. **没读就想发信** → 发送工具当场拦住，告诉它还差哪一步
3. **收件人、附件、套话都查过** → 才通电放行

![发送邮件的电路](docs/images/mail-guard.jpg)

**不用你自己搭。** 把要求告诉 AI，它自己照着说明书（skill）把电路接好；下次有新规矩，它继续往上接。

## 它能给你的 AI 搭什么

- **自适应记忆**：记下你的工作、日常与聊过的事，并学习 AI 实际翻阅记忆的路径。读过 A 之后常要用 B，下次它读到 A，B 就更容易被递到眼前。
- **自动化电路**：把「看到 X 就提醒自己」「做完 A 才能做 B」连成会自动触发的电路——不论是当场抓包 AI「嘴上答应记下了其实没写库」，还是把旅行筹备、长期计划连成跨对话的阶段条件。
- **物理刹车**：前置条件不满足时，在工具执行前直接卡死拦截，并告诉 AI 还差哪一步。
- **可复用模式（Tag 插件）**：像工业模具一样，把一类事情的正确做法写成 Tag 插件，新任务挂上标签，AI 就自动受这套做法约束。

![个人助手的记忆全景](docs/images/personal-assistant-map.jpg)

## 在线试试记忆

**[和大家一起养的 Kip 聊聊 →](https://aisoulkeeper.com/kip)** · **[看看它的记忆界面 →](https://aisoulkeeper.com/kip/memories/)**

把公开示例 AI「Kip」接进 ChatGPT 或 Claude，教它一件事，换个新对话看它还记不记得。（ChatGPT / Claude 网页端不支持挂载本地钩子，只能读写记忆；要使用自动提醒和工具拦截电路，需在下方支持的本地客户端中安装 Horon。Kip 的记忆全网公开，请勿输入隐私。）

## 安装

把这句话发给你的 AI，让它照着说明帮你安装：

```text
请帮我安装 Horon：阅读 https://github.com/Dataojitori/horon/blob/master/docs/INSTALL.md 并照做。
```

支持 **Claude Code**、**Codex**、**Antigravity**、**OpenCode**、**DeepSeek Harness**。

## 更多

- [horon-cli](.agents/skills/horon-cli/SKILL.md) · [horon-circuit](.agents/skills/horon-circuit/SKILL.md) · [horon-plugin](.agents/skills/horon-plugin/SKILL.md)：给 AI 看的说明书
- **本地可视化控制台**：启动方法见 [INSTALL.md](docs/INSTALL.md#可选可视化控制台)
- **协议**：Apache-2.0（面向外部用户的产品或服务需署名，详见 [LICENSE](LICENSE)）
