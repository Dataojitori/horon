import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "./api";
import type { GraphData, ViewMode, ConceptDetail, SessionInfo } from "./types";
import GalaxyView from "./components/GalaxyView";
import DissectionView from "./components/DissectionView";
import InspectorSidebar from "./components/InspectorSidebar";
import SearchBar from "./components/SearchBar";
import ReviewView from "./components/ReviewView";
import "./App.css";

// 下拉菜单里一个会话的显示文字，例如 "claude-code · 3m ago · 99bc279f"
function sessionLabel(s: SessionInfo): string {
  if (s.session_id === "devonly") return "offline (devonly)";
  const minutes = Math.max(0, Math.round((Date.now() - new Date(s.last_active_at).getTime()) / 60000));
  const ago =
    minutes < 60 ? `${minutes}m ago` : minutes < 1440 ? `${Math.round(minutes / 60)}h ago` : `${Math.round(minutes / 1440)}d ago`;
  return `${s.adapter ?? "unknown"} · ${ago} · ${s.session_id.slice(0, 8)}`;
}

function LoadingScreen() {
  return (
    <div className="loading-screen">
      <div className="loading-spinner" />
      <p>Loading graph...</p>
    </div>
  );
}

function ErrorScreen({ error }: { error: string }) {
  return (
    <div className="loading-screen error">
      <p>Failed to connect to Horon API</p>
      <p className="error-detail">{error}</p>
      <p className="error-hint">
        Make sure the API server is running on port 8710
      </p>
    </div>
  );
}

// 先取会话列表：主界面的每个请求都要带会话 ID（激活状态按会话显示），
// 所以拿到列表之前不渲染主界面，主界面里的会话因此一定有值。
export default function App() {
  const [sessions, setSessions] = useState<SessionInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api.getSessions().then(setSessions).catch((e) => setError(e.message));
  }, []);

  if (error) return <ErrorScreen error={error} />;
  if (!sessions) return <LoadingScreen />;
  return <Workspace initialSessions={sessions} />;
}

function Workspace({ initialSessions }: { initialSessions: SessionInfo[] }) {
  const [mode, setMode] = useState<ViewMode>("galaxy");
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [focalId, setFocalId] = useState<number | null>(null);
  const [inspectedConcept, setInspectedConcept] =
    useState<ConceptDetail | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [pendingReviewCount, setPendingReviewCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState(initialSessions);
  // 节点的激活状态按哪个会话显示。默认最近活跃的会话（列表第一项；列表里至少有 devonly）
  const [session, setSession] = useState(initialSessions[0].session_id);
  const sessionRef = useRef(session);
  sessionRef.current = session;
  const graphRequestId = useRef(0);

  const reloadGraph = useCallback(() => {
    const reqId = ++graphRequestId.current;
    api
      .getGraph(session)
      .then((data) => {
        if (reqId !== graphRequestId.current) return;
        setGraphData(data);
        setError(null);
      })
      .catch((e) => {
        if (reqId !== graphRequestId.current) return;
        setError(e.message);
      })
      .finally(() => {
        if (reqId === graphRequestId.current) setLoading(false);
      });

    api
      .getReviews()
      .then((items) => setPendingReviewCount(items.length))
      .catch((e) => console.error("Failed to fetch review count", e));
  }, [session]);

  const inspectRequestId = useRef(0);

  const inspectNode = useCallback((nodeId: number) => {
    const reqId = ++inspectRequestId.current;
    api
      .getConcept(nodeId, session)
      .then((detail) => {
        if (reqId !== inspectRequestId.current) return;
        setInspectedConcept(detail);
        setSidebarOpen(true);
      })
      .catch((e) => {
        // 单次 inspect 失败不该清空整张图，只是没法打开侧栏；记录即可。
        if (reqId !== inspectRequestId.current) return;
        console.error("Failed to inspect concept", nodeId, e);
      });
  }, [session]);

  // 审核页跳转节点。用 useCallback 固定引用：ReviewCard 是 memo 组件，
  // 这里若是内联箭头，App 每次重渲染（比如同意后 reloadGraph）都会让整页卡片跟着重渲染。
  const handleReviewNavigate = useCallback((nodeId: number) => {
    setFocalId(nodeId);
    setMode("dissection");
    inspectNode(nodeId);
  }, [inspectNode]);

  // 刚进入主界面、以及每次切换会话时：按当前会话重新取整张图（顺带刷新待审数量），
  // 侧栏开着的节点也按新会话刷新。
  // 依赖故意只写 session：侧栏换了别的节点不该触发重新取图。
  useEffect(() => {
    reloadGraph();
    if (sidebarOpen && inspectedConcept) inspectNode(inspectedConcept.id);
  }, [session]);

  const handleNodeClick = useCallback(
    (nodeId: number) => {
      setFocalId(nodeId);
      setMode("dissection");
      inspectNode(nodeId);
    },
    [inspectNode],
  );

  const handleBackToGalaxy = useCallback(() => {
    setMode("galaxy");
    setFocalId(null);
  }, []);

  const handleInspect = useCallback((nodeId: number) => {
    inspectNode(nodeId);
  }, [inspectNode]);

  const handleSearchSelect = useCallback(
    (conceptId: number) => {
      handleNodeClick(conceptId);
    },
    [handleNodeClick],
  );

  if (loading) return <LoadingScreen />;
  if (error) return <ErrorScreen error={error} />;

  return (
    <div className="app">
      <header className="topbar">
        <div className="topbar-left">
          <h1 className="logo">
            <span className="logo-glyph">&#x25C9;</span> Horon
          </h1>
          <div className="mode-switcher">
            <button
              className={`mode-btn ${mode === "galaxy" ? "active" : ""}`}
              onClick={() => {
                setMode("galaxy");
                setFocalId(null);
              }}
            >
              Galaxy
            </button>
            <button
              className={`mode-btn ${mode === "dissection" ? "active" : ""}`}
              onClick={() => {
                if (focalId == null && graphData?.nodes.length) {
                  setFocalId(graphData.nodes[0].id);
                }
                setMode("dissection");
              }}
            >
              Dissect
            </button>
            <button
              className={`mode-btn ${mode === "review" ? "active" : ""}`}
              onClick={() => {
                setMode("review");
                setFocalId(null);
              }}
            >
              Review
              {pendingReviewCount > 0 && (
                <span className="mode-btn-badge">{pendingReviewCount}</span>
              )}
            </button>
          </div>
        </div>
        <div className="topbar-center">
          <SearchBar onSelect={handleSearchSelect} />
        </div>
        <div className="topbar-right">
          {/* 获得焦点时刷新列表，让「几分钟前」和最近的会话保持最新 */}
          <select
            className="session-select"
            title="节点的激活状态按这个会话显示"
            value={session}
            onFocus={() =>
              api
                .getSessions()
                .then((freshSessions) => {
                  const selectedSessionId = sessionRef.current;
                  setSessions((currentSessions) => {
                    if (freshSessions.some((item) => item.session_id === selectedSessionId)) {
                      return freshSessions;
                    }
                    const selected = currentSessions.find(
                      (item) => item.session_id === selectedSessionId,
                    );
                    return selected ? [selected, ...freshSessions] : freshSessions;
                  });
                })
                .catch((e) => console.error("Failed to fetch sessions", e))
            }
            onChange={(e) => {
              // 切换会话时立刻废弃尚未完成的详情请求，避免旧会话的结果随后打开侧栏。
              inspectRequestId.current += 1;
              setSession(e.target.value);
            }}
          >
            {sessions.map((s) => (
              <option key={s.session_id} value={s.session_id}>
                {sessionLabel(s)}
              </option>
            ))}
          </select>
          {graphData && (
            <span className="stat">
              {graphData.nodes.length} concepts &middot;{" "}
              {graphData.links.length} links
            </span>
          )}
        </div>
      </header>

      <main className="viewport">
        {mode === "galaxy" && graphData && (
          <GalaxyView
            data={graphData}
            onNodeClick={handleNodeClick}
            onNodeHover={handleInspect}
          />
        )}
        {mode === "dissection" && focalId != null && (
          <DissectionView
            focalId={focalId}
            session={session}
            onNavigate={handleNodeClick}
            onInspect={handleInspect}
            onBack={handleBackToGalaxy}
          />
        )}
        {mode === "review" && (
          <ReviewView
            onRefreshGraph={reloadGraph}
            onNavigateToNode={handleReviewNavigate}
          />
        )}
      </main>

      <InspectorSidebar
        concept={inspectedConcept}
        open={sidebarOpen}
        onClose={() => setSidebarOpen(false)}
        onNavigate={handleNodeClick}
      />
    </div>
  );
}
