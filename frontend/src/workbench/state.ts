import type { Message, SessionEvent, Snapshot } from "./types";

export type Action =
  | { type: "snapshot"; value: Snapshot }
  | { type: "event"; value: SessionEvent }
  | { type: "events"; value: SessionEvent[] }
  | { type: "history"; value: Message[] };

function upsert<T extends { id: string }>(items: T[], value: T): T[] {
  return items.some((item) => item.id === value.id)
    ? items.map((item) => (item.id === value.id ? value : item))
    : [...items, value];
}

export function reducer(
  state: Snapshot | null,
  action: Action,
): Snapshot | null {
  if (action.type === "events")
    return action.value.reduce<Snapshot | null>(
      (current, event) => reducer(current, { type: "event", value: event }),
      state,
    );
  if (action.type === "snapshot") {
    return state?.session.id === action.value.session.id &&
      state.event_cursor > action.value.event_cursor
      ? state
      : action.value;
  }
  if (!state) return state;
  if (action.type === "history") {
    const messages = [...action.value, ...state.messages];
    return {
      ...state,
      messages: messages.filter(
        (message, index) =>
          messages.findIndex((item) => item.id === message.id) === index,
      ),
    };
  }
  const event = action.value;
  if (event.seq <= state.event_cursor || event.session_id !== state.session.id)
    return state;
  const next = { ...state, event_cursor: event.seq };
  const root = !state.runs.find((run) => run.id === event.run_id)?.parent_id;
  switch (event.type) {
    case "run.started":
      return {
        ...next,
        model_calls: Object.fromEntries(
          Object.entries(state.model_calls).map(([key, call]) => [
            key,
            call.run_id === event.run_id && call.status === "running"
              ? { ...call, status: "interrupted" }
              : call,
          ]),
        ),
        streams: Object.fromEntries(
          Object.entries(state.streams).filter(
            ([, stream]) => stream.run_id !== event.run_id,
          ),
        ),
      };
    case "session.created":
    case "session.updated":
      return { ...next, session: event.data.session };
    case "run.updated":
      return { ...next, runs: upsert(state.runs, event.data.run) };
    case "message.completed": {
      if (!root) return next;
      const message = event.data.message;
      const streams = { ...state.streams };
      delete streams[message.id];
      return { ...next, messages: upsert(state.messages, message), streams };
    }
    case "model.started": {
      const data = event.data;
      next.model_calls = {
        ...state.model_calls,
        [data.stream_id]: {
          id: data.stream_id,
          run_id: event.run_id,
          purpose: data.purpose,
          request_file: data.request_file,
          response_file: null,
          parent_tool_call_id: data.parent_tool_call_id,
          model: data.model,
          reasoning_effort: data.reasoning_effort,
          started_at: data.created_at,
          finished_at: null,
          duration_ms: null,
          first_text_ms: null,
          status: "running",
          error: null,
          provider_request_id: null,
          finish_reason: null,
          usage: {
            input_tokens: 0,
            output_tokens: 0,
            estimated_input_tokens: 0,
            estimated_output_tokens: 0,
            reasoning_tokens: 0,
            cached_input_tokens: 0,
            model_calls: 0,
            tool_calls: 0,
            cost_cny: 0,
            estimated: false,
            active_seconds: 0,
            waiting_seconds: 0,
          },
        },
      };
      if (!root || !event.data.publish) return next;
      const id = event.data.stream_id;
      return {
        ...next,
        streams: {
          ...state.streams,
          [id]: {
            id,
            run_id: event.run_id,
            content: "",
            tool_calls: [],
            created_at: event.data.created_at,
          },
        },
      };
    }
    case "message.delta": {
      const id = event.data.stream_id,
        stream = state.streams[id];
      if (!stream) return next;
      return {
        ...next,
        streams: {
          ...state.streams,
          [id]: { ...stream, content: stream.content + event.data.text },
        },
      };
    }
    case "model.finished": {
      const data = event.data,
        call = state.model_calls?.[data.stream_id];
      if (call)
        next.model_calls = {
          ...state.model_calls,
          [data.stream_id]: {
            ...call,
            finished_at: event.created_at,
            duration_ms: data.duration_ms,
            first_text_ms: data.first_text_ms,
            status: data.completed ? "completed" : "failed",
            usage: data.usage,
            response_file: data.response_file,
            error: data.error,
            provider_request_id: data.provider_request_id,
            finish_reason: data.finish_reason,
          },
        };
      if (event.data.completed) return next;
      const streams = { ...state.streams };
      delete streams[event.data.stream_id];
      return { ...next, streams };
    }
    case "task.updated":
      return {
        ...next,
        session: { ...state.session, brief: event.data.brief },
      };
    case "document.updated":
      return {
        ...next,
        documents: upsert(state.documents, event.data.document),
      };
    case "artifact.created":
      return {
        ...next,
        artifacts: upsert(state.artifacts, event.data.artifact),
      };
    case "evidence.created":
      return { ...next, evidence: upsert(state.evidence, event.data.evidence) };
    case "tool.queued":
    case "tool.started":
    case "tool.completed":
    case "tool.yielded":
    case "tool.resolved": {
      const execution = event.data.execution;
      const streams = { ...state.streams };
      if (event.type === "tool.queued" && execution.model_stream_id) {
        const stream = streams[execution.model_stream_id];
        if (stream)
          streams[execution.model_stream_id] = {
            ...stream,
            tool_calls: [
              ...stream.tool_calls.filter(
                (call) => call.id !== execution.call.id,
              ),
              execution.call,
            ],
          };
      }
      return {
        ...next,
        streams,
        tools: [
          ...state.tools.filter(
            (item) =>
              !(item.run_id === execution.run_id && item.id === execution.id),
          ),
          execution,
        ],
      };
    }
    case "context.compacted":
    case "context.compaction_failed":
    case "memory.updated":
    case "model.retry":
    case "orchestration.planned":
    case "skill.used":
    case "memory.recalled":
    case "budget.pressure":
    case "paper.resolved":
    case "answer.citations_checked":
      return next;
    case "retrieval.candidates":
      return root ? { ...next, retrieval_progress: event.data } : next;
    case "retrieval.batch":
      return next;
    case "retrieval.completed":
      return root
        ? { ...next, retrieval: event.data.result, retrieval_progress: null }
        : next;
    case "research.updated":
      return { ...next, research: event.data.research };
    default: {
      const unhandled: never = event;
      throw new Error(`Unhandled event: ${JSON.stringify(unhandled)}`);
    }
  }
}
