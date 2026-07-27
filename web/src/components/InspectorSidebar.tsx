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
        {concept.disclosure && (
          <p className="inspector-disclosure">{concept.disclosure}</p>
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
