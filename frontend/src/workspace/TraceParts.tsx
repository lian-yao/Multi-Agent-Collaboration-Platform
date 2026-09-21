/**
 * 执行轨迹的共用片段。
 *
 * 「一次工具调用」这一段在**三个**地方要长成一模一样：
 * 执行台的单 Agent 弹窗（`AgentStageModal`）、对话流内联的活动卡片
 * （`App.tsx` → `RunActivity`）、以及协作画布连线上的工具链。
 * 三处各写一份必然漂移——最典型的漂移是「弹窗标了『已截断』，对话流把预览当成全部」，
 * 读的人据此得出错误结论。所以这里只留一份实现。
 *
 * 内层「入参与出参」用原生 `<details>`：它是**每个工具各自独立**的开关，用户想开哪个
 * 开哪个，没有「整体展开/收起」的诉求，因此不需要受控（对比 `components/Disclosure.tsx`——
 * 那里的开关状态必须由调用方掌控，才用了受控组件）。
 *
 * 入参 / 出参的可读文案与截断判断都在 `collaboration.ts`：那是画布也在用的同一套，
 * 不在这里另写一份。
 */
import { Wrench } from "lucide-react";
import { toolCallStatusText } from "../components/Status";
import { isTruncated, payloadText } from "./collaboration";
import type { StageToolCall } from "../types/api";

export function ToolCallBlock({ call }: { call: StageToolCall }) {
  const tone =
    call.status === "succeeded" ? "green" : call.status === "failed" ? "rose" : "amber";
  return (
    <article className={`ws-trace-step ${call.status}`}>
      <div className="ws-trace-step-head">
        <span className={`ws-trace-mark ${tone}`}>
          <Wrench size={12} />
        </span>
        <b>{call.tool_name || "未命名工具"}</b>
        <span className="ws-trace-chip">
          {toolCallStatusText[call.status] ?? call.status}
        </span>
      </div>
      {call.error && (
        <p className="ws-trace-error" role="alert">
          {call.error}
        </p>
      )}
      <details className="ws-trace-io">
        <summary>入参与出参</summary>
        <dl>
          <div>
            <dt>入参</dt>
            <dd>
              <pre>{payloadText(call.input)}</pre>
            </dd>
          </div>
          <div>
            <dt>出参{isTruncated(call.output) && "（已截断）"}</dt>
            <dd>
              <pre>{payloadText(call.output)}</pre>
            </dd>
          </div>
        </dl>
      </details>
    </article>
  );
}
