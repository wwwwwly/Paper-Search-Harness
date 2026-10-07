import type { components, paths } from "../generated/api";

export type Session = components["schemas"]["Session"];
export type Run = components["schemas"]["Run"];
export type Message = components["schemas"]["Message"];
export type SessionEvent =
  paths["/api/sessions/{session_id}/event-history"]["get"]["responses"][200]["content"]["application/json"][number];
export type Snapshot = components["schemas"]["SessionSnapshot"];
export type Document = components["schemas"]["Document"];
export type Evidence = components["schemas"]["Evidence"];
export type Artifact = components["schemas"]["Artifact"];
export type Paper = components["schemas"]["Paper"];
export type Memory = components["schemas"]["Memory"];
export type ToolExecution = components["schemas"]["ToolExecution"];
export type ToolResult = components["schemas"]["ToolResult-Output"];
export type TaskBrief = components["schemas"]["TaskBrief"];
export type StreamingMessage = components["schemas"]["StreamingMessage"];

export type Configuration = components["schemas"]["Configuration"];
export type ToolInfo = components["schemas"]["ToolInfo"];
export type ToolCatalog = components["schemas"]["ToolCatalog"];

export type Effort = Session["effort"];
export const effortNames: Record<Effort, string> = {
  quick: "快速",
  deep: "深入",
};

export const effortDescriptions: Record<Effort, string> = {
  quick: "低强度思考 · 单 Agent",
  deep: "高强度筛选 · 最多三个并行 Worker",
};
