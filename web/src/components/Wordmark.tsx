import "./Wordmark.css";

interface WordmarkProps {
  /** 字号（px）。顶栏 19，加载界面 40。 */
  size?: number;
  /** loading：灯缓缓明灭；error：灯熄灭。其余情况是静态的。 */
  state?: "idle" | "loading" | "error";
}

// Horon 的标识：一盏亮着的灯，加上系统字体写的 “horon”。
// 这盏灯就是星图里“通电”节点的样子（罐光色、外面一圈光晕），标识和产品里看到的是同一个东西。
// 窄屏下 .wordmark-text 会被隐藏，只剩灯（见 App.css）。
export default function Wordmark({ size = 19, state = "idle" }: WordmarkProps) {
  return (
    <span
      className={`wordmark ${state !== "idle" ? `is-${state}` : ""}`}
      style={{ fontSize: size }}
      role="img"
      aria-label="Horon"
    >
      <span className="wordmark-lamp" aria-hidden="true" />
      <span className="wordmark-text" aria-hidden="true">
        horon
      </span>
    </span>
  );
}
