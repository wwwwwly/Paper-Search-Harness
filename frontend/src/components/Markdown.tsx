import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import type { Evidence } from "../workbench/types";

export function Markdown({
  text,
  evidence,
  onEvidence,
}: {
  text: string;
  evidence: Evidence[];
  onEvidence: (item: Evidence) => void;
}) {
  const linked = text.replace(
    /\[evidence:([a-zA-Z0-9_-]+)\]/g,
    "[$1](#evidence-$1)",
  );
  return (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      components={{
        a: ({ href, children }) => {
          if (href?.startsWith("#evidence-")) {
            const item = evidence.find((value) => value.id === href.slice(10));
            return (
              <button
                className="citation"
                onClick={() => item && onEvidence(item)}
                title={item?.excerpt || "证据尚未加载"}
              >
                {item?.page ? `第 ${item.page} 页` : "来源"}
              </button>
            );
          }
          return (
            <a href={href} target="_blank" rel="noreferrer">
              {children}
            </a>
          );
        },
      }}
    >
      {linked}
    </ReactMarkdown>
  );
}
