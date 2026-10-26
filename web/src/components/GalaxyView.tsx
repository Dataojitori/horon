import { useCallback, useRef, useEffect, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { forceCollide } from "d3-force";
import { formatBytes, type GraphData, type GraphNode } from "../types";
import { useTheme, type Theme } from "../theme";
import "./GalaxyView.css";

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

type RGB = readonly [number, number, number];

// 与 index.css 里的 --font-sans 同一套（canvas 读不到变量）。
// 中日文走系统字体，所以后面要带上各平台的 CJK 回退，否则中文节点名会掉到无衬线默认字体。
const CANVAS_FONT =
  'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Yu Gothic UI", sans-serif';

/**
 * 画布调色板。canvas 读不到 CSS 变量，所以两套主题的颜色在这里各写一份。
 *
 * 节点按正文体积着色，色阶是 [KB, 颜色] 的锚点，相邻锚点之间线性插值：
 * - 暗色（恒星色温）：冷蓝 → 冰青 → 星白 → 琥珀橙 → 赤红
 * - 亮色（同一方向，换成奶油底上看得清的柔和色）：薰衣草蓝 → 雾蓝 → 鼠尾草绿 → 琥珀 → 珊瑚红
 * 体积 ≥ 4.8 KB 视为超重，两套主题都会加一圈红色描边。
 */
interface Palette {
  /** 亮底：不用叠加发光，改画接触阴影 + 高光的“珍珠”质感 */
  pearl: boolean;
  /** 节点视觉半径倍数。亮底上没有发光撑场面，小点要略放大才有存在感 */
  nodeScale: number;
  stops: ReadonlyArray<readonly [number, RGB]>;
  /** 节点名：文字色与描边色 */
  label: (alpha: number) => { fill: string; stroke: string };
  /** 远景 Tag 星团徽章 */
  badge: (alpha: number) => { bg: string; border: string; text: string };
  /** 悬停卡片 */
  card: {
    bg: string;
    border: string;
    borderWarn: string;
    title: string;
    sub: string;
    subWarn: string;
    shadow: string | null;
  };
  overweightRing: string;
  hoverRing: string;
}

/**
 * 远景徽章的淡入：传进来的 a 随缩放在 0.1~0.85 之间变化，
 * 直接拿它当文字透明度，缩放到 0.5~0.7 时字会被压到看不清（暗色上合成出锈红色，对比度不足 4:1）。
 * 这里把它压成 0.25~1：刚出现时仍是淡入，到常用缩放区间就接近不透明。
 */
function badgeFade(a: number): number {
  return 0.25 + 0.75 * Math.min(1, a / 0.85);
}

const PALETTES: Record<Theme, Palette> = {
  dark: {
    pearl: false,
    nodeScale: 1,
    // 夜灯配色：薰衣草灰 → 暖奶油 → 柔和珊瑚 → 玫瑰红，不用荧光蓝和纯红。
    // 偏暖的一端比之前提亮：这些点只有 2~4px，颜色太沉会和梅紫底糊成一片锈色。
    stops: [
      [0, [160, 150, 214]],
      [1.5, [138, 164, 226]],
      [3.2, [238, 214, 178]],
      [4.8, [248, 168, 128]],
      [8, [240, 128, 142]],
    ],
    label: (a) => ({
      fill: `rgba(236, 227, 221, ${Math.min(1, a + 0.2)})`,
      stroke: `rgba(26, 21, 25, ${Math.min(1, a + 0.2)})`,
    }),
    badge: (a) => {
      const f = badgeFade(a);
      return {
        bg: `rgba(52, 40, 50, ${0.6 + 0.35 * f})`,
        border: `rgba(240, 140, 118, ${0.25 + 0.45 * f})`,
        // 奶油白而不是珊瑚色：周围满是珊瑚色的点，同色系的字会和它们混在一起
        text: `rgba(246, 236, 229, ${f})`,
      };
    },
    card: {
      bg: "rgba(46, 36, 45, 0.96)",
      border: "rgba(240, 140, 118, 0.5)",
      borderWarn: "rgba(226, 116, 124, 0.75)",
      title: "#ece3dd",
      sub: "rgba(179, 164, 171, 0.95)",
      subWarn: "rgba(235, 160, 166, 0.95)",
      shadow: "rgba(12, 6, 10, 0.5)",
    },
    overweightRing: "rgba(240, 128, 142, 0.9)",
    hoverRing: "rgb(236, 227, 221)",
  },
  light: {
    pearl: true,
    nodeScale: 1.3,
    // 大多数节点是体积小的，用安静的薰衣草灰；越“重”越暖，红色只留给超重的，一眼能找到
    stops: [
      [0, [160, 144, 198]],
      [1.5, [132, 152, 208]],
      [3.2, [226, 178, 106]],
      [4.8, [232, 124, 88]],
      [8, [204, 58, 72]],
    ],
    label: (a) => ({
      fill: `rgba(58, 42, 52, ${Math.min(1, a + 0.15)})`,
      stroke: "rgba(250, 243, 235, 0.92)",
    }),
    badge: (a) => {
      const f = badgeFade(a);
      return {
        bg: `rgba(255, 252, 248, ${0.6 + 0.35 * f})`,
        border: `rgba(226, 99, 76, ${0.25 + 0.4 * f})`,
        text: `rgba(140, 48, 32, ${f})`,
      };
    },
    card: {
      bg: "rgba(255, 255, 255, 0.97)",
      border: "rgba(226, 99, 76, 0.6)",
      borderWarn: "rgba(214, 69, 80, 0.75)",
      title: "#3a2a34",
      sub: "#6f5c67",
      subWarn: "#bc323d",
      shadow: "rgba(74, 52, 64, 0.28)",
    },
    overweightRing: "rgba(214, 69, 80, 0.85)",
    hoverRing: "rgb(226, 99, 76)",
  },
};

/** 按体积在色阶锚点间插值；heatRatio（0~1）决定节点光晕的强弱，和主题无关。 */
function getStarColor(
  byteSize: number = 0,
  stops: Palette["stops"],
): {
  r: number;
  g: number;
  b: number;
  isOverweight: boolean;
  heatRatio: number;
} {
  const kb = byteSize / 1024;
  const isOverweight = byteSize >= 4800; // >= 4.8 KB 视为超重

  // 光晕强弱：1.5KB 以下无；1.5~3.2 升到 0.25；3.2~4.8 升到 0.75；之后升到 1
  let heatRatio = 0;
  if (kb > 4.8) heatRatio = 0.75 + Math.min(1, (kb - 4.8) / 3.2) * 0.25;
  else if (kb > 3.2) heatRatio = 0.25 + ((kb - 3.2) / 1.6) * 0.5;
  else if (kb > 1.5) heatRatio = ((kb - 1.5) / 1.7) * 0.25;

  let r: number = stops[0][1][0];
  let g: number = stops[0][1][1];
  let b: number = stops[0][1][2];
  if (kb >= stops[stops.length - 1][0]) {
    [r, g, b] = stops[stops.length - 1][1];
  } else if (kb > 0) {
    for (let i = 1; i < stops.length; i++) {
      const [k1, c1] = stops[i];
      if (kb <= k1) {
        const [k0, c0] = stops[i - 1];
        const p = (kb - k0) / (k1 - k0);
        r = Math.round(c0[0] + (c1[0] - c0[0]) * p);
        g = Math.round(c0[1] + (c1[1] - c0[1]) * p);
        b = Math.round(c0[2] + (c1[2] - c0[2]) * p);
        break;
      }
    }
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
  const { theme } = useTheme();
  const palette = PALETTES[theme];

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
      const radius = (2 + t * 10) * palette.nodeScale;
      const isHovered = hoveredNode?.id === node.id;
      const { r, g, b, isOverweight, heatRatio } = getStarColor(
        node.byte_size || 0,
        palette.stops,
      );

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
        // 亮底上光晕要更淡，否则像一团脏污
        const k = palette.pearl ? 0.7 : 1;
        const alpha = (isHovered ? 0.38 : (0.08 + heatRatio * 0.18)) * k;
        grad.addColorStop(0, `rgba(${r},${g},${b},${alpha})`);
        // 终点用同色透明：渐变在非预乘空间插值，用黑色透明会在亮底上洇出一圈灰
        grad.addColorStop(1, `rgba(${r},${g},${b},0)`);
        ctx.fillStyle = grad;
        ctx.fill();
      }

      // 亮底：先垫一层很淡的接触阴影，节点才像“放在纸上”而不是“浮在黑里”
      if (palette.pearl) {
        ctx.beginPath();
        ctx.arc(x, y + radius * 0.22, radius * 1.04, 0, 2 * Math.PI);
        ctx.fillStyle = "rgba(74, 52, 64, 0.16)";
        ctx.fill();
      }

      // 核心星点
      ctx.beginPath();
      ctx.arc(x, y, radius, 0, 2 * Math.PI);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fill();

      if (isOverweight) {
        ctx.strokeStyle = palette.overweightRing;
        ctx.lineWidth = 1.2 / globalScale;
        ctx.stroke();
      }

      if (isHovered) {
        ctx.strokeStyle = palette.hoverRing;
        ctx.lineWidth = 1.5 / globalScale;
        ctx.stroke();
      }

      // 亮底：较大的节点左上角点一小块高光
      if (palette.pearl && radius >= 4) {
        ctx.beginPath();
        ctx.arc(x - radius * 0.32, y - radius * 0.36, radius * 0.26, 0, 2 * Math.PI);
        ctx.fillStyle = "rgba(255, 255, 255, 0.5)";
        ctx.fill();
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
          ctx.font = `400 ${fontSize}px ${CANVAS_FONT}`;
          ctx.textAlign = "center";
          ctx.textBaseline = "top";

          const textY = y + radius + 3 / globalScale;

          // 文字外轮廓：压住背后的星点，保证可读
          const { fill, stroke } = palette.label(alpha);
          ctx.strokeStyle = stroke;
          ctx.lineWidth = 2.2 / globalScale;
          ctx.lineJoin = "round";
          ctx.strokeText(labelToShow, x, textY);

          ctx.fillStyle = fill;
          ctx.fillText(labelToShow, x, textY);
        }
      }
    },
    [maxDegree, hoveredNode, palette],
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

        const screenFontSize = 11;
        const fontSize = Math.max(screenFontSize / globalScale, 3);
        ctx.font = `600 ${fontSize}px ${CANVAS_FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";

        const paddingX = 8 / globalScale;
        const paddingY = 4 / globalScale;
        const boxH = fontSize * 1.4 + paddingY * 2;

        // 仅对成员数 >= 3 的有意义聚合星团显示标题；成员多的优先
        const candidates: { tag: string; c: TagCentroid }[] = [];
        centroids.forEach((c, tag) => {
          if (c.count >= 3) candidates.push({ tag, c });
        });
        candidates.sort((a, b) => b.c.count - a.c.count);

        // 徽章互相压住时只留成员更多的那个，缩放后空间变大，被省掉的会自己出现
        const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];
        const gap = 4 / globalScale;

        for (const { tag, c } of candidates) {
          const text = `◈ ${tag} · ${c.count}`;
          const boxW = ctx.measureText(text).width + paddingX * 2;
          const x0 = c.x - boxW / 2;
          const y0 = c.y - boxH / 2;
          const x1 = x0 + boxW;
          const y1 = y0 + boxH;

          const overlaps = placed.some(
            (p) => x0 < p.x1 + gap && x1 > p.x0 - gap && y0 < p.y1 + gap && y1 > p.y0 - gap,
          );
          if (overlaps) continue;
          placed.push({ x0, y0, x1, y1 });

          // 绘制半透明星云徽章背景
          const badge = palette.badge(tagAlpha);
          ctx.fillStyle = badge.bg;
          drawRoundRect(ctx, x0, y0, boxW, boxH, boxH / 2);
          ctx.fill();

          ctx.strokeStyle = badge.border;
          ctx.lineWidth = 1 / globalScale;
          ctx.stroke();

          // 绘制文字
          ctx.fillStyle = badge.text;
          ctx.fillText(text, c.x, c.y);
        }
      }

      // 2. 悬停（Hover）节点置顶高亮卡片（保证不被任何其他节点/连线遮挡）
      if (hoveredNode && hoveredNode.x !== undefined && hoveredNode.y !== undefined) {
        const node = hoveredNode;
        const x = node.x ?? 0;
        const y = node.y ?? 0;
        const t = (node.degree || 0) / maxDegree;
        const radius = (2 + t * 10) * palette.nodeScale;

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

        ctx.font = `600 ${titleFontSize}px ${CANVAS_FONT}`;
        const mainWidth = ctx.measureText(mainText).width;

        let subWidth = 0;
        if (subText) {
          ctx.font = `400 ${subFontSize}px ${CANVAS_FONT}`;
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
        const card = palette.card;
        ctx.save();
        if (card.shadow) {
          ctx.shadowColor = card.shadow;
          ctx.shadowBlur = 14;
          ctx.shadowOffsetY = 4;
        }
        ctx.fillStyle = card.bg;
        drawRoundRect(ctx, cardX, cardY, cardW, cardH, 5 / globalScale);
        ctx.fill();
        ctx.restore();

        ctx.strokeStyle = isOverweight ? card.borderWarn : card.border;
        ctx.lineWidth = 1.2 / globalScale;
        ctx.stroke();

        // 卡片内标题文字
        ctx.font = `600 ${titleFontSize}px ${CANVAS_FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.fillStyle = card.title;
        ctx.fillText(mainText, x, cardY + padY);

        // 卡片内副标题（体积 / 标签）
        if (subText) {
          ctx.font = `400 ${subFontSize}px ${CANVAS_FONT}`;
          ctx.fillStyle = isOverweight ? card.subWarn : card.sub;
          ctx.fillText(
            subText,
            x,
            cardY + padY + titleFontSize + 4 / globalScale,
          );
        }
      }
    },
    [hoveredNode, maxDegree, palette],
  );

  // 主题切换、字体加载完成后，静止的画布不会自己重画，手动刷一帧
  useEffect(() => {
    fgRef.current?.refresh?.();
  }, [palette]);

  useEffect(() => {
    let cancelled = false;
    document.fonts?.ready.then(() => {
      if (!cancelled) fgRef.current?.refresh?.();
    });
    return () => {
      cancelled = true;
    };
  }, []);

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
    <div ref={containerRef} className="galaxy-stage">
      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={data as any}
        nodeId="id"
        nodeCanvasObject={nodeCanvasObject as any}
        onRenderFramePost={onRenderFramePost as any}
        nodePointerAreaPaint={((node: any, color: string, ctx: CanvasRenderingContext2D) => {
          const r = (2 + ((node as GraphNode).degree / maxDegree) * 10) * palette.nodeScale;
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, r + 6, 0, 2 * Math.PI);
          ctx.fill();
        }) as any}
        linkVisibility={false}
        nodeLabel={() => ""}
        onNodeClick={(node: any) => onNodeClick((node as GraphNode).id)}
        onNodeHover={handleNodeHover as any}
        backgroundColor="rgba(0,0,0,0)"
        cooldownTicks={280}
        warmupTicks={70}
      />
    </div>
  );
}
