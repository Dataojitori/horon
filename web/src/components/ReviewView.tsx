import React, { useState, useEffect, useCallback, useMemo, memo, useRef } from "react";
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

// 一页最多渲染多少张卡：把单次挂载的 LCS diff 计算量和 DOM 节点数封顶。
// 待审核堆积时，全量渲染 N 张卡 = N 次 O(m*n) diff + N 倍 diff 行 DOM，
// 低端机器上一次点击就要对账几万个节点——分页把这个上界固定下来。
const PAGE_SIZE = 20;

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

interface ReviewCardProps {
  item: ConceptReviewItem;
  isProcessing: boolean;
  onApprove: (conceptId: number) => void;
  onRollback: (conceptId: number, isCreation: boolean, isDeleted: boolean) => void;
  onNavigateToNode?: (nodeId: number) => void;
}

// memo 是本修的核心之一：点某张卡的"同意"时，父组件的 actionLoading /
// reviews 会更新并重渲染列表；没有 memo 的话剩下 N-1 张卡的 diff DOM
// 要全部参与对账——这就是之前"点一下就卡死"的直接原因。
// item 是快照对象引用，filter 只摘掉被处理的那一张，其余引用不变，
// 加上下面稳定的回调，memo 能让未动弹的卡整张跳过渲染。
const ReviewCard = memo(function ReviewCard({
  item,
  isProcessing,
  onApprove,
  onRollback,
  onNavigateToNode,
}: ReviewCardProps) {
  // 默认展开，保持老版本"一眼看到 diff"的体验；折叠后 DiffViewer 卸载，
  // 大正文节点的几千个 diff 行 DOM 直接消失，需要时再点开。
  const [expanded, setExpanded] = useState(true);

  const contentChange = item.changes.find((c) => c.field === "content");
  const disclosureChange = item.changes.find((c) => c.field === "disclosure");

  return (
    <div
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
          {(contentChange || disclosureChange) && (
            <button
              className="review-btn-toggle"
              onClick={() => setExpanded((v) => !v)}
              title={expanded ? "折叠 diff（卸载 diff DOM，省内存）" : "展开 diff"}
            >
              {expanded ? "折叠 diff" : "展开 diff"}
            </button>
          )}
          <button
            className="review-btn review-btn-approve"
            disabled={isProcessing}
            onClick={() => onApprove(item.concept_id)}
          >
            {isProcessing ? "处理中..." : "同意 (Approve)"}
          </button>
          <button
            className="review-btn review-btn-rollback"
            disabled={isProcessing}
            onClick={() => onRollback(item.concept_id, item.is_creation, item.is_deleted)}
          >
            {isProcessing ? "回滚中..." : "回滚 (Rollback)"}
          </button>
        </div>
      </div>

      {expanded && (
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
      )}
    </div>
  );
});

export default function ReviewView({
  onRefreshGraph,
  onNavigateToNode,
}: ReviewViewProps) {
  const [reviews, setReviews] = useState<ConceptReviewItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [actionLoading, setActionLoading] = useState<Record<number, boolean>>({});
  const [error, setError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  const contentRef = useRef<HTMLDivElement>(null);

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

  // 同意/回滚后当前页可能变空（如删掉本页最后一条），派生钳位保证永远落在有效页上。
  const totalPages = Math.max(1, Math.ceil(reviews.length / PAGE_SIZE));
  const currentPage = Math.min(page, totalPages);

  // 翻页后回到列表顶部，不然视线还留在底，下一页得手动滚回去。
  useEffect(() => {
    contentRef.current?.scrollTo({ top: 0 });
  }, [currentPage]);
  const pageItems = useMemo(
    () => reviews.slice((currentPage - 1) * PAGE_SIZE, currentPage * PAGE_SIZE),
    [reviews, currentPage]
  );

  const handleApprove = useCallback(
    async (conceptId: number) => {
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
    },
    [onRefreshGraph]
  );

  const handleRollback = useCallback(
    async (conceptId: number, isCreation: boolean, isDeleted: boolean) => {
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
    },
    [onRefreshGraph]
  );

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

      <div className="review-content" ref={contentRef}>
        {reviews.length === 0 ? (
          <div className="review-empty-state">
            <div className="review-empty-icon">&#x2713;</div>
            <h3>全部快照已审核完毕</h3>
            <p>目前没有来自 CLI 的待审核新建、修改或删除操作。</p>
          </div>
        ) : (
          <div className="review-cards-list">
            {pageItems.map((item) => (
              <ReviewCard
                key={item.concept_id}
                item={item}
                isProcessing={actionLoading[item.concept_id] || false}
                onApprove={handleApprove}
                onRollback={handleRollback}
                onNavigateToNode={onNavigateToNode}
              />
            ))}

            {totalPages > 1 && (
              <div className="review-pager">
                <button
                  className="review-btn-secondary"
                  disabled={currentPage <= 1}
                  onClick={() => setPage(currentPage - 1)}
                >
                  上一页
                </button>
                <span className="review-pager-label">
                  第 {currentPage} / {totalPages} 页（共 {reviews.length} 条，每页 {PAGE_SIZE} 条）
                </span>
                <button
                  className="review-btn-secondary"
                  disabled={currentPage >= totalPages}
                  onClick={() => setPage(currentPage + 1)}
                >
                  下一页
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
