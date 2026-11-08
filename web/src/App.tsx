import { useState, useEffect, useCallback, useRef, type CSSProperties } from "react";
import { api } from "./api";
import type { GraphData, ViewMode, ConceptDetail, SessionInfo } from "./types";
import GalaxyView from "./components/GalaxyView";
import DissectionView from "./components/DissectionView";
import InspectorSidebar from "./components/InspectorSidebar";
import SearchBar from "./components/SearchBar";
import ReviewView from "./components/ReviewView";
import ThemeToggle from "./components/ThemeToggle";
import Wordmark from "./components/Wordmark";
import { HOST } from "./host";
import "./App.css";

// 嵌在宿主网站里时，浏览器标签页写上这份记忆是谁的
if (HOST.title) document.title = `${HOST.title} – Horon`;

// 顶栏三个模式按钮的顺序；滑块位置按它算
const MODE_ORDER: ViewMode[] = ["galaxy", "dissection", "review"];

// 宿主的显示名。不认识的原样显示。
const ADAPTER_NAMES: Record<string, string> = {
  "claude-code": "Claude Code",
  codex: "Codex",
  antigravity: "Antigravity",
  dsh: "DeepSeek Harness",
};

function minutesSince(iso: string): number {
  return Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 60000));
}

function agoText(minutes: number): string {
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 1440) return `${Math.round(minutes / 60)} h ago`;
  const days = Math.round(minutes / 1440);
  return days === 1 ? "yesterday" : `${days} days ago`;
}

/** 会话最近 10 分钟内有动静，就当作“还在跑”，下拉框前的灯亮着。 */
const LIVE_MINUTES = 10;
/** 页面放着不动时，多久重新取一次会话列表。 */
const SESSIONS_REFRESH_MS = 60_000;

// 下拉菜单里一个会话的显示文字，例如 "Claude Code — 3 min ago"。
// 同一个宿主出现不止一次时，才在后面补会话 ID 的前 4 位来区分。
function sessionLabel(s: SessionInfo, all: SessionInfo[]): string {
  if (s.session_id === "devonly") return "Offline";
  const host = ADAPTER_NAMES[s.adapter ?? ""] ?? s.adapter ?? "Unknown host";
  const twins = all.filter((o) => o.adapter === s.adapter && o.session_id !== "devonly").length;
  const suffix = twins > 1 ? ` (${s.session_id.slice(0, 4)})` : "";
  return `${host} — ${agoText(minutesSince(s.last_active_at))}${suffix}`;
}

function LoadingScreen() {
  return (
    <div className="loading-screen">
      <Wordmark size={40} state="loading" />
      <p className="loading-text">Waking up the memory map…</p>
    </div>
  );
}

function ErrorScreen({ error }: { error: string }) {
  return (
    <div className="loading-screen error">
      <Wordmark size={40} state="error" />
      <p className="error-title">Can’t reach the Horon server</p>
      <p className="error-detail">{error}</p>
      <p className="error-hint">
        Start it with <code>start.bat</code>, or run the API on port 8710, then reload this page.
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
  const [inspectorWidth, setInspectorWidth] = useState(0);
  const [pendingReviewCount, setPendingReviewCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [sessions, setSessions] = useState(initialSessions);
  // 节点的激活状态按哪个会话显示。默认最近活跃的会话（列表第一项；列表里至少有 devonly）
  const [session, setSession] = useState(initialSessions[0].session_id);
  const sessionRef = useRef(session);
  const currentSession = sessions.find((s) => s.session_id === session);
  sessionRef.current = session;
  const graphRequestId = useRef(0);

  // 重新取会话列表，让灯和「几分钟前」跟上实际。当前选中的会话掉出列表时留在最前面。
  const refreshSessions = useCallback(() => {
    api
      .getSessions()
      .then((freshSessions) => {
        const selectedSessionId = sessionRef.current;
        setSessions((currentSessions) => {
          if (freshSessions.some((item) => item.session_id === selectedSessionId)) {
            return freshSessions;
          }
          const selected = currentSessions.find((item) => item.session_id === selectedSessionId);
          return selected ? [selected, ...freshSessions] : freshSessions;
        });
      })
      .catch((e) => console.error("Failed to fetch sessions", e));
  }, []);

  useEffect(() => {
    const timer = setInterval(refreshSessions, SESSIONS_REFRESH_MS);
    return () => clearInterval(timer);
  }, [refreshSessions]);

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
          {HOST.back && (
            <a className="host-back" href={HOST.back} title={HOST.label}>
              <svg viewBox="0 0 24 24" aria-hidden="true">
                <path d="m14.5 6-6 6 6 6" />
              </svg>
              <span className="host-back-text">{HOST.label}</span>
            </a>
          )}
          <h1 className="logo">
            <Wordmark />
            {HOST.title && <span className="host-title">{HOST.title}</span>}
          </h1>
          <div
            className="mode-switcher"
            style={{ "--i": MODE_ORDER.indexOf(mode) } as CSSProperties}
          >
            <span className="mode-thumb" aria-hidden="true" />
            <button
              className={`mode-btn ${mode === "galaxy" ? "active" : ""}`}
              onClick={() => {
                setMode("galaxy");
                setFocalId(null);
              }}
            >
              Map
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
              Focus
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
          <div className="session-select-wrap">
            <span
              className={`session-lamp ${
                currentSession && currentSession.session_id !== "devonly" &&
                minutesSince(currentSession.last_active_at) < LIVE_MINUTES
                  ? "is-live"
                  : ""
              }`}
              aria-hidden="true"
            />
            <select
              className="session-select"
              aria-label="Show live state from session"
              title="Which nodes are lit depends on the session you pick here"
              value={session}
              onFocus={refreshSessions}
              onChange={(e) => {
                // 切换会话时立刻废弃尚未完成的详情请求，避免旧会话的结果随后打开侧栏。
                inspectRequestId.current += 1;
                setSession(e.target.value);
              }}
            >
              {sessions.map((s) => (
                <option key={s.session_id} value={s.session_id}>
                  {sessionLabel(s, sessions)}
                </option>
              ))}
            </select>
            <svg className="session-chevron" viewBox="0 0 12 12" width="12" height="12" aria-hidden="true">
              <path d="M2.5 4.5 6 8l3.5-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
            </svg>
          </div>
          <ThemeToggle />
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
            // 侧栏浮在右边（right: 10px），挡住的宽度再留 10px 缝
            rightInset={sidebarOpen && inspectedConcept ? inspectorWidth + 20 : 0}
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
        onWidthChange={setInspectorWidth}
      />
    </div>
  );
}
