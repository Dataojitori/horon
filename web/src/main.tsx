import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
// 只有字标用打包的字体（Instrument Serif，SIL OFL 1.1，随包打进去，离线、内网部署也一致）。
// 正文和代码用系统字体，原因见 index.css 里的 --font-sans。
import "@fontsource/instrument-serif";
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
