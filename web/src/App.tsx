import { useState, useEffect, useCallback, useRef } from "react";
import { api, setDb as setDbModule } from "./api";
import type { DatabaseInfo, } from "./api";
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

// 库别名 → 徽标色：纯展示，同一个别名在不同前端上颜色一致，好认。
function dbColor(alias: string): string {
  let h = 0;
  for (let i = 0; i < alias.length; i++) h = (h * 31 + alias.charCodeAt(i)) >>> 0;
  return `hsl(${h % 360} 60% 45%)`;
}

// 先定库、再取会话：库选择（URL ?db= > 本地记忆 > 服务端缺省）决定之后所有请求带哪个库。
// 每个前端连的后端缺省库可能不同（主前端缺省 main，RP 前端缺省 rp），
// 所以缺省值向服务端要，而不是写死——两边前端缺省看到的和以前一字不差。
export default function App() {
  const [init, setInit] = useState<{
    sessions: SessionInfo[];
    db: string;
    databases: DatabaseInfo[];
  } | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listDatabases()
      .then((dbs) => {
        if (dbs.length === 0) throw new Error("No databases reported by API");
        const params = new URLSearchParams(window.location.search);
        const want = params.get("db") || localStorage.getItem("horon.db");
        const hit = dbs.find((d) => d.alias === want);
        const initial =
          hit?.alias ?? dbs.find((d) => d.default)?.alias ?? dbs[0].alias;
        setDbModule(initial);
        return api.getSessions().then((sessions) => {
          if (sessions.length === 0) throw new Error("No sessions reported by API");
          setInit({ sessions, db: initial, databases: dbs });
        });
      })
      .catch((e) => setError(e.message));
  }, []);

  if (error) return <ErrorScreen error={error} />;
  if (!init) return <LoadingScreen />;
  return (
    <Workspace
      initialSessions={init.sessions}
      initialDb={init.db}
      databases={init.databases}
    />
  );
}

function Workspace({
  initialSessions,
  initialDb,
  databases,
}: {
  initialSessions: SessionInfo[];
  initialDb: string;
  databases: DatabaseInfo[];
}) {
  const [mode, setMode] = useState<ViewMode>("galaxy");
  const [db, setDb] = useState(initialDb);
  const dbRef = useRef(db);
  dbRef.current = db;
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
    const reqDb = dbRef.current;
    api
      .getGraph(session)
      .then((data) => {
        if (reqId !== graphRequestId.current || reqDb !== dbRef.current) return;
        setGraphData(data);
        setError(null);
      })
      .catch((e) => {
        if (reqId !== graphRequestId.current || reqDb !== dbRef.current) return;
        setError(e.message);
      })
      .finally(() => {
        if (reqId === graphRequestId.current && reqDb === dbRef.current) setLoading(false);
      });

    api
      .getReviews()
      .then((items) => setPendingReviewCount(items.length))
      .catch((e) => console.error("Failed to fetch review count", e));
  }, [session]);

  const inspectRequestId = useRef(0);

  const inspectNode = useCallback((nodeId: number) => {
    const reqId = ++inspectRequestId.current;
    const reqDb = dbRef.current;
    api
      .getConcept(nodeId, session)
      .then((detail) => {
        if (reqId !== inspectRequestId.current || reqDb !== dbRef.current) return;
        setInspectedConcept(detail);
        setSidebarOpen(true);
      })
      .catch((e) => {
        // 单次 inspect 失败不该清空整张图，只是没法打开侧栏；记录即可。
        if (reqId !== inspectRequestId.current || reqDb !== dbRef.current) return;
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

  // 刚进入主界面、以及每次切换会话/库时：按当前会话重新取整张图（顺带刷新待审数量），
  // 侧栏开着的节点也按新会话刷新。
  // 依赖故意只写 session 和 db：侧栏换了别的节点不该触发重新取图。
  useEffect(() => {
    reloadGraph();
    if (sidebarOpen && inspectedConcept) inspectNode(inspectedConcept.id);
  }, [session, db]);

  // 切库：所有状态按新库重来。会话 id 两边库可能撞车（devonly 两边都有），
  // 所以即使新库第一个会话和旧值相同，也要强制重拉（见下面的手动 reloadGraph）。
  const handleDbChange = useCallback((next: string) => {
    if (next === dbRef.current) return;
    // 立刻废弃尚未完成的旧库请求，避免旧库的结果随后覆盖新库的界面。
    graphRequestId.current += 1;
    inspectRequestId.current += 1;
    setDbModule(next);
    try {
      localStorage.setItem("horon.db", next);
    } catch {
      // 隐私模式写不进 localStorage：无视，下次回服务端缺省。
    }
    const url = new URL(window.location.href);
    url.searchParams.set("db", next);
    window.history.replaceState(null, "", url);
    setDb(next);
    setGraphData(null);
    setFocalId(null);
    setInspectedConcept(null);
    setSidebarOpen(false);
    setMode("galaxy");
    setLoading(true);
    setError(null);
    api
      .getSessions()
      .then((freshSessions) => {
        if (dbRef.current !== next) return; // 切库中又切了一次，旧结果丢掉。
        if (freshSessions.length === 0) {
          setError("No sessions reported by API");
          setLoading(false);
          return;
        }
        setSessions(freshSessions);
        setSession(freshSessions[0].session_id);
        if (freshSessions[0].session_id === sessionRef.current) reloadGraph();
      })
      .catch((e) => {
        if (dbRef.current !== next) return;
        setError(e.message);
        setLoading(false);
      });
  }, [reloadGraph]);

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
          {/* 当前库：颜色徽 + 下拉。各库记忆/闸门/会话完全隔离，切库清空重拉。 */}
          <span
            className="db-badge"
            style={{ background: dbColor(db) }}
            title={`当前数据库：${db}`}
          />
          <select
            className="db-select"
            title="切换数据库（各库记忆/闸门/会话完全隔离）"
            value={db}
            onChange={(e) => handleDbChange(e.target.value)}
          >
            {databases.map((d) => (
              <option key={d.alias} value={d.alias}>
                {d.alias}
              </option>
            ))}
          </select>
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
