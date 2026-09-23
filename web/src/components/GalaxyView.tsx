import { useCallback, useRef, useEffect, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { forceCollide } from "d3-force";
import { formatBytes, type GraphData, type GraphNode } from "../types";

interface Props {
  data: GraphData;
  onNodeClick: (id: number) => void;
  onNodeHover: (id: number) => void;
}

interface TagCentroid {
  x: number;
  y: number;
  count: number;
}

/**
 * 恒星色温光谱平滑映射：
 * 0 ~ 1.5 KB: 静谧冷蓝 (rgb(74, 158, 255))
 * 1.5 ~ 3.2 KB: 冰青至星白 (rgb(120, 205, 255) -> rgb(220, 235, 255))
 * 3.2 ~ 4.8 KB: 暖金至琥珀橙 (rgb(245, 190, 85) -> rgb(251, 146, 60))
 * >= 4.8 KB: 炽热珊瑚红至赤红 (rgb(248, 113, 113) -> rgb(239, 68, 68))
 */
function getStarColor(byteSize: number = 0): {
  r: number;
  g: number;
  b: number;
  isOverweight: boolean;
  heatRatio: number;
} {
  const kb = byteSize / 1024;
  let r = 74;
  let g = 158;
  let b = 255;
  const isOverweight = byteSize >= 4800; // >= 4.8 KB 视为超重
  let heatRatio = 0;

  if (kb <= 1.5) {
    const p = Math.max(0, kb / 1.5);
    r = Math.round(74 + p * 16);
    g = Math.round(158 + p * 22);
    b = 255;
    heatRatio = 0;
  } else if (kb <= 3.2) {
    const p = (kb - 1.5) / 1.7;
    r = Math.round(90 + p * 130);
    g = Math.round(180 + p * 55);
    b = 255;
    heatRatio = p * 0.25;
  } else if (kb <= 4.8) {
    const p = (kb - 3.2) / 1.6;
    r = Math.round(220 + p * 31);
    g = Math.round(235 - p * 89);
    b = Math.round(255 - p * 195);
    heatRatio = 0.25 + p * 0.5;
  } else {
    const p = Math.min(1, (kb - 4.8) / 3.2);
    r = Math.round(251 - p * 12);
    g = Math.round(146 - p * 78);
    b = Math.round(60 + p * 8);
    heatRatio = 0.75 + p * 0.25;
  }

  return { r, g, b, isOverweight, heatRatio };
}

function drawRoundRect(
  ctx: CanvasRenderingContext2D,
  x: number,
  y: number,
  w: number,
  h: number,
  r: number,
) {
  if (typeof ctx.roundRect === "function") {
    ctx.beginPath();
    ctx.roundRect(x, y, w, h, r);
    return;
  }
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.lineTo(x + w - r, y);
  ctx.arcTo(x + w, y, x + w, y + r, r);
  ctx.lineTo(x + w, y + h - r);
  ctx.arcTo(x + w, y + h, x + w - r, y + h, r);
  ctx.lineTo(x + r, y + h);
  ctx.arcTo(x, y + h, x, y + h - r, r);
  ctx.lineTo(x, y + r);
  ctx.arcTo(x, y, x + r, y, r);
  ctx.closePath();
}

/**
 * 自定义同 Tag 聚类力：
 * 1. 统计各 Tag 的空间质心；
 * 2. 给予温和的向心拉力，配合 forceCollide 形成松散舒展的星团（Nebula）；
 * 3. 将 centroids 暴露给渲染层，用于远景呈现 Tag 星团徽章。
 */
function createTagClusterForce(initialStrength = 0.18) {
  let nodes: GraphNode[] = [];
  let strength = initialStrength;
  const centroids = new Map<string, TagCentroid>();

  function force(alpha: number) {
    if (!nodes || nodes.length === 0) return;

    centroids.clear();
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (!node.tags || node.tags.length === 0) continue;
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      for (let j = 0; j < node.tags.length; j++) {
        const tag = node.tags[j];
        let c = centroids.get(tag);
        if (!c) {
          c = { x: 0, y: 0, count: 0 };
          centroids.set(tag, c);
        }
        c.x += x;
        c.y += y;
        c.count += 1;
      }
    }

    for (const c of centroids.values()) {
      if (c.count > 0) {
        c.x /= c.count;
        c.y /= c.count;
      }
    }

    const k = strength * alpha;
    for (let i = 0; i < nodes.length; i++) {
      const node = nodes[i];
      if (!node.tags || node.tags.length === 0) continue;
      const nx = node.x ?? 0;
      const ny = node.y ?? 0;

      let targetX = 0;
      let targetY = 0;
      let validCount = 0;

      for (let j = 0; j < node.tags.length; j++) {
        const tag = node.tags[j];
        const c = centroids.get(tag);
        if (c && c.count >= 2) {
          targetX += c.x;
          targetY += c.y;
          validCount += 1;
        }
      }

      if (validCount > 0) {
        targetX /= validCount;
        targetY /= validCount;

        // 柔和施加向心加速度
        node.vx = (node.vx ?? 0) + (targetX - nx) * k;
        node.vy = (node.vy ?? 0) + (targetY - ny) * k;
      }
    }
  }

  force.initialize = (_nodes: GraphNode[]) => {
    nodes = _nodes;
  };

  force.strength = (_strength?: number) => {
    if (_strength === undefined) return strength;
    strength = _strength;
    return force;
  };

  force.getCentroids = () => centroids;

  return force;
}

export default function GalaxyView({ data, onNodeClick, onNodeHover }: Props) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const tagForceRef = useRef<ReturnType<typeof createTagClusterForce> | null>(null);

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

  useEffect(() => {
    return () => {
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
    };
  }, []);

  const maxDegree = Math.max(1, ...data.nodes.map((n) => n.degree));

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;

    const tagForce = createTagClusterForce(0.16);
    tagForceRef.current = tagForce;

    // 1. 排斥力：适度拉开距离，避免大节点和孤立节点堆叠
    fg.d3Force("charge")?.strength(-45).distanceMax(350);

    // 2. 连线力：保持因果/组合关系的自然张力
    fg.d3Force("link")?.distance(55).strength(0.5);

    // 3. 中心引力：轻柔聚拢在画布中央
    fg.d3Force("center")?.strength(0.06);

    // 4. 同 Tag 聚类力：使同一 Tag 成员自然收拢为一个星座/星云
    fg.d3Force("tagCluster", tagForce);

    // 5. 碰撞力：给每个节点分配物理半径，防止同 Tag 成员坍缩重叠在同一个点！
    fg.d3Force(
      "collide",
      forceCollide((node: any) => {
        const t = ((node as GraphNode).degree || 0) / maxDegree;
        const r = 2 + t * 10;
        return r + 14; // 留出充足的物理间隙
      }).iterations(2),
    );

    fg.d3ReheatSimulation?.();
  }, [data, maxDegree]);

  // 基础节点渲染（星辰光芒与分级文字）
  const nodeCanvasObject = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const t = node.degree / maxDegree;
      const radius = 2 + t * 10;
      const isHovered = hoveredNode?.id === node.id;
      const { r, g, b, isOverweight, heatRatio } = getStarColor(node.byte_size || 0);

      // 节点外围光晕 (Hover、大连接度骨干节点、或升温发热的大记忆)
      if (isHovered || t > 0.25 || isOverweight || heatRatio > 0.5) {
        ctx.beginPath();
        const glowMult = isHovered ? 3.2 : (2.0 + heatRatio * 0.8);
        ctx.arc(x, y, radius * glowMult, 0, 2 * Math.PI);
        const grad = ctx.createRadialGradient(
          x,
          y,
          0,
          x,
          y,
          radius * glowMult,
        );
        const alpha = isHovered ? 0.38 : (0.08 + heatRatio * 0.18);
        grad.addColorStop(0, `rgba(${r},${g},${b},${alpha})`);
        grad.addColorStop(1, "rgba(0,0,0,0)");
        ctx.fillStyle = grad;
        ctx.fill();
      }

      // 核心星点
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fill();

      if (isOverweight) {
        ctx.strokeStyle = "rgba(239, 68, 68, 0.8)";
        ctx.lineWidth = 1.2 / globalScale;
        ctx.stroke();
      }

      if (isHovered) {
        ctx.strokeStyle = "#ffffff";
        ctx.lineWidth = 1.5 / globalScale;
        ctx.stroke();
      }

      // LOD 分级渲染文字（Hover 节点的名字交由 onRenderFramePost 全局置顶渲染）
      if (!isHovered) {
        let labelToShow = "";
        let showLabel = false;
        let alpha = 0.75;

        if (globalScale >= 1.25) {
          // 近景特写：全量显示所有节点完整名称
          showLabel = true;
          labelToShow = node.name;
          alpha = 0.85;
        } else if (globalScale >= 0.7) {
          // 中景探索：骨干节点显示全名，孤立节点截断避免文字糊墙
          showLabel = true;
          if (node.degree > 0) {
            labelToShow = node.name;
            alpha = 0.85;
          } else {
            labelToShow =
              node.name.length > 7 ? `${node.name.slice(0, 6)}…` : node.name;
            alpha = Math.max(0.4, Math.min(0.75, (globalScale - 0.7) * 1.5));
          }
        } else if (globalScale >= 0.45 && node.degree >= 3) {
          // 远景宏观：仅极少数核心大骨干节点显示短标签
          showLabel = true;
          labelToShow =
            node.name.length > 6 ? `${node.name.slice(0, 5)}…` : node.name;
          alpha = 0.55;
        }

        if (showLabel && labelToShow) {
          const screenFontSize = 10;
          const fontSize = Math.max(screenFontSize / globalScale, 2.5);
          ctx.font = `400 ${fontSize}px Inter, -apple-system, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";

          const textY = y + radius + 3 / globalScale;

          // 微弱深色文字轮廓
          ctx.strokeStyle = `rgba(10, 10, 15, ${alpha + 0.1})`;
          ctx.lineWidth = 2.2 / globalScale;
          ctx.lineJoin = "round";
          ctx.strokeText(labelToShow, x, textY);

          ctx.fillStyle = `rgba(220, 230, 250, ${alpha})`;
          ctx.fillText(labelToShow, x, textY);
        }
      }
    },
    [maxDegree, hoveredNode],
  );

  // 全局置顶渲染：远景 Tag 星团名称 + Hover 焦点卡片
  const onRenderFramePost = useCallback(
    (ctx: CanvasRenderingContext2D, globalScale: number) => {
      // 1. 远景宏观视角下，绘制各 Tag 星团名称（宏观星云图例）
      if (globalScale < 0.85 && tagForceRef.current) {
        const centroids = tagForceRef.current.getCentroids();
        const tagAlpha = Math.min(
          0.85,
          Math.max(0.1, (0.85 - globalScale) * 1.8),
        );

        centroids.forEach((c, tag) => {
          // 仅对成员数 >= 3 的有意义聚合星团显示标题
          if (c.count < 3) return;

          const text = `◈ ${tag} · ${c.count}`;
          const screenFontSize = 11;
          const fontSize = Math.max(screenFontSize / globalScale, 3);
          ctx.font = `600 ${fontSize}px Inter, -apple-system, sans-serif`;
          ctx.textAlign = "center";
          ctx.textBaseline = "middle";

          const metrics = ctx.measureText(text);
          const paddingX = 8 / globalScale;
          const paddingY = 4 / globalScale;
          const boxW = metrics.width + paddingX * 2;
          const boxH = fontSize * 1.4 + paddingY * 2;
          const boxX = c.x - boxW / 2;
          const boxY = c.y - boxH / 2;

          // 绘制半透明星云徽章背景
          ctx.fillStyle = `rgba(16, 20, 36, ${tagAlpha * 0.85})`;
          drawRoundRect(ctx, boxX, boxY, boxW, boxH, 4 / globalScale);
          ctx.fill();

          ctx.strokeStyle = `rgba(90, 130, 230, ${tagAlpha * 0.5})`;
          ctx.lineWidth = 1 / globalScale;
          ctx.stroke();

          // 绘制文字
          ctx.fillStyle = `rgba(200, 225, 255, ${tagAlpha})`;
          ctx.fillText(text, c.x, c.y);
        });
      }

      // 2. 悬停（Hover）节点置顶高亮卡片（保证不被任何其他节点/连线遮挡）
      if (hoveredNode && hoveredNode.x !== undefined && hoveredNode.y !== undefined) {
        const node = hoveredNode;
        const x = node.x ?? 0;
        const y = node.y ?? 0;
        const t = (node.degree || 0) / maxDegree;
        const radius = 2 + t * 10;

        const mainText = node.name;
        const isOverweight = Boolean(node.byte_size && node.byte_size >= 4800);
        const sizeText = node.byte_size ? (isOverweight ? `⚠ ${formatBytes(node.byte_size)}` : formatBytes(node.byte_size)) : "";
        const tagText =
          node.tags && node.tags.length > 0 ? `#${node.tags.join(" #")}` : "";

        const subParts: string[] = [];
        if (sizeText) subParts.push(sizeText);
        if (tagText) subParts.push(tagText);
        const subText = subParts.join("  ·  ");

        const titleFontSize = Math.max(12 / globalScale, 3.5);
        const subFontSize = Math.max(9.5 / globalScale, 2.8);

        ctx.font = `600 ${titleFontSize}px Inter, -apple-system, sans-serif`;
        const mainWidth = ctx.measureText(mainText).width;

        let subWidth = 0;
        if (subText) {
          ctx.font = `400 ${subFontSize}px Inter, -apple-system, sans-serif`;
          subWidth = ctx.measureText(subText).width;
        }

        const contentWidth = Math.max(mainWidth, subWidth);
        const padX = 10 / globalScale;
        const padY = 6 / globalScale;
        const cardW = contentWidth + padX * 2;
        const cardH =
          (subText ? titleFontSize + subFontSize + 5 / globalScale : titleFontSize) +
          padY * 2;

        const cardX = x - cardW / 2;
        const cardY = y - radius - cardH - 6 / globalScale;

        // 卡片阴影与背景
        ctx.fillStyle = "rgba(8, 12, 24, 0.94)";
        drawRoundRect(ctx, cardX, cardY, cardW, cardH, 5 / globalScale);
        ctx.fill();

        ctx.strokeStyle = isOverweight ? "rgba(239, 68, 68, 0.85)" : "rgba(100, 160, 255, 0.7)";
        ctx.lineWidth = 1.2 / globalScale;
        ctx.stroke();

        // 卡片内标题文字
        ctx.font = `600 ${titleFontSize}px Inter, -apple-system, sans-serif`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = "#ffffff";
        ctx.fillText(mainText, x, cardY + padY);

        // 卡片内副标题（体积 / 标签）
        if (subText) {
          ctx.font = `400 ${subFontSize}px Inter, -apple-system, sans-serif`;
          ctx.fillStyle = isOverweight ? "rgba(248, 113, 113, 0.95)" : "rgba(140, 190, 255, 0.85)";
          ctx.fillText(
            subText,
            x,
            cardY + padY + titleFontSize + 4 / globalScale,
          );
        }
      }
    },
    [hoveredNode, maxDegree],
  );

  const handleNodeHover = useCallback(
    (node: GraphNode | null) => {
      setHoveredNode(node);
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
      if (node) {
        hoverTimerRef.current = setTimeout(() => {
          onNodeHover(node.id);
        }, 350);
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
        onRenderFramePost={onRenderFramePost as any}
        nodePointerAreaPaint={((node: any, color: string, ctx: CanvasRenderingContext2D) => {
          const r = 2 + ((node as GraphNode).degree / maxDegree) * 10;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, r + 6, 0, 2 * Math.PI);
          ctx.fill();
        }) as any}
        linkVisibility={false}
        nodeLabel={() => ""}
        onNodeClick={(node: any) => onNodeClick((node as GraphNode).id)}
        onNodeHover={handleNodeHover as any}
        backgroundColor="#0a0a0f"
        cooldownTicks={280}
        warmupTicks={70}
      />
    </div>
  );
}
