"""
Horon v3 Harness data models (Pydantic)

Core roles:
  plain:  Static knowledge / entity (is_active = 0)
  sensor: Perceptual input / external fact (lifespan in turn/session/permanent)
  logic:  Combinational & sequential logic (activation_type in CHAIN/AND/OR)
  guard:  Tool gateway gating valve (activation_type in CHAIN/AND/OR)
"""

from typing import Any, Literal

from pydantic import BaseModel

Role = Literal["plain", "sensor", "logic", "guard"]
Lifespan = Literal["turn", "session", "permanent"]
ActivationType = Literal["CHAIN", "AND", "OR"]
MatchField = Literal["name", "alias", "disclosure", "content"]


# ── 基础表映射 ──────────────────────────────────────────────

class Concept(BaseModel):
    id: int
    name: str
    content: str | None = None
    disclosure: str | None = None
    role: Role = "plain"
    is_active: int = 0
    lifespan: Lifespan | None = None
    activation_type: ActivationType | None = None
    on_fire: str | None = None
    created_at: str
    updated_at: str


class SearchMatch(BaseModel):
    """搜索命中的单一匹配项。"""
    field: MatchField
    target_id: str | None = None   # None for name/alias/disclosure/content
    snippet: str


class ConceptSearchResult(BaseModel):
    """search_concepts 的返回单位。"""
    concept_id: int
    concept_name: str
    matches: list[SearchMatch] = []


class ComposeMemberDetail(BaseModel):
    concept_id: int
    name: str
    order_index: int
    disclosure: str | None = None
    is_active: int = 0
    role: Role


class SensorHookDetail(BaseModel):
    id: int
    sensor_concept_id: int
    event_type: str
    tool: str | None = None
    match_pattern: str
    created_at: str


class ToolGuardDetail(BaseModel):
    id: int
    guard_concept_id: int
    tool: str
    args_pattern: str | None = None
    created_at: str


class InhibitionDetail(BaseModel):
    target_concept_id: int
    inhibitor_concept_id: int
    inhibitor_name: str | None = None
    target_name: str | None = None
    inhibitor_is_active: int = 0
    target_is_active: int = 0
    inhibitor_role: Role
    target_role: Role
    created_at: str


# ── Attention routing ────────────────────────────────────────

class TransitionSuggestion(BaseModel):
    """concept_transitions 排名前 N 的推荐跳转。"""
    concept_id: int
    concept_name: str
    weight: float
    disclosure: str | None = None


# ── Runtime Evaluation 返回 ───────────────────────────────────

class FiredAction(BaseModel):
    """发火动作详情。"""
    concept: str
    concept_id: int
    action: Any


class EvaluationResult(BaseModel):
    """求值引擎拓扑计算与时序推进的返回结构。"""
    active_changed: dict[str, int] = {}
    fired_actions: list[FiredAction] = []


# ── Mutation & Read 返回 ─────────────────────────────────────

class MutationResult(BaseModel):
    """DB write operation result with audit-relevant metadata."""
    message: str
    concept_id: int | None = None
    concept_name: str | None = None
    fired_actions: list[FiredAction] = []


class ReminderDetail(BaseModel):
    """read_concept 时附带的 reminder 摘要。"""
    id: int
    condition: str
    message: str
    created_at: str
    last_fired_at: str | None = None


class ReadResult(BaseModel):
    """read_concept 的完整返回。"""
    id: int
    name: str
    content: str | None = None
    disclosure: str | None = None
    role: Role = "plain"
    is_active: int = 0
    lifespan: Lifespan | None = None
    activation_type: ActivationType | None = None
    activation_rule: str | None = None
    on_fire: str | None = None
    aliases: list[str] = []
    tags: list[str] = []
    tag_source_info: str | None = None
    reminders: list[ReminderDetail] = []
    members: list[ComposeMemberDetail] = []
    sensor_hooks: list[SensorHookDetail] = []
    tool_guards: list[ToolGuardDetail] = []
    inhibitions: list[InhibitionDetail] = []
    inhibiting: list[InhibitionDetail] = []
    active_chain_orders: list[int] = []
    suggested_next: list[TransitionSuggestion] = []


# ── Backward Solver / Compile 诊断返回 ─────────────────────────

CompileStatus = Literal["active", "inhibited", "unmet_prerequisites"]


class ChainProgress(BaseModel):
    """CHAIN 节点按序推进状态。"""
    current_step: int
    total_steps: int
    waiting_for: str


class CompileResult(BaseModel):
    """compile 逆推诊断结果。"""
    target: str
    status: CompileStatus
    active_inhibitors: list[str] = []
    missing_prerequisites: list[str] = []
    chain_progress: ChainProgress | None = None
    diagnostic_tree: list[str] = []


# ── Snapshot & Review 模型 ─────────────────────────────────────

SnapshotField = Literal["content", "disclosure"]


class SnapshotChange(BaseModel):
    """单一字段的改动快照。"""
    field: SnapshotField
    original_value: str | None = None
    current_value: str | None = None
    created_at: str


class ConceptReviewItem(BaseModel):
    """按概念归总的人工审核项目。"""
    concept_id: int
    concept_name: str
    role: str | None = None
    is_deleted: bool = False
    is_creation: bool = False
    changes: list[SnapshotChange] = []
    created_at: str

