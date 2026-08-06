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
import type { NeighborhoodData, ConceptDetail, DisclosureDetail } from "../types";
import "./DissectionView.css";

interface Props {
  focalId: number;
  onNavigate: (id: number) => void;
  onInspect: (id: number) => void;
  onBack: () => void;
}

interface SimNode extends SimulationNodeDatum {
  id: number;
  name: string;
  disclosure: string | null;
  isFocal: boolean;
  isInternal: boolean;
  degree?: number;
}

interface SimLink extends SimulationLinkDatum<SimNode> {
  status: string | null;
  kind: "directed" | "undirected" | "internal-directed" | "internal-joint" | "internal-or-joint";
  short_code?: string;
  relation_id?: number;
  relation_name?: string;
}

function joinDisclosures(discs?: DisclosureDetail[]): string | null {
  return discs && discs.length > 0 ? discs.map((d) => d.text).join("; ") : null;
}

// Helper to extract unique internal members of a focal concept's variations
const getInternalMembers = (focalConcept: ConceptDetail) => {
  const map = new Map<number, { id: number; name: string; disclosure: string | null }>();
  focalConcept.variations.forEach((v) => {
    if (v.members) {
      v.members.forEach((m) => {
        if (m.concept_id !== focalConcept.id) {
          map.set(m.concept_id, {
            id: m.concept_id,
            name: m.name,
            disclosure: joinDisclosures(m.disclosures),
          });
        }
      });
    }
  });
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
  onNavigate,
  onInspect,
  onBack,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
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
  }, []);

  useEffect(() => {
    let current = true;
    setData(null);
    setLoadError(null);
    api
      .getNeighborhood(focalId)
      .then((result) => {
        if (current) setData(result);
      })
      .catch((e) => {
        if (current) setLoadError(e instanceof Error ? e.message : String(e));
      });
    return () => { current = false; };
  }, [focalId]);

  // Calculate geofence radius dynamically based on internal sub-elements count and sizes
  const getDynamicContainerRadius = () => {
    if (!data || !data.focal) return 150;
    const internalMembersList = getInternalMembers(data.focal);
    if (internalMembersList.length === 0) return 120;

    // Calculate dimensions of each internal node
    let maxHalfDiagonal = 0;
    let totalArea = 0;

    internalMembersList.forEach((m) => {
      const h = m.disclosure ? 42 : 28;
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

  const CONTAINER_RADIUS = getDynamicContainerRadius();

  useEffect(() => {
    if (!data) return;

    const focalConcept = data.focal;
    const cx = dimsRef.current.width / 2;
    const cy = dimsRef.current.height / 2;

    // 1. Focal node (fixed at center)
    const focalNode: SimNode = {
      id: focalConcept.id,
      name: focalConcept.name,
      disclosure: joinDisclosures(focalConcept.disclosures),
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
    const internalAngle = (2 * Math.PI) / Math.max(internalMembersList.length, 1);
    const internalR = Math.min(60, CONTAINER_RADIUS * 0.4);

    internalMembersList.forEach((m, i) => {
      nodes.push({
        id: m.id,
        name: m.name,
        disclosure: m.disclosure,
        isFocal: false,
        isInternal: true,
        x: cx + Math.cos(internalAngle * i) * internalR,
        y: cy + Math.sin(internalAngle * i) * internalR,
      });
    });

    // 3. External nodes (neighbors outside the geofence)
    const externalNeighbors = data.neighbors.filter(
      (n) => !internalNodeIds.has(n.id) && n.id !== focalConcept.id
    );
    const externalAngle = (2 * Math.PI) / Math.max(externalNeighbors.length, 1);
    const orbitR = CONTAINER_RADIUS + 90;

    externalNeighbors.forEach((n, i) => {
      nodes.push({
        id: n.id,
        name: n.name,
        disclosure: joinDisclosures(n.disclosures),
        isFocal: false,
        isInternal: false,
        degree: n.degree,
        x: cx + Math.cos(externalAngle * i) * orbitR,
        y: cy + Math.sin(externalAngle * i) * orbitR,
      });
    });

    // 4. Build links
    const links: SimLink[] = [];

    // Add internal links (composition relations), type-aware
    focalConcept.variations.forEach((v) => {
      const members = v.members;
      if (!members || members.length === 0) return;

      const sorted = [...members].sort((a, b) => a.order_index - b.order_index);

      if (v.type === "CHAIN") {
        // CHAIN: directed arrows between consecutive members
        for (let i = 0; i < sorted.length - 1; i++) {
          links.push({
            source: sorted[i].concept_id,
            target: sorted[i + 1].concept_id,
            status: v.status ?? "hypothesis",
            kind: "internal-directed",
            short_code: v.short_code,
          });
        }
      } else if (v.type === "AND") {
        // AND: undirected joint links between all pairs
        for (let i = 0; i < sorted.length; i++) {
          for (let j = i + 1; j < sorted.length; j++) {
            links.push({
              source: sorted[i].concept_id,
              target: sorted[j].concept_id,
              status: v.status ?? "hypothesis",
              kind: "internal-joint",
              short_code: v.short_code,
            });
          }
        }
      } else if (v.type === "OR") {
        // OR: alternatives — connected by a distinct visual dotted/dashed link
        // so they are grouped together in force simulation but styled as alternative options.
        for (let i = 0; i < sorted.length; i++) {
          for (let j = i + 1; j < sorted.length; j++) {
            links.push({
              source: sorted[i].concept_id,
              target: sorted[j].concept_id,
              status: v.status ?? "hypothesis",
              kind: "internal-or-joint",
              short_code: v.short_code,
            });
          }
        }
      }
    });

    // Add external links (connecting external neighbors to focal concept)
    data.internal_links.forEach((l) => {
      // Add links involving the focal concept
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
        
        if (node.disclosure) {
          const discW = Math.min(160, estimateStringWidth(node.disclosure, 10));
          contentW = Math.max(contentW, discW);
          contentH += 2 + 13; // margin-top + line-height of disclosure
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
        
        if (node.disclosure) {
          const discW = Math.min(140, estimateStringWidth(node.disclosure, 10));
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
            return orbitR;
          })
          .strength((l) => {
            if (l.kind.startsWith("internal")) {
              return 0.8;
            }
            return 0.3;
          }),
      )
      .force(
        "charge",
        forceManyBody<SimNode>().strength((node) => {
          if (node.isFocal) return 0;
          if (node.isInternal) return -350;
          return -400;
        })
      )
      .force("center", forceCenter(cx, cy).strength(0.06))
      .force(
        "collide",
        forceCollide<SimNode>().radius((node) => {
          const { w, h } = getNodeDimensions(node);
          return Math.sqrt((w * w + h * h) / 4) + 24;
        })
      )
      .force("x", forceX<SimNode>(cx).strength(0.03))
      .force("y", forceY<SimNode>(cy).strength(0.03))
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
        nodes.forEach((node) => {
          if (node.isFocal) return;
          const el = nodesRef.current.get(node.id);
          if (el) {
            el.style.transform = `translate(${node.x ?? 0}px, ${node.y ?? 0}px) translate(-50%, -50%)`;
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
                const gx = cx + (ndx / ndist) * CONTAINER_RADIUS;
                const gy = cy + (ndy / ndist) * CONTAINER_RADIUS;

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
                    ? link.kind === "internal-or-joint"
                      ? `${link.short_code} (OR)`
                      : (link.short_code || "")
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

    simRef.current = sim;

    return () => {
      sim.stop();
    };
  }, [data, CONTAINER_RADIUS]);

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
          &#x2190; Galaxy
        </button>
        <p className="dissection-error-title">Failed to load concept #{focalId}</p>
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
  const cx = dimensions.width / 2;
  const cy = dimensions.height / 2;

  return (
    <div
      ref={containerRef}
      className="dissection-container"
      onWheel={handleWheel}
      onMouseDown={handleMouseDown}
      onMouseMove={handleMouseMove}
      onMouseUp={handleMouseUp}
      onMouseLeave={handleMouseUp}
    >
      <button className="back-btn" onClick={onBack}>
        &#x2190; Galaxy
      </button>

      <div
        className="dissection-canvas"
        style={{
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
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
                return (
                  <g key={i}>
                    <line
                      stroke={statusColor(link.status)}
                      strokeWidth={isInternal ? 1.5 : 1.5}
                      strokeOpacity={isInternal ? 0.6 : 0.6}
                      strokeDasharray={
                        link.kind === "internal-or-joint"
                          ? "2 6"
                          : link.status === "hypothesis"
                          ? "4 4"
                          : undefined
                      }
                      markerEnd={
                        link.kind !== "internal-joint" && link.kind !== "internal-or-joint"
                          ? `url(#arrowhead-${link.status || "default"})`
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
                          fill={
                            isInternal
                              ? link.kind === "internal-or-joint"
                                ? "var(--accent-purple)"
                                : "var(--accent-blue)"
                              : "var(--text-secondary)"
                          }
                          fontSize="10px"
                          fontFamily={isInternal ? "var(--font-mono)" : "var(--font-sans)"}
                          fontWeight={isInternal ? "600" : "500"}
                          textAnchor="middle"
                          dominantBaseline="middle"
                          opacity={0.9}
                        >
                          {isInternal
                            ? link.kind === "internal-or-joint"
                              ? `${link.short_code} (OR)`
                              : link.short_code
                            : link.relation_name}
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

        {/* Circular Geofence Container */}
        <div
          className="geofence-circle"
          style={{
            position: "absolute",
            left: cx,
            top: cy,
            width: CONTAINER_RADIUS * 2,
            height: CONTAINER_RADIUS * 2,
            transform: "translate(-50%, -50%)",
            borderRadius: "50%",
            border: "2px dashed rgba(74, 158, 255, 0.25)",
            background: "radial-gradient(circle, rgba(74, 158, 255, 0.03) 0%, rgba(0, 0, 0, 0.2) 100%)",
            boxShadow: "inset 0 0 40px rgba(74, 158, 255, 0.05), 0 0 30px rgba(74, 158, 255, 0.02)",
            pointerEvents: "none",
            zIndex: 1,
          }}
        >
          <div
            className="geofence-title"
            onClick={(e) => {
              e.stopPropagation();
              onInspect(focal.id);
            }}
            onMouseEnter={() => onInspect(focal.id)}
            style={{
              pointerEvents: "auto",
              cursor: "pointer",
            }}
          >
            {focal.name}
          </div>
        </div>

        {/* Render nodes (Internal sub-elements and External neighbors) */}
        {simNodes
          .filter((node) => !node.isFocal)
          .map((node) => (
            <div
              key={node.id}
              ref={handleNodeRef(node.id)}
              className={`dissection-node ${node.isInternal ? "internal" : "neighbor"}`}
              onClick={(e) => {
                e.stopPropagation();
              }}
              onMouseDown={(e) => handleNodeMouseDown(e, node)}
              onMouseEnter={() => onInspect(node.id)}
            >
              <span className="node-name">{node.name}</span>
              {node.disclosure && (
                <span className="node-disclosure">{node.disclosure}</span>
              )}
            </div>
          ))}
      </div>
    </div>
  );
}
