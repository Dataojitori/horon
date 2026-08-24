"""
Horon v3 Harness data models (Pydantic)

Core roles:
  plain:  Static knowledge / entity (is_active = 0)
  sensor: Perceptual input / external fact (lifespan in turn/session/permanent)
  logic:  Combinational & sequential logic (activation_type in CHAIN/AND/OR)
  guard:  Tool gateway gating valve (activation_type in CHAIN/AND/OR)
"""

from typing import Literal

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
    role: Role = "plain"
    is_active: int = 0
    lifespan: Lifespan | None = None
    activation_type: ActivationType | None = None
    on_fire: str | None = None
    created_at: str
    updated_at: str


class DisclosureDetail(BaseModel):
    """disclosures 表的一行，附带 DB id 以便定向删除。"""
    id: int
    text: str
    created_at: str


class SearchMatch(BaseModel):
    """搜索命中的单一匹配项。"""
    field: MatchField
    target_id: str | None = None   # disclosure.id (str) / None for name/alias/content
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
    disclosures: list[DisclosureDetail] = []


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


# ── Mutation & Read 返回 ─────────────────────────────────────

class MutationResult(BaseModel):
    """DB write operation result with audit-relevant metadata."""
    message: str
    concept_id: int | None = None
    concept_name: str | None = None
    short_code: str | None = None


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
    role: Role = "plain"
    is_active: int = 0
    lifespan: Lifespan | None = None
    activation_type: ActivationType | None = None
    activation_rule: str | None = None
    on_fire: str | None = None
    disclosures: list[DisclosureDetail] = []
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
