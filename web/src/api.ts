import type { GraphData, ConceptDetail, NeighborhoodData, ConceptSearchResult, ConceptReviewItem, SessionInfo } from "./types";

const BASE = "/api";

// 当前库别名：由 App 顶栏的库选择器设置，各接口自动带上。
// 未设置（null）时不带 ?db=，走服务端缺省——老前端、不传参的调用行为不变。
let currentDb: string | null = null;

export function setDb(alias: string | null) {
  currentDb = alias && alias.trim() ? alias.trim() : null;
}

export function getDb(): string | null {
  return currentDb;
}

function withDb(url: string): string {
  if (!currentDb) return url;
  return url + (url.includes("?") ? "&" : "?") + `db=${encodeURIComponent(currentDb)}`;
}

export interface DatabaseInfo {
  alias: string;
  default: boolean;
}

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`API ${res.status}: ${detail}`);
  }
  return res.json();
}

export const api = {
  listDatabases: () => fetchJSON<DatabaseInfo[]>(`${BASE}/databases`),

  getSessions: () => fetchJSON<SessionInfo[]>(withDb(`${BASE}/sessions`)),

  // 以下三个接口返回的激活状态按 session 这个会话显示
  getGraph: (session: string) =>
    fetchJSON<GraphData>(withDb(`${BASE}/graph?session_id=${encodeURIComponent(session)}`)),

  getConcept: (id: number, session: string) =>
    fetchJSON<ConceptDetail>(withDb(`${BASE}/concepts/${id}?session_id=${encodeURIComponent(session)}`)),

  getNeighborhood: (id: number, session: string) =>
    fetchJSON<NeighborhoodData>(withDb(`${BASE}/neighborhood/${id}?session_id=${encodeURIComponent(session)}`)),

  searchConcepts: (q: string) =>
    fetchJSON<ConceptSearchResult[]>(withDb(`${BASE}/concepts/search?q=${encodeURIComponent(q)}`)),

  getReviews: () =>
    fetchJSON<ConceptReviewItem[]>(withDb(`${BASE}/reviews`)),

  approveReview: (conceptId: number) =>
    fetchJSON<{ message: string; concept_id: number }>(withDb(`${BASE}/reviews/${conceptId}/approve`), {
      method: "POST",
    }),

  rollbackReview: (conceptId: number) =>
    fetchJSON<{ message: string; concept_id: number }>(withDb(`${BASE}/reviews/${conceptId}/rollback`), {
      method: "POST",
    }),
};
