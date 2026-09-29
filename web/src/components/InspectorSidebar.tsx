import React, { useState, useRef, useCallback, useMemo } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { formatBytes, type ConceptDetail } from "../types";
import "./InspectorSidebar.css";

interface Props {
  concept: ConceptDetail | null;
  open: boolean;
  onClose: () => void;
  onNavigate: (id: number) => void;
}

// 侧栏宽度可拖：默认 380px，记 localStorage，下次打开还在。
// 上限取 min(1200, 视口-240)，保证左边星图永远留一块可点。
const INSPECTOR_MIN_W = 300;
const INSPECTOR_MAX_W = 1200;
const INSPECTOR_DEFAULT_W = 380;
const INSPECTOR_WIDTH_KEY = "horon:inspector-width";

function maxInspectorWidth(): number {
  return Math.max(
    INSPECTOR_MIN_W,
    Math.min(INSPECTOR_MAX_W, window.innerWidth - 240)
  );
}

function loadInspectorWidth(): number {
  try {
    const v = Number(localStorage.getItem(INSPECTOR_WIDTH_KEY));
    if (Number.isFinite(v)) {
      return Math.min(INSPECTOR_MAX_W, Math.max(INSPECTOR_MIN_W, v));
    }
  } catch {
    /* 无痕/禁存储时回默认宽度 */
  }
  return INSPECTOR_DEFAULT_W;
}

// 库里行首列表按规范写成 `\- `（防 markdown 误解析），直接渲染会露出反斜杠。
// 显示前还原成正常列表，只动显示、不动存量。限定行首 `\- `，正文中间的不碰。
function toDisplayMarkdown(src: string): string {
  return src.replace(/^\\- /gm, "- ");
}

export default function InspectorSidebar({ concept, open, onClose, onNavigate }: Props) {
  const [width, setWidth] = useState(loadInspectorWidth);
  const widthRef = useRef(width);
  const draggingRef = useRef(false);

  const displayContent = useMemo(
    () => toDisplayMarkdown(concept?.content ?? ""),
    [concept?.id, concept?.content]
  );

  const onDragMove = useCallback((e: MouseEvent) => {
    if (!draggingRef.current) return;
    const next = Math.min(
      maxInspectorWidth(),
      Math.max(INSPECTOR_MIN_W, window.innerWidth - e.clientX)
    );
    widthRef.current = next;
    setWidth(next);
  }, []);

  const stopDrag = useCallback(() => {
    if (!draggingRef.current) return;
    draggingRef.current = false;
    document.body.style.cursor = "";
    window.removeEventListener("mousemove", onDragMove);
    window.removeEventListener("mouseup", stopDrag);
    try {
      localStorage.setItem(INSPECTOR_WIDTH_KEY, String(widthRef.current));
    } catch {
      /* 存不住就算了，下次回默认 */
    }
  }, [onDragMove]);

  const startDrag = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      draggingRef.current = true;
      document.body.style.cursor = "ew-resize";
      window.addEventListener("mousemove", onDragMove);
      window.addEventListener("mouseup", stopDrag);
    },
    [onDragMove, stopDrag]
  );

  const resetWidth = useCallback(() => {
    widthRef.current = INSPECTOR_DEFAULT_W;
    setWidth(INSPECTOR_DEFAULT_W);
    try {
      localStorage.removeItem(INSPECTOR_WIDTH_KEY);
    } catch {
      /* 忽略 */
    }
  }, []);

  if (!concept) return null;

  const altAliases = concept.aliases?.filter((a) => a !== concept.name) ?? [];

  return (
    <div className={`inspector ${open ? "open" : ""}`} style={{ width }}>
      <div
        className="inspector-resize-handle"
        onMouseDown={startDrag}
        onDoubleClick={resetWidth}
        title="拖拽调整宽度，双击恢复默认"
      />
      <div className="inspector-header">
        <div className="inspector-title-group">
          <h2 className="inspector-title">{concept.name}</h2>
          <span className="concept-id">#{concept.id}</span>
        </div>
        <button className="inspector-close" onClick={onClose} aria-label="Close">
          &times;
        </button>
      </div>

      <div className="inspector-body">
        {/* ── Harness Meta Row ── */}
        <div className="inspector-meta-row">
          <span className={`role-badge role-${concept.role}`}>
            {concept.role}
          </span>
          <span className={`active-badge ${concept.is_active ? "is-active" : "is-inactive"}`}>
            {concept.is_active ? "Active" : "Inactive"}
          </span>
          {concept.lifespan && (
            <span className="meta-badge lifespan-badge">
              {concept.lifespan}
            </span>
          )}
          {concept.activation_type && (
            <span className="meta-badge activation-type-badge">
              {concept.activation_type}
            </span>
          )}
          {concept.byte_size !== undefined && (
            <span
              className={`meta-badge size-badge ${concept.byte_size >= 4800 ? "size-warning" : ""}`}
              title={
                concept.byte_size >= 4800
                  ? `正文大小: ${(concept.byte_size / 1024).toFixed(1)} KB (超过 4.8 KB 推荐阈值)`
                  : `正文大小: ${formatBytes(concept.byte_size)}`
              }
            >
              {concept.byte_size >= 4800 ? `⚠ ${formatBytes(concept.byte_size)}` : formatBytes(concept.byte_size)}
            </span>
          )}
        </div>

        {/* ── Aliases ── */}
        {altAliases.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Aliases</h3>
            <div className="alias-list">
              {altAliases.map((a) => (
                <span key={a} className="alias-tag">
                  {a}
                </span>
              ))}
            </div>
          </div>
        )}

        {/* ── Disclosure ── */}
        {concept.disclosure && (
          <div className="inspector-section">
            <h3 className="section-label">Disclosure</h3>
            <div className="field-content inspector-disclosure">
              {concept.disclosure}
            </div>
          </div>
        )}

        {/* ── Tags ── */}
        {concept.tags && concept.tags.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Tags</h3>
            <div className="alias-list">
              {concept.tags.map((t) => (
                <span key={t} className="tag-badge">
                  #{t}
                </span>
              ))}
            </div>
            {concept.tag_source_info && (
              <div className="tag-source-info">{concept.tag_source_info}</div>
            )}
          </div>
        )}

        {/* ── Activation & Composition (Unified Section) ── */}
        {((concept.members && concept.members.length > 0) || concept.activation_rule) && (
          <div className="inspector-section">
            <div className="section-header-row">
              <h3 className="section-label">Activation Rule & Composition</h3>
              {concept.activation_type && (
                <span className={`mode-badge mode-${concept.activation_type.toLowerCase()}`}>
                  {concept.activation_type === "CHAIN" && "➔ CHAIN"}
                  {concept.activation_type === "AND" && "⯌ AND (ALL)"}
                  {concept.activation_type === "OR" && "⯎ OR (ANY)"}
                </span>
              )}
            </div>

            {concept.members && concept.members.length > 0 ? (
              <div className="member-flow-list">
                {concept.members
                  .slice()
                  .sort((a, b) => a.order_index - b.order_index)
                  .map((m, idx, arr) => (
                    <div key={m.concept_id} className="member-flow-wrapper">
                      <button
                        type="button"
                        className="member-flow-card"
                        title={m.disclosure || undefined}
                        onClick={() => onNavigate(m.concept_id)}
                      >
                        <span className="member-order">{m.order_index}.</span>
                        <div className="member-info">
                          <span className="member-name">{m.name}</span>
                          {m.disclosure && (
                            <span className="member-disclosure-hint">{m.disclosure}</span>
                          )}
                        </div>
                        <span className="member-id">#{m.concept_id}</span>
                        <span className="member-nav-arrow" aria-hidden="true">→</span>
                      </button>

                      {idx < arr.length - 1 && (
                        <div className="member-connector">
                          <div className="connector-line" />
                          <span className={`connector-operator op-${(concept.activation_type || "and").toLowerCase()}`}>
                            {concept.activation_type === "CHAIN"
                              ? "THEN (↓)"
                              : concept.activation_type === "OR"
                              ? "OR (|)"
                              : "AND (&)"}
                          </span>
                          <div className="connector-line" />
                        </div>
                      )}
                    </div>
                  ))}
              </div>
            ) : (
              concept.activation_rule && (
                <div className="activation-rule-box">
                  <code>{concept.activation_rule}</code>
                </div>
              )
            )}
          </div>
        )}

        {/* ── On Fire Action ── */}
        {concept.on_fire && (
          <div className="inspector-section">
            <h3 className="section-label">On Fire</h3>
            <div className="on-fire-box">
              <code>{concept.on_fire}</code>
            </div>
          </div>
        )}

        {/* ── Sensor Hooks ── */}
        {concept.sensor_hooks && concept.sensor_hooks.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Sensor Hooks</h3>
            <div className="hook-list">
              {concept.sensor_hooks.map((h) => (
                <div key={h.id} className="hook-card">
                  <div className="hook-event">
                    <span className="event-type">{h.event_type}</span>
                    {h.tool && <span className="hook-tool">{h.tool}</span>}
                  </div>
                  <div className="hook-pattern">
                    <span className="field-label">Pattern</span>
                    <code>{h.match_pattern}</code>
                  </div>
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Tool Guards ── */}
        {concept.tool_guards && concept.tool_guards.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Tool Guards</h3>
            <div className="hook-list">
              {concept.tool_guards.map((g) => (
                <div key={g.id} className="hook-card">
                  <div className="hook-event">
                    <span className="guard-tool">{g.tool}</span>
                  </div>
                  {g.args_pattern && (
                    <div className="hook-pattern">
                      <span className="field-label">Args Pattern</span>
                      <code>{g.args_pattern}</code>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Inhibitions ── */}
        {((concept.inhibitions && concept.inhibitions.length > 0) ||
          (concept.inhibiting && concept.inhibiting.length > 0)) && (
          <div className="inspector-section">
            <h3 className="section-label">Inhibitions</h3>
            {concept.inhibitions && concept.inhibitions.length > 0 && (
              <div className="inhibition-group">
                <span className="field-label">Inhibited By</span>
                <div className="inhibition-list">
                  {concept.inhibitions.map((inh) => (
                    <button
                      key={inh.inhibitor_concept_id}
                      type="button"
                      className="inhibition-tag clickable"
                      onClick={() => onNavigate(inh.inhibitor_concept_id)}
                    >
                      {inh.inhibitor_name ?? `#${inh.inhibitor_concept_id}`}
                    </button>
                  ))}
                </div>
              </div>
            )}
            {concept.inhibiting && concept.inhibiting.length > 0 && (
              <div className="inhibition-group">
                <span className="field-label">Inhibits</span>
                <div className="inhibition-list">
                  {concept.inhibiting.map((inh) => (
                    <button
                      key={inh.target_concept_id}
                      type="button"
                      className="inhibition-tag clickable"
                      onClick={() => onNavigate(inh.target_concept_id)}
                    >
                      {inh.target_name ?? `#${inh.target_concept_id}`}
                    </button>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── Content：始终按 Markdown 渲染 ── */}
        {concept.content && (
          <div className="inspector-section">
            <h3 className="section-label">Content</h3>
            <div className="field-content content-content md-content">
              <ReactMarkdown remarkPlugins={[remarkGfm]}>
                {displayContent}
              </ReactMarkdown>
            </div>
          </div>
        )}

        {/* ── Reminders ── */}
        {concept.reminders && concept.reminders.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Reminders</h3>
            <div className="reminder-list">
              {concept.reminders.map((r) => (
                <div key={r.id} className="reminder-card">
                  <div className="reminder-msg">#{r.id}: {r.message}</div>
                  <div className="reminder-condition">
                    <span className="field-label">When</span>
                    <code>{r.condition}</code>
                  </div>
                  {r.last_fired_at && (
                    <div className="reminder-fired">
                      Last fired: {r.last_fired_at}
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── Suggested Next (Attention Routing) ── */}
        {concept.suggested_next && concept.suggested_next.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Suggested Next Transitions</h3>
            <div className="suggestion-list">
              {concept.suggested_next.map((s) => (
                <button
                  key={s.concept_id}
                  type="button"
                  className="suggestion-item"
                  title={s.disclosure || undefined}
                  onClick={() => onNavigate(s.concept_id)}
                >
                  <span className="suggestion-name">{s.concept_name}</span>
                  <span className="suggestion-weight" title="转移权重分数（非概率）">
                    权重 {s.weight.toFixed(2)}
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
