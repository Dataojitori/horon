import type { ConceptDetail } from "../types";
import "./InspectorSidebar.css";

interface Props {
  concept: ConceptDetail | null;
  open: boolean;
  onClose: () => void;
}

const STATUS_LABEL: Record<string, string> = {
  confirmed: "Confirmed",
  hypothesis: "Hypothesis",
  negated: "Negated",
};

export default function InspectorSidebar({ concept, open, onClose }: Props) {
  if (!concept) return null;

  return (
    <div className={`inspector ${open ? "open" : ""}`}>
      <div className="inspector-header">
        <h2 className="inspector-title">{concept.name}</h2>
        <button className="inspector-close" onClick={onClose}>
          &times;
        </button>
      </div>

      <div className="inspector-body">
        {concept.disclosures && concept.disclosures.length > 0 && (
          <div className="inspector-disclosures">
            {concept.disclosures.map((d) => (
              <p key={d.id} className="inspector-disclosure">
                #{d.id}: {d.text}
              </p>
            ))}
          </div>
        )}

        {concept.aliases.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Aliases</h3>
            <div className="alias-list">
              {concept.aliases.map((a) => (
                <span key={a} className="alias-tag">
                  {a}
                </span>
              ))}
            </div>
          </div>
        )}

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

        {concept.variations.length > 0 && (
          <div className="inspector-section">
            <h3 className="section-label">Variations</h3>
            {concept.variations.map((v) => {
              // 组合 variation 的 NULL status 与 hypothesis 语义相同；
              // 原子 variation 没有关系状态，仍然不显示 badge。
              const status = v.status ?? (v.expression ? "hypothesis" : null);

              return (
                <div key={v.short_code} className="variation-card">
                  <div className="variation-header">
                    <code className="var-code">{v.short_code}</code>
                    {status && (
                      <span className={`status-badge ${status}`}>
                        {STATUS_LABEL[status] ?? status}
                      </span>
                    )}
                    {v.expression && (
                      <span className="var-expression">{v.expression}</span>
                    )}
                  </div>
                  {v.content && (
                    <div className="var-field">
                      <span className="field-label">Content</span>
                      <div className="field-content content-content">
                        {v.content}
                      </div>
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
