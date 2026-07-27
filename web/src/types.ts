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
  kind: "directed" | "undirected" | "or";
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

export type VariationType = "CHAIN" | "AND" | "OR";

export interface ComposeMemberDetail {
  concept_id: number;
  name: string;
  order_index: number;
  disclosure: string | null;
}

export interface VariationDetail {
  concept_id: number;
  short_code: string;
  type: VariationType | null;
  status: string | null;
  content: string | null;
  expression: string | null;
  created_at: string;
  updated_at: string;
  members?: ComposeMemberDetail[];
}

export interface RelationMember {
  concept_id: number;
  concept_name: string;
  disclosure: string | null;
}

export interface DirectedRelation {
  expression: string;
  concept_id: number;
  concept_name: string;
  members: RelationMember[];
}

export interface ConceptDetail {
  id: number;
  name: string;
  disclosure: string | null;
  aliases: string[];
  variations: VariationDetail[];
  inbound_confirmed: DirectedRelation[];
  inbound_negated: DirectedRelation[];
  inbound_hypotheses: DirectedRelation[];
  outbound_confirmed: DirectedRelation[];
  outbound_negated: DirectedRelation[];
  outbound_hypotheses: DirectedRelation[];
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
