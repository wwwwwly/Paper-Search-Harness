import { useEffect, useState } from "react";
import type { Snapshot, Run } from "../../workbench/types";

export function ExecutionTrace({
  snapshot,
  run,
}: {
  snapshot: Snapshot;
  run: Run;
}) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    if (run.status !== "running") return;
    const timer = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(timer);
  }, [run.status]);
  const calls = Object.values(snapshot.model_calls).filter(
    (call) => call.run_id === run.id,
  );
  const toolNames = new Map(
    snapshot.tools.map((tool) => [`${tool.run_id}:${tool.id}`, tool.call.name]),
  );
  const running = calls.filter(
    (call) => call.status === "running" && run.status === "running",
  );
  const purpose = (name: string) =>
    ({
      agent: "主 Agent",
      summary: "上下文压缩",
      planning: "研究规划（历史）",
      delivery_repair: "交付修复（历史）",
      WorkingSummary: "上下文压缩（历史）",
      unclassified: "历史调用（未标注用途）",
    })[name] || name;
  return (
    <details className="research-progress execution-trace">
      <summary>
        运行记录 · {calls.length} 次模型请求
        {running.length
          ? ` · ${running.map((call) => purpose(call.purpose)).join("、")}执行中`
          : ""}
      </summary>
      <p className="muted">
        工具耗时包含其内部模型调用；并行请求的耗时不能直接相加。
      </p>
      {!!run.skills.length && (
        <p className="muted">
          已加载 Skill：
          {run.skills.map((item) => `${item.name}@${item.version}`).join("、")}
        </p>
      )}
      {calls.map((call) => (
        <div
          className={`trace-row ${call.error ? "inline-error" : ""}`}
          key={call.id}
        >
          <div>
            <strong>{purpose(call.purpose)}</strong> ·{" "}
            {call.reasoning_effort || "未记录"} ·{" "}
            {call.duration_ms !== null ||
            call.finished_at ||
            (call.status === "running" && run.status === "running")
              ? `${(
                  (call.duration_ms ??
                    (call.finished_at ? Date.parse(call.finished_at) : now) -
                      Date.parse(call.started_at)) / 1000
                ).toFixed(1)} 秒`
              : "耗时未记录"}{" "}
            ·{" "}
            {call.status === "running"
              ? run.status === "running"
                ? "执行中"
                : "中断，未收到结束记录"
              : call.status === "completed"
                ? "完成"
                : "未完成"}
          </div>
          <small>
            {call.parent_tool_call_id
              ? `所属工具：${toolNames.get(`${run.id}:${call.parent_tool_call_id}`) || call.parent_tool_call_id} · `
              : ""}
            {call.model}
          </small>
          <small>
            <a
              href={`/api/runs/${run.id}/model-calls/${call.id}/request`}
              target="_blank"
              rel="noreferrer"
            >
              实际模型输入
            </a>
            {call.response_file && (
              <>
                {" "}
                ·{" "}
                <a
                  href={`/api/runs/${run.id}/model-calls/${call.id}/response`}
                  target="_blank"
                  rel="noreferrer"
                >
                  原始文本输出
                </a>
              </>
            )}
          </small>
          {call.finished_at && (
            <div className="muted">
              {call.usage.estimated ? "估算" : "供应商返回"} token：输入{" "}
              {call.usage.input_tokens} / 输出 {call.usage.output_tokens}
              （含推理 {call.usage.reasoning_tokens}，缓存输入{" "}
              {call.usage.cached_input_tokens}）
            </div>
          )}
          {call.error && (
            <p>
              {call.error.code} · {call.error.message}
            </p>
          )}
        </div>
      ))}
      <a href={`/api/runs/${run.id}/trace`} target="_blank" rel="noreferrer">
        打开完整诊断 JSON
      </a>
    </details>
  );
}
