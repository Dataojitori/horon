# Horon

> **Unified Dynamic Memory & Execution Harness for Intelligent Agents**  
> **推理即记忆，记忆即推理**：将自适应的联想记忆网络与确定性的运行时门禁 Harness 融为一体。

[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.100+-green.svg)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18+-61dafb.svg)](https://reactjs.org/)

---

## 💡 为什么需要 Horon？

在构建复杂且需要长期自主演化的 AI Agent 时，开发者通常面临两个极端的困境：

1. **记忆系统是死板的向量检索库**：传统 Memory（如单纯的向量检索）只是静态文本切片匹配，缺乏**关联跃迁与自适应思维路径**——AI 无法随着任务重心的转移自动学习“下一步该联想到什么”。
2. **执行控制依赖脆弱的 Prompt 规训**：在 Prompt 里写“没有做步骤 A 严禁调用工具 B”，在长上下文或复杂任务中极易发生注意力稀释与幻觉跳步；而硬编码的静态 Workflow 图又过于僵化，无法与动态记忆协同。

**Horon 将“长效联想记忆”与“运行时确定性 Harness”统一在同一个概念图谱（Concept Graph）中**：
- **在记忆层（Memory）**：通过转移学习（Transition Learning）动态捕捉 AI 的注意力偏好与思维轨迹；通过 Tag 集群与生命周期钩子实现结构化认知归类与治理；
- **在执行层（Harness）**：通过 DAG 逻辑拓扑与运行时工具门禁（Tool Guards），从网关层物理拦截未授权的工具调用，并将缺失的前置差集精准反压回 AI 的注意力中。

---

## 🧠 核心特性

### 一、 动态联想记忆网络 (Dynamic Memory Network)

* **自适应注意力路由 (Adaptive Thought Routing)**：  
  概念节点之间的转移权重（`concept_transitions`）随 AI 实际探索和读取轨迹动态学习与衰减。**当 AI 的任务重心和兴趣发生改变时，系统推荐的下一步思维路径（`[ SUGGESTED NEXT ]`）会自动演化**，实现真正的认知流动。
* **多维 Tag 集群与认知治理 (Tag Clustering & Governance)**：  
  Tag 不仅是分类标签，更是认知集合与工程项目容器。每个 Tag 由源节点统领，支持挂载插件生命周期、结构约束、变动监听钩子（`on_mutation`）以及离线集群审计（`audit_cluster`），防止记忆系统发生无序膨胀。
* **书腰与意图语义检索 (Disclosure & Intent Recall)**：  
  每个概念具备精确的“书腰”（Disclosure，说明在何种场景下需要唤起）。AI 可以通过自然语言描述当前困惑或情境（`intent`），在不污染上下文的前提下精准打捞深层经验。

---

### 二、 确定性运行时 Harness (Deterministic Execution Harness)

* **事件驱动感觉神经 (Sensors)**：  
  支持 `turn`（单回合瞬态脉冲）、`session`（会话累积事实）、`permanent`（长期客观环境）三级生命周期的传感器，通过 Hook 自动监听外部事件与工具调用并点亮事实。
* **拓扑中继与时序状态机 (Logic & Chains)**：  
  支持 `AND`（并列前置）、`OR`（分支容灾）以及跨回合锁存步进进度的时序状态机（`CHAIN`：`A → B → C`）。
* **声明式负向抑制网络 (Declarative Inhibitions)**：  
  支持 `A ─⊣ B` 形式的一票否决机制。抑制解除后瞬间恢复通路，无需回滚历史状态。
* **工具物理拦截与逆推反压 (Physical Tool Guards & Compile)**：  
  1:1 挂载高危工具。条件不满足时在网关物理拦截调用（`Permission Denied`），并通过 `compile` 自动逆推依赖树，告知 AI “还差哪一步”，精准修正注意力。

---

## 🏛️ 系统全景架构

```text
[ 外部事件 / 用户交互 / 工具调用 ]
                 ↓ (Sensor Hooks)
┌─────────────────────────────────────────────────────────────┐
│                       HORON GRAPH                           │
│                                                             │
│   [ 记忆与知识 (Plain Concepts) ] ──(转移学习/兴趣演化)──┐   │
│   [ Tag 集群分类与生命周期插件 ]                          │   │
│                 ↕                                       │   │
│   [ 感觉传入神经 (Sensors) ]                            │   │
│                 ↓                                       │   │
│   [ 逻辑中继与时序锁存 (AND / OR / CHAIN) ]              ▼   │
│                 ↓                         [ 动态注意力推荐 ] │
│   [ 负向抑制门控 (Inhibitions) ]            (Suggested Next)│
│                 ↓                                           │
│   [ 效应器放行守卫 (Tool Guards) ]                           │
└────────────────┬────────────────────────────────────────────┘
                 ↓
      [ 工具物理网关拦截 / 放行 ]  ──(拦截时)──→ [ 逆推阻塞差集回传 AI ]
```

---

## 🚀 快速上手 (Quick Start)

### 1. 安装与依赖
```bash
git clone https://github.com/Dataojitori/horon.git
cd horon
pip install -r requirements.txt
```

### 2. 记忆沉淀与 Tag 归类
```bash
# 创建概念并添加书腰（Disclosure）
python frontend/cli.py create_concept "API限流退避规范" \
  --disclosure "当遇到外部API返回429或网络抖动时打开" \
  --content "采用指数退避重试，初始等待1s，最大重试3次。"

# 建立项目 Tag 并纳入成员
python frontend/cli.py create_concept "网络稳定性工程" --content "提升外部服务容错能力"
python frontend/cli.py create_tag "网络稳定性工程"
python frontend/cli.py add "API限流退避规范" tag "网络稳定性工程"

# 意图模糊检索
python frontend/cli.py intent "接口报错频次太高被封了怎么办"
```

### 3. 构建物理执行门禁 (Harness Circuit)
```bash
# 1. 定义前置事实与工具守卫
python frontend/cli.py create_concept "测试已通过" --role sensor --lifespan session
python frontend/cli.py create_concept "放行生产发布" --role guard --activation-rule "测试已通过"
python frontend/cli.py set "放行生产发布" tool_guard deploy_prod

# 2. 此时调用 deploy_prod 会被物理拦截，并返回缺失差集
python frontend/cli.py compile --target "放行生产发布"
# 输出: [✗ 未就绪: 缺少前置条件 (测试已通过)]

# 3. 外部事件满足或测试通过后，守卫自动放行
python frontend/cli.py set "测试已通过" active 1
python frontend/cli.py compile --target "放行生产发布"
# 输出: [✓ 正常通电放行]
```

---

## 🌌 可视化控制台 (Web UI)

Horon 内置基于 React + Vite 的可视化控制台：
- **Galaxy View（宏观力导向星图）**：全景浏览概念群落、Tag 集群分布、实时通电状态与转移学习权重。
- **Dissection View（微观解剖台）**：直观调试电路依赖、抑制源链路与时序进度。

```bash
# 启动后端 API 服务
python -m uvicorn backend.server:app --port 8710 --reload

# 启动前端控制台
cd web && npm run dev
```
