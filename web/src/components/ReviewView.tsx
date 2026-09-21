import React, { useState, useEffect, useCallback, useMemo } from "react";
import { api } from "../api";
import type { ConceptReviewItem } from "../types";
import "./ReviewView.css";

interface ReviewViewProps {
  onRefreshGraph?: () => void;
  onNavigateToNode?: (nodeId: number) => void;
}

interface DiffLine {
  type: "added" | "removed" | "unchanged";
  text: string;
  oldLineNum?: number;
  newLineNum?: number;
}

function computeLcsDiff(oldLines: string[], newLines: string[]): DiffLine[] {
  const m = oldLines.length;
  const n = newLines.length;

  // dp matrix
  const dp: number[][] = Array.from({ length: m + 1 }, () =>
    new Array(n + 1).fill(0)
  );

  for (let i = 0; i < m; i++) {
    for (let j = 0; j < n; j++) {
      if (oldLines[i] === newLines[j]) {
        dp[i + 1][j + 1] = dp[i][j] + 1;
      } else {
        dp[i + 1][j + 1] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  const result: DiffLine[] = [];
  let i = m;
  let j = n;

  while (i > 0 || j > 0) {
    if (i > 0 && j > 0 && oldLines[i - 1] === newLines[j - 1]) {
      result.push({
        type: "unchanged",
        text: oldLines[i - 1],
        oldLineNum: i,
        newLineNum: j,
      });
      i--;
      j--;
    } else if (j > 0 && (i === 0 || dp[i][j - 1] >= dp[i - 1][j])) {
      result.push({
        type: "added",
        text: newLines[j - 1],
        newLineNum: j,
      });
      j--;
    } else if (i > 0 && (j === 0 || dp[i][j - 1] < dp[i - 1][j])) {
      result.push({
        type: "removed",
        text: oldLines[i - 1],
        oldLineNum: i,
      });
      i--;
    }
  }

  return result.reverse();
}

function DiffViewer({
  original,
  current,
}: {
  original: string | null;
  current: string | null;
}) {
  const diffLines = useMemo<DiffLine[]>(() => {
    const oldLines = original != null ? original.split("\n") : [];
    const newLines = current != null ? current.split("\n") : [];

    if (original == null && current != null) {
      return newLines.map((line, idx) => ({
        type: "added" as const,
        text: line,
        newLineNum: idx + 1,
      }));
    }

    if (original != null && current == null) {
      return oldLines.map((line, idx) => ({
        type: "removed" as const,
        text: line,
        oldLineNum: idx + 1,
      }));
    }

    return computeLcsDiff(oldLines, newLines);
  }, [original, current]);

  if (original == null && current == null) {
    return <div className="diff-empty">(无内容 / Empty)</div>;
  }

  return (
    <div className="diff-container">
      <div className="diff-table">
        {diffLines.map((dl, idx) => (
          <div key={idx} className={`diff-row diff-${dl.type}`}>
            <div className="diff-gutter diff-gutter-old">
              {dl.oldLineNum ?? ""}
            </div>
            <div className="diff-gutter diff-gutter-new">
              {dl.newLineNum ?? ""}
            </div>
            <div className="diff-prefix">
              {dl.type === "added" ? "+" : dl.type === "removed" ? "-" : " "}
            </div>
            <div className="diff-content">{dl.text || " "}</div>
          </div>
        ))}
      </div>
    </div>
  );
}

export default function ReviewView({
  onRefreshGraph,
  onNavigateToNode,
}: ReviewViewProps) {
  const [reviews, setReviews] = useState<ConceptReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<Record<number, boolean>>({});
  const [error, setError] = useState<string | null>(null);

  const fetchReviews = useCallback(() => {
    setLoading(true);
    api
      .getReviews()
      .then((items) => {
        setReviews(items);
        setError(null);
      })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    fetchReviews();
  }, [fetchReviews]);

  const handleApprove = async (conceptId: number) => {
    setActionLoading((prev) => ({ ...prev, [conceptId]: true }));
    try {
      await api.approveReview(conceptId);
      setReviews((prev) => prev.filter((r) => r.concept_id !== conceptId));
      onRefreshGraph?.();
    } catch (e: any) {
      alert(`Approve failed: ${e.message}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [conceptId]: false }));
    }
  };

  const handleRollback = async (conceptId: number, isCreation: boolean, isDeleted: boolean) => {
    let promptMsg: string;
    if (isCreation && isDeleted) {
      promptMsg = "该节点为新建后被删除，回滚将直接清除快照记录。确定回滚吗？";
    } else if (isCreation) {
      promptMsg = "该节点为新建节点，回滚将直接删除该节点。确定回滚吗？";
    } else if (isDeleted) {
      promptMsg = "该节点已被删除，回滚将重新恢复为 plain（砖块）角色。确定回滚吗？";
    } else {
      promptMsg = "确定要将该节点的所有修改回滚到快照前状态吗？";
    }

    if (!window.confirm(promptMsg)) {
      return;
    }

    setActionLoading((prev) => ({ ...prev, [conceptId]: true }));
    try {
      await api.rollbackReview(conceptId);
      setReviews((prev) => prev.filter((r) => r.concept_id !== conceptId));
      onRefreshGraph?.();
    } catch (e: any) {
      alert(`回滚失败: ${e.message}`);
    } finally {
      setActionLoading((prev) => ({ ...prev, [conceptId]: false }));
    }
  };

  if (loading && reviews.length === 0) {
    return (
      <div className="review-loading">
        <div className="loading-spinner" />
        <p>加载待审核快照...</p>
      </div>
    );
  }

  return (
    <div className="review-view">
      <div className="review-toolbar">
        <div className="review-toolbar-left">
          <h2 className="review-title">人工审核与快照 (Review & Snapshots)</h2>
          <span className="review-badge-count">{reviews.length} 个节点待审核</span>
        </div>
        <div className="review-toolbar-right">
          <button className="review-btn-secondary" onClick={fetchReviews}>
            刷新 (Refresh)
          </button>
        </div>
      </div>

      {error && <div className="review-error-banner">{error}</div>}

      <div className="review-content">
        {reviews.length === 0 ? (
          <div className="review-empty-state">
            <div className="review-empty-icon">&#x2713;</div>
            <h3>全部快照已审核完毕</h3>
            <p>目前没有来自 CLI 的待审核新建、修改或删除操作。</p>
          </div>
        ) : (
          <div className="review-cards-list">
            {reviews.map((item) => {
              const isProcessing = actionLoading[item.concept_id] || false;
              const contentChange = item.changes.find((c) => c.field === "content");
              const disclosureChange = item.changes.find((c) => c.field === "disclosure");

              return (
                <div
                  key={item.concept_id}
                  className={`review-card ${
                    item.is_deleted
                      ? "review-card-deleted"
                      : item.is_creation
                      ? "review-card-created"
                      : ""
                  }`}
                >
                  <div className="review-card-header">
                    <div className="card-header-left">
                      <span className="concept-id-tag">#{item.concept_id}</span>
                      <span
                        className="concept-name-link"
                        onClick={() => !item.is_deleted && onNavigateToNode?.(item.concept_id)}
                        title={item.is_deleted ? "该节点已删除" : "点击跳转至节点"}
                      >
                        {item.concept_name}
                      </span>
                      {item.is_deleted && item.is_creation ? (
                        <span className="badge badge-deleted">CANCELLED (已取消创建)</span>
                      ) : item.is_deleted ? (
                        <span className="badge badge-deleted">DELETED (已删除)</span>
                      ) : item.is_creation ? (
                        <span className="badge badge-created">CREATED (新建)</span>
                      ) : (
                        <span className={`badge badge-role role-${item.role || "plain"}`}>
                          {item.role?.toUpperCase() || "PLAIN"}
                        </span>
                      )}
                      <span className="snapshot-timestamp">{item.created_at}</span>
                    </div>

                    <div className="card-header-actions">
                      <button
                        className="review-btn review-btn-approve"
                        disabled={isProcessing}
                        onClick={() => handleApprove(item.concept_id)}
                      >
                        {isProcessing ? "处理中..." : "同意 (Approve)"}
                      </button>
                      <button
                        className="review-btn review-btn-rollback"
                        disabled={isProcessing}
                        onClick={() => handleRollback(item.concept_id, item.is_creation, item.is_deleted)}
                      >
                        {isProcessing ? "回滚中..." : "回滚 (Rollback)"}
                      </button>
                    </div>
                  </div>

                  <div className="review-card-body">
                    {item.is_deleted && item.is_creation ? (
                      <div className="deletion-banner">
                        <span>该节点为新建后被删除。回滚将直接清除快照记录。</span>
                      </div>
                    ) : item.is_deleted ? (
                      <div className="deletion-banner">
                        <span>⚠ 该节点已被删除。回滚后将作为 <strong>plain（砖块）</strong> 角色恢复。</span>
                      </div>
                    ) : item.is_creation ? (
                      <div className="creation-banner">
                        <span>✨ 该节点为新建节点。回滚将直接删除此节点（若被其他概念引用，请先切断关联）。</span>
                      </div>
                    ) : null}

                    {disclosureChange && (
                      <div className="change-section">
                        <div className="change-section-title">
                          <span className="field-dot" /> 书腰变更 (Disclosure Diff)
                        </div>
                        <DiffViewer
                          original={disclosureChange.original_value}
                          current={disclosureChange.current_value}
                        />
                      </div>
                    )}

                    {contentChange && (
                      <div className="change-section">
                        <div className="change-section-title">
                          <span className="field-dot" /> 正文变更 (Content Diff)
                        </div>
                        <DiffViewer
                          original={contentChange.original_value}
                          current={contentChange.current_value}
                        />
                      </div>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
