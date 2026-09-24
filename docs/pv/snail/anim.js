'use strict';
/* =====================================================================
   「蜗牛与向日葵」 — 纯代码手绘动画
   frame(t): 根据时间 t(秒) 绘制确定性的一帧（无状态、可任意跳帧）
   ===================================================================== */
const W = 1920, H = 1080, FPS = 30, DUR = 48.25;
const cv = document.getElementById('c');
const ctx = cv.getContext('2d');

/* ---------------- 数学工具 ---------------- */
const TAU = Math.PI * 2;
const clamp = (v, a = 0, b = 1) => Math.max(a, Math.min(b, v));
const lerp = (a, b, t) => a + (b - a) * t;
const seg = (t, a, b) => clamp((t - a) / (b - a));
const eio = t => t < .5 ? 2 * t * t : 1 - Math.pow(-2 * t + 2, 2) / 2;
const eo = t => 1 - (1 - t) * (1 - t);
const ei = t => t * t;
const eob = t => { const c1 = 1.9, c3 = c1 + 1; return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2); };
const eel = t => t <= 0 ? 0 : t >= 1 ? 1 : Math.pow(2, -9 * t) * Math.sin((t * 9 - 0.75) * TAU / 3) + 1;
function h1(x) { const s = Math.sin(x * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); }
function nz(x, s = 0) { const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f); return lerp(h1(i + s * 57.13), h1(i + 1 + s * 57.13), u); }
const P2 = (a, b, t) => [lerp(a[0], b[0], t), lerp(a[1], b[1], t)];
function qpt(P, u) { const [a, b, c] = P, m = 1 - u; return [m * m * a[0] + 2 * m * u * b[0] + u * u * c[0], m * m * a[1] + 2 * m * u * b[1] + u * u * c[1]]; }
function qtan(P, u) { const [a, b, c] = P; const x = 2 * (1 - u) * (b[0] - a[0]) + 2 * u * (c[0] - b[0]), y = 2 * (1 - u) * (b[1] - a[1]) + 2 * u * (c[1] - b[1]); const L = Math.hypot(x, y) || 1; return [x / L, y / L]; }
function qb(a, b, c, n = 20) { const o = []; for (let i = 0; i <= n; i++) o.push(qpt([a, b, c], i / n)); return o; }
function ell(cx, cy, rx, ry, rot = 0, a0 = 0, a1 = TAU, n = 44) {
  const o = [], c = Math.cos(rot), s = Math.sin(rot);
  for (let i = 0; i <= n; i++) { const a = a0 + (a1 - a0) * i / n, x = Math.cos(a) * rx, y = Math.sin(a) * ry; o.push([cx + x * c - y * s, cy + x * s + y * c]); }
  return o;
}
// Catmull-Rom 平滑
function cr(pts, n = 6, closed = false) {
  const o = [], L = pts.length;
  const g = i => closed ? pts[(i + L) % L] : pts[clamp(i, 0, L - 1)];
  const last = closed ? L : L - 1;
  for (let i = 0; i < last; i++) {
    const p0 = g(i - 1), p1 = g(i), p2 = g(i + 1), p3 = g(i + 2);
    for (let k = 0; k < n; k++) {
      const t = k / n, t2 = t * t, t3 = t2 * t;
      o.push([0, 1].map(j => 0.5 * ((2 * p1[j]) + (-p0[j] + p2[j]) * t + (2 * p0[j] - 5 * p1[j] + 4 * p2[j] - p3[j]) * t2 + (-p0[j] + 3 * p1[j] - 3 * p2[j] + p3[j]) * t3)));
    }
  }
  if (!closed) o.push(pts[L - 1]);
  return o;
}
function angLerp(a, b, t) { let d = b - a; while (d > Math.PI) d -= TAU; while (d < -Math.PI) d += TAU; return a + d * t; }
// 给定“头顶方向”up 与行进方向 tan，求蜗牛的 rot 与 dir
function poseOn(up, tan) {
  const r = Math.atan2(up[0], -up[1]);
  const d = (Math.cos(r) * tan[0] + Math.sin(r) * tan[1]) >= 0 ? 1 : -1;
  return { rot: r, dir: d };
}

/* ---------------- 颜色 ---------------- */
const PAPER = '#F3ECDD', INK = '#29262C', GRAPH = '#7D776C', BODY = '#ECE5D5', SHELL = '#D5CBB6',
  PLANT = '#D9D2C0', DISK = '#A39A8C', EYEW = '#FFFCF4', YEL = '#F7B614';
const WASH = [0.80, 0.82, 0.88];     // 黎明前的冷灰“夜色”乘数
let N = 1;                            // 夜色强度 0..1
function hex(h) { return [parseInt(h.slice(1, 3), 16), parseInt(h.slice(3, 5), 16), parseInt(h.slice(5, 7), 16)]; }
const HEXC = {};
function nm(h, a = 1) { const c = HEXC[h] || (HEXC[h] = hex(h)); return `rgba(${c.map((v, i) => Math.round(v * lerp(1, WASH[i], N))).join(',')},${a})`; }
function mixY(h, k, a = 1) { const c = HEXC[h] || (HEXC[h] = hex(h)), y = hex(YEL); return `rgba(${c.map((v, i) => Math.round(lerp(v * lerp(1, WASH[i], N), y[i], k))).join(',')},${a})`; }
function yel(a = 1) { return `rgba(247,182,20,${a})`; }

/* ---------------- 手绘线条核心 ---------------- */
let cam = { x: 0, y: 0, z: 1 }, SC = 1, BOIL = 0, T = 0;
function resample(pts, step, closed) {
  const P = closed ? pts.concat([pts[0]]) : pts, out = []; let acc = 0;
  for (let i = 0; i < P.length - 1; i++) {
    const [x0, y0] = P[i], [x1, y1] = P[i + 1], dx = x1 - x0, dy = y1 - y0, L = Math.hypot(dx, dy) || 1e-6;
    const nx = -dy / L, ny = dx / L, n = Math.max(1, Math.ceil(L / step));
    for (let k = 0; k < n; k++) out.push([x0 + dx * k / n, y0 + dy * k / n, nx, ny, acc + L * k / n]);
    acc += L;
  }
  const l = P[P.length - 1], pn = out.length ? out[out.length - 1] : [0, 0, 0, 1];
  out.push([l[0], l[1], pn[2], pn[3], acc]);
  return out;
}
function jit(pts, j, s, closed) {
  const z = cam.z * SC, J = j / z, R = resample(pts, 8 / z, closed);
  return R.map(p => {
    const d = p[4] * z;
    const n = (nz(d / 70 + s * 13.7, BOIL + s) - .5) * 2 + (nz(d / 16 + s * 5.1, BOIL * 1.7 + s + 9) - .5) * .6;
    return [p[0] + p[2] * n * J, p[1] + p[3] * n * J];
  });
}
function path(P, closed) { ctx.beginPath(); ctx.moveTo(P[0][0], P[0][1]); for (let i = 1; i < P.length; i++) ctx.lineTo(P[i][0], P[i][1]); if (closed) ctx.closePath(); }
// 描边：o.w 屏幕线宽，o.col 颜色，o.c 闭合，o.j 抖动，o.s 种子，o.a 透明度，o.d=0 关闭第二遍铅笔
function sk(pts, o = {}) {
  if (!pts || pts.length < 2) return;
  const z = cam.z * SC, s = o.s || 0, j = (o.j ?? 1.2) * 1.45;
  ctx.lineJoin = 'round'; ctx.lineCap = 'round';
  path(jit(pts, j, s, o.c), o.c);
  ctx.strokeStyle = o.raw || nm(o.col || INK, o.a ?? 1); ctx.lineWidth = (o.w ?? 2.6) / z; ctx.stroke();
  if (o.d !== 0) {
    path(jit(pts, j * 1.8, s + 3.3, o.c), o.c);
    ctx.lineWidth = (o.w ?? 2.6) * .4 / z; ctx.strokeStyle = o.raw || nm(o.col || INK, (o.a ?? 1) * .4); ctx.stroke();
  }
}
// 填色：带“套色偏移”的手绘填充
function fl(pts, col, o = {}) {
  const z = cam.z * SC;
  ctx.save(); ctx.translate((o.ox ?? 0) / z, (o.oy ?? 0) / z);
  path(jit(pts, o.j ?? .8, (o.s || 0) + 7, true), true); ctx.fillStyle = col; ctx.fill(); ctx.restore();
}
// 铅笔排线
function hatch(pts, o = {}) {
  const z = cam.z * SC; ctx.save(); path(pts, true); ctx.clip();
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const p of pts) { x0 = Math.min(x0, p[0]); y0 = Math.min(y0, p[1]); x1 = Math.max(x1, p[0]); y1 = Math.max(y1, p[1]); }
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2, R = Math.hypot(x1 - x0, y1 - y0) / 2 + 2, sp = (o.sp ?? 7) / z, a = o.ang ?? -0.95;
  const ca = Math.cos(a), sa = Math.sin(a); let i = 0;
  for (let k = -R; k <= R; k += sp, i++) {
    const bx = cx - sa * k, by = cy + ca * k;
    sk([[bx - ca * R, by - sa * R], [bx + ca * R, by + sa * R]], { w: o.w ?? 1.1, col: o.col || GRAPH, a: o.a ?? .7, d: 0, s: i * 1.3 + (o.s || 0), j: 1.4, raw: o.raw });
  }
  ctx.restore();
}

/* ---------------- 纸张纹理（生成一次） ---------------- */
let TEX = null;
function makeTexture() {
  const c = document.createElement('canvas'); c.width = W; c.height = H; const g = c.getContext('2d');
  g.fillStyle = '#fff'; g.fillRect(0, 0, W, H);
  let seed = 12345; const rnd = () => { seed = (seed * 16807) % 2147483647; return seed / 2147483647; };
  for (let i = 0; i < 90; i++) {
    const x = rnd() * W, y = rnd() * H, r = 80 + rnd() * 320, gr = g.createRadialGradient(x, y, 0, x, y, r);
    gr.addColorStop(0, `rgba(150,130,100,${0.01 + rnd() * 0.018})`); gr.addColorStop(1, 'rgba(150,130,100,0)');
    g.fillStyle = gr; g.fillRect(x - r, y - r, 2 * r, 2 * r);
  }
  const id = g.getImageData(0, 0, W, H), d = id.data;
  for (let i = 0; i < d.length; i += 4) { const n = rnd() * 16 + (rnd() < 0.004 ? 40 : 0); d[i] -= n; d[i + 1] -= n; d[i + 2] -= n * 1.2; }
  g.putImageData(id, 0, 0);
  g.lineWidth = 1;
  for (let i = 0; i < 700; i++) {
    const x = rnd() * W, y = rnd() * H, a = rnd() * TAU, l = 6 + rnd() * 22;
    g.strokeStyle = `rgba(120,105,80,${0.05 + rnd() * 0.1})`; g.beginPath(); g.moveTo(x, y);
    g.quadraticCurveTo(x + Math.cos(a) * l * .5 + rnd() * 6, y + Math.sin(a) * l * .5 + rnd() * 6, x + Math.cos(a) * l, y + Math.sin(a) * l); g.stroke();
  }
  const vg = g.createRadialGradient(W / 2, H / 2, H * 0.45, W / 2, H / 2, H * 1.05);
  vg.addColorStop(0, 'rgba(0,0,0,0)'); vg.addColorStop(1, 'rgba(110,90,60,0.28)');
  g.fillStyle = vg; g.fillRect(0, 0, W, H);
  TEX = c;
}

/* =====================================================================
   世界几何
   ===================================================================== */
const stemX = y => 8 * Math.sin(y * 0.004);
const STEM_TOP = -1350;
const HC = [0, -1450], HRX = 300, HRY = 118;          // 花盘（透视椭圆）
// 叶子1：出错点 M1
function leaf1P(d) { return [[-14, -262], [-140, -330 + 20 * d], [-262, -292 + 130 * d]]; }
function onLeaf1(u, d) {
  const P = leaf1P(d), p = qpt(P, u), t = qtan(P, u);
  let n = [t[1], -t[0]]; if (n[1] > 0) n = [-n[0], -n[1]];
  return { x: p[0] + n[0] * 5, y: p[1] + n[1] * 5, up: n, tan: t };
}
// 高草：出错点 M2
const GR = [-520, 0], GL = 740;
function grassP(a) { const tip = [GR[0] - GL * Math.sin(a), GR[1] - GL * Math.cos(a)]; const ca = a * 0.35; return [GR, [GR[0] - GL * .55 * Math.sin(ca), GR[1] - GL * .55 * Math.cos(ca)], tip]; }
function onGrass(a, u) {
  const P = grassP(a), p = qpt(P, u), t = qtan(P, u);
  let n = [-t[1], t[0]]; if (n[0] < 0) n = [-n[0], -n[1]];  // 朝右侧（面向镜头一侧）
  const w = lerp(13, 2, u);
  return { x: p[0] + n[0] * w, y: p[1] + n[1] * w, up: n, tan: t };
}
function cupAt(a) { const P = grassP(a), tip = P[2], ax = qtan(P, 1); return { tip, ax }; }
// 花盘下沿（苞片）：出错点 M3
const BR = { cx: 0, cy: -1418, rx: 196, ry: 72 };
function underY(x) { const k = clamp(1 - Math.pow(x / BR.rx, 2), 0, 1); return BR.cy + BR.ry * Math.sqrt(k); }
// 纸轨道（机关 C3）
const TRK = cr([[-300, -1252], [-368, -1306], [-410, -1386], [-420, -1466], [-392, -1542], [-322, -1594], [-232, -1606], [-150, -1580]], 12);
const TRKL = (() => { const o = [0]; for (let i = 1; i < TRK.length; i++) o.push(o[i - 1] + Math.hypot(TRK[i][0] - TRK[i - 1][0], TRK[i][1] - TRK[i - 1][1])); return o; })();
function trackAt(p) {
  const L = TRKL[TRKL.length - 1] * clamp(p); let i = 1; while (i < TRKL.length - 1 && TRKL[i] < L) i++;
  const k = (L - TRKL[i - 1]) / ((TRKL[i] - TRKL[i - 1]) || 1), a = TRK[i - 1], b = TRK[i];
  const pt = P2(a, b, k), dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1, t = [dx / l, dy / l];
  let n = [-t[1], t[0]]; const c = [-230, -1440]; if ((c[0] - pt[0]) * n[0] + (c[1] - pt[1]) * n[1] < 0) n = [-n[0], -n[1]];
  return { pt, t, n };
}
const SPRING = [-262, 0];
const CRANE = [-60, -1580];

/* =====================================================================
   绘制：场景物件
   ===================================================================== */
function drawSky(S) {
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = nm(PAPER); ctx.fillRect(0, 0, W, H);
  const gy = H / 2 - cam.y * cam.z, hy = Math.min(H * 0.64, gy - 26);
  S.hy = hy;
  // 夜星（铅笔小十字）
  if (N > 0.05) for (let i = 0; i < 26; i++) {
    const x = ((h1(i * 3.1) * W * 1.3 - cam.x * cam.z * .03) % (W * 1.3) + W * 1.3) % (W * 1.3) - W * .15, y = h1(i * 7.7) * hy * .85;
    const r = 3 + h1(i) * 4, a = N * (0.35 + 0.35 * nz(T * 2 + i, i));
    sk([[x - r, y], [x + r, y]], { w: 1.4, a, d: 0 }); sk([[x, y - r], [x, y + r]], { w: 1.4, a, d: 0 });
  }
  const sx = W * 0.83;
  // 地平线晨光（阳光）
  const G = S.glow + S.sun;
  if (G > 0) {
    ctx.save(); ctx.globalCompositeOperation = 'multiply';
    const r = 700 + S.sun * 900, gr = ctx.createRadialGradient(sx, hy, 0, sx, hy, r);
    gr.addColorStop(0, yel(clamp(G * 0.6))); gr.addColorStop(0.5, yel(clamp(G * 0.18))); gr.addColorStop(1, yel(0));
    ctx.fillStyle = gr; ctx.fillRect(0, 0, W, H); ctx.restore();
  }
  // 太阳
  if (S.sun > 0) {
    const R = 92, cy = hy + R + 8 - S.sun * 360;
    ctx.save(); ctx.beginPath(); ctx.rect(0, 0, W, hy); ctx.clip();
    for (let i = 0; i < 14; i++) {
      const a = i / 14 * TAU + T * 0.15, r0 = R + 22, r1 = R + 70 + 30 * Math.sin(T * 3 + i * 2.1);
      sk([[sx + Math.cos(a) * r0, cy + Math.sin(a) * r0], [sx + Math.cos(a) * r1, cy + Math.sin(a) * r1]], { raw: yel(0.95), w: 9, d: 0, s: i });
    }
    fl(ell(sx, cy, R + 16, R + 16), nm('#FFFBEF', .9));
    const c = ell(sx, cy, R, R);
    fl(c, yel(1), { ox: 5, oy: 3 }); sk(c, { c: true, w: 2.8 });
    ctx.restore();
  }
  // 远山线
  const off = -cam.x * cam.z * 0.06; const hp = [];
  for (let x = -40; x <= W + 40; x += 40) hp.push([x, hy - 30 * Math.pow(nz((x - off) / 260, 4), 2) - 10 * nz((x - off) / 90, 9)]);
  sk(hp, { w: 2, col: GRAPH, a: .8 });
}
function drawGround() {
  const x0 = cam.x - W / 2 / cam.z - 60, x1 = cam.x + W / 2 / cam.z + 60;
  if (H / 2 - cam.y * cam.z > H + 80) return;
  const g = []; for (let x = Math.floor(x0 / 30) * 30; x <= x1; x += 30) g.push([x, 3 * Math.sin(x * 0.013) + 2 * nz(x / 50, 2)]);
  const gp = g.concat([[x1, 400], [x0, 400]]);
  fl(gp, nm('#E6DECB'));
  hatch([[x0, 8], [x1, 8], [x1, 60], [x0, 60]], { sp: 9, ang: -0.6, a: .35 });
  sk(g, { w: 2.8 });
  // 小草簇与石子
  for (let i = -60; i < 60; i++) {
    const x = i * 57 + 40 * h1(i * 1.7); if (x < x0 || x > x1) continue;
    if (h1(i * 3.3) < .55) { const hgt = 10 + 18 * h1(i); sk([[x, 1], [x - 4, -hgt]], { w: 1.8, d: 0, s: i }); sk([[x + 4, 1], [x + 8, -hgt * .7]], { w: 1.8, d: 0, s: i + 1 }); }
    else if (h1(i * 5.1) < .4) { sk(ell(x, 4, 7 + 5 * h1(i), 4, 0), { c: true, w: 1.6, d: 0, s: i }); }
  }
}
function drawLeaf(P, wmax, s, side = 1) {
  const up = [], dn = [];
  for (let i = 0; i <= 16; i++) {
    const u = i / 16, p = qpt(P, u), t = qtan(P, u), w = wmax * Math.sin(Math.PI * Math.pow(u, 0.8)) * (1 - u * 0.15);
    up.push([p[0] - t[1] * w, p[1] + t[0] * w]); dn.push([p[0] + t[1] * w, p[1] - t[0] * w]);
  }
  const outl = up.concat(dn.reverse());
  fl(outl, nm(PLANT), { ox: 2, oy: 2, s });
  hatch(dn.slice().reverse().concat(qb(...P, 16).reverse()), { sp: 8, a: .5, s });
  sk(outl, { c: true, s, w: 2.4 });
  sk(qb(...P, 16), { w: 1.5, s: s + 1, a: .8 });
}
function drawStem() {
  const L = [], R = [];
  for (let y = 10; y >= STEM_TOP; y -= 30) { const w = lerp(19, 13, -y / -STEM_TOP); L.push([stemX(y) - w, y]); R.push([stemX(y) + w, y]); }
  const outl = L.concat(R.slice().reverse());
  fl(outl, nm(PLANT), { ox: 2, oy: 1 });
  hatch(R.map(p => [p[0] - 9, p[1]]).concat(R.slice().reverse()), { sp: 7, a: .55 });
  sk(L, { w: 2.6 }); sk(R, { w: 2.6, s: 2 });
  for (let i = 0; i < 40; i++) { const y = -20 - i * 33; const x = stemX(y) + (i % 2 ? 1 : -1) * lerp(19, 13, -y / 1350); sk([[x, y], [x + (i % 2 ? 7 : -7), y - 6]], { w: 1.4, d: 0, s: i }); }
  drawLeaf([[stemX(-640) + 10, -640], [140, -720], [270, -690]], 40, 21);
  drawLeaf([[stemX(-1010) - 10, -1010], [-120, -1090], [-230, -1070]], 36, 33);
  drawLeaf([[stemX(-420) + 10, -420], [100, -470], [185, -430]], 28, 41);
}
function drawHead(S) {
  const [cx, cy] = HC, light = S.light;
  // 颈 & 苞片（下沿）
  const neck = [[stemX(STEM_TOP) - 13, STEM_TOP], [-30, -1395], [30, -1395], [stemX(STEM_TOP) + 13, STEM_TOP]];
  fl(neck, nm(PLANT));
  const br = ell(BR.cx, BR.cy, BR.rx, BR.ry, 0, 0, Math.PI, 30);
  const spikes = []; for (let i = 0; i <= 18; i++) { const a = i / 18 * Math.PI, r = i % 2 ? 1.12 : 1; spikes.push([BR.cx + Math.cos(a) * BR.rx * r, BR.cy + Math.sin(a) * BR.ry * r]); }
  // 花瓣（后排）
  const NP = 26;
  const petal = (i, back) => {
    const a = i / NP * TAU + 0.12 * h1(i), sa = Math.sin(a); if ((sa < 0) !== back) return;
    const b = [cx + Math.cos(a) * HRX * .5, cy + sa * HRY * .5], tp = [cx + Math.cos(a) * HRX * (1 + .07 * h1(i * 3)), cy + sa * HRY * (1 + .07 * h1(i * 3)) - 8];
    const dx = tp[0] - b[0], dy = tp[1] - b[1], l = Math.hypot(dx, dy), nx = -dy / l, ny = dx / l, w = 24 + 6 * h1(i * 7);
    const m1 = [lerp(b[0], tp[0], .45) + nx * w, lerp(b[1], tp[1], .45) + ny * w], m2 = [lerp(b[0], tp[0], .45) - nx * w, lerp(b[1], tp[1], .45) - ny * w];
    const pts = cr([b, m1, tp, m2], 5, true);
    const k = clamp(light * 1.5 - (1 - Math.cos(a)) * 0.25 - 0.1);
    fl(pts, k > 0 ? mixY(PAPER, k) : nm('#EEE7D6'), { ox: 2, oy: 2, s: i });
    if (k < .6) hatch(pts, { sp: 9, a: .35 * (1 - k), s: i, ang: a });
    sk(pts, { c: true, w: 2.2, s: i });
    sk([P2(b, tp, .15), P2(b, tp, .7)], { w: 1.2, a: .5, d: 0, s: i });
  };
  for (let i = 0; i < NP; i++) petal(i, true);
  fl(spikes.concat([[-30, -1395]]), nm(PLANT)); hatch(spikes, { sp: 6, a: .6 }); sk(spikes, { w: 2.4 });
  sk([[stemX(STEM_TOP) - 13, STEM_TOP], [-24, -1398]], { w: 2.4 }); sk([[stemX(STEM_TOP) + 13, STEM_TOP], [24, -1398]], { w: 2.4 });
  for (let i = 0; i < NP; i++) petal(i, false);
  // 花盘
  const dk = ell(cx, cy, 172, 70, 0, 0, TAU, 50);
  fl(dk, nm(DISK), { ox: 2, oy: 2 });
  ctx.fillStyle = nm(INK, .55);
  for (let i = 0; i < 170; i++) {
    const r = Math.sqrt((i + .5) / 170), th = i * 2.39996;
    const x = cx + Math.cos(th) * r * 160, y = cy + Math.sin(th) * r * 62, z = cam.z;
    ctx.beginPath(); ctx.arc(x + (h1(i + BOIL) - .5) * 1.5 / z, y, (1.6 + r * 1.4) / z * Math.max(1, cam.z * .5), 0, TAU); ctx.fill();
  }
  hatch(ell(cx, cy + 10, 172, 60, 0, 0.1, Math.PI - 0.1, 20), { sp: 6, a: .45 });
  sk(dk, { c: true, w: 2.8 });
  sk(ell(cx, cy, 120, 46, 0, 3.4, 5.9, 20), { w: 1.3, a: .5, d: 0 });
}
function drawGrass(S) {
  // 背景小草
  const bg = [[-600, 180, .2], [-470, 240, -.25], [-700, 120, -.2], [-60, 90, .3], [-820, 200, .15], [-980, 160, -.1], [-1120, 130, .2], [-1300, 190, -.2], [260, 150, -.2], [380, 210, .2], [520, 120, -.1]];
  bg.forEach(([x, h, a], i) => { const P = [[x, 2], [x - Math.sin(a * .4) * h * .5, -h * .5], [x - Math.sin(a) * h, -h * Math.cos(a)]]; blade(P, 9, 50 + i); });
  const P = grassP(S.grassA); blade(P, 11, 7); sk(qb(...P, 14).slice(0, 12), { w: 1.2, a: .5, d: 0 });
}
function blade(P, w0, s) {
  const L = [], R = [];
  for (let i = 0; i <= 14; i++) { const u = i / 14, p = qpt(P, u), t = qtan(P, u), w = lerp(w0, 1.5, Math.pow(u, 1.2)); L.push([p[0] - t[1] * w, p[1] + t[0] * w]); R.push([p[0] + t[1] * w, p[1] - t[0] * w]); }
  const o = L.concat(R.reverse());
  fl(o, nm(PLANT), { s }); hatch(L.concat(qb(...P, 14).reverse()), { sp: 7, a: .45, s }); sk(o, { c: true, w: 2.2, s });
}
function drawCup(a) {
  const { tip, ax } = cupAt(a), pp = [-ax[1], ax[0]];
  const b = [tip[0] - ax[0] * 4, tip[1] - ax[1] * 4], m = [tip[0] + ax[0] * 38, tip[1] + ax[1] * 38];
  const pts = [[b[0] + pp[0] * 13, b[1] + pp[1] * 13], [m[0] + pp[0] * 23, m[1] + pp[1] * 23], [m[0] - pp[0] * 23, m[1] - pp[1] * 23], [b[0] - pp[0] * 13, b[1] - pp[1] * 13]];
  fl(pts, yel(), { ox: 3, oy: 2 }); sk(pts, { c: true, w: 2.4 });
  for (let k = 1; k < 4; k++) { const u = k / 4; sk([P2(pts[0], pts[1], u), P2(pts[3], pts[2], u)], { w: 1.1, a: .5, d: 0, s: k }); }
  const rim = ell(m[0], m[1], 23, 6, Math.atan2(pp[1], pp[0]), 0, TAU, 20); sk(rim, { c: true, w: 2 });
  // 胶带
  const tp = [[b[0] + pp[0] * 17 - ax[0] * 10, b[1] + pp[1] * 17 - ax[1] * 10], [b[0] + pp[0] * 17 + ax[0] * 8, b[1] + pp[1] * 17 + ax[1] * 8], [b[0] - pp[0] * 17 + ax[0] * 8, b[1] - pp[1] * 17 + ax[1] * 8], [b[0] - pp[0] * 17 - ax[0] * 10, b[1] - pp[1] * 17 - ax[1] * 10]];
  fl(tp, nm('#FFFFFF', .55)); sk(tp, { c: true, w: 1.2, a: .6, d: 0 });
}
function drawSpring(h, crude, lean = 0) {
  const [x, y] = SPRING;
  ctx.save(); ctx.translate(x, y); ctx.rotate(lean);
  const base = [[-48, 0], [48, 0], [46, -8], [-46, -8]];
  fl(base, yel(), { ox: 2, oy: 1 }); sk(base, { c: true, w: 2.2 });
  const n = 6, hh = Math.max(h - 14, 4);
  for (let i = 0; i < n; i++) {
    const y0 = -8 - hh * i / n, y1 = -8 - hh * (i + 1) / n;
    const o0 = (i % 2 ? 10 : 0) + (crude ? (h1(i * 3.3) - .5) * 30 : 0), o1 = ((i + 1) % 2 ? 10 : 0) + (crude ? (h1((i + 1) * 3.3) - .5) * 30 : 0);
    const w0 = crude ? 26 + 16 * h1(i) : 38, w1 = crude ? 26 + 16 * h1(i + 1) : 38;
    const q = [[-w0 + o0, y0], [w0 + o0, y0], [w1 + o1, y1], [-w1 + o1, y1]];
    fl(q, yel(), { ox: 2, oy: 1, s: i });
    if (i % 2) hatch(q, { sp: 5, a: .5, s: i, ang: 0.9 });
    sk(q, { c: true, w: 2, s: i });
  }
  const tw = crude ? 40 : 46, tl = crude ? 0.12 : 0;
  ctx.translate(crude ? 4 : 0, -8 - hh); ctx.rotate(tl);
  const top = [[-tw, 0], [tw, 0], [tw - 2, -8], [-tw + 2, -8]];
  fl(top, yel(), { ox: 2, oy: 1 }); sk(top, { c: true, w: 2.2 });
  if (crude) { sk([[-20, -12], [10, 6]], { w: 5, col: '#FFFFFF', a: .6, d: 0 }); sk([[-20, -12], [10, 6]], { w: 1.2, a: .5, d: 0 }); }
  ctx.restore();
}
function drawTrack(p) {
  if (p <= 0) return;
  const n = Math.max(2, Math.floor(TRK.length * p)), pts = TRK.slice(0, n);
  const A = [], B = [];
  for (let i = 0; i < pts.length; i++) {
    const a = pts[Math.max(0, i - 1)], b = pts[Math.min(pts.length - 1, i + 1)], dx = b[0] - a[0], dy = b[1] - a[1], l = Math.hypot(dx, dy) || 1;
    const w = 15 + (i < 6 ? (6 - i) * 2.5 : 0);
    A.push([pts[i][0] - dy / l * w, pts[i][1] + dx / l * w]); B.push([pts[i][0] + dy / l * w, pts[i][1] - dx / l * w]);
  }
  // 支撑纸条
  if (p > .95) {
    [[[-405, -1390], [-285, -1420]], [[-400, -1515], [-270, -1505]], [[-300, -1596], [-250, -1560]]].forEach(([a, b], i) => {
      const q = [[a[0], a[1] - 4], [b[0], b[1] - 4], [b[0], b[1] + 4], [a[0], a[1] + 4]]; fl(q, yel(), { s: i }); sk(q, { c: true, w: 1.6, s: i });
    });
  }
  const o = A.concat(B.slice().reverse());
  fl(o, yel(), { ox: 3, oy: 2 });
  hatch(B.concat(pts.slice().reverse()), { sp: 5, a: .35, ang: .7 });
  sk(A, { w: 2.4 }); sk(B, { w: 2.4, s: 3 });
  for (let i = 4; i < pts.length - 1; i += 7) sk([A[i], B[i]], { w: 1.2, a: .6, d: 0, s: i });
  if (n >= 2) sk([A[0], B[0]], { w: 2.2 });
}
function drawCrane(x, y, sc, rot = 0) {
  if (sc <= 0) return;
  ctx.save(); ctx.translate(x, y); ctx.rotate(rot); ctx.scale(sc * 1.7, sc * 1.7);
  const body = [[-16, 0], [0, -7], [16, 0], [0, 5]];
  const tail = [[-10, -2], [-30, -26], [-24, -27], [-4, -3]];
  const neck = [[8, -3], [24, -30], [28, -30], [14, 0]], head = [[24, -30], [28, -30], [35, -24]];
  const wingB = [[-4, -4], [-2, -30], [6, -4]], wingF = [[-2, -3], [10, -40], [12, -3]];
  [wingB, tail, body, neck, head, wingF].forEach((q, i) => { fl(q, yel(), { s: i, ox: 1.5, oy: 1 }); if (i === 0 || i === 2) hatch(q, { sp: 3, a: .5 }); sk(q, { c: true, w: 1.8, s: i, d: 0 }); });
  ctx.restore();
}
/* ---------------- 纸条与象形图 ---------------- */
const PIC = {
  leaf: [[[-.36, -.2], [-.1, -.36], [.22, -.3], [.36, -.2], [.05, -.1], [-.36, -.2]], [[.34, -.12], [.37, .08], [.32, .28]], [[.24, .2], [.32, .3], [.4, .2]], [[.2, .38], [.44, .38]],
    [[-.38, .06], [-.14, .34]], [[-.14, .06], [-.38, .34]]],
  grass: [[[-.3, .4], [-.28, 0], [-.05, -.3], [.3, -.25], [.38, .05]], [[.4, .1], [.36, .34]], [[.28, .26], [.36, .36], [.44, .24]],
    [[-.1, .08], [.12, .36]], [[.12, .08], [-.1, .36]]],
  head: [[[-.38, -.12], [-.25, -.36], [.25, -.36], [.38, -.12], [-.38, -.12]], [[0, -.12], [0, .02]], [[-.14, .12], [-.06, .02], [.06, .02], [.14, .12]], [[.28, .0], [.28, .34]], [[.2, .26], [.28, .36], [.36, .26]],
    [[-.38, .14], [-.16, .38]], [[-.16, .14], [-.38, .38]]],
  sun: [ell(0, -.1, .17, .17, 0, 0, TAU, 16), ...Array.from({ length: 8 }, (_, i) => { const a = i / 8 * TAU; return [[Math.cos(a) * .24, -.1 + Math.sin(a) * .24], [Math.cos(a) * .34, -.1 + Math.sin(a) * .34]]; }),
    [[-.08, -.08], [0, -.02], [.08, -.08]], [[-.3, .4], [.3, .4]], ell(-.05, .3, .07, .07, 0, 0, TAU, 10)]
};
function drawNote(x, y, sz, rot, picto, prog = 1, flip = 1, pop = 1) {
  if (sz <= 0) return;
  ctx.save(); ctx.translate(x, y); ctx.rotate(rot); ctx.scale(flip * pop, pop);
  const h = sz / 2, pts = [[-h, -h], [h, -h * 1.02], [h, h * .6], [h * .62, h], [-h * .98, h]];
  fl(pts, yel(), { ox: 2, oy: 2, s: 3 }); sk(pts, { c: true, w: 1.9, d: 0, s: 3 });
  sk([[h, h * .6], [h * .7, h * .68], [h * .62, h]], { w: 1.3, d: 0 });
  if (picto && Math.abs(flip) > .25) {
    const L = PIC[picto], n = L.length, pr = prog * n;
    for (let i = 0; i < n; i++) {
      if (pr <= i) break;
      let q = L[i].map(p => [p[0] * sz, p[1] * sz]);
      if (pr < i + 1) q = q.slice(0, Math.max(2, Math.ceil(q.length * (pr - i))));
      sk(q, { w: 2, d: 0, s: i + 1, j: .8 });
    }
  }
  ctx.restore();
}
function drawZig(x, y, sz, hh, rot) {
  ctx.save(); ctx.translate(x, y); ctx.rotate(rot);
  const n = 4;
  for (let i = 0; i < n; i++) {
    const y0 = -hh * i / n, y1 = -hh * (i + 1) / n, o0 = i % 2 ? 5 : 0, o1 = (i + 1) % 2 ? 5 : 0;
    const q = [[-sz / 2 + o0, y0], [sz / 2 + o0, y0], [sz / 2 + o1, y1], [-sz / 2 + o1, y1]];
    fl(q, yel(), { s: i }); if (i % 2) hatch(q, { sp: 4, a: .5 }); sk(q, { c: true, w: 1.6, s: i, d: 0 });
  }
  ctx.restore();
}

/* ---------------- 蜗牛 ---------------- */
const SHELLC = [-10, -40];
const SLOTS = [[-4, -9, -.25], [12, 5, .3], [-17, 8, .12], [2, -2, -.1]];
function l2w(s, lx, ly) {
  const sq = s.sq ?? 1, sx = 1 / Math.sqrt(sq), fx = (s.fx ?? s.dir) * s.sc * sx, fy = s.sc * sq;
  const x = lx * fx, y = ly * fy, c = Math.cos(s.rot), n = Math.sin(s.rot);
  return [s.x + x * c - y * n, s.y + x * n + y * c];
}
function drawSnail(s) {
  if (!s || s.hide) return;
  ctx.save(); ctx.translate(s.x, s.y); ctx.rotate(s.rot);
  const sq = s.sq ?? 1, sx = 1 / Math.sqrt(sq), fx = (s.fx ?? s.dir);
  ctx.scale(fx * s.sc * sx, s.sc * sq); SC = s.sc;
  const cp = s.crawl || 0, st = s.st || 0;
  const rp = k => Math.sin(cp * TAU - k) * 2;
  // 身体
  const body = cr([[-60 - st * 10 + rp(0), -3], [-44, 0], [-20, 0], [5, 0], [28 + st * 6, 0], [42 + st * 8, -3], [51 + st * 8, -17], [52 + st * 6, -35], [46 + st * 4, -50], [34 + st * 3, -57], [22, -51], [17, -37], [8, -22], [-20, -15 + rp(1) * .5], [-46, -9]], 5, true);
  fl(body, nm(BODY), { ox: 2, oy: 2 });
  hatch([[-58, -2], [40, -2], [44, 0], [-58, 0], [-58, -8]], { sp: 5, a: .4, ang: -.3 });
  sk(body, { c: true, w: 2.8 });
  sk([[-50, -3], [30, -3]].map((p, i) => [p[0], p[1]]), { w: 1.1, a: .45, d: 0 });
  // 触角（下）
  sk(qb([48, -38], [56, -41], [60, -37]), { w: 2.4 });
  // 壳
  const [scx, scy] = SHELLC;
  const shell = ell(scx, scy, 31, 29, 0, 0, TAU, 36);
  fl(shell, nm(SHELL), { ox: 2, oy: 2 });
  hatch(ell(scx, scy, 31, 29, 0, -0.2, 2.5, 20).concat(ell(scx - 7, scy - 6, 26, 24, 0, 2.5, -0.2, 20)), { sp: 5, a: .55, ang: .8 });
  sk(shell, { c: true, w: 2.8 });
  const sp = []; for (let a = 0; a <= 4.3 * Math.PI; a += .25) { const r = 2 + a * 1.75; sp.push([scx + 3 + Math.cos(a + 1) * r, scy - 2 + Math.sin(a + 1) * r * .95]); }
  sk(sp, { w: 2.1, s: 5 });
  // 贴在壳上的纸条
  (s.notes || []).forEach((nt, i) => {
    const sl = SLOTS[nt.slot]; const fl2 = nt.flap ? Math.cos(nt.flap) : 1;
    drawNote(scx + sl[0], scy + sl[1], 26, sl[2] + (nt.wob || 0), nt.picto, 1, fl2);
  });
  // 嘴
  const m = s.mouth || 'derp';
  if (s.pencil) {
    const pc = [[44, -29], [78, -12], [80, -16], [46, -33]];
    fl(pc, nm('#B9B2A3')); sk(pc, { c: true, w: 1.8 }); sk([[78, -12], [86, -9], [80, -16]], { w: 1.8 });
    sk([[38, -31], [46, -29]], { w: 2.2 });
  } else if (m === 'derp') {
    sk(qb([33, -31], [40, -23], [48, -30]), { w: 2.4 });
    const tg = qb([43, -26], [46, -18], [49, -25], 10); fl(tg, nm('#C9BFB0')); sk(tg, { w: 1.8 });
  } else if (m === 'smile') sk(qb([33, -31], [40, -24], [48, -30]), { w: 2.4 });
  else if (m === 'grin') {
    const g = qb([31, -32], [40, -12], [50, -31], 12); fl(g, nm(INK)); sk(g.concat([[31, -32]]), { w: 2.4 });
    sk(qb([37, -20], [41, -15], [45, -20]), { w: 3, col: '#C9BFB0', d: 0 });
  } else if (m === 'open') { const e = ell(41, -25, 5.5, 7.5); fl(e, nm(INK)); sk(e, { c: true, w: 2 }); }
  else if (m === 'o') sk(ell(41, -26, 3, 3.6), { c: true, w: 2.2 });
  else if (m === 'wobble') sk([[32, -27], [36, -30], [40, -26], [44, -30], [48, -27]], { w: 2.2 });
  else if (m === 'flat') sk([[34, -27], [47, -28]], { w: 2.2 });
  // 眼柄 + 眼
  const eA = s.eA || [-2.05, -1.3], eL = s.eL || [28, 33], B = [[29, -54], [42, -52]];
  const pu = s.pu || [[Math.cos(nz(T * .7, 1) * 6) * .6, Math.sin(nz(T * .7, 1) * 6) * .6], [Math.cos(nz(T * .5, 2) * 6) * .6, Math.sin(nz(T * .5, 2) * 6) * .6]];
  const blink = s.blink ?? ((T * .41 + .3) % 1 < .045 ? 1 : 0);
  for (let k = 0; k < 2; k++) {
    const a = eA[k] + Math.sin(T * 2.2 + k * 1.7) * .06 + (s.bob || 0) * Math.sin(T * 9 + k), L = eL[k];
    const tip = [B[k][0] + Math.cos(a) * L, B[k][1] + Math.sin(a) * L];
    const mid = P2(B[k], tip, .5), bend = (k ? 5 : -5) * (s.floppy ?? 1);
    const c = [mid[0] - Math.sin(a) * bend, mid[1] + Math.cos(a) * bend];
    const stalk = qb(B[k], c, tip, 10);
    sk(stalk, { w: 8.5, s: 20 + k, d: 0, j: .6 }); sk(stalk, { w: 4.6, col: BODY, s: 20 + k, d: 0, j: .6 });
    const r = (k ? 11 : 10) * (s.eR || 1);
    if (s.happy) {
      const e0 = ell(tip[0], tip[1], r, r); fl(e0, nm(EYEW)); sk(e0, { c: true, w: 2.5, s: 30 + k });
      sk(ell(tip[0], tip[1] + r * .35, r * .62, r * .55, 0, Math.PI * 1.1, Math.PI * 1.9, 10), { w: 2.8, d: 0 });
      continue;
    }
    const e = ell(tip[0], tip[1], r, r * (1 - blink * .92));
    fl(e, nm(EYEW)); sk(e, { c: true, w: 2.5, s: 30 + k });
    if (blink < .6) {
      if (s.dizzy) {
        const q = []; for (let a2 = 0; a2 < 9; a2 += .5) q.push([tip[0] + Math.cos(a2 + T * 12) * a2 * .8, tip[1] + Math.sin(a2 + T * 12) * a2 * .8]); sk(q, { w: 1.6, d: 0 });
      } else {
        const pr = (s.pR || 3.6) * (s.eR || 1);
        const px = tip[0] + pu[k][0] * r * .52, py = tip[1] + pu[k][1] * r * .52;
        ctx.fillStyle = nm(INK); ctx.beginPath(); ctx.arc(px, py, pr, 0, TAU); ctx.fill();
        ctx.fillStyle = nm(EYEW); ctx.beginPath(); ctx.arc(px - pr * .35, py - pr * .35, pr * .3, 0, TAU); ctx.fill();
      }
    }
  }
  ctx.restore(); SC = 1;
}

/* ---------------- 特效 ---------------- */
function drawCloud(c) {
  const { x, y, r, t0, t1, seed } = c, k = seg(T, t0, t1);
  const sc = k < .15 ? eob(k / .15) : k > .85 ? 1 - ei((k - .85) / .15) : 1;
  if (sc <= 0) return;
  const R = r * sc, pts = [];
  for (let i = 0; i < 60; i++) { const a = i / 60 * TAU; const rr = R * (1 + .16 * Math.sin(a * 7 + T * 18) + .1 * nz(a * 2 + T * 6, seed)); pts.push([x + Math.cos(a) * rr * 1.25, y + Math.sin(a) * rr * .85]); }
  fl(pts, nm(PAPER)); sk(pts, { c: true, w: 2.8, s: seed });
  for (let i = 0; i < 5; i++) { const a = T * 7 + i * 1.3 + seed; sk(ell(x + Math.cos(a) * R * .4, y + Math.sin(a * 1.3) * R * .25, R * .28, R * .18, a, 0, 4, 10), { w: 1.6, a: .6, d: 0, s: i }); }
  for (let i = 0; i < 9; i++) {
    const ph = (T * 2.2 + i / 9 + seed * .1) % 1, a = h1(i * 3 + seed + Math.floor(T * 2.2 + i / 9)) * TAU;
    const d = R * (.7 + ph * 1.1);
    const px = x + Math.cos(a) * d * 1.2, py = y + Math.sin(a) * d * .8 - ph * 20;
    drawNote(px, py, 16 * (1 - ph * .5) * sc, a + ph * 8, null, 1, Math.cos(ph * 12));
  }
  if (Math.floor(T * 3 + seed) % 3 === 0) {       // 云里探出一只眼
    const a = h1(Math.floor(T * 3) + seed) * TAU, ex = x + Math.cos(a) * R * 1.05, ey = y + Math.sin(a) * R * .7;
    fl(ell(ex, ey, 11, 11), nm(EYEW)); sk(ell(ex, ey, 11, 11), { c: true, w: 2.4 });
    ctx.fillStyle = nm(INK); ctx.beginPath(); ctx.arc(ex + 3, ey + 2, 3.8, 0, TAU); ctx.fill();
  }
  if (Math.floor(T * 4 + seed) % 4 === 1) star(x + R * .9, y - R * .7, 10);
}
function star(x, y, r, a = 1) { for (let i = 0; i < 3; i++) { const t = i / 3 * Math.PI + T; sk([[x - Math.cos(t) * r, y - Math.sin(t) * r], [x + Math.cos(t) * r, y + Math.sin(t) * r]], { w: 2, d: 0, a, s: i }); } }
function drawFx(f) {
  const k = seg(T, f.t0, f.t1); if (k <= 0 || k >= 1) return;
  if (f.type === 'stars') {
    for (let i = 0; i < 3; i++) { const a = T * 6 + i * TAU / 3; star(f.x + Math.cos(a) * 34, f.y + Math.sin(a) * 10, 7); }
    sk(ell(f.x, f.y, 34, 10), { c: true, w: 1.2, a: .4, d: 0 });
  } else if (f.type === 'dust') {
    for (let i = -1; i <= 1; i += 2) for (let j = 0; j < 3; j++) {
      const d = 30 + eo(k) * (40 + j * 22), r = (8 + j * 4) * (1 - k * .6);
      sk(ell(f.x + i * d, f.y - 8 - j * 6 * k, r, r * .8), { c: true, w: 2, a: 1 - k, d: 0, s: j });
    }
    for (let i = 0; i < 5; i++) { const a = -Math.PI * (i + .5) / 5; sk([[f.x + Math.cos(a) * 30, f.y + Math.sin(a) * 18], [f.x + Math.cos(a) * (40 + 40 * eo(k)), f.y + Math.sin(a) * (22 + 30 * eo(k))]], { w: 2.2, a: 1 - k, d: 0, s: i }); }
  } else if (f.type === 'bang') {
    const s = f.s || 1;
    ctx.save(); ctx.translate(f.x, f.y); ctx.scale(eob(Math.min(1, k * 5)) * s, eob(Math.min(1, k * 5)) * s);
    const q = [[-5, -46], [5, -46], [2, -12], [-2, -12]]; fl(q, nm(INK)); sk(q, { c: true, w: 2 });
    ctx.fillStyle = nm(INK); ctx.beginPath(); ctx.arc(0, 0, 5, 0, TAU); ctx.fill();
    for (let i = 0; i < 3; i++) { const a = -Math.PI / 2 + (i - 1) * .7; sk([[Math.cos(a) * 58, -22 + Math.sin(a) * 40], [Math.cos(a) * 74, -22 + Math.sin(a) * 52]], { w: 2.4, d: 0 }); }
    ctx.restore();
  } else if (f.type === 'boing') {
    for (let i = 0; i < 3; i++) { const r = 20 + i * 14 + k * 30; sk(ell(f.x, f.y, r * 1.3, r * .5, 0, Math.PI * 1.1, Math.PI * 1.9, 12), { w: 2, a: 1 - k, d: 0, s: i }); }
  } else if (f.type === 'speed') {
    for (let i = 0; i < 5; i++) { const o = (i - 2) * 14, l = 60 + 40 * h1(i + BOIL); const px = -f.vy, py = f.vx; sk([[f.x + px * o - f.vx * 30, f.y + py * o - f.vy * 30], [f.x + px * o - f.vx * (30 + l), f.y + py * o - f.vy * (30 + l)]], { w: 2, a: .8, d: 0, s: i }); }
  } else if (f.type === 'sparkle') {
    for (let i = 0; i < 4; i++) { const a = i / 4 * TAU + .4; const d = 30 + k * 30; star(f.x + Math.cos(a) * d, f.y + Math.sin(a) * d * .7, 6 * (1 - k), 1 - k); }
  }
}
function drawWind(w) {
  const k = seg(T, w.t0, w.t1); if (k <= 0 || k >= 1) return;
  const span = w.span, head = w.x0 + k * span * 1.6;
  for (let i = 0; i < w.n; i++) {
    const y = w.y0 + (i - (w.n - 1) / 2) * w.gap + 15 * Math.sin(i * 2.3), hx = head - i * 60 * (h1(i) + .4), q = [];
    for (let x = hx - w.len; x <= hx; x += 14) q.push([x, y + Math.sin(x / 70 + i) * 14]);
    const e = q[q.length - 1]; for (let a = 0; a < 5; a += .35) q.push([e[0] + Math.sin(a) * 18 * (1 - a / 7), e[1] - 18 + Math.cos(a) * 18 * (1 - a / 7)]);
    sk(q, { w: 2.4 * (w.wk || 1), a: .75 * Math.sin(Math.PI * k), s: i });
  }
}
function drawBubble(b) {
  const k = seg(T, b.t0, b.t1); if (k <= 0 || k >= 1) return;
  const sc = k < .2 ? eob(k / .2) : k > .9 ? 1 - (k - .9) / .1 : 1;
  const { x, y, r } = b;
  [[b.fx, b.fy, 6], [lerp(b.fx, x, .35), lerp(b.fy, y + r * .8, .35), 10], [lerp(b.fx, x, .65), lerp(b.fy, y + r * .8, .65), 15]].forEach(([cx, cy, rr], i) => {
    if (k * 5 > i * .4) { const e = ell(cx, cy, rr, rr * .8); fl(e, nm(PAPER)); sk(e, { c: true, w: 2.2, s: i }); }
  });
  ctx.save(); ctx.translate(x, y); ctx.scale(sc, sc);
  const pts = []; for (let i = 0; i < 70; i++) { const a = i / 70 * TAU; const rr = r * (1 + .09 * Math.abs(Math.sin(a * 5))); pts.push([Math.cos(a) * rr * 1.3, Math.sin(a) * rr]); }
  fl(pts, nm('#FBF7EE')); sk(pts, { c: true, w: 2.8 });
  ctx.save(); path(pts, true); ctx.clip();
  // 梦：太阳从远处升起 + 花上的小蜗牛
  const su = eo(seg(T, b.t0 + .2, b.t1 - .3));
  const sy = 25 - su * 45;
  fl(ell(55, sy, 30, 30), yel()); sk(ell(55, sy, 30, 30), { c: true, w: 2 });
  for (let i = 0; i < 9; i++) { const a = i / 9 * TAU + T; sk([[55 + Math.cos(a) * 38, sy + Math.sin(a) * 38], [55 + Math.cos(a) * 52, sy + Math.sin(a) * 52]], { raw: yel(), w: 5, d: 0, s: i }); }
  fl([[-140, 30], [140, 30], [140, 120], [-140, 120]], nm('#FBF7EE')); sk([[-130, 30], [130, 30]], { w: 1.8 });
  sk([[-45, 120], [-40, 20]], { w: 2.4 });
  for (let i = 0; i < 12; i++) { const a = i / 12 * TAU; sk(cr([[-40, 5], [-40 + Math.cos(a + .2) * 22, 5 + Math.sin(a + .2) * 11], [-40 + Math.cos(a) * 38, 5 + Math.sin(a) * 17], [-40 + Math.cos(a - .2) * 22, 5 + Math.sin(a - .2) * 11]], 3, true), { c: true, w: 1.6, d: 0, s: i }); }
  fl(ell(-40, 5, 20, 9), nm(DISK)); sk(ell(-40, 5, 20, 9), { c: true, w: 1.8 });
  sk(ell(-44, -8, 8, 7), { c: true, w: 1.8 }); sk([[-52, -1], [-30, -1], [-28, -6]], { w: 1.8 }); sk([[-31, -5], [-29, -15]], { w: 1.4 }); sk([[-34, -5], [-36, -14]], { w: 1.4 });
  ctx.restore(); ctx.restore();
}

/* =====================================================================
   分镜 / 时间轴：scene(t) 返回该时刻的完整状态（纯函数）
   ===================================================================== */
function mkSnail(x, y, rot, dir, o = {}) { return Object.assign({ x, y, rot, dir, sc: .8, sq: 1, crawl: 0, mouth: 'derp', notes: [] }, o); }
function onGround(x, dir, o) { return mkSnail(x, 0, 0, dir, o); }
function fromPose(p, o) { const ps = poseOn(p.up, p.tan); return mkSnail(p.x, p.y, ps.rot, ps.dir, o); }
function climb(y, o) { return mkSnail(stemX(y) - 16, y, Math.PI / 2, -1, o); }       // 茎前侧向上爬（足在左）
function climbL(y, o) { return mkSnail(stemX(y) - 17, y, -Math.PI / 2, 1, o); }      // 茎左侧向上爬（足在右）
function arc(a, b, k, hgt) { return [lerp(a[0], b[0], k), lerp(a[1], b[1], k) - hgt * 4 * k * (1 - k)]; }
function leafDroop(t, tOn, tOff, uAt) {
  if (t < tOff) return .95 * Math.pow(clamp(uAt), 1.3);
  const k = t - tOff; return .95 * Math.exp(-4 * k) * Math.cos(k * 16);
}
// 纸条：贴上/吹落的时间表
const NOTE_SCHED = [
  { picto: 'leaf', slot: 0, on: 10.35, off: 20.25 },
  { picto: 'grass', slot: 1, on: 14.42, off: 20.45 },
  { picto: 'head', slot: 2, on: 18.98, off: 20.65 },
  { picto: 'sun', slot: 3, on: 52.85, off: 53.7 }
];
function notesAt(t) { return NOTE_SCHED.filter(n => t >= n.on && t < n.off).map(n => ({ picto: n.picto, slot: n.slot })); }
// 写纸条动作
function writeNote(S, s, t, t0, tw, tf, picto, slot) {
  if (t < t0) return;
  const nx = s.x + s.dir * 66, ny = s.y - 24;
  const tE = t0 + .12 + tw;
  if (t < tE) {
    S.notes.push({ x: nx, y: ny, sz: 36, rot: -.05 * s.dir, picto, prog: seg(t, t0 + .12, tE), pop: eob(seg(t, t0, t0 + .15)) });
    s.pencil = true; s.rot += Math.sin(t * 40) * .04; s.pu = [[.7, .5], [.7, .6]]; s.eA = [-1.5, -1.0];
  } else if (t < tE + tf) {
    const k = seg(t, tE, tE + tf), sl = SLOTS[slot], tg = l2w(s, SHELLC[0] + sl[0], SHELLC[1] + sl[1]);
    const p = arc([nx, ny], tg, eio(k), 60);
    S.notes.push({ x: p[0], y: p[1], sz: lerp(36, 26 * s.sc, k), rot: k * TAU * s.dir * -1 + sl[2] * k, picto, prog: 1 });
    s.eA = [-2.3, -2.0]; s.pu = [[-.6, -.6], [-.5, -.7]]; s.mouth = 'open';
  } else if (t < tE + tf + .5) { s.mouth = 'grin'; s.happy = true; }
}

const TL = {};  // 场景切点（供文档/调试）
function scene(t) {
  const S = { snail: null, grassA: .08, leafD: 0, spring: null, cup: false, cupFront: false, track: 0, crane: 0, craneRot: 0,
    notes: [], fx: [], clouds: [], winds: [], bubble: null, glow: 0, sun: 0, light: 0, night: 1, fade: 0, cam: null };
  let s = null;
  // 机关存在性（随时间累积）
  if (t >= 27.4 && t < 29.4) S.spring = { h: t < 28.0 ? 64 : lerp(64, 8, eo(seg(t, 28.0, 28.2))), crude: true, lean: t < 28 ? -.1 : -.1 - .25 * seg(t, 28, 28.2) };
  if (t >= 29.4) S.spring = { h: 70, crude: false, lean: 0 };
  if (t >= 32.1) S.cup = true;
  if (t >= 34.45) S.track = eo(seg(t, 34.45, 34.8));
  if (t >= 34.8) S.crane = eob(seg(t, 34.8, 35.1));
  S.glow = t < 37 ? 0 : .12 + .25 * seg(t, 37, 45);

  if (t >= 100) { /* ---------- 新 S2b：纸条刚贴上就被风吹走 ---------- */
    const u = t - 100;
    S.spring = null; S.cup = false; S.track = 0; S.glow = 0;
    s = onGround(-290, -1, { notes: u < .45 ? [{ picto: 'leaf', slot: 0, wob: u > .12 ? Math.sin(t * 40) * .35 : 0 }] : [] });
    if (u < .15) { s.mouth = 'grin'; s.happy = true; }
    else if (u < .6) { s.mouth = 'flat'; s.blink = .7; s.rot = -.06 * Math.sin(Math.PI * seg(u, .15, .9)); s.floppy = 2.5; s.eA = [-1.2, -.9]; }
    else if (u < 1.3) { const k = eob(seg(u, .6, .95)); s.mouth = 'o'; s.eA = [lerp(-2.05, -2.5, k), lerp(-1.3, -2.7, k)]; s.eL = [lerp(28, 50, k), lerp(33, 58, k)]; s.pu = [[-.8, -.5], [-.8, -.5]]; }
    else { s.mouth = 'flat'; s.eA = [-2.3, -2.2]; s.eL = [40, 44]; s.floppy = 3; s.pu = [[-.6, -.6], [-.5, -.7]]; s.blink = u > 1.5 && u < 1.6 ? 1 : 0; }
    S.winds.push({ t0: 100.0, t1: 101.4, x0: -800, span: 650, y0: -80, gap: 40, n: 5, len: 260 });
    if (u >= .45) {
      const base = onGround(-290, -1), sl = SLOTS[0], p0 = l2w(base, SHELLC[0] + sl[0], SHELLC[1] + sl[1]), tau = u - .45;
      S.notes.push({ x: p0[0] + 360 * tau + 160 * tau * tau, y: p0[1] - 130 * tau - 120 * tau * tau + 16 * Math.sin(tau * 7), sz: 21, rot: sl[2] + 3 * tau + .6 * Math.sin(tau * 9), picto: 'leaf', prog: 1, flip: Math.cos(tau * 10) });
    }
    const k = eio(seg(u, .5, 1.3));
    S.cam = { x: lerp(-250, -150, k), y: lerp(-90, -170, k), z: lerp(2.6, 2.0, k) };
    S.snail = s; S.night = 1; S.sun = 0; S.light = 0;
    return S;
  }
  if (t >= 60) { /* ---------- 新结尾：风又来了，旧纸条飞走，机关纹丝不动 ---------- */
    const v = t - 60;
    s = mkSnail(-30, -1466, Math.sin((t - 48.1) * 2.2) * .04, 1, { mouth: 'grin', happy: true, eA: [-1.3, -1.0], eL: [40, 44], eR: 1.18, pR: 4.8, pu: [[.9, -.2], [.9, -.2]] });
    const windOn = v > .2 && v < 2.6;
    if (windOn) s.floppy = 2.6;
    // 一张旧纸条（第一课“叶子”）被风卷着飞过，飞向远方
    let nx = null;
    if (v > .3) {
      const k = seg(v, .3, 5.6), kk = eo(k);
      nx = lerp(-560, 1700, k); const ny = -1580 - 620 * ei(k) + 30 * Math.sin(k * 14);
      S.notes.push({ x: nx, y: ny, sz: 34, rot: v * 3 + .5 * Math.sin(v * 8), picto: 'leaf', prog: 1, flip: Math.cos(v * 9) });
    }
    if (v > .45 && v < 1.9) {           // 眼睛追着纸条看
      s.happy = false; s.mouth = v > .7 ? 'o' : 'grin';
      const dx = clamp((nx - (-30)) / 250, -1, 1);
      s.pu = [[dx * .9, -.4], [dx * .9, -.35]]; s.eA = [lerp(-2.4, -1.0, (dx + 1) / 2), lerp(-2.6, -.8, (dx + 1) / 2)];
    } else if (v >= 1.9 && v < 2.7) {   // 转头看自己的纸轨道——它还在
      const k = eob(seg(v, 1.9, 2.25)); s.happy = false; s.mouth = 'smile';
      s.eA = [lerp(-1.3, -2.7, k), lerp(-1.0, -2.9, k)]; s.eL = [lerp(40, 46, k), lerp(44, 50, k)]; s.pu = [[-.9, .3], [-.9, .35]];
      if (v > 2.35) { s.happy = true; s.mouth = 'grin'; }
    } else if (v >= 2.7) { s.eA = [lerp(-2.7, -1.3, eio(seg(v, 2.7, 3.1))), lerp(-2.9, -1.0, eio(seg(v, 2.7, 3.1)))]; }
    S.winds.push({ t0: 60.15, t1: 62.5, x0: -800, span: 800, y0: -1545, gap: 44, n: 4, len: 280 });
    S.winds.push({ t0: 62.8, t1: 65.6, x0: -1900, span: 2100, y0: -1150, gap: 380, n: 3, len: 600, wk: 1.6 });
    S.grassA = .08 + .07 * Math.sin(v * 6) * Math.exp(-Math.max(0, v - 3.5) * .8) * seg(v, 2.9, 3.4);
    // 纸轨道被风吹得轻颤一下，但稳稳挂着
    S.trackWob = windOn ? Math.sin(t * 30) * 1.5 * Math.sin(Math.PI * seg(v, .6, 2.2)) : 0;
    if (v > 2.3 && v < 3) S.fx.push({ type: 'sparkle', x: -415, y: -1470, t0: 62.35, t1: 62.95 });
    const k = eio(seg(v, 2.8, 5.6));
    S.cam = { x: lerp(-60, -120, k), y: lerp(-1530, -800, k), z: 1 / lerp(1 / 1.9, 1 / .58, k) };
    S.fade = seg(v, 5.9, 6.5);
    S.snail = s; S.sun = 1; S.light = 1; S.night = 0;
    return S;
  }
  if (t < 6) { /* ---------- S1 目标 ---------- */
    s = onGround(-110, 1, { crawl: t * .3 });
    const up = eio(seg(t, 1.2, 1.8));
    s.eA = [lerp(-2.05, -1.8, up), lerp(-1.3, -1.45, up)]; s.eL = [lerp(28, 40, up), lerp(33, 44, up)];
    if (up > 0) s.pu = [[.1, -.9], [.2, -.9]];
    if (t > 4.6) { s.mouth = 'grin'; s.eR = 1.12; s.pR = 4.6; s.pu = [[.4, -.8], [.5, -.8]]; }
    S.bubble = { x: 30, y: -250, r: 88, t0: 4.55, t1: 6.05, fx: -75, fy: -135 };
    S.fade = 1 - seg(t, 0, .7);
  } else if (t < 10.5) { /* ---------- S2 错误1：叶尖 ---------- */
    if (t < 6.7) s = climb(lerp(-120, -245, seg(t, 6, 6.7)), { crawl: t * 1.6 });
    else if (t < 7.0) { const k = eio(seg(t, 6.7, 7.0)), a = climb(-245), b = fromPose(onLeaf1(0, 0)); s = mkSnail(lerp(a.x, b.x, k), lerp(a.y, b.y, k), angLerp(a.rot, b.rot, k), -1, { crawl: t * 1.6 }); }
    else if (t < 8.1) { const u = seg(t, 7.0, 8.1); S.leafD = .95 * Math.pow(u, 1.3); s = fromPose(onLeaf1(u, S.leafD), { crawl: t * 1.6, pu: [[.2, -.95], [.35, -.9]], eA: [-1.7, -1.3], eL: [34, 38] }); }
    else if (t < 8.55) {
      const tip = onLeaf1(1, .95), k = seg(t, 8.1, 8.55), land = [-290, 0];
      const p = k < .2 ? [lerp(tip.x, tip.x - 14, k / .2), tip.y + 8 * (k / .2)] : [lerp(tip.x - 14, land[0], (k - .2) / .8), lerp(tip.y + 8, 0, ei((k - .2) / .8))];
      s = mkSnail(p[0], p[1], lerp(poseOn(tip.up, tip.tan).rot, -TAU + 0, eio(k)), -1, { mouth: 'open', eR: 1.2, pR: 2.4, eA: [-2.6, -.6], eL: [36, 36] });
    } else {
      const k = seg(t, 8.55, 9.1); s = onGround(-290, -1, { sq: lerp(.4, 1, eel(k)), dizzy: t < 9.2, mouth: 'wobble', floppy: 3 });
      S.fx.push({ type: 'dust', x: -290, y: 0, t0: 8.55, t1: 9.1 }, { type: 'stars', x: -330, y: -82, t0: 8.6, t1: 9.35 });
      writeNote(S, s, t, 9.35, .7, .3, 'leaf', 0);
    }
    if (t >= 8.1) S.leafD = leafDroop(t, 7, 8.2, 1);
    const k = eio(seg(t, 8.1, 8.6));
    S.cam = { x: lerp(-130, -250, k), y: lerp(-210, -90, k), z: lerp(2.1, 2.6, k) };
  } else if (t < 14.5) { /* ---------- S3 错误2：爬错草 ---------- */
    const aB = eio(seg(t, 11.4, 12.6));
    S.grassA = t < 12.65 ? lerp(.08, 1.5, aB) : .08 + 1.42 * Math.exp(-4.5 * (t - 12.65)) * Math.cos((t - 12.65) * 9);
    const nts = notesAt(t);
    if (t < 12.6) {
      const u = t < 11.4 ? lerp(.06, .62, seg(t, 10.5, 11.4)) : lerp(.62, .95, seg(t, 11.4, 12.6));
      s = fromPose(onGrass(S.grassA, u), { crawl: t * 1.6, mouth: t > 11.6 ? 'grin' : 'derp', pu: [[.3, -.9], [.4, -.9]], eA: [-1.8, -1.3], eL: [34, 38] });
    } else if (t < 12.9) {
      const g = onGrass(1.5, .95), k = seg(t, 12.6, 12.9), ps = poseOn(g.up, g.tan);
      s = mkSnail(lerp(g.x, g.x - 20, k), lerp(g.y, 0, eio(k)), angLerp(ps.rot, 0, eio(k)), -1, { mouth: 'o' });
    } else {
      s = onGround(g2x(), -1, { mouth: 'o' });
      if (t < 13.5) { s.eA = [-2.4, -2.0]; s.eL = [38, 40]; s.pu = [[-.4, -.8], [-.3, -.9]]; }
      writeNote(S, s, t, 13.5, .45, .3, 'grass', 1);
    }
    s.notes = nts;
    const k = eio(seg(t, 12.7, 13.3));
    S.cam = { x: lerp(-790, g2x() + 10, k), y: lerp(-390, -85, k), z: lerp(1.1, 2.4, k) };
  } else if (t < 19) { /* ---------- S4 错误3：花盘下沿 ---------- */
    const nts = notesAt(t);
    if (t < 15.7) s = climbL(lerp(-880, -1335, eio(seg(t, 14.5, 15.7))), { crawl: t * 2, mouth: 'grin', pu: [[.9, -.2], [.9, -.1]], eA: [-1.3, -.9] });
    else if (t < 16.0) { const k = eio(seg(t, 15.7, 16.0)), a = climbL(-1335); s = mkSnail(lerp(a.x, -30, k), lerp(a.y, underY(-30), k), lerp(-Math.PI / 2, -Math.PI, k), 1, { crawl: t * 2, mouth: 'grin' }); }
    else if (t < 17.15) {
      const x = lerp(-30, -186, eio(seg(t, 16.0, 16.9))), dy = underY(x - 1) - underY(x + 1);
      const ps = poseOn([0, 1], [-2, dy]); s = mkSnail(x, underY(x), ps.rot + (t > 16.9 ? Math.sin(t * 50) * .08 : 0), 1, { crawl: t * 2, mouth: 'wobble', eL: [36, 40] });
      if (t > 16.9) { s.eR = 1.2; s.mouth = 'open'; }
    } else if (t < 18.0) {
      const k = seg(t, 17.15, 18.0), x0 = -186, y0 = underY(-186);
      s = mkSnail(lerp(x0, -200, k), lerp(y0, 0, ei(k)), lerp(Math.PI * .9, TAU * 2, k), 1, { mouth: 'open', eR: 1.25, pR: 2.2, eA: [-2.5, -.5], eL: [40, 40], floppy: 3 });
      S.fx.push({ type: 'speed', x: s.x, y: s.y - 30, vx: 0, vy: 1, t0: 17.3, t1: 17.98 });
    } else {
      const k = seg(t, 18.0, 18.5); s = onGround(-200, 1, { sq: lerp(.3, 1, eel(k)), dizzy: t < 18.4, mouth: 'wobble' });
      S.fx.push({ type: 'dust', x: -200, y: 0, t0: 18.0, t1: 18.6 }, { type: 'stars', x: -160, y: -82, t0: 18.05, t1: 18.5 });
      writeNote(S, s, t, 18.45, .25, .25, 'head', 2);
    }
    s.notes = nts;
    if (t < 17.15) { const k = eio(seg(t, 15.2, 16.3)); S.cam = { x: lerp(-40, -230, k), y: lerp(Math.max(s.y, underY(-40)) - 20, -1420, k), z: 1.6 }; }
    else { S.cam = { x: -170, y: Math.min(-70, s.y - 30), z: lerp(1.6, 2.5, eio(seg(t, 17.6, 18.2))) }; }
  } else if (t < 23) { /* ---------- S5 风吹走纸条 ---------- */
    s = onGround(-200, 1, { notes: notesAt(t) });
    if (t < 20.0) { s.mouth = 'grin'; s.happy = true; s.sq = 1 + .06 * Math.sin(t * 9); }
    else if (t < 20.9) { s.rot = -.07 * Math.sin(Math.PI * seg(t, 20, 20.9)); s.blink = .7; s.mouth = 'flat'; s.eA = [-2.5, -2.1]; s.floppy = 2.5; s.notes.forEach(n => { n.wob = Math.sin(t * 30 + n.slot) * .3; }); }
    else if (t < 21.9) { const k = eob(seg(t, 20.9, 21.3)); s.eA = [lerp(-2.05, -.75, k), lerp(-1.3, -.55, k)]; s.eL = [lerp(28, 52, k), lerp(33, 60, k)]; s.pu = [[.8, -.5], [.8, -.5]]; s.mouth = 'o'; }
    else { const k = eio(seg(t, 21.9, 22.6)); s.eA = [lerp(-.75, -2.05, k), -.55 + (k * -.2)]; s.eL = [lerp(52, 28, k), lerp(60, 44, k)]; s.pu = [[lerp(.8, 0, k), -.5], [.8, -.5]]; s.mouth = 'o'; }
    S.winds.push({ t0: 19.85, t1: 21.6, x0: -700, span: 900, y0: -80, gap: 42, n: 5, len: 260 });
    NOTE_SCHED.slice(0, 3).forEach(n => {
      if (t < n.off) return;
      const base = onGround(-200, 1), sl = SLOTS[n.slot], p0 = l2w(base, SHELLC[0] + sl[0], SHELLC[1] + sl[1]), tau = t - n.off;
      S.notes.push({ x: p0[0] + 330 * tau + 120 * tau * tau, y: p0[1] - 150 * tau - 90 * tau * tau + 16 * Math.sin(tau * 7), sz: 21, rot: sl[2] + 3 * tau + .6 * Math.sin(tau * 9), picto: n.picto, prog: 1, flip: Math.cos(tau * 10) });
    });
    const k = eio(seg(t, 20.5, 21.6));
    S.cam = { x: lerp(-185, -40, k), y: lerp(-62, -200, k), z: lerp(3.0, 1.9, k) };
  } else if (t < 26.5) { /* ---------- S6 再犯 + 灵感 ---------- */
    if (t < 23.8) { const u = lerp(.3, 1, seg(t, 23, 23.8)); S.leafD = .95 * Math.pow(u, 1.3); s = fromPose(onLeaf1(u, S.leafD), { crawl: t * 2.2, pu: [[.2, -.95], [.35, -.9]], eA: [-1.7, -1.3], eL: [34, 38] }); }
    else if (t < 24.2) {
      const tip = onLeaf1(1, .95), k = seg(t, 23.8, 24.2);
      s = mkSnail(lerp(tip.x, -290, k), lerp(tip.y, 0, ei(k)), lerp(poseOn(tip.up, tip.tan).rot, -TAU, eio(k)), -1, { mouth: 'open', eR: 1.2, pR: 2.4, eA: [-2.6, -.6], eL: [36, 36] });
    } else {
      const k = seg(t, 24.2, 24.7); s = onGround(-290, -1, { sq: lerp(.4, 1, eel(k)), mouth: 'flat' });
      S.fx.push({ type: 'dust', x: -290, y: 0, t0: 24.2, t1: 24.7 });
      if (t > 24.7) { s.pu = [[-.2, -.8], [.2, -.9]]; s.mouth = 'o'; }
      if (t > 25.1) { s.pu = [[.8, .3], [.7, .5]]; }
      if (t > 25.3 && t < 25.65) { const kk = eob(seg(t, 25.3, 25.5)); s.eA = [-2.05, lerp(-1.3, -.25, kk)]; s.eL = [28, lerp(33, 52, kk)]; s.pu = [[.8, .5], [1, .2]]; }
      if (t > 25.65) { s.eR = 1.25; s.pR = 2.6; s.mouth = 'open'; s.pu = [[.2, -.3], [.3, -.2]]; s.eL = [36, 40]; }
      if (t > 26.0) { s.mouth = 'grin'; }
      S.fx.push({ type: 'bang', x: -300, y: -118, t0: 25.85, t1: 26.5, s: .9 });
    }
    if (t >= 23.8) S.leafD = leafDroop(t, 23, 23.9, 1);
    // 飘下来的纸条（折成手风琴）
    if (t >= 24.35) {
      const k = seg(t, 24.35, 25.2), zx = lerp(-150, -385, eio(k)) + Math.sin(k * 9) * 30 * (1 - k), zy = lerp(-520, 0, eo(k));
      const bj = t > 25.6 ? Math.sin(Math.PI * seg(t, 25.6, 26.1)) * 70 : 0, hh = t > 25.6 && t < 25.66 ? 8 : 22 + (t > 25.6 ? 8 * Math.sin(t * 30) * (1 - seg(t, 25.6, 26.3)) : 0);
      S.zig = { x: zx, y: zy - bj, h: hh, rot: (1 - k) * Math.sin(k * 11) * .8 };
      if (t > 25.6) S.fx.push({ type: 'boing', x: -385, y: -10, t0: 25.6, t1: 26.1 });
    }
    const k = eio(seg(t, 24.3, 25.3));
    S.cam = { x: lerp(-130, -330, k), y: lerp(-210, -60, k), z: lerp(2.1, 3.0, k) };
  } else if (t < 31) { /* ---------- S7 造弹簧 v1/v2 ---------- */
    S.clouds.push({ x: -250, y: -62, r: 88, t0: 26.5, t1: 27.4, seed: 1 });
    if (t < 26.5 + .15) s = onGround(-150, -1);
    else if (t < 27.4) s = null;
    else if (t < 27.7) s = onGround(-150, -1, { mouth: 'grin', happy: true });
    else if (t < 28.0) { const k = seg(t, 27.7, 28.0), p = arc([-150, 0], [-262, -64], eio(k), 40); s = mkSnail(p[0], p[1], 0, -1, { mouth: 'grin' }); }
    else if (t < 28.8) { const h = S.spring.h; s = mkSnail(-262, -h - 6, S.spring.lean, -1, { sq: lerp(1, .42, eo(seg(t, 28.0, 28.2))), mouth: 'wobble', dizzy: t > 28.25, floppy: 3 }); if (t > 28.2) S.fx.push({ type: 'dust', x: -262, y: -10, t0: 28.2, t1: 28.7 }); }
    else if (t < 29.4) {
      // 从塌掉的弹簧里爬出来，亲手一节一节重折、扶正（不再用云团，免得像重播）
      const k = seg(t, 28.97, 29.35), n = Math.min(6, k * 6), st = Math.floor(n);
      S.spring = t < 28.97 ? { h: 8, crude: true, lean: -.35 } : { h: 8 + 62 * (st + eob(n - st)) / 6, crude: false, lean: lerp(-.3, 0, eio(k)) };
      if (t < 28.97) { const kk = eio(seg(t, 28.8, 28.97)), p = arc([-262, -14], [-150, 0], kk, 30); s = mkSnail(p[0], p[1], 0, -1, { sq: lerp(.42, 1, kk), mouth: 'wobble' }); }
      else {
        s = onGround(-150, -1, { mouth: 'wobble', pu: [[.9, .1], [.9, .2]], eA: [-1.7, -1.1], eL: [30, 36] });
        s.rot = Math.sin(t * 45) * .05;
        for (let i = 0; i < 6; i++) {      // 一张张黄纸甩过去，贴成一节
          const t0 = 28.97 + i * .38 / 6 - .06, kk = seg(t, t0, t0 + .09);
          if (kk > 0 && kk < 1) { const p = arc([-190, -48], [-262, -8 - (i + .5) * 62 / 6], kk, 30); S.notes.push({ x: p[0], y: p[1], sz: 16, rot: kk * 6, picto: null, prog: 1, flip: Math.cos(kk * 9) }); }
        }
        if (t > 29.3) S.fx.push({ type: 'sparkle', x: -262, y: -50, t0: 29.3, t1: 29.4 });
      }
    }
    else if (t < 29.6) s = onGround(-150, -1, { mouth: 'grin', happy: true });
    else if (t < 29.9) { const k = seg(t, 29.6, 29.9), p = arc([-150, 0], [-262, -76], eio(k), 40); s = mkSnail(p[0], p[1], 0, -1, { mouth: 'grin' }); }
    else {
      // 弹：压缩 → 发射 → 回落 → 小弹
      let h = 70, y;
      if (t < 30.05) { h = lerp(70, 30, eo(seg(t, 29.9, 30.05))); y = -h - 6; }
      else if (t < 30.7) { const k = seg(t, 30.05, 30.7); h = t < 30.15 ? lerp(30, 108, seg(t, 30.05, 30.12)) : 70 + 38 * Math.exp(-8 * (t - 30.12)) * Math.cos((t - 30.12) * 30); y = -76 - 700 * 4 * k * (1 - k) * (k < .5 ? 1 : 1); }
      else if (t < 30.8) { h = lerp(70, 42, Math.sin(Math.PI * seg(t, 30.7, 30.8))); y = -h - 6; }
      else { const k = seg(t, 30.8, 31); h = 70; y = -76 - 60 * 4 * k * (1 - k); }
      S.spring.h = h;
      s = mkSnail(-262, y, t > 30.1 && t < 30.7 ? (t - 30.1) * 10.5 : 0, -1, { mouth: 'grin', sq: t < 30.05 ? lerp(1, .6, seg(t, 29.9, 30.05)) : 1, eL: t > 30.05 && t < 30.7 ? [40, 44] : [28, 33], happy: t > 30.8 });
      S.fx.push({ type: 'boing', x: -262, y: -90, t0: 30.05, t1: 30.5 });
    }
    S.cam = { x: -250, y: -120, z: 2.25 };
  } else if (t < 33.5) { /* ---------- S8 草尖纸杯 ---------- */
    S.clouds.push({ x: -1235, y: -55, r: 66, t0: 31.4, t1: 32.1, seed: 3 });
    if (t < 31.5) {
      S.grassA = lerp(.9, 1.5, eio(seg(t, 31, 31.45)));
      s = fromPose(onGrass(S.grassA, .9), { mouth: 'grin', pu: [[.3, -.9], [.4, -.9]] });
    } else {
      S.grassA = t < 32.2 ? 1.5 : lerp(1.5, .08, eio(seg(t, 32.2, 32.8))) + (t > 32.8 ? -.12 * Math.exp(-5 * (t - 32.8)) * Math.sin((t - 32.8) * 12) : 0);
      if (t < 32.1) s = null;
      else { s = onGround(-1165, -1, { mouth: 'grin', happy: t < 32.3 || t > 33.0 }); s.eA = [-1.9, -1.6]; s.pu = [[-.2, -.95], [-.1, -.95]]; }
    }
    S.cam = { x: -790, y: -390, z: 1.1 };
  } else if (t < 37) { /* ---------- S9 花盘下的纸轨道 + 纸鹤 ---------- */
    S.clouds.push({ x: -290, y: -1430, r: 96, t0: 33.9, t1: 34.45, seed: 4 });
    const ang = x => { const dy = underY(x - 1) - underY(x + 1); return poseOn([0, 1], [-2, dy]).rot; };
    if (t < 34.0) { const x = lerp(-80, -186, seg(t, 33.5, 34.0)); s = mkSnail(x, underY(x), ang(x), 1, { crawl: t * 2, mouth: 'derp' }); }
    else if (t < 34.45) s = null;
    else if (t < 35.8) { s = mkSnail(-186, underY(-186), ang(-186), 1, { mouth: 'grin', happy: t > 34.6, sq: 1 + .05 * Math.sin(t * 10) }); if (t > 34.5) S.fx.push({ type: 'sparkle', x: -410, y: -1470, t0: 34.75, t1: 35.35 }, { type: 'sparkle', x: -260, y: -1610, t0: 34.9, t1: 35.5 }); }
    else if (t < 36.35) { const k = seg(t, 35.8, 36.35); const y0 = underY(-186); s = mkSnail(-186, t < 36.0 ? y0 : lerp(y0, y0 + 700, ei(seg(t, 36.0, 36.35))), ang(-186) + (t < 36 ? Math.sin(t * 60) * .1 : (t - 36) * 6), 1, { mouth: 'open', eR: 1.2, pR: 2.2 }); }
    S.craneRot = t > 36.4 ? Math.sin((t - 36.4) * 9) * .25 * Math.exp(-(t - 36.4) * 2) : 0;
    S.cam = { x: -230, y: -1420, z: 1.6 };
  } else if (t < 39.15) { /* ---------- S10 最终尝试：照样踩空 ---------- */
    if (t < 38.7) { const u = lerp(.1, 1, seg(t, 37, 38.7)); S.leafD = .95 * Math.pow(u, 1.3); s = fromPose(onLeaf1(u, S.leafD), { crawl: t * 1.8, mouth: 'grin', pu: [[.2, -.95], [.35, -.9]], eA: [-1.7, -1.3], eL: [34, 38] }); }
    else {
      const tip = onLeaf1(1, .95), k = seg(t, 38.7, 39.15), tg = [-262, -76];
      const p = k < .25 ? [lerp(tip.x, tip.x - 10, k / .25), tip.y + 6 * k / .25] : [lerp(tip.x - 10, tg[0], (k - .25) / .75), lerp(tip.y + 6, tg[1], ei((k - .25) / .75))];
      s = mkSnail(p[0], p[1], lerp(poseOn(tip.up, tip.tan).rot, 0, eio(k)), -1, { mouth: 'open', eR: 1.2, pR: 2.4, eA: [-2.6, -.6], eL: [36, 36] });
      S.leafD = leafDroop(t, 37, 38.8, 1);
    }
    const k = eio(seg(t, 37.8, 38.5));
    S.cam = { x: lerp(-380, -210, k), y: lerp(-760, -190, k), z: lerp(.62, 1.9, k) };
  } else if (t < 40.9) { /* ---------- S11 弹簧 → 飞向草尖纸杯 ---------- */
    const c = cupAt(.08), land = [c.tip[0] + c.ax[0] * 14, c.tip[1] + c.ax[1] * 14];
    if (t < 39.42) {
      const h = t < 39.35 ? lerp(70, 28, eo(seg(t, 39.15, 39.35))) : lerp(28, 112, seg(t, 39.35, 39.42));
      S.spring.h = h; s = mkSnail(-262, -h - 6, 0, -1, { sq: t < 39.35 ? lerp(1, .55, eo(seg(t, 39.15, 39.35))) : 1.2, mouth: 'wobble' });
    } else {
      const k = seg(t, 39.42, 40.6);
      S.spring.h = 70 + 42 * Math.exp(-7 * (t - 39.42)) * Math.cos((t - 39.42) * 28);
      const p = arc([-262, -118], land, k, 420 * .25 + 0) ; const yy = lerp(-118, land[1], k) - 440 * 4 * k * (1 - k) * .6;
      s = mkSnail(p[0], yy, t < 40.6 ? eio(k) * -TAU * 2 : 0, -1, { mouth: 'grin', eL: [38, 42], eA: [-2.4, -1.9], floppy: 2 });
      if (t >= 40.6) { const ps = poseOn(c.ax, [-c.ax[1], c.ax[0]]); s = mkSnail(land[0], land[1], ps.rot, -1, { mouth: 'grin', sq: lerp(.7, 1, eel(seg(t, 40.6, 40.9))) }); S.cupFront = true; }
      S.fx.push({ type: 'boing', x: -262, y: -90, t0: 39.4, t1: 39.9 });
      if (k < .9) S.fx.push({ type: 'speed', x: s.x, y: s.y - 20, vx: -.5, vy: -.85, t0: 39.5, t1: 40.3 });
    }
    S.cam = _inner ? null : followCam(t, 39.15, 1.9, .95);
  } else if (t < 42.35) { /* ---------- S12 草弯 → 回弹甩飞 ---------- */
    let a;
    if (t < 41.3) a = lerp(.08, .95, eo(seg(t, 40.9, 41.3)));
    else if (t < 41.46) a = lerp(.95, 0, ei(seg(t, 41.3, 41.46)));
    else a = .08 - .5 * Math.exp(-3.5 * (t - 41.46)) * Math.cos((t - 41.46) * 11 - 1.4);
    S.grassA = a;
    if (t < 41.46) {
      const c = cupAt(a), p = [c.tip[0] + c.ax[0] * 14, c.tip[1] + c.ax[1] * 14], ps = poseOn(c.ax, [-c.ax[1], c.ax[0]]);
      s = mkSnail(p[0], p[1], ps.rot, -1, { mouth: t < 41.3 ? 'grin' : 'open', eR: t < 41.3 ? 1 : 1.2, floppy: 2.5, eA: t > 41.2 ? [-2.7, -2.4] : undefined });
      S.cupFront = true;
    } else {
      const k = seg(t, 41.46, 42.35), p0 = [-520, -754], p1 = [-300, -1262];
      s = mkSnail(lerp(p0[0], p1[0], eo(k)), lerp(p0[1], p1[1], eo(k)), k * TAU * 1.25, -1, { mouth: 'grin', eL: [40, 44], eA: [-2.5, -2.2] });
      S.fx.push({ type: 'speed', x: s.x, y: s.y - 20, vx: .4, vy: -.9, t0: 41.5, t1: 42.2 });
    }
    S.cam = _inner ? null : followCam(t, 40.9, 1.0, 1.25);
  } else if (t < 44.8) { /* ---------- S13 纸轨道绕过花盘 → 落上花盘 ---------- */
    if (t < 43.35) {
      const k = seg(t, 42.35, 43.35), tr = trackAt(ei(k) * .5 + k * .5);
      const up = tr.n, ps = poseOn(up, tr.t);
      s = mkSnail(tr.pt[0] + up[0] * 15, tr.pt[1] + up[1] * 15, ps.rot, ps.dir, { mouth: 'grin', eL: [36, 44], floppy: 2.5, st: .3 });
      S.fx.push({ type: 'speed', x: s.x, y: s.y, vx: tr.t[0], vy: tr.t[1], t0: 42.4, t1: 43.35 });
    } else if (t < 43.8) {
      const k = seg(t, 43.35, 43.8), ex = trackAt(1).pt, p = arc([ex[0], ex[1] - 10], [-30, -1466], k, 70);
      s = mkSnail(p[0], p[1], lerp(0, -TAU, eio(k)), -1, { mouth: 'open', eR: 1.15 });
    } else { s = mkSnail(-30, -1466, 0, -1, { sq: lerp(.5, 1, eel(seg(t, 43.8, 44.3))), mouth: 'flat', pu: [[-.9, 0], [-.8, .2]] }); S.fx.push({ type: 'dust', x: -30, y: -1466, t0: 43.8, t1: 44.3 }); }
    S.craneRot = t > 43.2 ? Math.sin((t - 43.2) * 14) * .5 * Math.exp(-(t - 43.2) * 2.5) : 0;
    if (t < 43.8) {
      S.cam = { x: -250, y: -1440, z: 1.35 };
      // 从 S12 的跟拍机位平滑过渡到轨道固定机位，避免换段时镜头跳一下
      if (!_inner && t < 42.85) {
        const c0 = followCam(42.3499, 40.9, 1.0, 1.25), q = eio(seg(t, 42.35, 42.85));
        S.cam = { x: lerp(c0.x, -250, q), y: lerp(c0.y, -1440, q), z: lerp(c0.z, 1.35, q) };
      }
    }
    else { const k = eio(seg(t, 43.9, 44.8)); S.cam = { x: lerp(-250, 100, k), y: lerp(-1440, -1500, k), z: lerp(1.35, 2.0, k) }; }
  } else if (t < 51.5) { /* ---------- S14 背对太阳 → 日出 ---------- */
    s = mkSnail(-30, -1466, 0, -1, { mouth: 'o' });
    if (t < 46.3) { s.pu = [[-.9, Math.sin(t * 2) * .3], [-.8, .2 + Math.cos(t * 2.3) * .3]]; s.eA = [-2.05 + Math.sin(t * 1.4) * .2, -1.3]; }
    else if (t < 46.8) { s.pu = [[.2, .9], [.1, .95]]; s.eA = [-1.6, -1.0]; }
    else if (t < 47.6) {
      const k1 = eob(seg(t, 46.8, 47.3)), k2 = eob(seg(t, 47.0, 47.5));
      s.eA = [lerp(-1.6, -2.75, k1), lerp(-1.0, -2.95, k2)]; s.eL = [lerp(28, 40, k1), lerp(33, 44, k2)]; s.pu = [[-.95, .1], [-.95, 0]];
      if (t > 47.3) { s.mouth = 'open'; s.eR = 1.18; s.pR = 4.8; }
    } else if (t < 48.1) {
      const k = eio(seg(t, 47.6, 48.1)); s.fx = lerp(-1, 1, k); s.dir = k < .5 ? -1 : 1;
      s.eA = [lerp(-2.75, -1.25, k), lerp(-2.95, -1.0, k)]; s.eL = [40, 44]; s.mouth = 'open'; s.eR = 1.18; s.pR = 4.8; s.pu = [[.9, -.2], [.9, -.2]];
      if (Math.abs(s.fx) < .15) s.fx = s.fx < 0 ? -.15 : .15;
    } else {
      s.dir = 1; s.mouth = 'grin'; s.happy = t > 48.6; s.eA = [-1.3, -1.0]; s.eL = [40, 44]; s.eR = 1.18; s.pR = 4.8; s.pu = [[.9, -.2], [.9, -.2]];
      s.rot = Math.sin((t - 48.1) * 2.2) * .05;
    }
    S.fx.push({ type: 'bang', x: -30, y: -1560, t0: 47.3, t1: 47.9, s: .8 });
    const k = eio(seg(t, 47.6, 51.5));
    S.cam = { x: lerp(100, 110, k), y: lerp(-1500, -1515, k), z: lerp(2.0, 2.6, k) };
  } else { /* ---------- S15 再写一张纸条 → 又被吹走 → 拉远 ---------- */
    s = mkSnail(-30, -1466, 0, 1, { mouth: 'grin', happy: true, eA: [-1.3, -1.0], eL: [40, 44], notes: notesAt(t) });
    s.happy = !(t > 51.6 && t < 52.9);
    writeNote(S, s, t, 51.6, .8, .3, 'sun', 3);
    if (t >= 53.7) {
      const tau = t - 53.7, sl = SLOTS[3], base = mkSnail(-30, -1466, 0, 1), p0 = l2w(base, SHELLC[0] + sl[0], SHELLC[1] + sl[1]);
      S.notes.push({ x: p0[0] + 180 * tau + 60 * tau * tau, y: p0[1] - 90 * tau - 30 * tau * tau + 14 * Math.sin(tau * 6), sz: 21 + tau * 3, rot: 2.4 * tau + .5 * Math.sin(tau * 8), picto: 'sun', prog: 1, flip: Math.cos(tau * 8) });
      s.happy = false; s.mouth = 'grin'; s.eA = [lerp(-1.3, -.7, eo(seg(t, 53.7, 54.4))), -1.0]; s.eL = [lerp(40, 58, eo(seg(t, 53.7, 54.4))), 44];
      s.pu = [[.8, -.5], [.9, -.2]];
    }
    if (t > 53.2 && t < 53.7) s.notes.forEach(n => n.wob = Math.sin(t * 40) * .25);
    S.winds.push({ t0: 53.1, t1: 54.7, x0: -500, span: 450, y0: -1520, gap: 36, n: 3, len: 200, wk: .8 });
    const k = eio(seg(t, 54.2, 56.6));
    S.cam = { x: lerp(110, -120, k), y: lerp(-1515, -800, k), z: 1 / lerp(1 / 2.6, 1 / .58, k) };
    S.fade = seg(t, 56.1, 57);
  }
  // 日出
  S.sun = eo(seg(t, 45.0, 49.5));
  S.light = seg(t, 45.4, 48.8);
  S.night = 1 - eio(seg(t, 45.4, 49.0));
  S.snail = s;
  return S;
}
function g2x() { const g = onGrass(1.5, .95); return g.x - 20; }
// 跟拍镜头：用过去 0.35 秒的蜗牛位置做平滑（确定性）
function followCam(t, t0, z0, z1) {
  let x = 0, y = 0, w = 0;
  for (let i = 0; i < 7; i++) { const tt = Math.max(t0, t - i * .05), sn = scene_snail(tt); if (!sn) continue; const ww = 1 - i / 8; x += sn.x * ww; y += sn.y * ww; w += ww; }
  const z = lerp(z0, z1, eio(seg(t, t0, t0 + .8)));
  return { x: x / w, y: y / w - 40, z };
}
let _inner = false;
function scene_snail(t) { _inner = true; const s = scene(t).snail; _inner = false; return s; }


/* ---------------- 第二版剪辑：新时间 → 分镜段（旧时间轴） ----------------
   每段 [新起, 新止, 段起, 段止, 去掉壳上纸条?] ；100+ 与 60+ 是新写的段落 */
const EDIT = [
  [0, 4.5, 0, 6],            // 目标（1.33 倍速）
  [4.5, 8.85, 6, 10.35],     // 错误1：踩空叶尖 → 写纸条贴上
  [8.85, 10.6, 100, 101.75], // 风来，纸条刚贴上就被吹走
  [10.6, 14.1, 23, 26.5],    // 同一构图，立刻又踩空 → 折纸弹起 → “！”
  [14.1, 16.4, 26.5, 28.8],  // 机关1：施工云团 → 歪弹簧，一踩就塌
  [16.4, 17.7, 28.8, 29.6],  // 从塌掉的弹簧里爬出来，亲手一节节重折、扶正
  [17.7, 19.1, 29.6, 31],    // 新弹簧一弹冲天
  [19.1, 21.1, 10.5, 12.6, 1], // 错误2：爬错草，草弯到地（同一镜头接下去）
  [21.1, 23.15, 31.5, 33.5], //   就地在草尖装纸杯，草带杯弹回
  [23.15, 24.65, 14.5, 16.9, 1], // 错误3：一路爬上花盘底下，倒挂往外蹭（一个跟拍镜头）
  [24.65, 27.25, 34.0, 36.6], //   原地施工纸轨道（0.45 秒），又掉下去
  [27.25, 35.05, 37, 44.8],  // 最后一次：照样踩空 → 连锁
  [35.05, 41.75, 44.8, 51.5], // 日出
  [41.75, 48.25, 60, 66.5]   // 结尾：风又来，纸条飞走，机关还在
];
function remap(t) {
  for (const e of EDIT) if (t < e[1]) return { t: lerp(e[2], e[3], clamp((t - e[0]) / (e[1] - e[0]))), noNotes: !!e[4] };
  const e = EDIT[EDIT.length - 1]; return { t: e[3] - 1e-6, noNotes: false };
}

/* =====================================================================
   帧绘制
   ===================================================================== */
function frame(t) {
  const tNew = clamp(t, 0, DUR - 1e-6), R = remap(tNew);
  t = R.t;
  T = t; BOIL = Math.floor(t * 8);
  if (!TEX) makeTexture();
  const S = scene(t);
  if (R.noNotes && S.snail) S.snail.notes = [];
  N = S.night;
  cam = S.cam || { x: -60, y: -80, z: 2.6 };
  if (!S.cam && t < 6) {
    // 开场：近景 → 仰摇到花盘 → 回到蜗牛
    const k1 = eio(seg(t, 1.5, 3.7)), k2 = eio(seg(t, 4.2, 4.75));
    const a = { x: -95, y: -55, z: 3.4 }, b = { x: 0, y: -1290, z: 0.95 }, c = { x: -40, y: -150, z: 2.4 };
    const m = { x: lerp(a.x, b.x, k1), y: lerp(a.y, b.y, k1), z: 1 / lerp(1 / a.z, 1 / b.z, k1) };
    cam = { x: lerp(m.x, c.x, k2), y: lerp(m.y, c.y, k2), z: 1 / lerp(1 / m.z, 1 / c.z, k2) };
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0); ctx.globalCompositeOperation = 'source-over'; ctx.globalAlpha = 1;
  drawSky(S);
  ctx.setTransform(cam.z, 0, 0, cam.z, W / 2 - cam.x * cam.z, H / 2 - cam.y * cam.z);
  drawGround();
  drawGrass(S);
  if (S.cup && !S.cupFront) drawCup(S.grassA);
  drawStem();
  drawHead(S);
  ctx.save(); ctx.translate(S.trackWob || 0, (S.trackWob || 0) * .5); drawTrack(S.track); ctx.restore();
  drawLeaf(leaf1P(S.leafD), 44, 11);
  if (S.spring) drawSpring(S.spring.h, S.spring.crude, S.spring.lean);
  // 日出时的影子
  if (S.snail && S.light > 0 && t > 44.8) {
    const L = 40 + S.light * 150, sh = ell(S.snail.x - L * .55, S.snail.y + 2, L * .6, 12);
    hatch(sh, { sp: 5, a: .8 * S.light, ang: .5 });
  }
  drawSnail(S.snail);
  if (S.cup && S.cupFront) drawCup(S.grassA);
  if (S.zig) drawZig(S.zig.x, S.zig.y, 34, S.zig.h, S.zig.rot);
  S.notes.forEach(n => drawNote(n.x, n.y, n.sz, n.rot, n.picto, n.prog, n.flip ?? 1, n.pop ?? 1));
  S.clouds.forEach(drawCloud);
  S.fx.forEach(drawFx);
  S.winds.forEach(drawWind);
  if (S.bubble) drawBubble(S.bubble);
  // 阳光：整体暖黄
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  if (S.light > 0) {
    ctx.save(); ctx.globalCompositeOperation = 'multiply';
    const sx = W * .83, sy = S.hy - S.sun * 300, gr = ctx.createRadialGradient(sx, sy, 50, sx, sy, W * 1.1);
    gr.addColorStop(0, yel(.22 * S.light)); gr.addColorStop(.5, yel(.08 * S.light)); gr.addColorStop(1, yel(.02 * S.light));
    ctx.fillStyle = gr; ctx.fillRect(0, 0, W, H); ctx.restore();
  }
  ctx.save(); ctx.globalCompositeOperation = 'multiply'; ctx.drawImage(TEX, 0, 0); ctx.restore();
  if (S.fade > 0) { ctx.fillStyle = `rgba(243,236,221,${S.fade})`; ctx.fillRect(0, 0, W, H); }
}
window.frame = frame; window.DUR = DUR; window.FPS = FPS;
