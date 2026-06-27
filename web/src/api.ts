import type { GraphData, ConceptDetail, NeighborhoodData } from "./types";

const BASE = "/api";

async function fetchJSON<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) {
    const detail = await res.text();
    throw new Error(`API ${res.status}: ${detail}`);
  }
  return res.json();
}

export const api = {
  getGraph: () => fetchJSON<GraphData>(`${BASE}/graph`),

  getConcept: (id: number) =>
    fetchJSON<ConceptDetail>(`${BASE}/concepts/${id}`),

  getNeighborhood: (id: number) =>
    fetchJSON<NeighborhoodData>(`${BASE}/neighborhood/${id}`),

  searchConcepts: (q: string) =>
    fetchJSON<ConceptDetail[]>(`${BASE}/concepts/search?q=${encodeURIComponent(q)}`),
};
