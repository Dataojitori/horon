import { useState, useRef, useEffect, useCallback } from "react";
import { api } from "../api";
import type { ConceptSearchResult } from "../types";
import "./SearchBar.css";

interface Props {
  onSelect: (conceptId: number) => void;
}

interface SearchResult {
  id: number;
  name: string;
  snippet: string | null;
}

export default function SearchBar({ onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchResult[]>([]);
  const [open, setOpen] = useState(false);
  const [activeIdx, setActiveIdx] = useState(-1);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const wrapperRef = useRef<HTMLDivElement>(null);

  const searchRequestId = useRef(0);

  const doSearch = useCallback((q: string) => {
    const reqId = ++searchRequestId.current;
    if (q.length < 1) {
      setResults([]);
      setOpen(false);
      return;
    }
    api
      .searchConcepts(q)
      .then((res) => {
        if (reqId !== searchRequestId.current) return;
        const mapped = res.map((c: ConceptSearchResult) => ({
          id: c.concept_id,
          name: c.concept_name,
          snippet: c.matches && c.matches.length > 0 ? c.matches[0].snippet : null,
        }));
        setResults(mapped);
        setOpen(mapped.length > 0);
        setActiveIdx(-1);
      })
      .catch(() => {
        if (reqId !== searchRequestId.current) return;
        setResults([]);
        setOpen(false);
      });
  }, []);

  const handleChange = (e: React.ChangeEvent<HTMLInputElement>) => {
    const val = e.target.value;
    searchRequestId.current++;
    setQuery(val);
    if (timerRef.current) clearTimeout(timerRef.current);
    timerRef.current = setTimeout(() => doSearch(val), 200);
  };

  const handleSelect = (id: number) => {
    if (timerRef.current) {
      clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    searchRequestId.current++;
    setOpen(false);
    setQuery("");
    setResults([]);
    setActiveIdx(-1);
    onSelect(id);
  };

  const handleKeyDown = (e: React.KeyboardEvent) => {
    if (!open) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      setActiveIdx((i) => Math.min(i + 1, results.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setActiveIdx((i) => (i < 0 ? -1 : Math.max(i - 1, 0)));
    } else if (e.key === "Enter" && activeIdx >= 0) {
      e.preventDefault();
      handleSelect(results[activeIdx].id);
    } else if (e.key === "Escape") {
      setOpen(false);
    }
  };

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (
        wrapperRef.current &&
        !wrapperRef.current.contains(e.target as Node)
      ) {
        setOpen(false);
      }
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div ref={wrapperRef} className="search-wrapper">
      <input
        className="search-input"
        type="text"
        value={query}
        onChange={handleChange}
        onKeyDown={handleKeyDown}
        onFocus={() => results.length > 0 && setOpen(true)}
        placeholder="Search concepts..."
      />
      {open && (
        <div className="search-dropdown">
          {results.map((r, i) => (
            <div
              key={r.id}
              className={`search-item ${i === activeIdx ? "active" : ""}`}
              onMouseDown={() => handleSelect(r.id)}
              onMouseEnter={() => setActiveIdx(i)}
            >
              <span className="search-name">{r.name}</span>
              {r.snippet && (
                <span className="search-snippet">{r.snippet}</span>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
