export type Role = "plain" | "sensor" | "logic" | "guard";
export type Lifespan = "turn" | "session" | "permanent";
export type ActivationType = "CHAIN" | "AND" | "OR";

export interface GraphNode {
  id: number;
  name: string;
  role?: Role;
  is_active?: number;
  lifespan?: Lifespan | null;
  activation_type?: ActivationType | null;
  disclosure?: string | null;
  degree: number;
  tags?: string[];
  byte_size?: number;
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
  kind: "directed" | "undirected" | "or" | "inhibition";
}

export interface GraphData {
  nodes: GraphNode[];
  links: GraphLink[];
}

export interface ComposeMemberDetail {
  concept_id: number;
  name: string;
  order_index: number;
  disclosure: string | null;
}

export interface SensorHookDetail {
  id: number;
  sensor_concept_id: number;
  event_type: string;
  tool: string | null;
  match_pattern: string;
  created_at: string;
}

export interface ToolGuardDetail {
  id: number;
  guard_concept_id: number;
  tool: string;
  args_pattern: string | null;
  created_at: string;
}

export interface InhibitionDetail {
  target_concept_id: number;
  inhibitor_concept_id: number;
  inhibitor_name?: string | null;
  target_name?: string | null;
  created_at: string;
}

export interface TransitionSuggestion {
  concept_id: number;
  concept_name: string;
  weight: number;
  disclosure?: string | null;
}

export interface ReminderDetail {
  id: number;
  condition: string;
  message: string;
  created_at: string;
  last_fired_at: string | null;
}

export interface ConceptDetail {
  id: number;
  name: string;
  content: string | null;
  role: Role;
  is_active: number;
  lifespan: Lifespan | null;
  activation_type: ActivationType | null;
  activation_rule: string | null;
  on_fire: string | null;
  disclosure: string | null;
  byte_size?: number;
  aliases: string[];
  tags: string[];
  tag_source_info: string | null;
  reminders?: ReminderDetail[];
  members: ComposeMemberDetail[];
  sensor_hooks: SensorHookDetail[];
  tool_guards: ToolGuardDetail[];
  inhibitions: InhibitionDetail[];
  inhibiting: InhibitionDetail[];
  suggested_next?: TransitionSuggestion[];
}

export interface NeighborNode {
  id: number;
  name: string;
  disclosure: string | null;
  degree: number;
  byte_size?: number;
}

export function formatBytes(bytes?: number | null): string {
  if (bytes === undefined || bytes === null || bytes <= 0) return "0 B";
  if (bytes < 1024) return `${bytes} B`;
  return `${(bytes / 1024).toFixed(1)} KB`;
}

export interface InternalLink {
  source: number;
  target: number;
  variation_code: string;
  status: string | null;
  kind: "directed" | "undirected" | "inhibition";
  relation_id?: number;
  relation_name?: string;
}

export interface NeighborhoodData {
  focal: ConceptDetail;
  neighbors: NeighborNode[];
  internal_links: InternalLink[];
}

// ── Search results ──────────────────────────────────────

export interface SearchMatch {
  field: "name" | "alias" | "disclosure" | "content";
  target_id: string | null;
  snippet: string;
}

export interface ConceptSearchResult {
  id?: number;
  concept_id: number;
  name?: string;
  concept_name: string;
  matches: SearchMatch[];
}

export type SnapshotField = "content" | "disclosure";

export interface SnapshotChange {
  field: SnapshotField;
  original_value: string | null;
  current_value: string | null;
  created_at: string;
}

export interface ConceptReviewItem {
  concept_id: number;
  concept_name: string;
  role: Role | null;
  is_deleted: boolean;
  is_creation: boolean;
  changes: SnapshotChange[];
  created_at: string;
}

export type ViewMode = "galaxy" | "dissection" | "review";
