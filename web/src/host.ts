// 把这个前端嵌进自己网站的宿主，可以在页面 <head> 里留一行：
//   <meta name="horon-host" content='{"back": "/home", "label": "My AIs", "title": "Nocturne"}'>
// back / label：顶栏最左边的返回链接（回宿主自己的页面）；title：这份记忆属于谁，写在字标旁边。
// 都是可选的；没有这行 meta（本地直接跑 Horon）就什么都不显示。
// 用 meta 不用内联脚本：宿主的 CSP 常常不放行内联脚本。

export interface HostInfo {
  back: string | null;
  label: string;
  title: string | null;
}

const NONE: HostInfo = { back: null, label: "Back", title: null };

function text(v: unknown, max: number): string | null {
  return typeof v === "string" && v.trim() ? v.trim().slice(0, max) : null;
}

function readHost(): HostInfo {
  const raw = document.querySelector<HTMLMetaElement>('meta[name="horon-host"]')?.content;
  if (!raw) return NONE;
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(raw);
  } catch {
    console.error("horon-host meta is not valid JSON:", raw);
    return NONE;
  }
  // 只接受本站的绝对路径（/ 开头、不是 //），挡住 javascript: 和跳去别的网站
  const back = text(data.back, 500);
  return {
    back: back && back.startsWith("/") && !back.startsWith("//") ? back : null,
    label: text(data.label, 40) ?? "Back",
    title: text(data.title, 80),
  };
}

export const HOST: HostInfo = readHost();
