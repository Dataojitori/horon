export interface GraphNode {
  id: number;
  name: string;
  disclosure: string | null;
  degree: number;
  x?: number;
  y?: number;
  vx?: number;
  vy?: number;
}

export interface GraphLink {
  source: number | GraphNode;
  target: number | GraphNode;
  relation_id: number;
  status: string | null;
  kind: "directed" | "undirected";
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

export interface ComposeMemberDetail {
  concept_id: number;
  name: string;
  position: number;
  disclosure: string | null;
}

export interface VariationDetail {
  concept_id: number;
  short_code: string;
  status: string | null;
  evidence: string | null;
  unless: string | null;
  expression: string | null;
  created_at: string;
  updated_at: string;
  members?: ComposeMemberDetail[];
}

export interface RelationRow {
  expression: string;
  concept_id: number;
  concept_name: string;
  from_concept_id: number;
  from_concept_disclosure: string | null;
}

export interface OutboundRelation {
  expression: string;
  concept_id: number;
  concept_name: string;
  target_concept_id: number;
  target_concept_disclosure: string | null;
}

export interface ConceptDetail {
  id: number;
  name: string;
  disclosure: string | null;
  aliases: string[];
  variations: VariationDetail[];
  inbound_confirmed: RelationRow[];
  inbound_negated: RelationRow[];
  inbound_hypotheses: RelationRow[];
  outbound_confirmed: OutboundRelation[];
  outbound_negated: OutboundRelation[];
  outbound_hypotheses: OutboundRelation[];
  alerts: string[];
}

export interface NeighborNode {
  id: number;
  name: string;
  disclosure: string | null;
  degree: number;
}

export interface InternalLink {
  source: number;
  target: number;
  variation_code: string;
  status: string | null;
  kind: "directed" | "undirected";
  relation_id?: number;
  relation_name?: string;
}

export interface NeighborhoodData {
  focal: ConceptDetail;
  neighbors: NeighborNode[];
  internal_links: InternalLink[];
}

export type ViewMode = "galaxy" | "dissection";
