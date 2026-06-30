"""
Horon v2 data models (Pydantic)

Concept → Variation 分层结构：
  Concept:   概念的对外身份（名字 + disclosure），组合的参与单位。
  Variation: 同一概念的不同解释（concept_id + short_code），
             每个 variation 有独立的 status / evidence / unless / compose_members。
"""

from typing import Literal

from pydantic import BaseModel

Status = Literal["hypothesis", "confirmed", "negated"]


# ── 基础表映射 ──────────────────────────────────────────────

class Concept(BaseModel):
    id: int
    name: str
    disclosure: str | None = None
    created_at: str
    updated_at: str


class Variation(BaseModel):
    concept_id: int
    short_code: str
    status: Status | None = None
    evidence: str | None = None
    unless: str | None = None
    created_at: str
    updated_at: str


class ComposeMemberDetail(BaseModel):
    concept_id: int
    name: str
    position: int
    disclosure: str | None = None


class VariationDetail(Variation):
    """Variation + 其组合表达式。"""
    expression: str | None = None
    members: list[ComposeMemberDetail] = []



# ── 关系查询 ─────────────────────────────────────────────────

class RelationMember(BaseModel):
    concept_id: int
    concept_name: str
    disclosure: str | None = None


class DirectedRelation(BaseModel):
    """有向关系（inbound 或 outbound）。方向由其所在的列表上下文决定。"""
    expression: str               # "A → B"
    concept_id: int               # 关系概念的 ID
    concept_name: str             # 关系概念的名字
    members: list[RelationMember] # 关系另一端的概念列表（同一 position 并列的成员）


# ── read_concept 返回 ───────────────────────────────────────

class MutationResult(BaseModel):
    """DB write operation result with audit-relevant metadata."""
    message: str
    concept_id: int | None = None
    concept_name: str | None = None
    short_code: str | None = None


class ReadResult(BaseModel):
    """read_concept 的完整返回。"""
    id: int
    name: str
    disclosure: str | None = None
    aliases: list[str] = []
    variations: list[VariationDetail] = []
    inbound_confirmed: list[DirectedRelation] = []
    inbound_negated: list[DirectedRelation] = []
    inbound_hypotheses: list[DirectedRelation] = []
    outbound_confirmed: list[DirectedRelation] = []
    outbound_negated: list[DirectedRelation] = []
    outbound_hypotheses: list[DirectedRelation] = []
    alerts: list[str] = []


# 注：compile 的输出不在这里建模。
# 数据层（上面这些）映射数据库表结构，形状稳定，模型是真契约；
# 推理层的输出是一份只构造一次、无人复用的侦察报告——直接用 dict，
# 返回形状由 README 的“编译报告”章节定义。
