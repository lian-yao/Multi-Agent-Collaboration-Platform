/**
 * 渐进揭示：前端**呈现层**的「流式」。
 *
 * ## 它是什么，不是什么
 *
 * 本模块**不产生任何新数据**。后端没有事件流：助手正文是工作流走到终态时一次性
 * 落库的（`app/workflows/pipeline.py::finalize_activity` → `upsert_message`），
 * 前端靠轮询 `GET /sessions/{id}/messages` 拿到它。这里只是把**已经拿到的**正文
 * 按帧逐步显示出来。
 *
 * 所以它是**呈现效果**，不是传输协议。不要据此判断「前端已经在流式接收 token」——
 * 真要逐 token 流式，得先在后端开只读事件流端点，那时本模块应当换成消费端。
 * 这条边界写在这里是为了避免下一次有人把「看起来在流式」当成「已经在流式」。
 *
 * ## 为什么按总时长反推步长，而不是每帧固定几个字
 *
 * 报告正文动辄数千字。按固定步长推进会让一篇长文打十几秒，读的人以为页面卡住了。
 * 这里按**总时长**反推每帧的步长：短消息有逐字感，长报告也不会拖到让人等。
 *
 * ## 内容变长时必须从断点接着打
 *
 * 运行中的阶段产出先只有一小段，轮询回来才是全文。若每次重头再来，界面看起来
 * 像一直在重试。因此这里记录已显示的字符数 `shown`，文本变长时从 `shown` 继续。
 *
 * ## 两种降级为「立即全文」的情况
 *
 * 1. 非浏览器环境（`renderToStaticMarkup` 离屏冒烟、SSR）——那里根本不跑 effect，
 *    若初始值是 0 就会渲染出空正文，冒烟断言与首帧都会拿到空白。
 * 2. 用户声明 `prefers-reduced-motion: reduce`。
 */
import { useEffect, useRef, useState } from "react";

const MIN_DURATION_MS = 320;
const MAX_DURATION_MS = 1800;
/** 每个字符折算的毫秒数；越短的消息越有「一个字一个字出来」的观感。 */
const MS_PER_CHAR = 8;

function canAnimate(): boolean {
  if (typeof window === "undefined") return false;
  if (typeof window.matchMedia !== "function") return true;
  return !window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

export type StreamText = {
  /** 当前应显示的正文（`text` 的前缀，或在结束/降级时就是 `text` 本身）。 */
  visible: string;
  /** 是否仍在推进；调用方据此决定要不要画光标。 */
  streaming: boolean;
};

/**
 * @param text    要逐字显示的正文。
 * @param enabled 是否播放。历史消息应传 `false`——整屏重放会让人以为任务在重新执行。
 */
export function useStreamText(text: string, enabled: boolean): StreamText {
  const animate = enabled && canAnimate();
  // 惰性初值：不播放时直接就是全文，避免首帧闪一下空白天。
  const [shown, setShown] = useState(() => (animate ? 0 : text.length));

  // 用 ref 记住已显示长度：文本变长时要从中断处继续，而被中断的位置只存在于上一次渲染里。
  // 这个 effect 声明在推进 effect **之前**，同一批提交里它先跑，所以推进 effect 读到的
  // 一定是本次渲染对应的值。
  const shownRef = useRef(shown);
  useEffect(() => {
    shownRef.current = shown;
  }, [shown]);

  useEffect(() => {
    if (!animate) {
      if (shownRef.current !== text.length) setShown(text.length);
      return;
    }
    const from = Math.min(shownRef.current, text.length);
    if (from >= text.length) return;
    const remaining = text.length - from;
    const duration = Math.min(
      MAX_DURATION_MS,
      Math.max(MIN_DURATION_MS, remaining * MS_PER_CHAR),
    );
    const startedAt = performance.now();
    let handle = requestAnimationFrame(function tick(now) {
      const ratio = (now - startedAt) / duration;
      // 至少推进一个字符：否则极长的文本在 ratio 精度耗尽后会停在中途。
      const next = Math.min(
        text.length,
        from + Math.max(1, Math.ceil(remaining * ratio)),
      );
      setShown(next);
      if (next < text.length) handle = requestAnimationFrame(tick);
    });
    return () => cancelAnimationFrame(handle);
  }, [text, animate]);

  return {
    visible: shown >= text.length ? text : text.slice(0, shown),
    streaming: animate && shown < text.length,
  };
}
