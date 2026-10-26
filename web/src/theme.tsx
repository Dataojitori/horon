import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export type Theme = "light" | "dark";

/** 默认亮色。用户切换后记在 localStorage，下次打开沿用。 */
export const DEFAULT_THEME: Theme = "light";
export const THEME_STORAGE_KEY = "horon:theme";

// index.html 里有一段内联脚本在首屏绘制前做同样的事（防止暗色用户先闪一下白屏），
// 改这里的存储键或取值时，两边要一起改。
function isTheme(v: unknown): v is Theme {
  return v === "light" || v === "dark";
}

function readStoredTheme(): Theme {
  try {
    const v = localStorage.getItem(THEME_STORAGE_KEY);
    if (isTheme(v)) return v;
  } catch {
    /* 无痕/禁存储时回默认主题 */
  }
  return DEFAULT_THEME;
}

function readInitialTheme(): Theme {
  const fromDom = document.documentElement.dataset.theme;
  return isTheme(fromDom) ? fromDom : readStoredTheme();
}

// 浏览器地址栏/系统标题栏的颜色跟着主题走
const THEME_COLOR: Record<Theme, string> = {
  light: "#F7EDE2",
  dark: "#1a1519",
};

function applyTheme(theme: Theme) {
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  document
    .querySelector('meta[name="theme-color"]')
    ?.setAttribute("content", THEME_COLOR[theme]);
}

interface ThemeContextValue {
  theme: Theme;
  setTheme: (theme: Theme) => void;
  toggleTheme: () => void;
}

const ThemeContext = createContext<ThemeContextValue | null>(null);

// 切换时给 <html> 临时挂一个类，让背景/文字/边框柔和过渡 ~0.4s；
// 过渡期间不影响平移缩放（过渡只作用于颜色类属性），结束后摘掉，免得日常交互被拖慢。
const TRANSITION_CLASS = "theme-transition";
const TRANSITION_MS = 420;

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [theme, setThemeState] = useState<Theme>(readInitialTheme);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 首次挂载时把 DOM 和状态对齐（内联脚本没跑成时也能兜住）
  useEffect(() => {
    applyTheme(theme);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const setTheme = useCallback((next: Theme) => {
    const root = document.documentElement;
    const reduceMotion = window.matchMedia?.("(prefers-reduced-motion: reduce)").matches;
    if (!reduceMotion) {
      root.classList.add(TRANSITION_CLASS);
      if (timerRef.current) clearTimeout(timerRef.current);
      timerRef.current = setTimeout(() => {
        root.classList.remove(TRANSITION_CLASS);
        timerRef.current = null;
      }, TRANSITION_MS);
    }
    applyTheme(next);
    setThemeState(next);
    try {
      localStorage.setItem(THEME_STORAGE_KEY, next);
    } catch {
      /* 存不住就算了，刷新后回默认 */
    }
  }, []);

  const toggleTheme = useCallback(() => {
    setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark");
  }, [setTheme]);

  // 开了两个标签页时，一边切换另一边跟着变
  useEffect(() => {
    const onStorage = (e: StorageEvent) => {
      if (e.key === THEME_STORAGE_KEY && isTheme(e.newValue)) {
        applyTheme(e.newValue);
        setThemeState(e.newValue);
      }
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  useEffect(
    () => () => {
      if (timerRef.current) clearTimeout(timerRef.current);
    },
    [],
  );

  const value = useMemo(
    () => ({ theme, setTheme, toggleTheme }),
    [theme, setTheme, toggleTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
}

export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) throw new Error("useTheme must be used inside <ThemeProvider>");
  return ctx;
}
