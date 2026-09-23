/**
 * 外观主题（明/暗）的解析与应用 —— 决策与取舍见
 * `doc/decisions/040-dark-mode-token-layer.md`。
 *
 * 三句话讲完这套设计：
 *
 * 1. **偏好与外观分两层**。`ThemePreference`（`system`/`light`/`dark`）是用户的选择，
 *    存 `localStorage`；`ResolvedTheme`（`light`/`dark`）是当下真正生效的外观。
 *    「跟随系统」不是第三种颜色，而是一句「别替我决定」——所以它**不写存储**。
 * 2. **DOM 上只落解析后的结果**（`<html data-theme="light|dark">`），CSS 只需要
 *    `:root[data-theme="dark"]` 一个块。代价是「跟随系统」要靠 `matchMedia` 的
 *    change 事件来跟随，好处是样式层不必同时维护属性选择器与媒体查询两份暗色取值
 *    ——两份迟早会漂移。
 * 3. **首屏不闪白**由 `index.html` 里那段同步脚本负责（它在样式表之前跑），
 *    本模块只负责首屏之后的状态与切换。
 */

import { useCallback, useEffect, useState } from "react";

export type ThemePreference = "system" | "light" | "dark";
export type ResolvedTheme = "light" | "dark";

export const THEME_STORAGE_KEY = "macp-theme";

export const THEME_MEDIA_QUERY = "(prefers-color-scheme: dark)";

export const THEME_ORDER: readonly ThemePreference[] = ["system", "dark", "light"];
/** 切换顺序。从「跟随系统」出发的第一击落在**深色**：绝大多数环境下系统是浅色，
 * 而用户点这个按钮十有八九是要暗色。按钮的 `title` 会写明下一击去哪里，
 * 所以循环顺序不需要靠猜。 */

export const THEME_LABEL: Record<ThemePreference, string> = {
  system: "跟随系统",
  dark: "深色",
  light: "浅色",
};

export const THEME_NEXT_LABEL: Record<ThemePreference, string> = {
  system: "深色",
  dark: "浅色",
  light: "跟随系统",
};

/** 把任意来源的值（localStorage、旧版本、手改）收敛成合法偏好。 */
export function normalizePreference(value: unknown): ThemePreference {
  return value === "light" || value === "dark" ? value : "system";
}

/** 偏好 + 系统外观 → 真正生效的外观。 */
export function resolveTheme(
  preference: ThemePreference,
  prefersDark: boolean,
): ResolvedTheme {
  if (preference === "dark") return "dark";
  if (preference === "light") return "light";
  return prefersDark ? "dark" : "light";
}

/** 循环里的下一个偏好。 */
export function nextPreference(preference: ThemePreference): ThemePreference {
  const index = THEME_ORDER.indexOf(preference);
  return THEME_ORDER[(index + 1) % THEME_ORDER.length];
}

/** 按钮提示：说清现状与下一击的结果，省掉一次试错。 */
export function themeButtonTitle(
  preference: ThemePreference,
  resolved: ResolvedTheme,
): string {
  const now =
    preference === "system"
      ? `跟随系统（当前${THEME_LABEL[resolved]}）`
      : THEME_LABEL[preference];
  return `外观：${now}，点击切换到${THEME_NEXT_LABEL[preference]}`;
}

/** 读系统外观；没有 `matchMedia`（极老的宿主或测试替身）时按浅色处理。 */
export function systemPrefersDark(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return window.matchMedia(THEME_MEDIA_QUERY).matches;
}

/** 读持久化的偏好；存储被禁/被清 → 回到「跟随系统」，而不是替用户挑一个。 */
export function readStoredPreference(): ThemePreference {
  if (typeof window === "undefined") return "system";
  try {
    return normalizePreference(window.localStorage.getItem(THEME_STORAGE_KEY));
  } catch {
    return "system";
  }
}

/** 落盘：`system` 是删除键而不是写 "system"，这样「没选过」与「选了跟随系统」在
 * 存储层是同一种状态，不需要给未来留一个要迁移的旧值。 */
export function persistPreference(preference: ThemePreference): void {
  if (typeof window === "undefined") return;
  try {
    if (preference === "system") {
      window.localStorage.removeItem(THEME_STORAGE_KEY);
    } else {
      window.localStorage.setItem(THEME_STORAGE_KEY, preference);
    }
  } catch {
    /* 存储不可用不影响本次会话的外观，只是下次打开回到跟随系统 */
  }
}

/** 把解析后的外观写到 `<html data-theme>`，顺带告诉浏览器原生控件用哪套配色。 */
export function applyResolvedTheme(theme: ResolvedTheme): void {
  if (typeof document === "undefined") return;
  const root = document.documentElement;
  root.dataset.theme = theme;
  root.style.colorScheme = theme;
  // 移动端浏览器地址栏取的是 `theme-color`：不跟着换，深色页面顶上会留一条浅色。
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? DARK_CHROME : LIGHT_CHROME);
}

const LIGHT_CHROME = "#f7f9fa";
const DARK_CHROME = "#1c2022";
/** 与 `theme.css` 里 `--t-bg-f7f9fa` / `--t-bg-14232d` 的取值保持一致；
 * `index.html` 的首屏脚本里是同一对常量（那段不能 import，只能重复一次）。 */

/**
 * 主题状态：偏好、生效外观、循环切换。
 *
 * 「跟随系统」期间会订阅系统外观变化——用户在系统里换了深色，这里要跟着变；
 * 显式选了明/暗之后订阅仍然在，但不参与解析（`resolveTheme` 直接返回偏好）。
 */
export function useTheme(): {
  preference: ThemePreference;
  resolved: ResolvedTheme;
  cycle: () => void;
  setPreference: (next: ThemePreference) => void;
} {
  const [preference, setPreference] = useState<ThemePreference>(readStoredPreference);
  const [prefersDark, setPrefersDark] = useState<boolean>(systemPrefersDark);
  const resolved = resolveTheme(preference, prefersDark);

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const query = window.matchMedia(THEME_MEDIA_QUERY);
    const onChange = (event: MediaQueryListEvent) => setPrefersDark(event.matches);
    query.addEventListener("change", onChange);
    // 首屏脚本与本钩子之间系统可能已经变过，挂载时对齐一次。
    setPrefersDark(query.matches);
    return () => query.removeEventListener("change", onChange);
  }, []);

  useEffect(() => {
    applyResolvedTheme(resolved);
    persistPreference(preference);
  }, [preference, resolved]);

  const cycle = useCallback(() => {
    setPreference((current) => nextPreference(current));
  }, []);

  return { preference, resolved, cycle, setPreference };
}
