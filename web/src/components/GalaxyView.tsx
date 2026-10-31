import { useCallback, useRef, useEffect, useMemo, useState } from "react";
import ForceGraph2D from "react-force-graph-2d";
import { forceCollide, forceX, forceY } from "d3-force";
import { formatBytes, OVERSIZE_BYTES, type GraphData, type GraphNode, type GraphLink, type Role } from "../types";
import { useTheme, type Theme } from "../theme";
import "./GalaxyView.css";

interface Props {
  data: GraphData;
  onNodeClick: (id: number) => void;
  onNodeHover: (id: number) => void;
}

type RGB = readonly [number, number, number];

// 与 index.css 里的 --font-sans 同一套（canvas 读不到变量）。
// 中日文走系统字体，所以后面要带上各平台的 CJK 回退，否则中文节点名会掉到无衬线默认字体。
const CANVAS_FONT =
  'system-ui, -apple-system, "Segoe UI", "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei UI", "Yu Gothic UI", sans-serif';

/* ════════════════════════════════════════════════════════════
   星图怎么画（输入 → 画面）
   - 每个节点按它所属的 Tag 归进一个“星座”：节点有好几个 Tag 时，取成员最多的那个；
     节点本身就是某个 Tag 的源节点时，归进它自己那个 Tag。
   - 同一星座的成员各画一根细线连到 Tag 的源节点，这根线也参与力学布局，
     所以源节点会自然落在星座中心。
   - 颜色按星座分：成员最多的 Tag 先挑颜色，十种颜色轮着用。没有 Tag 的节点是淡墨色。
   - 形状按角色分：记忆（plain）实心圆、传感器（sensor）带圈的点、逻辑（logic）菱形、守卫（guard）方块。
   - 通电（is_active=1）的节点亮成罐光色，外面一圈缓慢呼吸的光晕；电路连线的上游通电时，线上有光点流动。
   - 星座名直接写在源节点上，字号按成员数变大；放大到能看清单个节点名时，星座名淡出。
   ════════════════════════════════════════════════════════════ */

/** 一个星座的颜色：dot 画点和线，label 写字（比 dot 深，保证字在底色上 ≥ 4.5:1）。 */
interface Hue {
  dot: RGB;
  label: string;
}

interface Palette {
  hues: readonly Hue[];
  /** 没有 Tag 的节点 */
  loose: RGB;
  /** 图上的字：描边色（和底色同色，压住背后的点和线） */
  halo: string;
  ink: string;
  inkDim: string;
  /** 通电：灯芯、描边、光晕 */
  lampCore: string;
  lampRim: string;
  lampGlow: RGB;
  /** 电路连线：AND / OR / CHAIN / 抑制 */
  wire: { and: RGB; or: RGB; chain: RGB; inhibition: RGB };
  danger: string;
  /** 星座连线的不透明度 */
  spokeAlpha: number;
  /** 传感器圈里的底色 */
  hollow: string;
  card: { bg: string; border: string; shadow: string };
}

const PALETTES: Record<Theme, Palette> = {
  light: {
    hues: [
      { dot: [146, 88, 140], label: "#6f3e66" }, // 梅紫
      { dot: [95, 138, 90], label: "#3d6a3a" }, // 苔绿
      { dot: [196, 104, 92], label: "#8e3f34" }, // 赤陶（压暗，不当强调色用）
      { dot: [190, 144, 56], label: "#7a560c" }, // 赭黄
      { dot: [125, 104, 173], label: "#5a4590" }, // 紫
      { dot: [80, 134, 132], label: "#335f5d" }, // 灰青
      { dot: [200, 96, 128], label: "#8e3454" }, // 玫瑰
      { dot: [134, 134, 64], label: "#5c5c18" }, // 橄榄
      { dot: [156, 108, 84], label: "#6e4636" }, // 陶土
      { dot: [112, 100, 132], label: "#4f4364" }, // 灰紫
    ],
    loose: [146, 134, 156],
    halo: "rgba(236, 227, 234, 0.92)",
    ink: "#2e2236",
    inkDim: "#685a72",
    lampCore: "#ffd27a",
    lampRim: "#2e2236",
    lampGlow: [240, 168, 60],
    wire: {
      and: [150, 118, 196],
      or: [196, 146, 64],
      chain: [98, 150, 104],
      inhibition: [204, 74, 84],
    },
    danger: "#b83a45",
    spokeAlpha: 0.2,
    hollow: "#f7f2f6",
    card: { bg: "rgba(251, 248, 250, 0.98)", border: "rgba(46, 34, 54, 0.16)", shadow: "rgba(46, 34, 54, 0.22)" },
  },
  dark: {
    hues: [
      { dot: [214, 160, 204], label: "#e2b8da" },
      { dot: [150, 200, 152], label: "#b0d6b2" },
      { dot: [240, 150, 124], label: "#f4b29c" },
      { dot: [230, 190, 110], label: "#ecca8c" },
      { dot: [190, 170, 232], label: "#cfbdf0" },
      { dot: [130, 190, 186], label: "#a6d4d0" },
      { dot: [236, 150, 176], label: "#f0b3c6" },
      { dot: [196, 196, 120], label: "#d6d69a" },
      { dot: [206, 164, 140], label: "#dcbaa6" },
      { dot: [180, 168, 196], label: "#cbbfd8" },
    ],
    loose: [136, 124, 146],
    halo: "rgba(30, 23, 36, 0.9)",
    ink: "#ede4ee",
    inkDim: "#a596ad",
    lampCore: "#ffd27a",
    lampRim: "#fff3dc",
    lampGlow: [255, 210, 122],
    wire: {
      and: [190, 168, 230],
      or: [226, 186, 118],
      chain: [150, 200, 152],
      inhibition: [226, 116, 124],
    },
    danger: "#eba0a6",
    spokeAlpha: 0.22,
    hollow: "#1e1724",
    card: { bg: "rgba(47, 37, 55, 0.98)", border: "rgba(240, 230, 242, 0.16)", shadow: "rgba(10, 6, 14, 0.55)" },
  },
};

const rgba = (c: RGB, a: number) => `rgba(${c[0]},${c[1]},${c[2]},${a})`;

/** 画布上实际要画的线：数据里的电路连线，加上前端算出来的“成员 → Tag 源节点”。 */
interface DrawLink {
  source: number | GraphNode;
  target: number | GraphNode;
  kind: GraphLink["kind"] | "tag";
  status: string | null;
  hue?: number;
}

/** 预先算好的星座信息，渲染时只查表。 */
interface Layout {
  links: DrawLink[];
  /** 节点 id → 星座的 Tag 名（没有就是 undefined） */
  clusterOf: Map<number, string>;
  /** Tag 名 → 颜色序号 */
  hueOf: Map<string, number>;
  /** Tag 名 → 成员数 */
  count: Map<string, number>;
  /** Tag 名 → 源节点 id（库里有同名节点时） */
  hubOf: Map<string, number>;
  /** 节点 id → 它作为源节点的 Tag 的成员数（不是源节点就是 0） */
  hubSize: Map<number, number>;
}

function buildLayout(data: GraphData, hueCount: number): Layout {
  const count = new Map<string, number>();
  for (const n of data.nodes) for (const t of n.tags ?? []) count.set(t, (count.get(t) ?? 0) + 1);

  const idByName = new Map<string, number>();
  for (const n of data.nodes) idByName.set(n.name, n.id);

  const hubOf = new Map<string, number>();
  const hubSize = new Map<number, number>();
  for (const [tag, c] of count) {
    const id = idByName.get(tag);
    if (id !== undefined && c >= 2) {
      hubOf.set(tag, id);
      hubSize.set(id, c);
    }
  }

  // 成员多的 Tag 先挑颜色，最大的几个星座颜色一定不重复
  const hueOf = new Map<string, number>();
  [...count.entries()]
    .filter(([, c]) => c >= 2)
    .sort((a, b) => b[1] - a[1])
    .forEach(([tag], i) => hueOf.set(tag, i % hueCount));

  const clusterOf = new Map<number, string>();
  for (const n of data.nodes) {
    const tags = n.tags ?? [];
    let best: string | undefined;
    if (count.has(n.name) && tags.includes(n.name) && (count.get(n.name) ?? 0) >= 2) {
      best = n.name;
    } else {
      for (const t of tags) {
        const c = count.get(t) ?? 0;
        if (c >= 2 && (!best || c > (count.get(best) ?? 0))) best = t;
      }
    }
    if (best) clusterOf.set(n.id, best);
  }

  const links: DrawLink[] = data.links.map((l) => ({
    source: typeof l.source === "object" ? l.source.id : l.source,
    target: typeof l.target === "object" ? l.target.id : l.target,
    kind: l.kind,
    status: l.status,
  }));
  for (const n of data.nodes) {
    const tag = clusterOf.get(n.id);
    if (!tag) continue;
    const hub = hubOf.get(tag);
    if (hub === undefined || hub === n.id) continue;
    links.push({ source: n.id, target: hub, kind: "tag", status: null, hue: hueOf.get(tag) });
  }

  return { links, clusterOf, hueOf, count, hubOf, hubSize };
}

/** 节点半径（画布单位）。源节点按成员数变大，普通节点按正文体积变大，电路节点再加一点。 */
function nodeRadius(node: GraphNode, hubSize: number): number {
  if (hubSize > 0) return 3.2 + Math.sqrt(hubSize) * 0.9;
  const kb = (node.byte_size ?? 0) / 1024;
  return 2 + Math.min(2.4, kb * 0.45) + Math.min(1.5, node.degree * 0.5);
}

/** 按角色描出节点形状的路径（只建路径，不填色）。 */
function shapePath(ctx: CanvasRenderingContext2D, role: Role | undefined, x: number, y: number, r: number) {
  ctx.beginPath();
  if (role === "logic") {
    const d = r * 1.25;
    ctx.moveTo(x, y - d);
    ctx.lineTo(x + d, y);
    ctx.lineTo(x, y + d);
    ctx.lineTo(x - d, y);
    ctx.closePath();
  } else if (role === "guard") {
    const h = r * 0.95;
    ctx.rect(x - h, y - h, h * 2, h * 2);
  } else {
    ctx.arc(x, y, r, 0, 2 * Math.PI);
  }
}

const reduceMotion = () => window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;

export default function GalaxyView({ data, onNodeClick, onNodeHover }: Props) {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  const fgRef = useRef<any>(null);
  const containerRef = useRef<HTMLDivElement>(null);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  const [hoveredNode, setHoveredNode] = useState<GraphNode | null>(null);
  const hoverTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const fittedRef = useRef(false);
  const { theme } = useTheme();
  const palette = PALETTES[theme];

  const layout = useMemo(() => buildLayout(data, PALETTES.light.hues.length), [data]);
  // 交给力学布局的数据：节点用原对象（坐标写回去，切换主题时不重排），线用新算的这一套
  const graphData = useMemo(() => ({ nodes: data.nodes, links: layout.links }), [data, layout]);
  // 同一张图里，用户一旦开始操作视野，后续冷却或 resize 都不再自动适配。
  const viewInteractedRef = useRef(false);
  useEffect(() => {
    viewInteractedRef.current = false;
  }, [graphData]);

  const counts = useMemo(() => {
    const c = { plain: 0, sensor: 0, logic: 0, guard: 0, lit: 0 };
    for (const n of data.nodes) {
      c[(n.role ?? "plain") as Role] += 1;
      if (n.is_active) c.lit += 1;
    }
    return c;
  }, [data]);
  const tagTotal = layout.hubOf.size;
  // 有东西亮着（节点或电路线），画布就一直重画，光晕才会呼吸、光点才会流动
  const hasLive = counts.lit > 0 || data.links.some((l) => l.status === "active");

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

  useEffect(() => {
    const fg = fgRef.current;
    if (!fg) return;
    fittedRef.current = false;
    firstTickRef.current = false;

    fg.d3Force("charge")?.strength(-38).distanceMax(320);
    fg.d3Force("link")
      ?.distance((l: DrawLink) => (l.kind === "tag" ? 26 + Math.sqrt(layout.count.get(nameOfHub(l)) ?? 4) * 5 : 48))
      .strength((l: DrawLink) => (l.kind === "tag" ? 0.32 : 0.5));
    fg.d3Force("center")?.strength(0.05);
    // 横竖向心力按屏幕比例给：宽屏上竖向拉得紧、横向拉得松，整张图摊成和屏幕一样的椭圆，铺满而不是缩在中间一个圆里
    const aspect = Math.max(0.4, Math.min(2.4, dimensions.width / Math.max(1, dimensions.height)));
    fg.d3Force("x", forceX(0).strength(0.035 / aspect));
    fg.d3Force("y", forceY(0).strength(0.035 * aspect));
    fg.d3Force(
      "collide",
      forceCollide((node: GraphNode) => nodeRadius(node, layout.hubSize.get(node.id) ?? 0) + 4).iterations(2),
    );
    fg.d3ReheatSimulation?.();

    function nameOfHub(l: DrawLink): string {
      const t = typeof l.target === "object" ? l.target.id : l.target;
      for (const [tag, id] of layout.hubOf) if (id === t) return tag;
      return "";
    }
    // 只在数据或屏幕比例明显变化时重排；尺寸的小抖动不该让整张图重新散开
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [graphData, layout, Math.round((dimensions.width / Math.max(1, dimensions.height)) * 4)]);

  const colorOf = useCallback(
    (id: number): { dot: RGB; label: string } => {
      const tag = layout.clusterOf.get(id);
      const h = tag !== undefined ? layout.hueOf.get(tag) : undefined;
      if (h === undefined) return { dot: palette.loose, label: palette.inkDim };
      return palette.hues[h];
    },
    [layout, palette],
  );

  // ── 线 ───────────────────────────────────────────────────
  const linkCanvasObject = useCallback(
    (link: DrawLink, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const s = link.source as GraphNode;
      const t = link.target as GraphNode;
      if (s.x === undefined || t.x === undefined || s.y === undefined || t.y === undefined) return;
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(t.x, t.y);
      if (link.kind === "tag") {
        const hue = link.hue !== undefined ? palette.hues[link.hue].dot : palette.loose;
        ctx.strokeStyle = rgba(hue, palette.spokeAlpha);
        ctx.lineWidth = 0.7 / globalScale;
        ctx.setLineDash([]);
        ctx.stroke();
        return;
      }
      const live = link.status === "active";
      const wire =
        link.kind === "inhibition"
          ? palette.wire.inhibition
          : link.kind === "or"
            ? palette.wire.or
            : link.kind === "directed"
              ? palette.wire.chain
              : palette.wire.and;
      ctx.strokeStyle = live ? rgba(palette.lampGlow, 0.9) : rgba(wire, 0.7);
      ctx.lineWidth = (live ? 1.8 : 1.2) / globalScale;
      ctx.setLineDash(link.kind === "inhibition" ? [4 / globalScale, 3 / globalScale] : []);
      ctx.stroke();
      ctx.setLineDash([]);
    },
    [palette],
  );

  // ── 点 ───────────────────────────────────────────────────
  const nodeCanvasObject = useCallback(
    (node: GraphNode, ctx: CanvasRenderingContext2D, globalScale: number) => {
      const x = node.x ?? 0;
      const y = node.y ?? 0;
      const hubSize = layout.hubSize.get(node.id) ?? 0;
      const lit = Boolean(node.is_active);
      // 通电的节点放大一圈：它们是整张图里最该被看见的东西
      const r = nodeRadius(node, hubSize) * (lit ? 1.5 : 1) + (lit ? 1 : 0);
      const { dot } = colorOf(node.id);
      const isHovered = hoveredNode?.id === node.id;

      // 通电：先画呼吸的光晕
      if (lit) {
        const breathe = reduceMotion() ? 0.5 : 0.5 + 0.5 * Math.sin(performance.now() / 900 + node.id);
        const R = r * 3 + (10 + breathe * 6) / Math.sqrt(globalScale);
        const g = ctx.createRadialGradient(x, y, r * 0.8, x, y, R);
        g.addColorStop(0, rgba(palette.lampGlow, 0.55 + 0.25 * breathe));
        g.addColorStop(0.45, rgba(palette.lampGlow, 0.18 + 0.1 * breathe));
        g.addColorStop(1, rgba(palette.lampGlow, 0));
        ctx.beginPath();
        ctx.arc(x, y, R, 0, 2 * Math.PI);
        ctx.fillStyle = g;
        ctx.fill();
      }

      if (isHovered) {
        ctx.beginPath();
        ctx.arc(x, y, r + 5 / globalScale, 0, 2 * Math.PI);
        ctx.fillStyle = rgba(dot, 0.18);
        ctx.fill();
      }

      shapePath(ctx, node.role, x, y, r);
      if (lit) {
        ctx.fillStyle = palette.lampCore;
        ctx.fill();
        ctx.strokeStyle = palette.lampRim;
        ctx.lineWidth = Math.max(0.8 / globalScale, r * 0.16);
        ctx.stroke();
      } else if (node.role === "sensor") {
        // 传感器：空心圈 + 中间一个小点
        ctx.fillStyle = palette.hollow;
        ctx.fill();
        ctx.strokeStyle = rgba(dot, 1);
        ctx.lineWidth = Math.max(1 / globalScale, r * 0.32);
        ctx.stroke();
        ctx.beginPath();
        ctx.arc(x, y, r * 0.38, 0, 2 * Math.PI);
        ctx.fillStyle = rgba(dot, 1);
        ctx.fill();
      } else {
        ctx.fillStyle = rgba(dot, hubSize > 0 ? 1 : 0.9);
        ctx.fill();
      }

      // 源节点：外面再描一圈同色细环，一眼看出是星座中心
      if (hubSize > 0) {
        ctx.beginPath();
        ctx.arc(x, y, r + 2.2, 0, 2 * Math.PI);
        ctx.strokeStyle = rgba(dot, 0.55);
        ctx.lineWidth = 1 / globalScale;
        ctx.stroke();
      }

      // 正文太长：一小段红色缺口环
      if ((node.byte_size ?? 0) >= OVERSIZE_BYTES) {
        ctx.beginPath();
        ctx.arc(x, y, r + 1.6, -Math.PI * 0.85, -Math.PI * 0.15);
        ctx.strokeStyle = palette.danger;
        ctx.lineWidth = 1.4 / globalScale;
        ctx.stroke();
      }

    },
    [layout, colorOf, hoveredNode, palette],
  );

  // ── 置顶层：星座名、悬停卡片 ─────────────────────────────
  const onRenderFramePost = useCallback(
    (ctx: CanvasRenderingContext2D, globalScale: number) => {
      const nodeById = new Map<number, GraphNode>();
      for (const n of data.nodes) nodeById.set(n.id, n);

      // 屏幕里看得见的范围（画布坐标），只给这里面的节点排字
      const m = ctx.getTransform();
      const k = m.a || 1;
      const view = {
        x0: -m.e / k,
        y0: -m.f / k,
        x1: (ctx.canvas.width - m.e) / k,
        y1: (ctx.canvas.height - m.f) / k,
      };
      // 窄屏上地图缩得更小，星座名按屏宽一起缩，不然手机上会盖住半张图、还会出屏
      const sizeScale = Math.max(0.68, Math.min(1, dimensions.width / 1100));
      const placed: { x0: number; y0: number; x1: number; y1: number }[] = [];
      const hits = (bx: { x0: number; y0: number; x1: number; y1: number }, pad: number) =>
        placed.some((p) => bx.x0 < p.x1 + pad && bx.x1 > p.x0 - pad && bx.y0 < p.y1 + pad && bx.y1 > p.y0 - pad);

      // 1. 星座名：远景到中景显示，放大到能读单个节点名时淡出
      const fade = Math.max(0, Math.min(1, (2.3 - globalScale) / 0.5));
      if (fade > 0) {
        const items = [...layout.hubOf.entries()]
          .map(([tag, id]) => ({ tag, node: nodeById.get(id), c: layout.count.get(tag) ?? 0 }))
          .filter((it) => it.node && it.c >= 3)
          .sort((a, b) => b.c - a.c);

        ctx.textBaseline = "middle";
        ctx.lineJoin = "round";
        for (const { tag, node, c } of items) {
          const n = node as GraphNode;
          const screenSize = Math.min(19, 11 + Math.sqrt(c) * 0.95) * sizeScale;
          const fs = screenSize / globalScale;
          const countFs = fs * 0.72;
          ctx.font = `650 ${fs}px ${CANVAS_FONT}`;
          const nameW = ctx.measureText(tag).width;
          ctx.font = `500 ${countFs}px ${CANVAS_FONT}`;
          const countText = String(c);
          const countW = ctx.measureText(countText).width;
          const gap = 5 / globalScale;
          const w = nameW + gap + countW;
          const r = nodeRadius(n, layout.hubSize.get(n.id) ?? 0);
          const cx = n.x ?? 0;
          const cy = (n.y ?? 0) - r - 6 / globalScale - fs / 2;
          const box = { x0: cx - w / 2, y0: cy - fs / 2, x1: cx + w / 2, y1: cy + fs / 2 };
          if (hits(box, 3 / globalScale)) continue;
          placed.push(box);

          const { label } = colorOf(n.id);
          ctx.globalAlpha = fade;
          ctx.textAlign = "left";
          ctx.font = `650 ${fs}px ${CANVAS_FONT}`;
          ctx.lineWidth = 4 / globalScale;
          ctx.strokeStyle = palette.halo;
          ctx.strokeText(tag, box.x0, cy);
          ctx.fillStyle = label;
          ctx.fillText(tag, box.x0, cy);
          ctx.font = `500 ${countFs}px ${CANVAS_FONT}`;
          ctx.strokeText(countText, box.x0 + nameW + gap, cy + fs * 0.06);
          ctx.fillStyle = palette.inkDim;
          ctx.fillText(countText, box.x0 + nameW + gap, cy + fs * 0.06);
          ctx.globalAlpha = 1;
        }
      }

      // 2. 单个节点名：放大到 1.8 倍以上才出现（再小就是一锅字）。
      //    重要的先占位：通电的、星座中心、正文大的；会压到已经排好的字就不画，放大以后空间够了自然会出来。
      const labelFade = Math.max(0, Math.min(1, (globalScale - 1.8) / 0.4));
      if (labelFade > 0) {
        const fs = 11 / globalScale;
        ctx.font = `400 ${fs}px ${CANVAS_FONT}`;
        ctx.textAlign = "center";
        ctx.textBaseline = "top";
        ctx.lineJoin = "round";
        const visible = data.nodes.filter(
          (n) =>
            n.id !== hoveredNode?.id &&
            (n.x ?? 0) > view.x0 && (n.x ?? 0) < view.x1 && (n.y ?? 0) > view.y0 && (n.y ?? 0) < view.y1,
        );
        const rank = (n: GraphNode) =>
          (n.is_active ? 1e6 : 0) + (layout.hubSize.get(n.id) ?? 0) * 1e3 + (n.byte_size ?? 0) / 100;
        visible.sort((a, b) => rank(b) - rank(a));
        ctx.globalAlpha = labelFade;
        for (const n of visible) {
          const label = globalScale >= 3 || n.name.length <= 14 ? n.name : `${n.name.slice(0, 13)}…`;
          const w = ctx.measureText(label).width;
          const r = nodeRadius(n, layout.hubSize.get(n.id) ?? 0);
          const x = n.x ?? 0;
          const ty = (n.y ?? 0) + r + 3 / globalScale;
          const box = { x0: x - w / 2, y0: ty, x1: x + w / 2, y1: ty + fs * 1.2 };
          if (hits(box, 2 / globalScale)) continue;
          placed.push(box);
          ctx.lineWidth = 3 / globalScale;
          ctx.strokeStyle = palette.halo;
          ctx.strokeText(label, x, ty);
          ctx.fillStyle = palette.ink;
          ctx.fillText(label, x, ty);
        }
        ctx.globalAlpha = 1;
      }

      // 3. 悬停卡片：名字一行，下面一行写星座和体积
      if (hoveredNode && hoveredNode.x !== undefined && hoveredNode.y !== undefined) {
        const n = hoveredNode;
        const r = nodeRadius(n, layout.hubSize.get(n.id) ?? 0);
        const tag = layout.clusterOf.get(n.id);
        const { label: tagColor } = colorOf(n.id);
        const tooLong = (n.byte_size ?? 0) >= OVERSIZE_BYTES;
        const sizeText = n.byte_size ? `${formatBytes(n.byte_size)}${tooLong ? " — too long" : ""}` : "";

        const titleFs = 13 / globalScale;
        const subFs = 11 / globalScale;
        ctx.font = `600 ${titleFs}px ${CANVAS_FONT}`;
        const titleW = ctx.measureText(n.name).width;
        ctx.font = `400 ${subFs}px ${CANVAS_FONT}`;
        const tagW = tag ? ctx.measureText(tag).width : 0;
        const sizeW = sizeText ? ctx.measureText(sizeText).width : 0;
        const subGap = tag && sizeText ? 10 / globalScale : 0;
        const hasSub = Boolean(tag || sizeText);
        const padX = 10 / globalScale;
        const padY = 7 / globalScale;
        const w = Math.max(titleW, tagW + subGap + sizeW) + padX * 2;
        const h = titleFs * 1.25 + (hasSub ? subFs * 1.3 + 3 / globalScale : 0) + padY * 2;
        const x0 = (n.x ?? 0) - w / 2;
        const y0 = (n.y ?? 0) - r - h - 8 / globalScale;

        ctx.save();
        ctx.shadowColor = palette.card.shadow;
        ctx.shadowBlur = 16;
        ctx.shadowOffsetY = 4;
        ctx.fillStyle = palette.card.bg;
        ctx.beginPath();
        ctx.roundRect(x0, y0, w, h, 6 / globalScale);
        ctx.fill();
        ctx.restore();
        ctx.strokeStyle = palette.card.border;
        ctx.lineWidth = 1 / globalScale;
        ctx.stroke();

        ctx.textAlign = "left";
        ctx.textBaseline = "top";
        ctx.font = `600 ${titleFs}px ${CANVAS_FONT}`;
        ctx.fillStyle = palette.ink;
        ctx.fillText(n.name, x0 + padX, y0 + padY);
        if (hasSub) {
          const sy = y0 + padY + titleFs * 1.25 + 3 / globalScale;
          ctx.font = `500 ${subFs}px ${CANVAS_FONT}`;
          if (tag) {
            ctx.fillStyle = tagColor;
            ctx.fillText(tag, x0 + padX, sy);
          }
          if (sizeText) {
            ctx.font = `400 ${subFs}px ${CANVAS_FONT}`;
            ctx.fillStyle = tooLong ? palette.danger : palette.inkDim;
            ctx.fillText(sizeText, x0 + padX + tagW + subGap, sy);
          }
        }
      }
    },
    [data, layout, colorOf, hoveredNode, palette, dimensions.width],
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
      if (containerRef.current) containerRef.current.style.cursor = node ? "pointer" : "";
      if (hoverTimerRef.current) clearTimeout(hoverTimerRef.current);
      if (node) {
        hoverTimerRef.current = setTimeout(() => {
          onNodeHover(node.id);
        }, 350);
      }
    },
    [onNodeHover],
  );

  // 铺满屏幕（四周留一点边）分两次：第一帧立刻按预热后的布局铺满，不让人先看到缩在中间的一小团；
  // 布局停下来以后再平滑校准一次。
  const firstTickRef = useRef(false);
  const fitPad = dimensions.width < 640 ? 14 : 48;
  const handleViewInteraction = useCallback(() => {
    viewInteractedRef.current = true;
  }, []);
  const handleEngineTick = useCallback(() => {
    if (firstTickRef.current || viewInteractedRef.current) return;
    // 库的 zoomToFit 不检查坐标：有节点还没坐标、或画布还没尺寸时会算出 NaN 倍率，整张图画不出来。
    // 所以等坐标齐了、画布有尺寸了再铺，没齐就等下一帧。
    if (dimensions.width <= 0 || dimensions.height <= 0) return;
    if (!data.nodes.every((n) => Number.isFinite(n.x) && Number.isFinite(n.y))) return;
    firstTickRef.current = true;
    fgRef.current?.zoomToFit?.(0, fitPad);
  }, [fitPad, data, dimensions.width, dimensions.height]);
  const handleEngineStop = useCallback(() => {
    if (fittedRef.current || viewInteractedRef.current) return;
    fittedRef.current = true;
    fgRef.current?.zoomToFit?.(reduceMotion() ? 0 : 700, fitPad);
  }, [fitPad]);

  return (
    <div
      ref={containerRef}
      className="galaxy-stage"
      onWheelCapture={handleViewInteraction}
      onPointerDownCapture={handleViewInteraction}
    >
      <ForceGraph2D
        ref={fgRef}
        width={dimensions.width}
        height={dimensions.height}
        graphData={graphData as any}
        nodeId="id"
        nodeCanvasObject={nodeCanvasObject as any}
        onRenderFramePost={onRenderFramePost as any}
        nodePointerAreaPaint={((node: any, color: string, ctx: CanvasRenderingContext2D) => {
          const r = nodeRadius(node as GraphNode, layout.hubSize.get((node as GraphNode).id) ?? 0);
          ctx.fillStyle = color;
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, r + 5, 0, 2 * Math.PI);
          ctx.fill();
        }) as any}
        linkCanvasObjectMode={() => "replace"}
        linkCanvasObject={linkCanvasObject as any}
        linkDirectionalParticles={((l: DrawLink) => (l.kind !== "tag" && l.status === "active" && !reduceMotion() ? 2 : 0)) as any}
        linkDirectionalParticleSpeed={0.006}
        linkDirectionalParticleWidth={3}
        linkDirectionalParticleColor={() => palette.lampCore}
        nodeLabel={() => ""}
        onNodeClick={(node: any) => onNodeClick((node as GraphNode).id)}
        onNodeHover={handleNodeHover as any}
        onEngineTick={handleEngineTick}
        onEngineStop={handleEngineStop}
        autoPauseRedraw={!hasLive}
        backgroundColor="rgba(0,0,0,0)"
        cooldownTicks={220}
        warmupTicks={80}
      />

      {/* 图例兼计数：形状是什么意思、各有多少、此刻亮着几个 */}
      <div className="galaxy-legend">
        <span className="legend-item">
          <svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="4" className="lg-fill" /></svg>
          <strong>{counts.plain}</strong> memories
        </span>
        <span className="legend-item">
          <svg viewBox="0 0 12 12" aria-hidden="true">
            <circle cx="6" cy="6" r="3.8" className="lg-ring" />
            <circle cx="6" cy="6" r="1.5" className="lg-fill" />
          </svg>
          <strong>{counts.sensor}</strong> sensors
        </span>
        <span className="legend-item">
          <svg viewBox="0 0 12 12" aria-hidden="true"><path d="M6 1.2 10.8 6 6 10.8 1.2 6Z" className="lg-fill" /></svg>
          <strong>{counts.logic}</strong> logic
        </span>
        <span className="legend-item">
          <svg viewBox="0 0 12 12" aria-hidden="true"><rect x="2.2" y="2.2" width="7.6" height="7.6" className="lg-fill" /></svg>
          <strong>{counts.guard}</strong> guards
        </span>
        <span className="legend-sep" aria-hidden="true" />
        <span className="legend-item">
          <strong>{tagTotal}</strong> tags
        </span>
        <span className={`legend-item legend-lit ${counts.lit > 0 ? "is-on" : ""}`}>
          <svg viewBox="0 0 12 12" aria-hidden="true"><circle cx="6" cy="6" r="3.6" className="lg-lamp" /></svg>
          <strong>{counts.lit}</strong> lit now
        </span>
      </div>
    </div>
  );
}
