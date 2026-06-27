import { useCallback, useRef, useEffect, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import type { GraphData, GraphNode } from "../types";

interface Props {
  data: GraphData;
  onNodeClick: (id: number) => void;
  onNodeHover: (id: number) => void;
}


export default function GalaxyView({ data, onNodeClick, onNodeHover }: Props) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  const [hoveredId, setHoveredId] = useState<number | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      setDimensions({ width, height });
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);

  // 卸载时清掉悬停定时器，否则切换到解剖视图后这个挂起的定时器仍会
  // 触发 onNodeHover，把已经离开的节点重新塞进 Inspector 侧栏。
  useEffect(() => {
    return () => {
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fg.d3Force("charge")?.strength(-40);
    fg.d3Force("link")?.distance(45).strength(0.4);
    fg.d3Force("center")?.strength(0.1);
  }, [data]);

  const maxDegree = Math.max(1, ...data.nodes.map((n) => n.degree));

  const nodeCanvasObject = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const t = node.degree / maxDegree;
      const radius = 2 + t * 10;
      const isHovered = node.id === hoveredId;

      // degree 越高，颜色从冷蓝 (74,158,255) 渐变到暖色高亮，三个通道都要随 t 移动。
      const r = Math.round(74 + t * 81);
      const g = Math.round(158 - t * 49);
      const b = Math.round(255 - t * 120);

      if (isHovered || t > 0.3) {
        ctx.beginPath();
        ctx.arc(x, y, radius * 2.5, 0, 2 * Math.PI);
        const grad = ctx.createRadialGradient(
          x, y, 0,
          x, y, radius * 2.5,
        );
        grad.addColorStop(0, `rgba(${r},${g},${b},${isHovered ? 0.25 : 0.12})`);
        grad.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = grad;
        ctx.fill();
      }

      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fill();

      if (globalScale > 1.5 || isHovered) {
        const fontSize = Math.max(10 / globalScale, 2.5);
        ctx.font = `${fontSize}px Inter, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = isHovered
          ? "rgba(255,255,255,0.95)"
          : "rgba(255,255,255,0.6)";
        ctx.fillText(node.name, x, y + radius + 2);
      }
    },
    [maxDegree, hoveredId],
  );

  const handleNodeHover = useCallback(
    (node: GraphNode | null) => {
      setHoveredId(node?.id ?? null);
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
      if (node) {
        hoverTimerRef.current = setTimeout(() => {
          onNodeHover(node.id);
        }, 400);
      }
    },
    [onNodeHover],
  );

  return (
    <div ref={containerRef} style={{ width: "100%", height: "100%" }}>
      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={data as any}
        nodeId="id"
        nodeCanvasObject={nodeCanvasObject as any}
        nodePointerAreaPaint={((node: any, color: string, ctx: CanvasRenderingContext2D) => {
          const r = 2 + ((node as GraphNode).degree / maxDegree) * 10;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, r + 4, 0, 2 * Math.PI);
          ctx.fill();
        }) as any}
        linkVisibility={false}
        nodeLabel={() => ""}
        onNodeClick={(node: any) => onNodeClick((node as GraphNode).id)}
        onNodeHover={handleNodeHover as any}
        backgroundColor="#0a0a0f"
        cooldownTicks={200}
        warmupTicks={50}
      />
    </div>
  );
}
