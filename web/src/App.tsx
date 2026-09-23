import { useState, useEffect, useCallback, useRef } from "react";
import { api } from "./api";
import type { GraphData, ViewMode, ConceptDetail } from "./types";
import GalaxyView from "./components/GalaxyView";
import DissectionView from "./components/DissectionView";
import InspectorSidebar from "./components/InspectorSidebar";
import SearchBar from "./components/SearchBar";
import ReviewView from "./components/ReviewView";
import "./App.css";

export default function App() {
  const [mode, setMode] = useState<ViewMode>("galaxy");
  const [graphData, setGraphData] = useState<GraphData | null>(null);
  const [focalId, setFocalId] = useState<number | null>(null);
  const [inspectedConcept, setInspectedConcept] =
    useState<ConceptDetail | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [pendingReviewCount, setPendingReviewCount] = useState<number>(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const reloadGraph = useCallback(() => {
    api
      .getGraph()
      .then(setGraphData)
      .catch((e) => setError(e.message));

    api
      .getReviews()
      .then((items) => setPendingReviewCount(items.length))
      .catch((e) => console.error("Failed to fetch review count", e));
  }, []);

  useEffect(() => {
    api.getGraph()
      .then(setGraphData)
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));

    api.getReviews()
      .then((items) => setPendingReviewCount(items.length))
      .catch((e) => console.error("Failed to fetch review count", e));
  }, []);

  const inspectRequestId = useRef(0);

  const inspectNode = useCallback((nodeId: number) => {
    const reqId = ++inspectRequestId.current;
    api
      .getConcept(nodeId)
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
  }, []);

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

  if (loading) {
    return (
      <div className="loading-screen">
        <div className="loading-spinner" />
        <p>Loading graph...</p>
      </div>
    );
  }

  if (error) {
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
            onNavigate={handleNodeClick}
            onInspect={handleInspect}
            onBack={handleBackToGalaxy}
          />
        )}
        {mode === "review" && (
          <ReviewView
            onRefreshGraph={reloadGraph}
            onNavigateToNode={(nodeId) => {
              setFocalId(nodeId);
              setMode("dissection");
              inspectNode(nodeId);
            }}
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
