import { useEffect, useState } from "react";
import { api } from "../../workbench/client";
import type { Artifact } from "../../workbench/types";

export function Reports({ onOpen }: { onOpen: (artifact: Artifact) => void }) {
  const [reports, setReports] = useState<Artifact[]>([]),
    [error, setError] = useState("");
  useEffect(() => {
    void api
      .reports()
      .then(setReports)
      .catch((reason) => setError(String(reason)));
  }, []);
  return (
    <main className="library-page">
      <span className="eyebrow">RESEARCH OUTPUTS</span>
      <h1>研究报告</h1>
      <p className="muted">每次修订保留版本，随时返回原会话与证据。</p>
      {error && <p className="inline-error">{error}</p>}
      <div className="paper-grid report-library">
        {[...reports].reverse().map((artifact) => (
          <button
            className="report-card"
            key={artifact.id}
            onClick={() => onOpen(artifact)}
          >
            <span className="eyebrow">
              {artifact.previous_id ? "REVISED REPORT" : "RESEARCH REPORT"}
            </span>
            <strong>{artifact.title}</strong>
            <small>{new Date(artifact.created_at).toLocaleString()}</small>
          </button>
        ))}
      </div>
      {!reports.length && (
        <div className="empty-page">在研究会话中让助手生成第一份报告。</div>
      )}
    </main>
  );
}
