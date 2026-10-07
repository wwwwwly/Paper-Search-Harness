import { Markdown } from "../../components/Markdown";
import { useEffect, useRef, useState } from "react";
import {
  ArrowDown,
  ArrowUp,
  ChevronDown,
  LoaderCircle,
  Paperclip,
  Square,
} from "lucide-react";
import type {
  Evidence,
  Configuration,
  Effort,
  Message,
  Snapshot,
  ToolExecution,
} from "../../workbench/types";

import { effortDescriptions, effortNames } from "../../workbench/types";
import { ExecutionTrace } from "./ExecutionTrace";

const phaseNames = {
  queued: "排队",
  research: "研究",
  delivery: "交付",
  waiting: "等待",
  finished: "结束",
} as const;

function ToolActivity({
  tool,
  canResolve,
  onResolve,
}: {
  tool: ToolExecution;
  canResolve: boolean;
  onResolve: (tool: ToolExecution) => void;
}) {
  return (
    <details
      className={`tool-card ${tool.result?.is_error || tool.error ? "tool-error" : ""}`}
    >
      <summary>
        {["queued", "executing"].includes(tool.state) && !tool.error ? (
          <LoaderCircle size={14} className="spin" />
        ) : (
          <span className="tool-dot" />
        )}
        <span>{tool.call.name}</span>
        <small>
          {["completed", "yielded"].includes(tool.state)
            ? tool.result?.is_error
              ? "调用失败"
              : "已完成"
            : tool.error
              ? "已中断"
              : "执行记录"}
          {tool.duration_ms != null &&
            ` · ${(tool.duration_ms / 1000).toFixed(1)} 秒`}
        </small>
        <ChevronDown size={14} />
      </summary>
      <pre>{JSON.stringify(tool.call.arguments, null, 2)}</pre>
      {tool.result && <pre>{tool.result.content}</pre>}
      {tool.error && (
        <p className="inline-error">
          {tool.error.code} · {tool.error.message}
        </p>
      )}
      {!["completed", "yielded"].includes(tool.state) && canResolve && (
        <button className="subtle" onClick={() => onResolve(tool)}>
          确认中断调用的结果
        </button>
      )}
    </details>
  );
}

export function Conversation({
  snapshot,
  connected,
  sending,
  onSend,
  config,
  effort,
  onEffort,
  onCancel,
  onResume,
  onUpload,
  onEvidence,
  onResolve,
  onHistory,
}: {
  snapshot: Snapshot | null;
  config: Configuration | null;
  effort: Effort;
  onEffort: (value: Effort) => void;
  connected: boolean;
  sending: boolean;
  onSend: (text: string) => Promise<void>;
  onCancel: (id: string) => void;
  onResume: (id: string, extend: boolean) => void;
  onUpload: (file: File) => void;
  onEvidence: (evidence: Evidence) => void;
  onResolve: (tool: ToolExecution) => void;
  onHistory: () => void;
}) {
  const [text, setText] = useState("");
  const [follow, setFollow] = useState(true);
  const bottom = useRef<HTMLDivElement>(null),
    scroll = useRef<HTMLDivElement>(null),
    file = useRef<HTMLInputElement>(null);
  const runs = snapshot?.runs.filter((run) => !run.parent_id) ?? [];
  const eligible = runs.filter(
    (run) => !run.deadline_at || Date.parse(run.deadline_at) > Date.now(),
  );
  const active =
    eligible.find((run) => run.status === "running") ??
    eligible.find((run) => run.status === "waiting") ??
    eligible.find((run) => run.status === "queued");
  const last = [...runs].sort((a, b) =>
    b.created_at.localeCompare(a.created_at),
  )[0];
  const displayed = active ?? last;
  const researchTasks =
    snapshot?.research.tasks.filter(
      (task) => task.root_run_id === displayed?.id,
    ) ?? [];
  const messages: Message[] = (
    snapshot?.messages.filter((message) => message.role !== "tool") ?? []
  )
    .slice()
    .sort((a, b) => a.created_at.localeCompare(b.created_at));
  const streams = Object.values(snapshot?.streams ?? {});
  const effortConfig = config?.efforts.find((item) => item.effort === effort);
  useEffect(() => {
    if (follow)
      bottom.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [snapshot?.event_cursor, follow]);
  useEffect(() => {
    setText("");
    setFollow(true);
  }, [snapshot?.session.id]);
  async function send() {
    if (!text.trim() || sending) return;
    const current = text;
    setText("");
    try {
      await onSend(current);
      setFollow(true);
    } catch {
      setText(current);
    }
  }
  const examples = [
    "帮我梳理 Agent 长期记忆的代表工作，并比较各自的评测方式",
    "找近期关于检索增强生成的论文，重点关注检索质量与证据可靠性",
    "我想比较几篇论文的方法，先帮我明确研究问题",
  ];
  return (
    <section className="conversation">
      <header className="conversation-header">
        <div>
          <span className="eyebrow">RESEARCH SESSION</span>
          <h2>{snapshot?.session.title || "开始一项新研究"}</h2>
        </div>
        <span className={`connection ${connected ? "" : "offline"}`}>
          <i />
          {snapshot ? (connected ? "已连接" : "正在连接") : "准备就绪"}
        </span>
      </header>
      <div
        className="messages"
        ref={scroll}
        onScroll={() => {
          if (scroll.current)
            setFollow(
              scroll.current.scrollHeight -
                scroll.current.scrollTop -
                scroll.current.clientHeight <
                120,
            );
        }}
      >
        {!messages.length && (
          <div className="welcome">
            <span className="welcome-symbol">
              P<span>∴</span>
            </span>
            <p className="eyebrow">FROM QUESTIONS TO EVIDENCE</p>
            <h1>把一个问题，研究清楚。</h1>
            <p>
              发现论文、阅读全文、比较证据。
              <br />
              在同一个会话中，持续推进你的研究。
            </p>
            <div className="suggestions">
              {examples.map((example) => (
                <button key={example} onClick={() => setText(example)}>
                  {example}
                  <ArrowUp size={16} />
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.length >= 100 && (
          <button className="history-button" onClick={onHistory}>
            加载更早的消息
          </button>
        )}
        {messages.map((message) => (
          <article className={`message ${message.role}`} key={message.id}>
            <div className="message-label">
              {message.role === "user" ? "你" : "Paper Search"}
            </div>
            {message.content && (
              <div className="markdown">
                <Markdown
                  text={message.content}
                  evidence={snapshot?.evidence ?? []}
                  onEvidence={onEvidence}
                />
              </div>
            )}
            {!!message.document_ids?.length && (
              <div className="attachment-note">
                附加 {message.document_ids.length} 份文档
              </div>
            )}
            {message.tool_calls?.map((call) => {
              const tool = snapshot?.tools.find(
                (item) => item.run_id === message.run_id && item.id === call.id,
              );
              return tool ? (
                <ToolActivity
                  key={call.id}
                  tool={tool}
                  canResolve={["waiting", "failed", "interrupted"].includes(
                    snapshot?.runs.find((run) => run.id === tool.run_id)
                      ?.status ?? "",
                  )}
                  onResolve={onResolve}
                />
              ) : (
                <div className="tool-pending" key={call.id}>
                  {call.name} · 等待执行
                </div>
              );
            })}
          </article>
        ))}
        {streams.map((stream) => (
          <article className="message assistant" key={stream.id}>
            <div className="message-label">
              Paper Search <LoaderCircle size={12} className="spin" />
            </div>
            <div className="markdown">
              <Markdown
                text={stream.content || "正在研究…"}
                evidence={snapshot?.evidence ?? []}
                onEvidence={onEvidence}
              />
            </div>
            {stream.tool_calls.map((call) => {
              const tool = snapshot?.tools.find(
                (item) => item.run_id === stream.run_id && item.id === call.id,
              );
              return tool ? (
                <ToolActivity
                  key={call.id}
                  tool={tool}
                  canResolve={false}
                  onResolve={onResolve}
                />
              ) : null;
            })}
          </article>
        ))}
        {active && streams.length === 0 && (
          <div className="working">
            <LoaderCircle size={15} className="spin" />
            {active.status === "queued"
              ? "任务已排队"
              : active.status === "waiting"
                ? active.reason
                : "正在处理工具与研究资料"}
          </div>
        )}
        {last && ["failed", "waiting", "interrupted"].includes(last.status) && (
          <div className="run-notice">
            <strong>
              {last.status === "waiting" ? "研究已暂停" : "执行需要接续"}
            </strong>
            <p>{last.reason}</p>
            {!last.waiting_document_ids.length && (
              <>
                {!last.reason?.includes("预算") && (
                  <button
                    className="subtle"
                    onClick={() => onResume(last.id, false)}
                  >
                    从已保存记录继续
                  </button>
                )}
                <button
                  className="subtle"
                  onClick={() => onResume(last.id, true)}
                >
                  增加一轮预算并继续
                </button>
              </>
            )}
          </div>
        )}
        {snapshot?.session.brief.objective && (
          <details className="research-progress">
            <summary>当前研究理解</summary>
            <p>{snapshot.session.brief.objective}</p>
            <p>
              交付：{snapshot.session.brief.delivery.format} ·{" "}
              {snapshot.session.brief.delivery.count
                ? `${snapshot.session.brief.delivery.count_mode === "exact" ? "要求" : "最多"} ${snapshot.session.brief.delivery.count} 篇`
                : "最多 5 篇"}
            </p>
            {!!snapshot.session.brief.constraints.length && (
              <p>要求：{snapshot.session.brief.constraints.join("；")}</p>
            )}
            {!!snapshot.session.brief.assumptions.length && (
              <p>暂定理解：{snapshot.session.brief.assumptions.join("；")}</p>
            )}
          </details>
        )}
        {snapshot &&
          (researchTasks.length > 0 ||
            snapshot.research.questions.length > 0) && (
            <details className="research-progress">
              <summary>
                研究任务与缺口 · 依据版本{" "}
                {snapshot.session.brief.basis_revision}
              </summary>
              {researchTasks.map((task) => (
                <div className="candidate-card" key={task.id}>
                  <strong>
                    {task.assignment.kind} · {task.assignment.goal}
                  </strong>
                  <small>
                    {task.status} · 分工版本 {task.task_revision} · {task.id}
                  </small>
                  {!!task.assignment.depends_on.length && (
                    <small>依赖：{task.assignment.depends_on.join("、")}</small>
                  )}
                  {task.reason && <p>{task.reason}</p>}
                  <small>
                    发现{" "}
                    {
                      snapshot.research.findings.filter(
                        (item) => item.run_id === task.id,
                      ).length
                    }{" "}
                    条
                  </small>
                </div>
              ))}
              {snapshot.research.questions.map((item) => (
                <p key={item.id}>
                  {item.status} · {item.text} {item.reason}
                </p>
              ))}
            </details>
          )}
        {snapshot?.research.result && (
          <details className="research-progress">
            <summary>
              已确认选择 · {snapshot.research.result.status} · 交付版本{" "}
              {snapshot.research.result.result_revision}
            </summary>
            {snapshot.research.result.selected.map((item) => (
              <div className="candidate-card" key={item.paper_id}>
                <a
                  href={item.url || undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  {item.title}
                </a>
                <p>{item.assessment.reason}</p>
                <small>依据：{item.assessment.evidence_ids.join("、")}</small>
                {item.assessment.quality_signals.map((signal, index) => (
                  <p key={index}>
                    {signal.kind} · {signal.value}
                  </p>
                ))}
                {item.assessment.limitations.map((text, index) => (
                  <small key={index}>局限：{text}</small>
                ))}
              </div>
            ))}
            {snapshot.research.result.gaps.map((gap, index) => (
              <p key={index}>{gap}</p>
            ))}
          </details>
        )}
        {snapshot?.retrieval && (
          <details className="research-progress">
            <summary>
              研究进展 · {snapshot.retrieval.candidate_count} 篇候选 · 来源排序
            </summary>
            <p>
              新增 {snapshot.retrieval.new_candidates} 篇 · 来源查询{" "}
              {(snapshot.retrieval.retrieval_ms / 1000).toFixed(1)} 秒
            </p>
            <p>
              尚待解决：
              {snapshot.session.brief.open_questions.join("；") ||
                "当前没有记录未解决问题"}
            </p>
            {snapshot.retrieval.diagnostics.map((item, index) => (
              <p
                key={index}
                className={item.status === "error" ? "inline-error" : "muted"}
              >
                {item.provider} · {item.status} · {item.count} 篇 ·{" "}
                {item.sort === "newest" ? "按发表日期" : "按相关性"} ·{" "}
                {item.date_from || "不限起始"} 至 {item.date_to || "不限结束"}{" "}
                {item.error}
              </p>
            ))}
            {snapshot.retrieval.hits.map((hit) => (
              <div className="candidate-card" key={hit.paper.canonical_id}>
                <a
                  href={hit.paper.landing_page_url || undefined}
                  target="_blank"
                  rel="noreferrer"
                >
                  {hit.paper.title}
                </a>
                <small>
                  {hit.paper.publication_date || hit.paper.year} ·{" "}
                  {hit.evidence_level} ·{" "}
                  {snapshot.session.brief.hard_constraints.excluded_ids.includes(
                    hit.paper.canonical_id,
                  )
                    ? "已排除"
                    : "候选"}
                </small>
                {!!hit.paper.affiliations.length && (
                  <small>
                    机构（来源元数据）：
                    {[
                      ...new Set(
                        hit.paper.affiliations.map((item) => item.name),
                      ),
                    ].join("、")}
                  </small>
                )}
                <p>召回融合候选；研究判断见已确认结果。</p>
              </div>
            ))}
          </details>
        )}
        {snapshot?.retrieval_progress && active?.status === "running" && (
          <p className="working">
            已召回 {snapshot.retrieval_progress.candidate_count} 篇 · 符合硬约束{" "}
            {snapshot.retrieval_progress.eligible_count} 篇
          </p>
        )}
        {snapshot && displayed && (
          <ExecutionTrace snapshot={snapshot} run={displayed} />
        )}
        {displayed && (
          <details className="research-progress">
            <summary>
              预算分解 · token 软目标{" "}
              {displayed.token_soft_target.toLocaleString()}
            </summary>
            <p>
              截止：{displayed.deadline_at || "尚未启动"} · 收尾预留{" "}
              {displayed.execution.limits.finalization_reserve_seconds} 秒 ·
              预算段 {displayed.budget_extensions + 1}
            </p>
            {snapshot?.runs
              .filter(
                (item) =>
                  item.id === displayed.id || item.parent_id === displayed.id,
              )
              .map((item) => (
                <p key={item.id}>
                  {item.parent_id ? "Worker" : "主 Agent"} · {item.id} · 自身
                  token{" "}
                  {(
                    item.own_usage.input_tokens + item.own_usage.output_tokens
                  ).toLocaleString()}{" "}
                  · 中断估算{" "}
                  {(
                    item.own_usage.estimated_input_tokens +
                    item.own_usage.estimated_output_tokens
                  ).toLocaleString()}
                </p>
              ))}
          </details>
        )}
        {displayed && (
          <p className="usage-line">
            {effortNames[displayed.execution.effort]} · 阶段{" "}
            {phaseNames[displayed.phase]} · {displayed.usage.model_calls}{" "}
            次模型调用 · {displayed.usage.tool_calls} 次工具调用 · token{" "}
            {(
              displayed.usage.input_tokens + displayed.usage.output_tokens
            ).toLocaleString()}
            {displayed.reason ? ` · ${displayed.reason}` : ""}
          </p>
        )}
        <div ref={bottom} />
      </div>
      {!follow && (
        <button className="follow-button" onClick={() => setFollow(true)}>
          <ArrowDown size={15} />
          回到最新
        </button>
      )}
      <div className="composer-container">
        <div className="effort-control">
          <label>
            思考强度{" "}
            <select
              aria-label="思考强度"
              value={effort}
              onChange={(event) => onEffort(event.target.value as Effort)}
            >
              {config?.efforts.map((item) => (
                <option key={item.effort} value={item.effort}>
                  {effortNames[item.effort]}
                </option>
              ))}
            </select>
          </label>
          <small>
            {effortDescriptions[effort]} · {effortConfig?.model.model}
          </small>
          {active && (
            <small>
              当前：{effortNames[active.execution.effort]} · 改档用于下一项研究
            </small>
          )}
        </div>
        <div className="composer">
          <textarea
            value={text}
            onChange={(event) => setText(event.target.value)}
            placeholder={
              active ? "补充要求，或调整当前研究方向…" : "描述你想研究的问题…"
            }
            aria-label="研究问题"
            onKeyDown={(event) => {
              if (
                event.key === "Enter" &&
                !event.shiftKey &&
                !event.nativeEvent.isComposing
              ) {
                event.preventDefault();
                void send();
              }
            }}
          />
          <div className="composer-actions">
            <div>
              <input
                ref={file}
                type="file"
                accept="application/pdf,.pdf"
                hidden
                onChange={(event) => {
                  const selected = event.target.files?.[0];
                  if (selected) onUpload(selected);
                  event.target.value = "";
                }}
              />
              <button
                className="icon-button"
                title="上传 PDF"
                onClick={() => file.current?.click()}
              >
                <Paperclip size={18} />
              </button>
            </div>
            <div>
              {active && (
                <button
                  className="stop-button"
                  onClick={() => onCancel(active.id)}
                >
                  <Square size={13} />
                  停止
                </button>
              )}
              <button
                className="send-button"
                disabled={!text.trim() || sending}
                onClick={() => void send()}
                aria-label="发送"
              >
                <ArrowUp size={19} />
              </button>
            </div>
          </div>
        </div>
        <p className="composer-hint">
          Enter 发送 · Shift + Enter 换行 · 重要结论请核对原文证据
        </p>
      </div>
    </section>
  );
}
