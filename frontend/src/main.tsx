import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import "./styles.css";
// 颜色令牌层（ADR-038）：`:root` 是浅色取值、`:root[data-theme="dark"]` 是暗色取值。
// 令牌按 `var()` 在计算值阶段解析，与样式表的先后顺序无关，但要**都在**文档里。
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
