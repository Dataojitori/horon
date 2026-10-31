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

/** 快照时间 "2026-10-29T21:27:58" 显示成 "Oct 29, 21:27"；不是这个格式就原样返回。 */
function formatWhen(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  const date = d.toLocaleDateString("en-US", { month: "short", day: "numeric" });
  const time = d.toLocaleTimeString("en-GB", { hour: "2-digit", minute: "2-digit" });
  return `${date}, ${time}`;
}

const ROLE_NAME: Record<string, string> = {
  plain: "Memory",
  sensor: "Sensor",
  logic: "Logic",
  guard: "Guard",
};

// 一页最多渲染多少张卡：把单次挂载的 LCS diff 计算量和 DOM 节点数封顶。
// 待审核堆积时，全量渲染 N 张卡 = N 次 O(m*n) diff + N 倍 diff 行 DOM，
// 低端机器上一次点击就要对账几万个节点——分页把这个上界固定下来。
const PAGE_SIZE = 20;

// "仅改动"模式下，每处改动上下各保留几行未改动的上下文。
const DIFF_CONTEXT = 2;

// 全局显示模式："full" 显示全文 diff；"changes" 只显示改动行及其上下文，
// 连续的未改动行折成一行"省略 N 行"。存 localStorage，刷新页面后保持上次的选择。
type DiffMode = "full" | "changes";
const DIFF_MODE_KEY = "horon.review.diffMode";

// 折叠后的一行：要么是原 diff 行，要么是"省略了 count 行未改动"的占位。
type FoldedRow = DiffLine | { type: "skip"; count: number };

/**
 * 输入：完整的 diff 行序列、上下文行数 context。
 * 行为：保留所有 added/removed 行，以及它们前后各 context 行的 unchanged 行；
 *       其余连续的 unchanged 行合并成一个 { type: "skip", count } 占位。
 * 输出：折叠后的行序列。没有任何改动时返回单个 skip（整段未改动）。
 */
function foldUnchanged(lines: DiffLine[], context: number): FoldedRow[] {
  const keep = new Array(lines.length).fill(false);
  lines.forEach((dl, idx) => {
    if (dl.type === "unchanged") return;
    const lo = Math.max(0, idx - context);
    const hi = Math.min(lines.length - 1, idx + context);
    for (let k = lo; k <= hi; k++) keep[k] = true;
  });

  const out: FoldedRow[] = [];
  let skipped = 0;
  lines.forEach((dl, idx) => {
    if (keep[idx]) {
      if (skipped > 0) {
        out.push({ type: "skip", count: skipped });
        skipped = 0;
      }
      out.push(dl);
    } else {
      skipped++;
    }
  });
  if (skipped > 0) out.push({ type: "skip", count: skipped });
  return out;
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
  mode,
}: {
  original: string | null;
  current: string | null;
  mode: DiffMode;
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

  const rows = useMemo<FoldedRow[]>(
    () => (mode === "changes" ? foldUnchanged(diffLines, DIFF_CONTEXT) : diffLines),
    [diffLines, mode]
  );

  if (original == null && current == null) {
    return <div className="diff-empty">(empty)</div>;
  }

  return (
    <div className="diff-container">
      <div className="diff-table">
        {rows.map((dl, idx) =>
          dl.type === "skip" ? (
            <div key={idx} className="diff-row diff-skip">
              {dl.count} unchanged {dl.count === 1 ? "line" : "lines"}
            </div>
          ) : (
          <div key={idx} className={`diff-row diff-${dl.type}`}>
            {/* 只放一列：现在这一版的行号。被删掉的行在现在这一版里不存在，所以留空。 */}
            <div className="diff-gutter">{dl.newLineNum ?? ""}</div>
            <div className="diff-prefix">
              {dl.type === "added" ? "+" : dl.type === "removed" ? "-" : " "}
            </div>
            <div className="diff-content">{dl.text || " "}</div>
          </div>
          )
        )}
      </div>
    </div>
  );
}

interface ReviewCardProps {
  item: ConceptReviewItem;
  isProcessing: boolean;
  diffMode: DiffMode;
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
  diffMode,
  onApprove,
  onRollback,
  onNavigateToNode,
}: ReviewCardProps) {
  const contentChange = item.changes.find((c) => c.field === "content");
  const whenToReadChange = item.changes.find((c) => c.field === "when_to_read");

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
            title={item.is_deleted ? "This memory was deleted" : "Open this memory"}
          >
            {item.concept_name}
          </span>
          {item.is_deleted && item.is_creation ? (
            <span className="badge badge-deleted">Created, then deleted</span>
          ) : item.is_deleted ? (
            <span className="badge badge-deleted">Deleted</span>
          ) : item.is_creation ? (
            <span className="badge badge-created">New</span>
          ) : (
            <span className={`badge badge-role kind-${item.role || "plain"}`}>
              {ROLE_NAME[item.role || "plain"]}
            </span>
          )}
          <span className="snapshot-timestamp" title={item.created_at}>{formatWhen(item.created_at)}</span>
        </div>

        <div className="card-header-actions">
          <button
            className="review-btn review-btn-approve"
            disabled={isProcessing}
            onClick={() => onApprove(item.concept_id)}
          >
            {isProcessing ? "Saving…" : "Approve"}
          </button>
          <button
            className="review-btn review-btn-rollback"
            disabled={isProcessing}
            onClick={() => onRollback(item.concept_id, item.is_creation, item.is_deleted)}
          >
            {isProcessing ? "Rolling back…" : "Roll back"}
          </button>
        </div>
      </div>

      <div className="review-card-body">
        {item.is_deleted && item.is_creation ? (
          <div className="deletion-banner">
            <span>This memory was created and then deleted. Rolling back only clears this record.</span>
          </div>
        ) : item.is_deleted ? (
          <div className="deletion-banner">
            <span>This memory was deleted. Rolling back brings it back as a plain memory.</span>
          </div>
        ) : item.is_creation ? (
          <div className="creation-banner">
            <span>This memory is new. Rolling back deletes it; if other memories link to it, remove those links first.</span>
          </div>
        ) : null}

        {whenToReadChange && (
          <div className="change-section">
            <div className="change-section-title">
              When to read
            </div>
            <DiffViewer
              original={whenToReadChange.original_value}
              current={whenToReadChange.current_value}
              mode={diffMode}
            />
          </div>
        )}

        {contentChange && (
          <div className="change-section">
            <div className="change-section-title">
              Note
            </div>
            <DiffViewer
              original={contentChange.original_value}
              current={contentChange.current_value}
              mode={diffMode}
            />
          </div>
        )}
      </div>
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
  const [diffMode, setDiffMode] = useState<DiffMode>(() =>
    localStorage.getItem(DIFF_MODE_KEY) === "changes" ? "changes" : "full"
  );
  useEffect(() => {
    localStorage.setItem(DIFF_MODE_KEY, diffMode);
  }, [diffMode]);
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
        promptMsg = "This memory was created and then deleted. Roll back to clear the record?";
      } else if (isCreation) {
        promptMsg = "This memory is new. Rolling back deletes it. Roll back?";
      } else if (isDeleted) {
        promptMsg = "This memory was deleted. Roll back to restore it as a plain memory?";
      } else {
        promptMsg = "Undo every change to this memory since the snapshot?";
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
        alert(`Couldn’t roll back: ${e.message}`);
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
        <p>Loading changes…</p>
      </div>
    );
  }

  return (
    <div className="review-view">
      <div className="review-toolbar">
        <div className="review-toolbar-left">
          <h2 className="review-title">Changes to review</h2>
          <span className="review-badge-count">{reviews.length} waiting</span>
        </div>
        <div className="review-toolbar-right">
          <div className="review-mode-switch" title="How every change below is shown">
            <button
              className={diffMode === "full" ? "active" : ""}
              onClick={() => setDiffMode("full")}
            >
              Full text
            </button>
            <button
              className={diffMode === "changes" ? "active" : ""}
              onClick={() => setDiffMode("changes")}
            >
              Changes only
            </button>
          </div>
          <button className="review-btn-secondary" onClick={fetchReviews}>
            Refresh
          </button>
        </div>
      </div>

      {error && <div className="review-error-banner">{error}</div>}

      <div className="review-content" ref={contentRef}>
        {reviews.length === 0 ? (
          <div className="review-empty-state">
            
            <h3>Nothing to review</h3>
            <p>When a memory is created, edited or deleted from the CLI, the change waits here for you.</p>
          </div>
        ) : (
          <div className="review-cards-list">
            {pageItems.map((item) => (
              <ReviewCard
                key={item.concept_id}
                item={item}
                isProcessing={actionLoading[item.concept_id] || false}
                diffMode={diffMode}
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
                  Previous
                </button>
                <span className="review-pager-label">
                  Page {currentPage} of {totalPages}
                </span>
                <button
                  className="review-btn-secondary"
                  disabled={currentPage >= totalPages}
                  onClick={() => setPage(currentPage + 1)}
                >
                  Next
                </button>
              </div>
            )}
          </div>
        )}
      </div>
    </div>
  );
}
