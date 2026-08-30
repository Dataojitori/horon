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
    created_at: str


# ── Attention routing ────────────────────────────────────────

class TransitionSuggestion(BaseModel):
    """concept_transitions 排名前 N 的推荐跳转。"""
    concept_id: int
    concept_name: str
    weight: float


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
    suggested_next: list[TransitionSuggestion] = []

