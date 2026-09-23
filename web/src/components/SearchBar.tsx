import { useState, useRef, useEffect, useCallback } from "react";
import { api } from "../api";
import type { ConceptSearchResult, SearchMatch } from "../types";
import "./SearchBar.css";

interface Props {
  onSelect: (conceptId: number) => void;
}

interface ItemMatch {
  field: "name" | "alias" | "disclosure" | "content";
  snippet: string;
  focusedSnippet: string;
}

interface SearchItemData {
  id: number;
  name: string;
  bestMatch: ItemMatch | null;
}

const FIELD_INFO: Record<string, { label: string; badgeClass: string }> = {
  alias: { label: "别名", badgeClass: "badge-alias" },
  disclosure: { label: "说明", badgeClass: "badge-disclosure" },
  content: { label: "正文", badgeClass: "badge-content" },
  name: { label: "名称", badgeClass: "badge-name" },
};

function cleanSnippet(snippet: string): string {
  return snippet
    .replace(/[\r\n]+/g, " ")
    .replace(/^#+\s*/g, "")
    .replace(/(\*\*|__)(.*?)\1/g, "$2")
    .replace(/(\*|_)(.*?)\1/g, "$2")
    .replace(/`([^`]+)`/g, "$1")
    .replace(/\s+/g, " ")
    .trim();
}

function focusSnippet(
  snippet: string,
  query: string,
  prefixChars = 8,
  totalChars = 50
): string {
  const cleaned = cleanSnippet(snippet);
  const q = query.trim().toLowerCase();
  if (!q) return cleaned;

  const lower = cleaned.toLowerCase();
  const pos = lower.indexOf(q);

  if (pos === -1) {
    return cleaned.length > totalChars
      ? cleaned.slice(0, totalChars) + "..."
      : cleaned;
  }

  // 如果 query 在前面（pos <= prefixChars + 2），从头开始展示
  if (pos <= prefixChars + 2) {
    const end = Math.min(cleaned.length, pos + q.length + 35);
    return cleaned.slice(0, end) + (end < cleaned.length ? "..." : "");
  }

  // 否则从 pos - prefixChars 开始，保证关键词一定出现在视野最前部
  const start = pos - prefixChars;
  const end = Math.min(cleaned.length, pos + q.length + 30);
  let res = cleaned.slice(start, end);
  if (start > 0) res = "..." + res;
  if (end < cleaned.length) res = res + "...";
  return res;
}

function HighlightText({ text, query }: { text: string; query: string }) {
  if (!query || !query.trim()) {
    return <>{text}</>;
  }

  const escaped = query.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const regex = new RegExp(`(${escaped})`, "gi");
  const parts = text.split(regex);

  return (
    <>
      {parts.map((part, index) =>
        regex.test(part) ? (
          <mark key={index} className="search-highlight">
            {part}
          </mark>
        ) : (
          part
        )
      )}
    </>
  );
}

export default function SearchBar({ onSelect }: Props) {
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<SearchItemData[]>([]);
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
      .then((res: ConceptSearchResult[]) => {
        if (reqId !== searchRequestId.current) return;
        const mapped: SearchItemData[] = res.map((c) => {
          const id = c.concept_id ?? c.id ?? 0;
          const name = c.concept_name ?? c.name ?? "";

          let bestMatch: ItemMatch | null = null;
          if (c.matches && c.matches.length > 0) {
            // 优先选择非 name 字段的 match 作为补充 context
            const nonNameMatch = c.matches.find(
              (m: SearchMatch) => m.field !== "name" && m.snippet
            );
            const chosen = nonNameMatch || c.matches[0];
            if (chosen && chosen.snippet) {
              bestMatch = {
                field: chosen.field,
                snippet: cleanSnippet(chosen.snippet),
                focusedSnippet: focusSnippet(chosen.snippet, q),
              };
            }
          }

          return {
            id,
            name,
            bestMatch,
          };
        });

        setResults(mapped);
        setOpen(true);
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
    timerRef.current = setTimeout(() => doSearch(val), 150);
  };

  const handleSelect = (id: number) => {
    if (!id) return;
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
    } else if (e.key === "Enter") {
      e.preventDefault();
      const targetIdx =
        activeIdx >= 0 ? activeIdx : results.length > 0 ? 0 : -1;
      if (targetIdx >= 0 && results[targetIdx]) {
        handleSelect(results[targetIdx].id);
      }
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
        onFocus={() => {
          if (query.trim().length > 0) {
            setOpen(true);
          }
        }}
        placeholder="Search concepts, aliases, content..."
      />
      {open && (
        <div className="search-dropdown">
          {results.length === 0 ? (
            <div className="search-empty">未找到相关概念</div>
          ) : (
            results.map((r, i) => {
              const showSnippet =
                r.bestMatch &&
                (r.bestMatch.field !== "name" || r.bestMatch.snippet !== r.name);
              const fieldMeta = r.bestMatch
                ? FIELD_INFO[r.bestMatch.field]
                : null;

              return (
                <div
                  key={r.id || i}
                  className={`search-item ${i === activeIdx ? "active" : ""}`}
                  onMouseDown={() => handleSelect(r.id)}
                  onMouseEnter={() => setActiveIdx(i)}
                  title={r.bestMatch ? r.bestMatch.snippet : r.name}
                >
                  <div className="search-name">
                    <HighlightText text={r.name} query={query} />
                  </div>
                  {showSnippet && r.bestMatch && (
                    <div className="search-snippet">
                      {fieldMeta && (
                        <span className={`search-badge ${fieldMeta.badgeClass}`}>
                          {fieldMeta.label}
                        </span>
                      )}
                      <span className="search-snippet-text">
                        <HighlightText
                          text={r.bestMatch.focusedSnippet}
                          query={query}
                        />
                      </span>
                    </div>
                  )}
                </div>
              );
            })
          )}
        </div>
      )}
    </div>
  );
}
