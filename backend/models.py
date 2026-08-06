"""
Horon v2 data models (Pydantic)

Concept → Variation 分層結構：
  Concept:   概念の対外身份（名字），組合の参與単位。
  Variation: 同一概念の不同解釈（concept_id + short_code），
             每個 variation 有独立の status / content / compose_members。
  Disclosure: 概念の書腰（触発条件），帮助 agent 决定是否深入阅読。
              每個 concept 可以有多条 disclosure。
"""

from typing import Literal

from pydantic import BaseModel

Status = Literal["hypothesis", "confirmed", "negated"]
VariationType = Literal["CHAIN", "AND", "OR"]
MatchField = Literal["name", "alias", "disclosure", "variation"]


# ── 基础表映射 ──────────────────────────────────────────────

class Concept(BaseModel):
    id: int
    name: str
    disclosure: str | None = None
    created_at: str
    updated_at: str


class DisclosureDetail(BaseModel):
    """disclosures 表の一行，附帯 DB id 以便定向削除。"""
    id: int
    text: str
    created_at: str


class SearchMatch(BaseModel):
    """搜索命中的単一匹配項。"""
    field: MatchField
    target_id: str | None = None   # disclosure.id (str) / variation short_code; None for name/alias
    snippet: str


class ConceptSearchResult(BaseModel):
    """search_concepts の返回単位。"""
    concept_id: int
    concept_name: str
    matches: list[SearchMatch] = []


class Variation(BaseModel):
    concept_id: int
    short_code: str
    type: VariationType | None = None
    status: Status | None = None
    content: str | None = None
    # 价值通道：这条 variation 与现实碰撞后对我的利害。
    # NULL=从未审视，-1.0=harmful，0.0=neutral，+1.0=beneficial。
    # 写入只经 set valence 的符号标签，库里不存在手写数值。
    valence: float | None = None
    created_at: str
    updated_at: str


class ComposeMemberDetail(BaseModel):
    concept_id: int
    name: str
    order_index: int
    disclosures: list[DisclosureDetail] = []


class VariationDetail(Variation):
    """Variation + 其组合表达式。"""
    expression: str | None = None
    members: list[ComposeMemberDetail] = []



# ── 关系查询 ─────────────────────────────────────────────────

class RelationMember(BaseModel):
    concept_id: int
    concept_name: str
    disclosures: list[DisclosureDetail] = []


class DirectedRelation(BaseModel):
    """有向关系（inbound 或 outbound）。方向由其所在的列表上下文决定。"""
    expression: str               # "A → B"
    concept_id: int               # 关系概念的 ID
    concept_name: str             # 关系概念的名字
    # 价值投影：母链 variation 的 valence 随行带出（读取时继承，不落库到 hop）。
    # 站在节点上看出边时，这个字段就是"执行前的预感"。
    valence: float | None = None
    members: list[RelationMember]


# ── Attention routing ────────────────────────────────────────

class TransitionSuggestion(BaseModel):
    """concept_transitions 排名前 N の推薦跳転。"""
    concept_id: int
    concept_name: str
    weight: float


# ── read_concept 返回 ───────────────────────────────────────

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
    disclosures: list[DisclosureDetail] = []
    aliases: list[str] = []
    tags: list[str] = []
    tag_source_info: str | None = None
    reminders: list[ReminderDetail] = []
    variations: list[VariationDetail] = []
    suggested_next: list[TransitionSuggestion] = []
    inbound_confirmed: list[DirectedRelation] = []
    inbound_negated: list[DirectedRelation] = []
    inbound_hypotheses: list[DirectedRelation] = []
    outbound_confirmed: list[DirectedRelation] = []
    outbound_negated: list[DirectedRelation] = []
    outbound_hypotheses: list[DirectedRelation] = []
