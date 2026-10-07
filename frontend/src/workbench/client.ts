import type { components } from "../generated/api";
import type {
  Artifact,
  Configuration,
  Document,
  Memory,
  Message,
  Paper,
  Run,
  Session,
  SessionEvent,
  Snapshot,
  ToolCatalog,
  ToolResult,
} from "./types";

export class ApiError extends Error {
  constructor(
    public readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`/api${path}`, init);
  if (!response.ok) {
    const body = await response
      .json()
      .catch(() => ({ detail: response.statusText }));
    throw new ApiError(
      response.status,
      typeof body.detail === "string"
        ? body.detail
        : JSON.stringify(body.detail),
    );
  }
  return response.json() as Promise<T>;
}

const json = (method: string, value: unknown): RequestInit => ({
  method,
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(value),
});

export const api = {
  config: () => request<Configuration>("/config"),
  refreshModels: () =>
    request<Configuration["models"]>("/config/models/refresh", {
      method: "POST",
    }),
  sessions: () => request<Session[]>("/sessions"),
  create: (effort: Session["effort"]) =>
    request<Session>("/sessions", json("POST", { effort })),
  snapshot: (id: string) => request<Snapshot>(`/sessions/${id}`),
  messages: (id: string, before: string) =>
    request<Message[]>(
      `/sessions/${id}/messages?before=${encodeURIComponent(before)}`,
    ),
  send: (id: string, value: components["schemas"]["SendMessage"]) =>
    request<Message>(`/sessions/${id}/messages`, json("POST", value)),
  cancel: (id: string) =>
    request<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  resume: (id: string, extend = false) =>
    request<Run>(`/runs/${id}/resume`, json("POST", { extend_budget: extend })),
  resolve: (id: string, call: string, result: ToolResult) =>
    request(`/runs/${id}/tools/${call}/resolve`, json("POST", result)),
  webSource: (id: string) =>
    request<components["schemas"]["SourceSnapshotView"]>(`/sources/${id}`),
  papers: () => request<Paper[]>("/papers"),
  reports: () => request<Artifact[]>("/reports"),
  createMemory: (key: string, content: string) =>
    request<Memory>(
      "/memories",
      json("POST", { key, content, kind: "research" }),
    ),
  reindexMemory: (id: string) =>
    request(`/memories/${id}/reindex`, { method: "POST" }),
  cancelDocument: (id: string) =>
    request<Document>(`/documents/${id}/cancel`, { method: "POST" }),
  retryDocument: (id: string) =>
    request<Document>(`/documents/${id}/retry`, { method: "POST" }),
  document: (id: string) => request<Document>(`/documents/${id}`),
  memories: (session?: string) =>
    request<Memory[]>(`/memories${session ? `?session_id=${session}` : ""}`),
  editMemory: (id: string, content: string) =>
    request<Memory>(`/memories/${id}`, json("PATCH", { content })),
  deleteMemory: (id: string) =>
    request(`/memories/${id}`, { method: "DELETE" }),
  tools: () => request<ToolCatalog>("/tools"),
  async upload(session: string, file: File) {
    const form = new FormData();
    form.set("session_id", session);
    form.set("file", file);
    return request<Document>("/documents", { method: "POST", body: form });
  },
  async artifact(id: string) {
    const response = await fetch(`/api/artifacts/${id}/content`);
    if (!response.ok) throw new Error("无法读取报告");
    return response.text();
  },
};

export function subscribe(
  session: string,
  after: number,
  onEvent: (event: SessionEvent) => void,
  onConnection: (connected: boolean) => void,
) {
  const source = new EventSource(
    `/api/sessions/${session}/events?after=${after}`,
  );
  source.onopen = () => onConnection(true);
  source.onerror = () => onConnection(false);
  source.addEventListener("update", (event) => {
    try {
      onEvent(JSON.parse((event as MessageEvent).data) as SessionEvent);
    } catch {
      onConnection(false);
    }
  });
  return () => source.close();
}
