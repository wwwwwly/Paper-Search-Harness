import { useEffect, useState } from "react";
import { Download, FileText, LoaderCircle, X } from "lucide-react";
import { api } from "../../workbench/client";
import type {
  Artifact,
  Document,
  Evidence,
  Snapshot,
} from "../../workbench/types";
import { Markdown } from "../../components/Markdown";
import { PdfReader } from "../documents/PdfReader";

const statusName: Record<string, string> = {
  pending: "排队中",
  downloading: "下载中",
  parsing: "解析中",
  indexing: "建立索引",
  ready: "可阅读",
  failed: "处理失败",
  cancelled: "已取消",
};

export function Resources({
  snapshot,
  selectedEvidence,
  selectedReport,
  onEvidence,
  onCloseEvidence,
}: {
  snapshot: Snapshot | null;
  selectedEvidence: Evidence | null;
  selectedReport: Artifact | null;
  onEvidence: (evidence: Evidence) => void;
  onCloseEvidence: () => void;
}) {
  const [tab, setTab] = useState<"documents" | "reports" | "evidence">(
    "documents",
  );
  const [documentId, setDocumentId] = useState<string | null>(null);
  const [report, setReport] = useState<{
    artifact: Artifact;
    text: string;
  } | null>(null);
  const [webSource, setWebSource] = useState<{
    text: string;
    url: string;
    retrieved: string;
  } | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  useEffect(() => {
    setDocumentId(null);
    setReport(null);
    setWebSource(null);
    setError("");
  }, [snapshot?.session.id]);
  useEffect(() => {
    let alive = true;
    setWebSource(null);
    if (selectedEvidence?.document_id) {
      setDocumentId(selectedEvidence.document_id);
      setReport(null);
    } else if (selectedEvidence) {
      setDocumentId(null);
      setReport(null);
      setTab("evidence");
      if (selectedEvidence.snapshot_id) {
        void api
          .webSource(selectedEvidence.snapshot_id)
          .then((value) => {
            if (alive)
              setWebSource({
                text: value.text,
                url: value.snapshot.url,
                retrieved: value.snapshot.retrieved_at,
              });
          })
          .catch((reason) => {
            if (alive) setError(String(reason));
          });
      }
    }
    return () => {
      alive = false;
    };
  }, [selectedEvidence]);
  async function openReport(artifact: Artifact) {
    setLoading(true);
    setError("");
    try {
      setReport({ artifact, text: await api.artifact(artifact.id) });
      setDocumentId(null);
    } catch (reason) {
      setError(String(reason));
    } finally {
      setLoading(false);
    }
  }
  useEffect(() => {
    if (selectedReport && snapshot?.session.id === selectedReport.session_id)
      void openReport(selectedReport);
  }, [selectedReport, snapshot?.session.id]);
  const reports =
    snapshot?.artifacts.filter((item) => item.kind === "report") ?? [];
  return (
    <aside className="resources">
      <header>
        <div>
          <span className="eyebrow">YOUR RESEARCH</span>
          <h3>{documentId ? "原文阅读" : report ? "研究报告" : "研究资料"}</h3>
        </div>
        {(documentId || report || webSource) && (
          <button
            className="icon-button"
            title="返回资料列表"
            onClick={() => {
              setDocumentId(null);
              setReport(null);
              setWebSource(null);
              onCloseEvidence();
            }}
          >
            <X size={18} />
          </button>
        )}
      </header>
      {webSource ? (
        <div className="report-preview">
          <a href={webSource.url} target="_blank" rel="noreferrer">
            打开来源网页
          </a>
          <p className="muted">
            快照时间：{new Date(webSource.retrieved).toLocaleString()}
          </p>
          <blockquote>{selectedEvidence?.excerpt}</blockquote>
          <div className="markdown">
            <Markdown
              text={webSource.text}
              evidence={[]}
              onEvidence={onEvidence}
            />
          </div>
        </div>
      ) : documentId ? (
        <PdfReader documentId={documentId} evidence={selectedEvidence} />
      ) : report ? (
        <div className="report-preview">
          <div className="report-actions">
            <a
              href={`/api/artifacts/${report.artifact.id}/content`}
              download={`${report.artifact.title}.md`}
            >
              <Download size={15} />
              Markdown
            </a>
            <a href="/api/papers/bibtex" download>
              <Download size={15} />
              BibTeX
            </a>
          </div>
          <div className="markdown">
            <Markdown
              text={report.text}
              evidence={snapshot?.evidence ?? []}
              onEvidence={onEvidence}
            />
          </div>
        </div>
      ) : (
        <>
          <nav className="resource-tabs">
            {(["documents", "reports", "evidence"] as const).map((value) => (
              <button
                className={tab === value ? "selected" : ""}
                key={value}
                onClick={() => setTab(value)}
              >
                {
                  { documents: "文档", reports: "报告", evidence: "证据" }[
                    value
                  ]
                }
                <span>
                  {value === "documents"
                    ? (snapshot?.documents.length ?? 0)
                    : value === "reports"
                      ? reports.length
                      : (snapshot?.evidence.length ?? 0)}
                </span>
              </button>
            ))}
          </nav>
          <div className="resource-content">
            {error && <p className="inline-error">{error}</p>}
            {loading && <LoaderCircle size={18} className="spin" />}
            {tab === "documents" && (
              <>
                {!snapshot?.documents.length && (
                  <Empty
                    title="把原文放在手边"
                    text="上传 PDF，或让助手获取论文全文。解析后即可搜索段落、定位证据。"
                  />
                )}
                {snapshot?.documents.map((document) => (
                  <DocumentCard
                    key={document.id}
                    document={document}
                    onOpen={() => {
                      if (document.file_id) setDocumentId(document.id);
                    }}
                  />
                ))}
              </>
            )}
            {tab === "reports" && (
              <>
                {!reports.length && (
                  <Empty
                    title="让研究成为积累"
                    text="让助手生成研究报告。后续追问与修订会保存为新版本。"
                  />
                )}
                {[...reports].reverse().map((artifact) => (
                  <button
                    className="report-card"
                    key={artifact.id}
                    onClick={() => void openReport(artifact)}
                  >
                    <span className="eyebrow">
                      {artifact.previous_id
                        ? "REVISED REPORT"
                        : "RESEARCH REPORT"}
                    </span>
                    <strong>{artifact.title}</strong>
                    <small>
                      {new Date(artifact.created_at).toLocaleString()}
                    </small>
                  </button>
                ))}
              </>
            )}
            {tab === "evidence" && (
              <>
                {!snapshot?.evidence.length && (
                  <Empty
                    title="每个结论，都有出处"
                    text="助手阅读并保存原文片段后，证据会出现在这里。"
                  />
                )}
                {snapshot?.evidence.map((item) => (
                  <button
                    key={item.id}
                    className={`evidence-card ${selectedEvidence?.id === item.id ? "selected" : ""}`}
                    onClick={() => onEvidence(item)}
                  >
                    <span>
                      {item.level === "fulltext"
                        ? `全文 · 第 ${item.page} 页`
                        : item.level === "abstract"
                          ? "论文摘要"
                          : "网页资料"}
                    </span>
                    <p>{item.excerpt}</p>
                    <small>{item.paper_id ?? item.document_id}</small>
                  </button>
                ))}
              </>
            )}
          </div>
          {snapshot?.session.brief?.objective && (
            <div className="brief-card">
              <span className="eyebrow">CURRENT FOCUS</span>
              <p>{snapshot.session.brief.objective}</p>
              {snapshot.session.brief.constraints?.map((item) => (
                <span className="constraint" key={item}>
                  {item}
                </span>
              ))}
            </div>
          )}
        </>
      )}
    </aside>
  );
}

function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="resource-empty">
      <FileText size={30} strokeWidth={1.2} />
      <h4>{title}</h4>
      <p>{text}</p>
    </div>
  );
}

function DocumentCard({
  document,
  onOpen,
}: {
  document: Document;
  onOpen: () => void;
}) {
  const [error, setError] = useState("");
  return (
    <div>
      <button className="document-card" onClick={onOpen}>
        <FileText size={21} />
        <div>
          <strong>{document.name}</strong>
          <span className={"document-status " + document.status}>
            {statusName[document.status]}
          </span>
          {document.error && <p className="inline-error">{document.error}</p>}
        </div>
      </button>
      <div className="document-actions">
        {["failed", "cancelled"].includes(document.status) ? (
          <button
            className="subtle"
            onClick={() =>
              void api
                .retryDocument(document.id)
                .catch((reason) => setError(String(reason)))
            }
          >
            重新处理
          </button>
        ) : (
          document.status !== "ready" && (
            <button
              className="subtle"
              onClick={() =>
                void api
                  .cancelDocument(document.id)
                  .catch((reason) => setError(String(reason)))
              }
            >
              取消处理
            </button>
          )
        )}
        {error && <p className="inline-error">{error}</p>}
      </div>
    </div>
  );
}
