import { useEffect, useRef, useState, useCallback } from "react";
import {
  forceSimulation,
  forceLink,
  forceManyBody,
  forceCenter,
  forceCollide,
  forceX,
  forceY,
  type SimulationNodeDatum,
  type SimulationLinkDatum,
} from "d3-force";
import { api } from "../api";
import { formatBytes, OVERSIZE_BYTES, stateLabel, type NeighborhoodData, type ConceptDetail, type Role } from "../types";
import "./DissectionView.css";

// 背景点阵的间距（px，缩放为 1 时）
const GRID_SIZE = 28;

// 没有内部成员的节点（大多数记忆）中间放一张卡片，不画空圈。卡片宽度固定，高度渲染后量。
const FOCAL_CARD_W = 320;
// 左右两侧的联想每边最多放几个：左边“读完常来读它”（进边），右边“读完它常去读”（出边）
const MAX_PER_SIDE = 5;

const MODE_LABEL: Record<string, string> = {
  CHAIN: "In order",
  AND: "All of",
  OR: "Any of",
};

interface Props {
  focalId: number;
  session: string; // 激活状态按这个会话显示
  onNavigate: (id: number) => void;
  onInspect: (id: number) => void;
  onBack: () => void;
  /** 右边被侧栏挡住的宽度（px）。图整体左移一半，居中在侧栏左边露出来的那块 */
  rightInset?: number;
}

interface SimNode extends SimulationNodeDatum {
  id: number;
  name: string;
  when_to_read: string | null;
  isFocal: boolean;
  isInternal: boolean;
  role?: Role;
  is_active?: number;
  /** 联想节点（来自转移权重）：in 画在左边（读完它常来读焦点），out 画在右边（读完焦点常去读它） */
  isAssoc?: boolean;
  side?: "in" | "out";
  degree?: number;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  status: string | null;
  kind: "directed" | "undirected" | "inhibition" | "internal-directed" | "assoc";
  /** 联想线的粗细：转移权重除以同一侧的最大权重（0~1） */
  strength?: number;
  /** 两个方向都常连着读：线两头都画箭头 */
  twoWay?: boolean;
  short_code?: string;
  relation_id?: number;
  relation_name?: string;
}

// Helper to extract unique internal members of a focal concept
const getInternalMembers = (focalConcept: ConceptDetail) => {
  const map = new Map<number, { id: number; name: string; when_to_read: string | null; role: Role; is_active: number }>();
  if (focalConcept.members) {
    focalConcept.members.forEach((m) => {
      if (m.concept_id !== focalConcept.id) {
        map.set(m.concept_id, {
          id: m.concept_id,
          name: m.name,
          when_to_read: m.when_to_read,
          role: m.role,
          is_active: m.is_active,
        });
      }
    });
  }
  return Array.from(map.values());
};

const estimateStringWidth = (str: string, fontSize: number) => {
  let width = 0;
  for (let i = 0; i < str.length; i++) {
    const code = str.charCodeAt(i);
    if (code >= 0 && code <= 128) {
      width += fontSize * 0.55; // English/ASCII
    } else {
      width += fontSize; // Chinese/Unicode
    }
  }
  return width;
};

export default function DissectionView({
  focalId,
  session,
  onNavigate,
  onInspect,
  onBack,
  rightInset = 0,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  // 仿真结束时的自动缩放要按可见宽度算，回调里读这个 ref 拿最新值
  const rightInsetRef = useRef(rightInset);
  rightInsetRef.current = rightInset;
  const viewShift = -rightInset / 2;
  const svgRef = useRef<SVGSVGElement>(null);
  const nodesRef = useRef<Map<number, HTMLDivElement>>(new Map());
  const simRef = useRef<ReturnType<typeof forceSimulation<SimNode>> | null>(
    null,
  );
  // 焦点节点对象（被仿真复用），resize 时需要把它的固定坐标同步到新中心。
  const focalNodeRef = useRef<SimNode | null>(null);
  const [data, setData] = useState<NeighborhoodData | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [simNodes, setSimNodes] = useState<SimNode[]>([]);
  const [simLinks, setSimLinks] = useState<SimLink[]>([]);
  const [dimensions, setDimensions] = useState({ width: 800, height: 600 });
  // 仿真的 tick 闭包通过这个 ref 读取实时尺寸，从而无需在 resize 时重建整张仿真。
  const dimsRef = useRef(dimensions);

  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  // 每次打开节点只自动适配一次；用户开始操作后，视野由用户掌握。
  // 用 ref 让仿真的 end 回调立即看到交互，不因重渲染或卡片尺寸变化而重置。
  const autoFitPendingRef = useRef(true);
  const isPanning = useRef(false);
  const panStart = useRef({ x: 0, y: 0 });
  const panOrigin = useRef({ x: 0, y: 0 });
  // 拖拽进行中时存放清理函数；组件卸载时调用，避免遗留 window 监听器与
  // 卡在 alphaTarget(0.3) 的仿真。
  const dragCleanupRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    const el = containerRef.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const { width, height } = entries[0].contentRect;
      dimsRef.current = { width, height };
      setDimensions({ width, height });
    });
    ro.observe(el);
    return () => ro.disconnect();
    // 画布容器要等数据到了才渲染（之前是加载圈），所以数据到了以后要重新挂一次监听；
    // 只挂在首次渲染上会一直拿默认的 800×600，整个布局偏到左上角。
  }, [data !== null && !loadError]);

  useEffect(() => {
    let current = true;
    autoFitPendingRef.current = true;
    setData(null);
    setLoadError(null);
    api
      .getNeighborhood(focalId, session)
      .then((result) => {
        if (current) setData(result);
      })
      .catch((e) => {
        if (current) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => { current = false; };
  }, [focalId, session]);

  // Calculate geofence radius dynamically based on internal sub-elements count and sizes
  const getDynamicContainerRadius = () => {
    if (!data || !data.focal) return 150;
    const internalMembersList = getInternalMembers(data.focal);
    if (internalMembersList.length === 0) return 120;

    // Calculate dimensions of each internal node
    let maxHalfDiagonal = 0;
    let totalArea = 0;

    internalMembersList.forEach((m) => {
      const h = m.when_to_read ? 42 : 28;
      const w = Math.min(180, Math.max(80, m.name.length * 8 + 20));
      const halfDiagonal = Math.sqrt((w * w + h * h) / 4);
      if (halfDiagonal > maxHalfDiagonal) {
        maxHalfDiagonal = halfDiagonal;
      }
      totalArea += w * h;
    });

    const areaRadius = Math.sqrt((totalArea * 4.5) / Math.PI);

    const minRadiusByNodeSize = maxHalfDiagonal + 50;

    const countRadius = 100 + internalMembersList.length * 30;

    return Math.max(140, Math.min(400, Math.max(areaRadius, minRadiusByNodeSize, countRadius)));
  };

  const hasMembers = data ? getInternalMembers(data.focal).length > 0 : false;
  // 卡片模式下，量到的卡片尺寸。量到之前先用估计值。
  const focalCardRef = useRef<HTMLDivElement>(null);
  const geofenceHeaderRef = useRef<HTMLDivElement>(null);
  const [focalBox, setFocalBox] = useState({ w: FOCAL_CARD_W, h: 120 });
  const focalBoxRef = useRef(focalBox);
  focalBoxRef.current = focalBox;
  useEffect(() => {
    const el = focalCardRef.current;
    if (!el) return;
    const measure = () => {
      const w = el.offsetWidth;
      const h = el.offsetHeight;
      if (w > 0 && h > 0) setFocalBox((b) => (b.w === w && b.h === h ? b : { w, h }));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(el);
    return () => ro.disconnect();
  }, [data, hasMembers]);

  // 有成员时是圆形容器的半径；卡片模式下取卡片对角线的一半，外圈节点照样绕着它排
  const CONTAINER_RADIUS = hasMembers
    ? getDynamicContainerRadius()
    : Math.round(Math.sqrt(focalBox.w * focalBox.w + focalBox.h * focalBox.h) / 2);

  useEffect(() => {
    if (!data) return;

    const focalConcept = data.focal;
    // 先同步量一次画布的真实尺寸：画布和数据在同一次渲染里出现，尺寸监听的第一次回报
    // 要晚于这里；不量的话初始布局会围着默认的 800×600 中心排，之后再被拽回来就乱了
    const el = containerRef.current;
    if (el && el.clientWidth > 0 && el.clientHeight > 0) {
      dimsRef.current = { width: el.clientWidth, height: el.clientHeight };
    }
    const cx = dimsRef.current.width / 2;
    const cy = dimsRef.current.height / 2;

    // 1. Focal node (fixed at center)
    const focalNode: SimNode = {
      id: focalConcept.id,
      name: focalConcept.name,
      when_to_read: focalConcept.when_to_read,
      isFocal: true,
      isInternal: false,
      degree: 0,
      x: cx,
      y: cy,
      fx: cx,
      fy: cy,
    };
    focalNodeRef.current = focalNode;
    const nodes: SimNode[] = [focalNode];

    // 2. Internal nodes (sub-elements inside the geofence)
    const internalMembersList = getInternalMembers(focalConcept);
    const internalNodeIds = new Set(internalMembersList.map((m) => m.id));
    const count = internalMembersList.length;

    if (focalConcept.activation_type === "CHAIN") {
      // Sort members for CHAIN to arrange from top to bottom
      const memberOrderMap = new Map(
        (focalConcept.members || []).map((m) => [m.concept_id, m.order_index]),
      );
      const sortedList = [...internalMembersList].sort(
        (a, b) => (memberOrderMap.get(a.id) ?? 0) - (memberOrderMap.get(b.id) ?? 0),
      );
      const stepY = count > 1 ? Math.min(65, (CONTAINER_RADIUS * 1.3) / (count - 1)) : 0;
      const startY = cy - ((count - 1) * stepY) / 2;
      sortedList.forEach((m, i) => {
        nodes.push({
          id: m.id,
          name: m.name,
          when_to_read: m.when_to_read,
          role: m.role,
          is_active: m.is_active,
          isFocal: false,
          isInternal: true,
          x: cx,
          y: startY + i * stepY,
        });
      });
    } else {
      // AND / OR: circular distribution inside container
      const internalAngle = (2 * Math.PI) / Math.max(count, 1);
      const internalR = Math.min(CONTAINER_RADIUS * 0.45, 60 + count * 6);
      internalMembersList.forEach((m, i) => {
        nodes.push({
          id: m.id,
          name: m.name,
          when_to_read: m.when_to_read,
          role: m.role,
          is_active: m.is_active,
          isFocal: false,
          isInternal: true,
          x: cx + Math.cos(internalAngle * i - Math.PI / 2) * internalR,
          y: cy + Math.sin(internalAngle * i - Math.PI / 2) * internalR,
        });
      });
    }

    // 3. External nodes (neighbors outside the geofence)
    const externalNeighbors = data.neighbors.filter(
      (n) => !internalNodeIds.has(n.id) && n.id !== focalConcept.id,
    );
    const orbitR = CONTAINER_RADIUS + 90;
    // 横向留给阅读顺序（左进右出），电路关系放纵向：把它当条件用的放上面，抑制关系放下面
    const inhibitionIds = new Set<number>();
    data.internal_links.forEach((l) => {
      if (l.kind === "inhibition") {
        inhibitionIds.add(l.source);
        inhibitionIds.add(l.target);
      }
    });
    const topNeighbors = externalNeighbors.filter((n) => !inhibitionIds.has(n.id));
    const bottomNeighbors = externalNeighbors.filter((n) => inhibitionIds.has(n.id));
    const placeOnArc = (list: typeof externalNeighbors, center: number) => {
      const spread = (80 * Math.PI) / 180;
      list.forEach((n, i) => {
        const t = list.length > 1 ? i / (list.length - 1) : 0.5;
        const ang = center - spread / 2 + t * spread;
        nodes.push({
          id: n.id,
          name: n.name,
          when_to_read: n.when_to_read,
          role: n.role,
          is_active: n.is_active,
          isFocal: false,
          isInternal: false,
          degree: n.degree,
          x: cx + Math.cos(ang) * orbitR,
          y: cy + Math.sin(ang) * orbitR,
        });
      });
    };
    placeOnArc(topNeighbors, -Math.PI / 2);
    placeOnArc(bottomNeighbors, Math.PI / 2);

    // 3b. 联想：左边是读完它们常来读焦点的（进边），右边是读完焦点常去读的（出边）。
    //     已经作为成员或电路邻居出现的不再重复；两个方向都有的只放右边，线两头画箭头。
    const shown = new Set(nodes.map((n) => n.id));
    const outgoing = (focalConcept.suggested_next ?? [])
      .filter((t) => !shown.has(t.concept_id))
      .slice(0, MAX_PER_SIDE);
    const outIds = new Set(outgoing.map((t) => t.concept_id));
    const inIds = new Set((data.incoming ?? []).map((t) => t.concept_id));
    const incoming = (data.incoming ?? [])
      .filter((t) => !shown.has(t.concept_id) && !outIds.has(t.concept_id))
      .slice(0, MAX_PER_SIDE);
    const maxOut = Math.max(1e-6, ...outgoing.map((t) => t.weight));
    const maxIn = Math.max(1e-6, ...incoming.map((t) => t.weight));
    const assocR = externalNeighbors.length > 0 ? orbitR + 110 : CONTAINER_RADIUS + 120;
    // 每侧排在一段 ±50° 的弧上，权重最高的在最上面
    const sideAngle = (i: number, n: number, right: boolean) => {
      const spread = (100 * Math.PI) / 180;
      const t = n > 1 ? i / (n - 1) : 0.5;
      const a = -spread / 2 + t * spread;
      return right ? a : Math.PI - a;
    };
    const pushAssoc = (list: typeof outgoing, side: "in" | "out") =>
      list.forEach((t, i) => {
        const ang = sideAngle(i, list.length, side === "out");
        nodes.push({
          id: t.concept_id,
          name: t.concept_name,
          when_to_read: t.when_to_read ?? null,
          role: t.role,
          is_active: t.is_active,
          isFocal: false,
          isInternal: false,
          isAssoc: true,
          side,
          x: cx + Math.cos(ang) * assocR,
          y: cy + Math.sin(ang) * assocR,
        });
      });
    pushAssoc(incoming, "in");
    pushAssoc(outgoing, "out");

    // 4. Build links
    const links: SimLink[] = [];
    // 线的方向就是阅读方向：进边从左边的节点指向焦点，出边从焦点指向右边的节点
    incoming.forEach((t) => {
      links.push({ source: t.concept_id, target: focalConcept.id, status: null, kind: "assoc", strength: t.weight / maxIn });
    });
    outgoing.forEach((t) => {
      links.push({
        source: focalConcept.id,
        target: t.concept_id,
        status: null,
        kind: "assoc",
        strength: t.weight / maxOut,
        twoWay: inIds.has(t.concept_id),
      });
    });

    // Add internal links ONLY for CHAIN (directed flow between consecutive steps)
    // AND / OR do NOT draw pairwise criss-cross links to avoid visual clutter.
    const members = focalConcept.members;
    if (members && members.length > 0 && focalConcept.activation_type === "CHAIN") {
      const sorted = [...members].sort((a, b) => a.order_index - b.order_index);
      const status = focalConcept.is_active ? "active" : "inactive";
      for (let i = 0; i < sorted.length - 1; i++) {
        links.push({
          source: sorted[i].concept_id,
          target: sorted[i + 1].concept_id,
          status: status,
          kind: "internal-directed",
        });
      }
    }

    // Add external links (connecting external neighbors to focal concept)
    // Defensive check: ignore any links connecting internal nodes directly to focal boundary
    data.internal_links.forEach((l) => {
      if (internalNodeIds.has(l.source) || internalNodeIds.has(l.target)) {
        return;
      }
      if (l.target === focalConcept.id && l.source !== focalConcept.id) {
        links.push({
          source: l.source,
          target: focalConcept.id,
          status: l.status,
          kind: l.kind,
          relation_id: l.relation_id,
          relation_name: l.relation_name,
        });
      } else if (l.source === focalConcept.id && l.target !== focalConcept.id) {
        links.push({
          source: focalConcept.id,
          target: l.target,
          status: l.status,
          kind: l.kind,
          relation_id: l.relation_id,
          relation_name: l.relation_name,
        });
      }
    });

    setSimNodes(nodes);
    setSimLinks(links);

    if (simRef.current) simRef.current.stop();

    // 节点 DOM 尺寸在仿真期间不变；测到一次就缓存，避免每个 tick 在
    // O(n^2) 循环里反复读 offsetWidth 触发同步重排（layout thrashing）。
    const dimCache = new Map<number, { w: number; h: number }>();
    let headerBox: { w: number; h: number; top: number } | null = null;
    const getNodeDimensions = (node: SimNode) => {
      if (node.isFocal) {
        return { w: CONTAINER_RADIUS * 2, h: CONTAINER_RADIUS * 2 };
      }

      const cached = dimCache.get(node.id);
      if (cached) return cached;

      const el = nodesRef.current.get(node.id);
      if (el && el.offsetWidth > 0) {
        const dim = { w: el.offsetWidth, h: el.offsetHeight };
        dimCache.set(node.id, dim);
        return dim;
      }

      if (node.isInternal) {
        const paddingX = 28; // 14px * 2
        const paddingY = 16; // 8px * 2
        
        const nameW = estimateStringWidth(node.name, 12);
        let contentW = nameW;
        let contentH = 15; // line-height of name
        
        if (node.when_to_read) {
          const discW = Math.min(160, estimateStringWidth(node.when_to_read, 10));
          contentW = Math.max(contentW, discW);
          contentH += 2 + 13; // margin-top + line-height of when_to_read
        }
        
        return {
          w: Math.max(80, contentW + paddingX),
          h: contentH + paddingY,
        };
      } else {
        const paddingX = 28; // 14px * 2
        const paddingY = 16; // 8px * 2
        
        const nameW = estimateStringWidth(node.name, 13);
        let contentW = nameW;
        let contentH = 16; // line-height of name
        
        if (node.when_to_read) {
          const discW = Math.min(140, estimateStringWidth(node.when_to_read, 10));
          contentW = Math.max(contentW, discW);
          contentH += 2 + 13;
        }
        
        return {
          w: Math.max(100, contentW + paddingX),
          h: contentH + paddingY,
        };
      }
    };

    const sim = forceSimulation<SimNode>(nodes)
      .force(
        "link",
        forceLink<SimNode, SimLink>(links)
          .id((d) => d.id)
          .distance((l) => {
            if (l.kind.startsWith("internal")) {
              return 140;
            }
            if (l.kind === "assoc") return assocR;
            return orbitR;
          })
          .strength((l) => {
            if (l.kind.startsWith("internal")) {
              return 0.8;
            }
            if (l.kind === "assoc") return 0.35;
            return 0.3;
          }),
      )
      .force(
        "charge",
        forceManyBody<SimNode>().strength((node) => {
          if (node.isFocal) return 0;
          if (node.isInternal) return -350;
          if (node.isAssoc) return -160;
          return -400;
        })
      )
      .force("center", forceCenter(cx, cy).strength(0.06))
      .force(
        "collide",
        forceCollide<SimNode>().radius((node) => {
          const { w, h } = getNodeDimensions(node);
          // 联想卡片又宽又扁，按对角线当圆算会把一圈卡片推得很远；
          // 它们之间的重叠交给下面 tick 里的矩形碰撞处理，这里只给一个小半径
          if (node.isAssoc) return h / 2 + 12;
          return Math.sqrt((w * w + h * h) / 4) + 24;
        })
      )
      // 横向拉得松、竖向拉得紧：外圈摊成和屏幕一样的横椭圆，而不是竖着顶出屏幕
      .force("x", forceX<SimNode>(cx).strength((n) => (n.isAssoc ? 0.01 : 0.03)))
      .force("y", forceY<SimNode>(cy).strength((n) => (n.isAssoc ? 0.09 : 0.03)))
      .alphaDecay(0.02)
      .on("tick", () => {
        // resize 时不重建仿真，几何中心改为每帧从 ref 读取实时值。
        const cx = dimsRef.current.width / 2;
        const cy = dimsRef.current.height / 2;
        // 1. Hard geofence constraints
        nodes.forEach((node) => {
          if (node.isFocal) return;

          const nx = node.x ?? cx;
          const ny = node.y ?? cy;
          const dx = nx - cx;
          const dy = ny - cy;
          const dist = Math.sqrt(dx * dx + dy * dy);

          const { w, h } = getNodeDimensions(node);
          const halfDiagonal = Math.sqrt((w * w + h * h) / 4);

          if (node.isInternal) {
            // Keep inside geofence: center distance + halfDiagonal must be <= CONTAINER_RADIUS
            // Add a small safety buffer of 4px
            const maxDist = Math.max(10, CONTAINER_RADIUS - halfDiagonal - 4);
            if (dist > maxDist && dist > 0) {
              node.x = cx + (dx / dist) * maxDist;
              node.y = cy + (dy / dist) * maxDist;
            }
          } else {
            // Keep outside geofence: center distance - halfDiagonal must be >= CONTAINER_RADIUS + margin
            const minDist = CONTAINER_RADIUS + halfDiagonal + 20; // 20px safety margin
            if (dist < minDist && dist > 0) {
              node.x = cx + (dx / dist) * minDist;
              node.y = cy + (dy / dist) * minDist;
            }
          }
        });

        // 1b. 圆形容器的标题框挂在圆上沿外面，上面的圆形围栏管不到它；外圈卡片碰到它就推开
        //     标题框尺寸在仿真期间不变，量一次就缓存，免得每个 tick 强制重排
        if (!headerBox) {
          const el = geofenceHeaderRef.current;
          if (el && el.offsetWidth > 0) {
            headerBox = { w: el.offsetWidth, h: el.offsetHeight, top: el.offsetTop };
          }
        }
        if (headerBox) {
          const hw = headerBox.w / 2 + 16;
          const hh = headerBox.h / 2 + 12;
          const hcx = cx;
          const hcy = cy - CONTAINER_RADIUS + headerBox.top + headerBox.h / 2;
          nodes.forEach((node) => {
            if (node.isFocal || node.isInternal) return;
            const { w, h } = getNodeDimensions(node);
            const dx = (node.x ?? hcx) - hcx;
            const dy = (node.y ?? hcy) - hcy;
            const overlapX = hw + w / 2 - Math.abs(dx);
            const overlapY = hh + h / 2 - Math.abs(dy);
            if (overlapX <= 0 || overlapY <= 0) return;
            // 标题框下面是圆，往下推会被圆形围栏再推回来，来回打架。
            // 所以卡片在标题框中线以上、且上下重叠更少时，整张推到框的上面；其余都往旁边推出框外
            if (dy < 0 && overlapY < overlapX) node.y = hcy - (hh + h / 2);
            else node.x = hcx + (dx >= 0 ? 1 : -1) * (hw + w / 2);
          });
        }

        // 2. Resolve rectangular collisions to prevent overlapping
        const boxes = nodes
          .filter((n) => !n.isFocal)
          .map((node) => {
            const { w, h } = getNodeDimensions(node);
            return {
              node,
              w,
              h,
              padX: node.isInternal ? 20 : 20,
              padY: node.isInternal ? 24 : 14,
            };
          });

        const iterations = 5;
        for (let iter = 0; iter < iterations; iter++) {
          for (let i = 0; i < boxes.length; i++) {
            for (let j = i + 1; j < boxes.length; j++) {
              const boxA = boxes[i];
              const boxB = boxes[j];
              const nodeA = boxA.node;
              const nodeB = boxB.node;

              const ax = nodeA.x ?? 0;
              const ay = nodeA.y ?? 0;
              const bx = nodeB.x ?? 0;
              const by = nodeB.y ?? 0;

              const dx = bx - ax;
              const dy = by - ay;

              const minDistanceX = (boxA.w + boxB.w) / 2 + boxA.padX + boxB.padX;
              const minDistanceY = (boxA.h + boxB.h) / 2 + boxA.padY + boxB.padY;

              const overlapX = minDistanceX - Math.abs(dx);
              const overlapY = minDistanceY - Math.abs(dy);

              if (overlapX > 0 && overlapY > 0) {
                if (overlapX < overlapY) {
                  const pushX = overlapX;
                  const dirX = dx >= 0 ? 1 : -1;
                  
                  if (nodeA.fx !== undefined && nodeB.fx === undefined) {
                    nodeB.x = (nodeB.x ?? 0) + pushX * dirX;
                  } else if (nodeB.fx !== undefined && nodeA.fx === undefined) {
                    nodeA.x = (nodeA.x ?? 0) - pushX * dirX;
                  } else if (nodeA.fx === undefined && nodeB.fx === undefined) {
                    nodeA.x = (nodeA.x ?? 0) - pushX * 0.5 * dirX;
                    nodeB.x = (nodeB.x ?? 0) + pushX * 0.5 * dirX;
                  }
                } else {
                  const pushY = overlapY;
                  const dirY = dy >= 0 ? 1 : -1;

                  if (nodeA.fy !== undefined && nodeB.fy === undefined) {
                    nodeB.y = (nodeB.y ?? 0) + pushY * dirY;
                  } else if (nodeB.fy !== undefined && nodeA.fy === undefined) {
                    nodeA.y = (nodeA.y ?? 0) - pushY * dirY;
                  } else if (nodeA.fy === undefined && nodeB.fy === undefined) {
                    nodeA.y = (nodeA.y ?? 0) - pushY * 0.5 * dirY;
                    nodeB.y = (nodeB.y ?? 0) + pushY * 0.5 * dirY;
                  }
                }
              }
            }
          }
        }

        // 3. Update DOM node positions
        // 仿真算出来的坐标是小数，再配上 translate(-50%, -50%)（奇数宽度会落在半个像素上），
        // 每张卡片的文字都落在不同的亚像素位置，于是有的清楚有的发虚。
        // 这里把左上角吸附到物理像素（按 devicePixelRatio，125% 缩放下 1 个 CSS 像素 = 1.25 个物理像素），
        // 卡片尺寸用已缓存的整数宽高，自己算居中，不再用百分比平移。
        const dpr = window.devicePixelRatio || 1;
        const snap = (v: number) => Math.round(v * dpr) / dpr;
        nodes.forEach((node) => {
          if (node.isFocal) return;
          const el = nodesRef.current.get(node.id);
          if (el) {
            const { w, h } = getNodeDimensions(node);
            el.style.transform = `translate(${snap((node.x ?? 0) - w / 2)}px, ${snap((node.y ?? 0) - h / 2)}px)`;
          }
        });

        // 4. Update SVG link positions and text midpoints
        const svg = svgRef.current;
        if (!svg) return;
        const lineEls = svg.querySelectorAll("line");
        const textEls = svg.querySelectorAll(".link-label");
        // 标签背景与否定徽章也按 DOM 顺序一次性取出，避免在每条连线的循环里
        // 反复 querySelectorAll 全树扫描（每 tick O(连线数) 次）。
        const bgEls = svg.querySelectorAll<SVGRectElement>(".link-label-bg");
        const badgeEls = svg.querySelectorAll<SVGGElement>(".negated-badge");
        let textIdx = 0;
        let negatedIdx = 0;

        links.forEach((link, i) => {
          const isInternal = link.kind.startsWith("internal");
          const hasLabel = (isInternal && link.short_code) || (!isInternal && link.relation_name);
          const isNegated = link.status === "negated";
          // 标签/徽章的游标必须按 DOM 顺序为每条带标签/否定的连线推进，
          // 与 dist 是否为 0、lineEl 是否已渲染无关，否则后续连线的标签会整体错位。
          const labelIdx = hasLabel ? textIdx++ : -1;
          const badgeIdx = isNegated ? negatedIdx++ : -1;

          const lineEl = lineEls[i];
          if (!lineEl) return;
          const s = link.source as SimNode;
          const t = link.target as SimNode;

          const sx = s.x ?? 0;
          const sy = s.y ?? 0;
          const tx = t.x ?? 0;
          const ty = t.y ?? 0;

          const dx = tx - sx;
          const dy = ty - sy;
          const dist = Math.sqrt(dx * dx + dy * dy);

          if (dist > 0) {
            if (s.isFocal || t.isFocal) {
              // External link: connects neighbor to geofence boundary
              const neighborNode = s.isFocal ? t : s;
              const ndx = (neighborNode.x ?? 0) - cx;
              const ndy = (neighborNode.y ?? 0) - cy;
              const ndist = Math.sqrt(ndx * ndx + ndy * ndy);

              if (ndist > 0) {
                // 焦点那一端：有成员时落在圆边上；卡片模式落在卡片的矩形边上（外扩 6px）
                let gx: number;
                let gy: number;
                if (hasMembers) {
                  gx = cx + (ndx / ndist) * CONTAINER_RADIUS;
                  gy = cy + (ndy / ndist) * CONTAINER_RADIUS;
                } else {
                  const { w: fw, h: fh } = focalBoxRef.current;
                  const tx0 = Math.abs(ndx) > 0 ? (fw / 2 + 6) / Math.abs(ndx) : Infinity;
                  const ty0 = Math.abs(ndy) > 0 ? (fh / 2 + 6) / Math.abs(ndy) : Infinity;
                  const tt = Math.min(tx0, ty0);
                  gx = cx + ndx * tt;
                  gy = cy + ndy * tt;
                }

                const neighborDim = getNodeDimensions(neighborNode);
                const absDx = Math.abs(ndx);
                const absDy = Math.abs(ndy);
                const tX = absDx > 0 ? (neighborDim.w / 2) / absDx : Infinity;
                const tY = absDy > 0 ? (neighborDim.h / 2) / absDy : Infinity;
                const intersectT = Math.min(tX, tY);
                const offset = intersectT * ndist + 8;

                // If neighborNode is internal, the boundary of the container is further outwards,
                // so the edge of the node facing the boundary is in the +nd direction.
                // If it's external, the boundary is towards the center, so it's in the -nd direction.
                const dirSign = neighborNode.isInternal ? -1 : 1;
                const rx = (neighborNode.x ?? 0) - dirSign * (ndx / ndist) * offset;
                const ry = (neighborNode.y ?? 0) - dirSign * (ndy / ndist) * offset;

                if (s.isFocal) {
                  lineEl.setAttribute("x1", String(gx));
                  lineEl.setAttribute("y1", String(gy));
                  lineEl.setAttribute("x2", String(rx));
                  lineEl.setAttribute("y2", String(ry));
                } else {
                  lineEl.setAttribute("x1", String(rx));
                  lineEl.setAttribute("y1", String(ry));
                  lineEl.setAttribute("x2", String(gx));
                  lineEl.setAttribute("y2", String(gy));
                }
              }
            } else {
              // Internal link: connects two internal sub-elements
              const sourceDim = getNodeDimensions(s);
              const targetDim = getNodeDimensions(t);
              const absDx = Math.abs(dx);
              const absDy = Math.abs(dy);
              
              // Target intersection offset
              const tX = absDx > 0 ? (targetDim.w / 2) / absDx : Infinity;
              const tY = absDy > 0 ? (targetDim.h / 2) / absDy : Infinity;
              const intersectT = Math.min(tX, tY);
              const offsetT = intersectT * dist + 8;

              // Source intersection offset
              const sX = absDx > 0 ? (sourceDim.w / 2) / absDx : Infinity;
              const sY = absDy > 0 ? (sourceDim.h / 2) / absDy : Infinity;
              const intersectS = Math.min(sX, sY);
              const offsetS = intersectS * dist + 8;

              if (dist > offsetT + offsetS) {
                const ratio1 = offsetS / dist;
                const ratio2 = (dist - offsetT) / dist;
                const x1 = sx + dx * ratio1;
                const y1 = sy + dy * ratio1;
                const x2 = sx + dx * ratio2;
                const y2 = sy + dy * ratio2;

                lineEl.setAttribute("x1", String(x1));
                lineEl.setAttribute("y1", String(y1));
                lineEl.setAttribute("x2", String(x2));
                lineEl.setAttribute("y2", String(y2));
              } else {
                lineEl.setAttribute("x1", String(sx));
                lineEl.setAttribute("y1", String(sy));
                lineEl.setAttribute("x2", String(sx));
                lineEl.setAttribute("y2", String(sy));
              }
            }

            // Update text label midpoint for internal and relation links
            const x1 = parseFloat(lineEl.getAttribute("x1") || "0");
            const y1 = parseFloat(lineEl.getAttribute("y1") || "0");
            const x2 = parseFloat(lineEl.getAttribute("x2") || "0");
            const y2 = parseFloat(lineEl.getAttribute("y2") || "0");

            if (hasLabel) {
              const rectEl = bgEls[labelIdx];
              const textEl = textEls[labelIdx];
              if (textEl && rectEl) {
                const mx = (x1 + x2) / 2;
                const my = (y1 + y2) / 2;
                const ldx = x2 - x1;
                const ldy = y2 - y1;
                const len = Math.sqrt(ldx * ldx + ldy * ldy);
                const perpX = len > 0 ? -(ldy / len) * 14 : 0;
                const perpY = len > 0 ? (ldx / len) * 14 : -14;
                const labelX = mx + perpX;
                const labelY = my + perpY;
                textEl.setAttribute("x", String(labelX));
                textEl.setAttribute("y", String(labelY));
                if (rectEl) {
                  const labelText = isInternal
                    ? (link.short_code || "")
                    : (link.relation_name || "");
                  const textLen = estimateStringWidth(labelText, 10) + 12;
                  const rectH = 18;
                  rectEl.setAttribute("x", String(labelX - textLen / 2));
                  rectEl.setAttribute("y", String(labelY - rectH / 2));
                  rectEl.setAttribute("width", String(textLen));
                  rectEl.setAttribute("height", String(rectH));
                }
              }
            }

            if (isNegated) {
              const badgeEl = badgeEls[badgeIdx];
              if (badgeEl) {
                const mx = (x1 + x2) / 2;
                const my = (y1 + y2) / 2;
                badgeEl.setAttribute("transform", `translate(${mx}, ${my})`);
              }
            }
          } else {
            lineEl.setAttribute("x1", String(sx));
            lineEl.setAttribute("y1", String(sy));
            lineEl.setAttribute("x2", String(tx));
            lineEl.setAttribute("y2", String(ty));
          }
        });
      });

    sim.on("end", () => {
      if (!autoFitPendingRef.current) return;
      autoFitPendingRef.current = false;
      const { width, height } = dimsRef.current;
      const ccx = width / 2;
      const ccy = height / 2;
      let ext = { x: CONTAINER_RADIUS, y: CONTAINER_RADIUS + 60 };
      nodes.forEach((node) => {
        if (node.isFocal) return;
        const { w, h } = getNodeDimensions(node);
        ext = {
          x: Math.max(ext.x, Math.abs((node.x ?? ccx) - ccx) + w / 2),
          y: Math.max(ext.y, Math.abs((node.y ?? ccy) - ccy) + h / 2),
        };
      });
      const margin = 28;
      const visibleWidth = Math.max(240, width - rightInsetRef.current);
      const fit = Math.min((visibleWidth / 2 - margin) / ext.x, (height / 2 - margin) / ext.y);
      setPan({ x: 0, y: 0 });
      // 只缩小不放大；最小 0.8，再小字就读不清了，宁可让最外圈的几张出屏、靠拖动去看
      setZoom(Math.max(0.8, Math.min(1, fit)));
    });

    simRef.current = sim;

    return () => {
      sim.stop();
    };
  }, [data, CONTAINER_RADIUS, hasMembers]);

  // resize 只更新中心相关的力与焦点节点坐标，再轻推一下 alpha，
  // 不重建整张仿真——避免每次 ResizeObserver 触发都把节点弹回初始圆环。
  useEffect(() => {
    const sim = simRef.current;
    if (!sim) return;
    const cx = dimensions.width / 2;
    const cy = dimensions.height / 2;

    const focal = focalNodeRef.current;
    if (focal) {
      focal.x = cx;
      focal.y = cy;
      focal.fx = cx;
      focal.fy = cy;
    }

    sim.force("center", forceCenter(cx, cy).strength(0.06));
    (sim.force("x") as ReturnType<typeof forceX<SimNode>> | undefined)?.x(cx);
    (sim.force("y") as ReturnType<typeof forceY<SimNode>> | undefined)?.y(cy);
    sim.alpha(0.3).restart();
  }, [dimensions]);

  const handleWheel = useCallback((e: React.WheelEvent) => {
    e.preventDefault();
    autoFitPendingRef.current = false;
    setZoom((z) => Math.max(0.3, Math.min(3, z - e.deltaY * 0.001)));
  }, []);

  const handleMouseDown = useCallback(
    (e: React.MouseEvent) => {
      if (e.button !== 0) return;
      if ((e.target as HTMLElement).closest(".dissection-node")) return;
      isPanning.current = true;
      panStart.current = { x: e.clientX, y: e.clientY };
      panOrigin.current = { ...pan };
    },
    [pan],
  );

  const handleMouseMove = useCallback((e: React.MouseEvent) => {
    if (!isPanning.current) return;
    autoFitPendingRef.current = false;
    setPan({
      x: panOrigin.current.x + (e.clientX - panStart.current.x),
      y: panOrigin.current.y + (e.clientY - panStart.current.y),
    });
  }, []);

  const handleMouseUp = useCallback(() => {
    isPanning.current = false;
  }, []);

  // Handle node dragging
  const handleNodeMouseDown = useCallback(
    (e: React.MouseEvent, node: SimNode) => {
      e.stopPropagation();
      if (!simRef.current) return;
      autoFitPendingRef.current = false;
      
      const sim = simRef.current;
      sim.alphaTarget(0.3).restart();
      
      node.fx = node.x;
      node.fy = node.y;
      
      const startTime = Date.now();
      const startX = e.clientX;
      const startY = e.clientY;
      const initFx = node.fx ?? 0;
      const initFy = node.fy ?? 0;
      
      const handleMouseMove = (moveEvent: MouseEvent) => {
        const dx = (moveEvent.clientX - startX) / zoom;
        const dy = (moveEvent.clientY - startY) / zoom;
        node.fx = initFx + dx;
        node.fy = initFy + dy;
      };
      
      const handleMouseUp = (upEvent: MouseEvent) => {
        window.removeEventListener("mousemove", handleMouseMove);
        window.removeEventListener("mouseup", handleMouseUp);
        dragCleanupRef.current = null;

        sim.alphaTarget(0);
        node.fx = undefined;
        node.fy = undefined;

        // Calculate drag distance and click duration
        const dx = upEvent.clientX - startX;
        const dy = upEvent.clientY - startY;
        const dist = Math.sqrt(dx * dx + dy * dy);
        const duration = Date.now() - startTime;

        // If the movement is small and release is fast, it is a click.
        // We use a threshold of 6px and 300ms to be robust against tiny trackpad shakes.
        if (dist < 6 && duration < 300) {
          onNavigate(node.id);
        }
      };
      
      window.addEventListener("mousemove", handleMouseMove);
      window.addEventListener("mouseup", handleMouseUp);
      dragCleanupRef.current = () => {
        window.removeEventListener("mousemove", handleMouseMove);
        window.removeEventListener("mouseup", handleMouseUp);
        sim.alphaTarget(0);
      };
    },
    [zoom, onNavigate]
  );

  // 卸载时若仍在拖拽，清掉残留的 window 监听器并把仿真 alphaTarget 复位。
  useEffect(() => {
    return () => dragCleanupRef.current?.();
  }, []);

  const statusColor = (s: string | null) => {
    if (s === "confirmed") return "var(--status-confirmed)";
    if (s === "hypothesis") return "var(--status-hypothesis)";
    if (s === "negated") return "var(--status-negated)";
    return "var(--text-dim)";
  };

  const handleNodeRef = useCallback(
    (id: number) => (el: HTMLDivElement | null) => {
      if (el) nodesRef.current.set(id, el);
      else nodesRef.current.delete(id);
    },
    [],
  );

  if (loadError) {
    return (
      <div className="dissection-loading dissection-error">
        <button className="back-btn" onClick={onBack}>
          <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M10 3.5 5.5 8l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Map
        </button>
        <p className="dissection-error-title">Couldn’t open memory #{focalId}</p>
        <p className="dissection-error-detail">{loadError}</p>
      </div>
    );
  }

  if (!data) {
    return (
      <div className="dissection-loading">
        <div className="loading-spinner" />
      </div>
    );
  }

  const focal = data.focal;
  const internalMembersList = getInternalMembers(focal);
  const cx = dimensions.width / 2;
  const cy = dimensions.height / 2;
  const dpr = window.devicePixelRatio || 1;
  const snapPx = (v: number) => Math.round(v * dpr) / dpr;

  return (
    <div
      ref={containerRef}
      className="dissection-container"
      // 点阵底跟着平移/缩放走，画布才像一整张可以推拉的桌面
      style={{
        // 侧栏开合时的整体左移不带上点阵：点阵均匀，挪不挪看不出，跟着跳反而和滑动的图对不上
        backgroundPosition: `${pan.x + dimensions.width / 2}px ${pan.y + dimensions.height / 2}px`,
        backgroundSize: `${GRID_SIZE * zoom}px ${GRID_SIZE * zoom}px`,
      }}
      onWheel={handleWheel}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
    >
      <button className="back-btn" onClick={onBack}>
        <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
          <path d="M10 3.5 5.5 8l4.5 4.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
        </svg>
        Map
      </button>

      {simNodes.some((n) => n.isAssoc) && (
        <div className="reading-flow-hint" aria-hidden="true" style={{ right: rightInset }}>
          <span>Often read before it</span>
          <svg viewBox="0 0 72 12" width="72" height="12">
            <path d="M2 6h22M19 2.5 24 6l-5 3.5M48 6h22M65 2.5 70 6l-5 3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" />
            <circle cx="36" cy="6" r="4" fill="currentColor" />
          </svg>
          <span>Often read after it</span>
        </div>
      )}

      {/* 侧栏开着时整张图左移，单独一层做，平移/拖动的 transform 不用带过渡 */}
      <div
        className="dissection-shift"
        // 不挪时写 none，不留一个变换图层（留着文字会按位图渲染、发虚）
        style={{ transform: viewShift ? `translateX(${Math.round(viewShift * dpr) / dpr}px)` : "none" }}
      >
      <div
        className="dissection-canvas"
        style={{
          // 平移也吸附到物理像素，缩放为 1 时文字才不会落在半像素上
          transform: `translate(${Math.round(pan.x * dpr) / dpr}px, ${Math.round(pan.y * dpr) / dpr}px) scale(${zoom})`,
          transformOrigin: `${dimensions.width / 2}px ${dimensions.height / 2}px`,
        }}
      >
        <svg
          ref={svgRef}
          className="dissection-links"
          width={dimensions.width}
          height={dimensions.height}
        >
          <defs>
            {/* 联想线的箭头：大小固定（userSpaceOnUse），不随线宽变 */}
            <marker
              id="arrow-assoc"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="9"
              markerHeight="9"
              markerUnits="userSpaceOnUse"
              orient="auto-start-reverse"
            >
              <path d="M1 1.5 9 5 1 8.5" fill="none" className="assoc-arrow" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
            </marker>
            <marker
              id="arrowhead-confirmed"
              viewBox="0 0 10 7"
              refX="10"
              refY="3.5"
              markerWidth="9"
              markerHeight="7"
              orient="auto"
            >
              <polygon points="0 0, 10 3.5, 0 7" fill="var(--status-confirmed)" />
            </marker>
            <marker
              id="arrowhead-hypothesis"
              viewBox="0 0 10 7"
              refX="10"
              refY="3.5"
              markerWidth="9"
              markerHeight="7"
              orient="auto"
            >
              <polygon points="0 0, 10 3.5, 0 7" fill="var(--status-hypothesis)" />
            </marker>
            <marker
              id="arrowhead-negated"
              viewBox="0 0 10 7"
              refX="10"
              refY="3.5"
              markerWidth="9"
              markerHeight="7"
              orient="auto"
            >
              <polygon points="0 0, 10 3.5, 0 7" fill="var(--status-negated)" />
            </marker>
            <marker
              id="arrowhead-default"
              viewBox="0 0 10 7"
              refX="10"
              refY="3.5"
              markerWidth="9"
              markerHeight="7"
              orient="auto"
            >
              <polygon points="0 0, 10 3.5, 0 7" fill="var(--text-secondary)" />
            </marker>
          </defs>
          {simLinks.map((link, i) => {
            const isInternal = link.kind.startsWith("internal");
            const isNegated = link.status === "negated";
            const hasLabel = (isInternal && link.short_code) || (!isInternal && link.relation_name);
            if (link.kind === "assoc") {
              // 联想线：虚线，越常一起读的越粗
              return (
                <g key={i}>
                  <line
                    className="assoc-line"
                    strokeWidth={0.8 + 1.8 * (link.strength ?? 0)}
                    strokeDasharray="2 5"
                    strokeLinecap="round"
                    markerEnd="url(#arrow-assoc)"
                    markerStart={link.twoWay ? "url(#arrow-assoc)" : undefined}
                  />
                </g>
              );
            }
            return (
              <g key={i}>
                <line
                  stroke={statusColor(link.status)}
                  strokeWidth={isInternal ? 1.8 : 1.5}
                  strokeOpacity={isInternal ? 0.8 : 0.6}
                  strokeDasharray={
                    link.status === "hypothesis"
                      ? "4 4"
                      : undefined
                  }
                  markerEnd={
                    link.kind === "directed" || link.kind === "internal-directed"
                      ? `url(#arrowhead-${["confirmed", "hypothesis", "negated"].includes(link.status ?? "") ? link.status : "default"})`
                      : undefined
                  }
                />
                {hasLabel && (
                  <g
                    className={`link-label-group ${!isInternal ? "clickable" : ""}`}
                    onClick={(e) => {
                      if (!isInternal && link.relation_id) {
                        e.stopPropagation();
                        onNavigate(link.relation_id);
                      }
                    }}
                    onMouseEnter={() => {
                      if (!isInternal && link.relation_id) {
                        onInspect(link.relation_id);
                      }
                    }}
                  >
                    <rect className="link-label-bg" style={!isInternal ? { opacity: 0.92 } : undefined} />
                    <text
                      className="link-label"
                      fill={isInternal ? "var(--t-accent)" : "var(--text-secondary)"}
                      fontSize="10px"
                      fontFamily={isInternal ? "var(--font-mono)" : "var(--font-sans)"}
                      fontWeight={isInternal ? "600" : "500"}
                      textAnchor="middle"
                      dominantBaseline="middle"
                      opacity={0.9}
                    >
                      {isInternal ? link.short_code : link.relation_name}
                    </text>
                  </g>
                )}
                {isNegated && (
                  <g className="negated-badge">
                    <circle r="8" className="negated-badge-circle" />
                    <path d="M -3.5,-3.5 L 3.5,3.5 M 3.5,-3.5 L -3.5,3.5" className="negated-badge-cross" />
                  </g>
                )}
              </g>
            );
          })}
        </svg>

        {hasMembers ? (
          /* 有成员：圆形容器，成员排在里面，标题挂在圆的上沿 */
          <div
            className={`geofence-circle ${focal.activation_type ? `mode-${focal.activation_type.toLowerCase()}` : ""}`}
            style={{
              position: "absolute",
              // 不用 translate(-50%, -50%) 居中：容器宽度是奇数时圆心会落在半个像素上，里面的标题文字就虚了
              left: snapPx(cx - CONTAINER_RADIUS),
              top: snapPx(cy - CONTAINER_RADIUS),
              width: CONTAINER_RADIUS * 2,
              height: CONTAINER_RADIUS * 2,
              borderRadius: "50%",
              pointerEvents: "none",
              zIndex: 1,
            }}
          >
            <div
              ref={geofenceHeaderRef}
              className="geofence-header"
              onClick={(e) => {
                e.stopPropagation();
                onInspect(focal.id);
              }}
              onMouseEnter={() => onInspect(focal.id)}
              style={{ pointerEvents: "auto", cursor: "pointer" }}
            >
              <div className="geofence-title">{focal.name}</div>
              <FocalMeta focal={focal} memberCount={internalMembersList.length} />
            </div>
          </div>
        ) : (
          /* 没有成员（大多数记忆）：中间一张卡片，写名字、什么时候读、状态 */
          <div
            ref={focalCardRef}
            className={`focal-card kind-${focal.role} ${focal.is_active ? "is-lit" : ""}`}
            style={{
              left: snapPx(cx - focalBox.w / 2),
              top: snapPx(cy - focalBox.h / 2),
              width: FOCAL_CARD_W,
            }}
            onClick={(e) => {
              e.stopPropagation();
              onInspect(focal.id);
            }}
            onMouseEnter={() => onInspect(focal.id)}
          >
            <div className="focal-card-title">{focal.name}</div>
            {focal.when_to_read && <p className="focal-card-when">{focal.when_to_read}</p>}
            <FocalMeta focal={focal} memberCount={0} />
          </div>
        )}

        {/* Render nodes (Internal sub-elements and External neighbors) */}
        {simNodes
          .filter((node) => !node.isFocal)
          .map((node) => (
            <div
              key={node.id}
              ref={handleNodeRef(node.id)}
              className={`dissection-node ${node.isInternal ? "internal" : node.isAssoc ? `assoc side-${node.side}` : "neighbor"} ${node.is_active ? "is-lit" : ""}`}
              onClick={(e) => {
                e.stopPropagation();
              }}
              onMouseDown={(e) => handleNodeMouseDown(e, node)}
              onMouseEnter={() => onInspect(node.id)}
            >
              <span className="node-name">{node.name}</span>
              {node.when_to_read && (
                <span className="node-when-to-read">{node.when_to_read}</span>
              )}
              {node.role && node.role !== "plain" && (
                <span className="node-circuit-meta">
                  <span>{ROLE_NAME[node.role]}</span>
                  <span className={`focal-state ${node.is_active ? "is-lit" : ""}`}>
                    <span className="focal-lamp" aria-hidden="true" />
                    {stateLabel(node.role, node.is_active)}
                  </span>
                </span>
              )}
            </div>
          ))}
      </div>
      </div>
    </div>
  );
}

const ROLE_NAME: Record<string, string> = {
  plain: "Memory",
  sensor: "Sensor",
  logic: "Logic",
  guard: "Guard",
};

/**
 * 焦点节点标题下面那一行：角色、组合方式、通电状态、体积。
 * 记忆（plain）没有电位，不写状态；体积只在太长时用红字提醒。
 */
function FocalMeta({ focal, memberCount }: { focal: ConceptDetail; memberCount: number }) {
  const isCircuit = focal.role !== "plain";
  const tooLong = (focal.byte_size ?? 0) >= OVERSIZE_BYTES;
  return (
    <div className="focal-meta">
      <span className={`focal-role kind-${focal.role}`}>{ROLE_NAME[focal.role] ?? focal.role}</span>
      {focal.activation_type && memberCount > 0 && (
        <span className={`focal-mode mode-${focal.activation_type.toLowerCase()}`}>
          {MODE_LABEL[focal.activation_type]} {memberCount}
        </span>
      )}
      {isCircuit && (
        <span className={`focal-state ${focal.is_active ? "is-lit" : ""}`}>
          <span className="focal-lamp" aria-hidden="true" />
          {stateLabel(focal.role, focal.is_active)}
        </span>
      )}
      {focal.byte_size !== undefined && focal.byte_size > 0 && (
        <span className={`focal-size ${tooLong ? "is-too-long" : ""}`} title="Size of the note">
          {formatBytes(focal.byte_size)}
          {tooLong && " — too long"}
        </span>
      )}
    </div>
  );
}
