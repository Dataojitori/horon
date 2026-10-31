import React, { useState, useRef, useCallback } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import remarkBreaks from "remark-breaks";
import { formatBytes, OVERSIZE_BYTES, type ConceptDetail } from "../types";
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
    // 没存过时 getItem 返回 null，Number(null) 是 0，会被钳成最小宽度，所以先判空。
    const raw = localStorage.getItem(INSPECTOR_WIDTH_KEY);
    const v = raw == null ? NaN : Number(raw);
    if (Number.isFinite(v)) {
      return Math.min(maxInspectorWidth(), Math.max(INSPECTOR_MIN_W, v));
    }
  } catch {
    /* 无痕/禁存储时回默认宽度 */
  }
  return INSPECTOR_DEFAULT_W;
}

export default function InspectorSidebar({ concept, open, onClose, onNavigate }: Props) {
  const [width, setWidth] = useState(loadInspectorWidth);
  const widthRef = useRef(width);
  const draggingRef = useRef(false);

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
  const isCircuit = concept.role !== "plain";
  const tooLong = (concept.byte_size ?? 0) >= OVERSIZE_BYTES;
  const fireActions = parseOnFire(concept.on_fire);
  const maxWeight = Math.max(1e-6, ...(concept.suggested_next ?? []).map((s) => s.weight));
  const op = OPERATOR_WORD[concept.activation_type ?? "AND"];

  return (
    <div className={`inspector ${open ? "open" : ""}`} style={{ width }}>
      <div
        className="inspector-resize-handle"
        onMouseDown={startDrag}
        onDoubleClick={resetWidth}
        title="Drag to resize, double-click to reset"
      />
      <div className="inspector-header">
        <div className="inspector-title-group">
          <h2 className="inspector-title">{concept.name}</h2>
          {/* 角色、存活多久、通电状态、体积：一行普通的字，不做胶囊 */}
          <div className="inspector-meta">
            <span className={`meta-role kind-${concept.role}`}>{ROLE_NAME[concept.role] ?? concept.role}</span>
            {isCircuit && (
              <span className={`meta-state ${concept.is_active ? "is-lit" : ""}`}>
                <span className="meta-lamp" aria-hidden="true" />
                {concept.is_active ? "Lit" : "Off"}
              </span>
            )}
            {concept.lifespan && <span>{LIFESPAN_TEXT[concept.lifespan]}</span>}
            {concept.byte_size !== undefined && concept.byte_size > 0 && (
              <span className={tooLong ? "meta-too-long" : undefined}>
                {formatBytes(concept.byte_size)}
                {tooLong && " — too long"}
              </span>
            )}
            <span className="concept-id">#{concept.id}</span>
          </div>
        </div>
        <button className="inspector-close" onClick={onClose} aria-label="Close">
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
            <path d="m4 4 8 8M12 4l-8 8" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
          </svg>
        </button>
      </div>

      <div className="inspector-body">
        {/* ── 什么时候读 ── */}
        {concept.when_to_read && (
          <div className="inspector-section">
            <h3 className="section-label">When to read</h3>
            <div className="field-content inspector-when-to-read">{concept.when_to_read}</div>
          </div>
        )}

        {/* ── 别名、Tag ── */}
        {(altAliases.length > 0 || (concept.tags && concept.tags.length > 0)) && (
          <div className="inspector-section inspector-section-split">
            {concept.tags && concept.tags.length > 0 && (
              <div>
                <h3 className="section-label">Tags</h3>
                <div className="chip-list">
                  {concept.tags.map((t) => (
                    <span key={t} className="chip chip-tag">
                      {t}
                    </span>
                  ))}
                </div>
                {concept.tag_source_info && <div className="tag-source-info">{concept.tag_source_info}</div>}
              </div>
            )}
            {altAliases.length > 0 && (
              <div>
                <h3 className="section-label">Also called</h3>
                <div className="chip-list">
                  {altAliases.map((a) => (
                    <span key={a} className="chip">
                      {a}
                    </span>
                  ))}
                </div>
              </div>
            )}
          </div>
        )}

        {/* ── 什么时候通电：成员和组合方式 ── */}
        {((concept.members && concept.members.length > 0) || concept.activation_rule) && (
          <div className="inspector-section">
            <h3 className="section-label">
              Lights up when
              {concept.activation_type && (
                <span className={`section-label-aside mode-${concept.activation_type.toLowerCase()}`}>
                  {MODE_TEXT[concept.activation_type]}
                </span>
              )}
            </h3>

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
                        title={m.when_to_read || undefined}
                        onClick={() => onNavigate(m.concept_id)}
                      >
                        {concept.activation_type === "CHAIN" && <span className="member-order">{idx + 1}</span>}
                        <div className="member-info">
                          <span className="member-name">{m.name}</span>
                          {m.when_to_read && <span className="member-when-to-read-hint">{m.when_to_read}</span>}
                        </div>
                      </button>

                      {idx < arr.length - 1 && (
                        <div className={`member-connector op-${(concept.activation_type || "and").toLowerCase()}`}>
                          {op}
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

        {/* ── 通电时做什么：把 on_fire 的 JSON 拆开，提醒原文按引用显示 ── */}
        {concept.on_fire && (
          <div className="inspector-section">
            <h3 className="section-label">When lit</h3>
            {fireActions ? (
              <div className="fire-list">
                {fireActions.map((a, i) => (
                  <div key={i} className="fire-item">
                    <span className="fire-kind">{FIRE_KIND[a.kind] ?? a.kind}</span>
                    <p className="fire-text">{a.text}</p>
                  </div>
                ))}
              </div>
            ) : (
              <div className="activation-rule-box">
                <code>{concept.on_fire}</code>
              </div>
            )}
          </div>
        )}

        {/* ── 传感器在听什么 ── */}
        {concept.sensor_hooks && concept.sensor_hooks.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Listens for</h3>
            <div className="hook-list">
              {concept.sensor_hooks.map((h) => (
                <div key={h.id} className="hook-card">
                  <div className="hook-event">
                    <span className="event-type">{EVENT_TEXT[h.event_type] ?? h.event_type}</span>
                    {h.tool && <code className="hook-tool">{h.tool}</code>}
                  </div>
                  <div className="hook-pattern">
                    <span className="field-label">Matches</span>
                    <code>{h.match_pattern}</code>
                  </div>
                  {h.jev_questions?.map((q, i) => (
                    <div key={i} className="hook-pattern">
                      <span className="field-label">Then asks (yes ≥ {h.jev_threshold})</span>
                      <span>{q}</span>
                    </div>
                  ))}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── 守卫拦哪些工具 ── */}
        {concept.tool_guards && concept.tool_guards.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Blocks these tools until lit</h3>
            <div className="hook-list">
              {concept.tool_guards.map((g) => (
                <div key={g.id} className="hook-card">
                  <div className="hook-event">
                    <code className="hook-tool">{g.tool}</code>
                  </div>
                  {g.args_pattern && (
                    <div className="hook-pattern">
                      <span className="field-label">When the arguments match</span>
                      <code>{g.args_pattern}</code>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        )}

        {/* ── 抑制 ── */}
        {((concept.inhibitions && concept.inhibitions.length > 0) ||
          (concept.inhibiting && concept.inhibiting.length > 0)) && (
          <div className="inspector-section">
            {concept.inhibitions && concept.inhibitions.length > 0 && (
              <div className="inhibition-group">
                <h3 className="section-label">Held off by</h3>
                <div className="chip-list">
                  {concept.inhibitions.map((inh) => (
                    <button
                      key={inh.inhibitor_concept_id}
                      type="button"
                      className="chip chip-link chip-danger"
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
                <h3 className="section-label">Holds off</h3>
                <div className="chip-list">
                  {concept.inhibiting.map((inh) => (
                    <button
                      key={inh.target_concept_id}
                      type="button"
                      className="chip chip-link chip-danger"
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

        {/* ── 正文：始终按 Markdown 渲染 ── */}
        {concept.content && (
          <div className="inspector-section">
            <h3 className="section-label">Note</h3>
            <div className="field-content content-content md-content">
              {/* 单换行照原样换行（remark-breaks）；只认 ~~ 为删除线，正文里 3~5 这类范围写法不会被误划 */}
              <ReactMarkdown remarkPlugins={[[remarkGfm, { singleTilde: false }], remarkBreaks]}>
                {concept.content}
              </ReactMarkdown>
            </div>
          </div>
        )}

        {/* ── 读完它常接着读的：条的长短是转移权重相对本组最大值的比例 ── */}
        {concept.suggested_next && concept.suggested_next.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Often read next</h3>
            <div className="suggestion-list">
              {concept.suggested_next.map((s) => (
                <button
                  key={s.concept_id}
                  type="button"
                  className="suggestion-item"
                  title={`${s.when_to_read ? s.when_to_read + "\n" : ""}Weight ${s.weight.toFixed(2)}`}
                  onClick={() => onNavigate(s.concept_id)}
                >
                  <span className="suggestion-name">{s.concept_name}</span>
                  <span className="suggestion-bar" aria-hidden="true">
                    <span style={{ width: `${Math.max(8, (s.weight / maxWeight) * 100)}%` }} />
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

const ROLE_NAME: Record<string, string> = {
  plain: "Memory",
  sensor: "Sensor",
  logic: "Logic",
  guard: "Guard",
};

const LIFESPAN_TEXT: Record<string, string> = {
  turn: "Resets every turn",
  session: "Resets every session",
  permanent: "Stays until changed",
};

const MODE_TEXT: Record<string, string> = {
  CHAIN: "in order",
  AND: "all of these",
  OR: "any of these",
};

const OPERATOR_WORD: Record<string, string> = {
  CHAIN: "then",
  AND: "and",
  OR: "or",
};

const EVENT_TEXT: Record<string, string> = {
  user_message: "User message",
  model_message: "AI reply",
  tool_call: "Tool call",
  tool_result: "Tool result",
};

const FIRE_KIND: Record<string, string> = {
  notify: "Reminder",
  set_focus: "Sets focus",
  add_todo: "Adds a to-do",
};

/**
 * on_fire 存的是 JSON：一个动作 {"notify": "…"}，或几个动作 [{"notify": "…"}, {"add_todo": "…"}]。
 * 拆成 [{kind, text}] 给界面逐条显示；不是这个形状（解析失败、值不是字符串）就返回 null，界面退回显示原文。
 */
function parseOnFire(raw: string | null): { kind: string; text: string }[] | null {
  if (!raw) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    const list = Array.isArray(parsed) ? parsed : [parsed];
    const out: { kind: string; text: string }[] = [];
    for (const act of list) {
      if (!act || typeof act !== "object") return null;
      for (const [kind, text] of Object.entries(act as Record<string, unknown>)) {
        if (typeof text !== "string") return null;
        out.push({ kind, text });
      }
    }
    return out.length > 0 ? out : null;
  } catch {
    return null;
  }
}
