import "./Wordmark.css";

interface WordmarkProps {
  /** 字号（px）。顶栏 30，加载界面 56。 */
  size?: number;
  /** loading：句点缓缓明灭；error：句点熄灭。其余情况是静态的。 */
  state?: "idle" | "loading" | "error";
}

// Horon 的标识就是字标本身：衬线 “Horon” 加一个珊瑚色句点。
// 句点是图谱里“被点亮的节点”；除此之外不画图形，也就不会跟任何已有的图标撞形。
// 窄屏下 .wordmark-rest 会被隐藏，只剩 “H.”（见 App.css）。
export default function Wordmark({ size = 30, state = "idle" }: WordmarkProps) {
  return (
    <span
      className={`wordmark ${state !== "idle" ? `is-${state}` : ""}`}
      style={{ fontSize: size }}
      role="img"
      aria-label="Horon"
    >
      <span aria-hidden="true">H</span>
      <span className="wordmark-rest" aria-hidden="true">
        oron
      </span>
      <span className="wordmark-dot" aria-hidden="true" />
    </span>
  );
}
