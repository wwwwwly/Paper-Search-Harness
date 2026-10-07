import { useEffect, useState } from "react";
import { PanelRight, X } from "lucide-react";
import { Conversation } from "./features/chat/Conversation";
import { ToolResolutionDialog } from "./features/chat/ToolResolutionDialog";
import { Library } from "./features/library/Library";
import { Resources } from "./features/library/Resources";
import { Reports } from "./features/reports/Reports";
import { Settings } from "./features/settings/Settings";
import { api } from "./workbench/client";
import { Sidebar, type Page } from "./workbench/Sidebar";
import { useResearchSession } from "./workbench/useResearchSession";
import type { Artifact, Evidence, ToolExecution } from "./workbench/types";

export default function App() {
  const research = useResearchSession();
  const {
    config,
    sessions,
    selected,
    selectSession,
    effort,
    setEffort,
    visible,
    connected,
    sending,
    error,
    setError,
    send,
    upload,
    history,
    act,
  } = research;
  const [page, setPage] = useState<Page>("chat");
  const [selectedReport, setSelectedReport] = useState<Artifact | null>(null);
  const [resources, setResources] = useState(true);
  const [evidence, setEvidence] = useState<Evidence | null>(null);
  const [resolving, setResolving] = useState<ToolExecution | null>(null);
  useEffect(() => {
    setEvidence(null);
    setResolving(null);
  }, [selected]);
  function showEvidence(value: Evidence) {
    setEvidence(value);
    setResources(true);
  }

  return (
    <div className={`workbench ${resources ? "" : "hide-resources"}`}>
      <Sidebar
        page={page}
        selected={selected}
        sessions={sessions}
        onPage={setPage}
        onNew={() => {
          selectSession(null);
          setPage("chat");
          setSelectedReport(null);
        }}
        onSelect={(id) => {
          selectSession(id);
          setPage("chat");
          setSelectedReport(null);
        }}
      />
      <div className="workspace-body">
        {error && (
          <div className="error-banner" role="alert">
            <span>{error}</span>
            <button
              className="icon-button"
              aria-label="关闭错误提示"
              onClick={() => setError("")}
            >
              <X size={16} />
            </button>
          </div>
        )}
        {page === "chat" ? (
          <div className="research-layout">
            <button
              className="resource-toggle icon-button"
              title="显示或隐藏资料"
              onClick={() => setResources((value) => !value)}
            >
              <PanelRight size={18} />
            </button>
            <Conversation
              snapshot={visible}
              connected={connected}
              sending={sending}
              onSend={send}
              config={config}
              effort={effort}
              onEffort={setEffort}
              onCancel={(id) => void act(() => api.cancel(id))}
              onResume={(id, extend) => void act(() => api.resume(id, extend))}
              onUpload={(file) =>
                void act(async () => {
                  await upload(file);
                  setResources(true);
                })
              }
              onEvidence={showEvidence}
              onResolve={setResolving}
              onHistory={() => void act(history)}
            />
            {resources && (
              <Resources
                snapshot={visible}
                selectedEvidence={evidence}
                selectedReport={
                  selectedReport?.session_id === selected
                    ? selectedReport
                    : null
                }
                onEvidence={showEvidence}
                onCloseEvidence={() => {
                  setEvidence(null);
                  setSelectedReport(null);
                }}
              />
            )}
          </div>
        ) : page === "library" ? (
          <Library />
        ) : page === "reports" ? (
          <Reports
            onOpen={(artifact) => {
              selectSession(artifact.session_id);
              setSelectedReport(artifact);
              setPage("chat");
              setResources(true);
            }}
          />
        ) : (
          <Settings config={config} />
        )}
      </div>
      {resolving && (
        <ToolResolutionDialog
          key={`${resolving.run_id}:${resolving.id}`}
          tool={resolving}
          onClose={() => setResolving(null)}
          onSave={(content) =>
            act(async () => {
              await api.resolve(resolving.run_id, resolving.id, {
                content,
                data: null,
                artifact_ids: [],
                evidence_ids: [],
                sources: [],
                snapshot_ids: [],
                is_error: false,
                error_code: null,
                retryable: false,
              });
              setResolving(null);
            })
          }
        />
      )}
    </div>
  );
}
