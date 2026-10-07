import { useEffect, useState } from "react";
import { api } from "../../workbench/client";
import { Download, ExternalLink, Search } from "lucide-react";
import type { Paper } from "../../workbench/types";

export function Library() {
  const [papers, setPapers] = useState<Paper[]>([]),
    [query, setQuery] = useState(""),
    [error, setError] = useState("");
  useEffect(() => {
    void api
      .papers()
      .then(setPapers)
      .catch((reason) => setError(String(reason)));
  }, []);
  const visible = papers.filter((paper) =>
    `${paper.title} ${paper.abstract ?? ""} ${paper.authors?.join(" ")}`
      .toLowerCase()
      .includes(query.toLowerCase()),
  );
  return (
    <main className="library-page">
      <header>
        <div>
          <span className="eyebrow">PERSONAL COLLECTION</span>
          <h1>论文库</h1>
          <p>研究中发现的论文，持续保存在这里。</p>
        </div>
        <a className="subtle" href="/api/papers/bibtex" download>
          <Download size={16} />
          导出 BibTeX
        </a>
      </header>
      <label className="library-search">
        <Search size={18} />
        <input
          placeholder="搜索标题、作者或摘要"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
      </label>
      {error && <p className="inline-error">{error}</p>}
      <div className="paper-grid">
        {visible.map((paper) => (
          <article className="paper-card" key={paper.canonical_id}>
            <div className="paper-meta">
              <span>{paper.publication_date ?? paper.year ?? "日期未知"}</span>
              <span>{paper.venue || paper.provider}</span>
            </div>
            <h3>{paper.title}</h3>
            <p className="paper-authors">
              {paper.authors?.slice(0, 4).join(", ")}
            </p>
            <p className="paper-abstract">
              {paper.abstract || "该来源未提供摘要。"}
            </p>
            <footer>
              <span>{paper.is_open_access ? "开放获取" : "元数据记录"}</span>
              {paper.landing_page_url && (
                <a
                  href={paper.landing_page_url}
                  target="_blank"
                  rel="noreferrer"
                >
                  论文页面
                  <ExternalLink size={13} />
                </a>
              )}
            </footer>
          </article>
        ))}
      </div>
      {!visible.length && (
        <div className="empty-page">
          还没有匹配的论文。开始一个研究会话来建立你的文献库。
        </div>
      )}
    </main>
  );
}
