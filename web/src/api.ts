import type { GraphData, ConceptDetail, NeighborhoodData, ConceptSearchResult, ConceptReviewItem, SessionInfo } from "./types";

const BASE = "/api";

async function fetchJSON<T>(url: string, options?: RequestInit): Promise<T> {
  const res = await fetch(url, options);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`API ${res.status}: ${detail}`);
  }
  return res.json();
}

export const api = {
  getSessions: () => fetchJSON<SessionInfo[]>(`${BASE}/sessions`),

  // 以下三个接口返回的激活状态按 session 这个会话显示
  getGraph: (session: string) =>
    fetchJSON<GraphData>(`${BASE}/graph?session_id=${encodeURIComponent(session)}`),

  getConcept: (id: number, session: string) =>
    fetchJSON<ConceptDetail>(`${BASE}/concepts/${id}?session_id=${encodeURIComponent(session)}`),

  getNeighborhood: (id: number, session: string) =>
    fetchJSON<NeighborhoodData>(`${BASE}/neighborhood/${id}?session_id=${encodeURIComponent(session)}`),

  searchConcepts: (q: string) =>
    fetchJSON<ConceptSearchResult[]>(`${BASE}/concepts/search?q=${encodeURIComponent(q)}`),

  getReviews: () =>
    fetchJSON<ConceptReviewItem[]>(`${BASE}/reviews`),

  approveReview: (conceptId: number) =>
    fetchJSON<{ message: string; concept_id: number }>(`${BASE}/reviews/${conceptId}/approve`, {
      method: "POST",
    }),

  rollbackReview: (conceptId: number) =>
    fetchJSON<{ message: string; concept_id: number }>(`${BASE}/reviews/${conceptId}/rollback`, {
      method: "POST",
    }),
};
