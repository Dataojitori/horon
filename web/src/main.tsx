import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// 不打包任何网络字体，全部用系统字体，原因见 index.css 里的 --font-sans。
import "./index.css";
import App from "./App";
import { ThemeProvider } from "./theme";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <App />
    </ThemeProvider>
  </StrictMode>,
);
