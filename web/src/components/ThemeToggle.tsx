import { useTheme } from "../theme";
import "./ThemeToggle.css";

function SunIcon() {
  return (
    <svg className="theme-icon theme-icon-sun" viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
      <circle cx="10" cy="10" r="3.6" fill="currentColor" />
      <g stroke="currentColor" strokeWidth="1.6" strokeLinecap="round">
        <path d="M10 2.2v1.8" />
        <path d="M10 16v1.8" />
        <path d="M2.2 10H4" />
        <path d="M16 10h1.8" />
        <path d="m4.5 4.5 1.3 1.3" />
        <path d="m14.2 14.2 1.3 1.3" />
        <path d="m15.5 4.5-1.3 1.3" />
        <path d="m5.8 14.2-1.3 1.3" />
      </g>
    </svg>
  );
}

function MoonIcon() {
  return (
    <svg className="theme-icon theme-icon-moon" viewBox="0 0 20 20" width="14" height="14" aria-hidden="true">
      <path
        d="M16.2 12.4A6.6 6.6 0 0 1 7.6 3.8a.5.5 0 0 0-.7-.6 7.6 7.6 0 1 0 9.9 9.9.5.5 0 0 0-.6-.7Z"
        fill="currentColor"
      />
    </svg>
  );
}

/** 顶栏右侧的亮/暗主题开关：滑块在哪边，就是当前主题。 */
export default function ThemeToggle() {
  const { theme, toggleTheme } = useTheme();
  const isDark = theme === "dark";
  // switch 角色的名字要固定（开/关状态由 aria-checked 表达），title 才给"点了会怎样"的提示
  const hint = isDark ? "Switch to light mode" : "Switch to dark mode";

  return (
    <button
      type="button"
      role="switch"
      aria-checked={isDark}
      aria-label="Dark mode"
      title={hint}
      className={`theme-toggle ${isDark ? "is-dark" : ""}`}
      onClick={toggleTheme}
    >
      <SunIcon />
      <MoonIcon />
      <span className="theme-toggle-thumb" aria-hidden="true" />
    </button>
  );
}
