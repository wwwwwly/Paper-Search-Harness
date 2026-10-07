import {
  BookOpen,
  ChevronRight,
  FileText,
  MessageSquare,
  Plus,
  Settings2,
} from "lucide-react";
import type { Session } from "./types";

export type Page = "chat" | "library" | "reports" | "settings";

export function Sidebar({
  page,
  selected,
  sessions,
  onPage,
  onSelect,
  onNew,
}: {
  page: Page;
  selected: string | null;
  sessions: Session[];
  onPage: (page: Page) => void;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  return (
    <aside className="sidebar">
      <a
        className="brand"
        href="#"
        onClick={(event) => {
          event.preventDefault();
          onPage("chat");
        }}
      >
        <span className="brand-mark">
          P<span>∴</span>
        </span>
        <div>
          Paper Search<small>研究工作台</small>
        </div>
      </a>
      <button className="new-session" onClick={onNew}>
        <Plus size={17} />
        新建研究<span>↗</span>
      </button>
      <nav className="primary-nav">
        <button
          className={page === "chat" ? "active" : ""}
          onClick={() => onPage("chat")}
        >
          <MessageSquare size={17} />
          研究会话
        </button>
        <button
          className={page === "library" ? "active" : ""}
          onClick={() => onPage("library")}
        >
          <BookOpen size={17} />
          论文库
        </button>
        <button
          className={page === "reports" ? "active" : ""}
          onClick={() => onPage("reports")}
        >
          <FileText size={17} />
          研究报告
        </button>
      </nav>
      <div className="session-label">
        RECENT RESEARCH <span>{sessions.length}</span>
      </div>
      <div className="session-list">
        {sessions.map((session) => (
          <button
            key={session.id}
            className={
              selected === session.id && page === "chat" ? "selected" : ""
            }
            onClick={() => onSelect(session.id)}
          >
            <span>{session.title}</span>
            <small>
              {new Date(session.updated_at).toLocaleDateString("zh-CN", {
                month: "short",
                day: "numeric",
              })}
            </small>
            <ChevronRight size={13} />
          </button>
        ))}
        {!sessions.length && <p>新的问题，会成为下一段研究。</p>}
      </div>
      <footer className="sidebar-footer">
        <button
          className={page === "settings" ? "active" : ""}
          onClick={() => onPage("settings")}
        >
          <Settings2 size={16} />
          设置与记忆<span className="local-badge">LOCAL</span>
        </button>
      </footer>
    </aside>
  );
}
