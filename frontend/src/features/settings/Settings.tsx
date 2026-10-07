import { useEffect, useState } from "react";
import { Check, Pencil, Plus, RefreshCw, Trash2 } from "lucide-react";
import { api } from "../../workbench/client";
import type { Configuration, Memory, ToolInfo } from "../../workbench/types";

export function Settings({ config }: { config: Configuration | null }) {
  const [current, setCurrent] = useState(config);
  const [memories, setMemories] = useState<Memory[]>([]),
    [tools, setTools] = useState<ToolInfo[]>([]);
  const [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const [editing, setEditing] = useState<string | null>(null),
    [content, setContent] = useState("");
  const [newKey, setNewKey] = useState(""),
    [newContent, setNewContent] = useState("");
  async function refresh() {
    try {
      const [configuration, items, result] = await Promise.all([
        api.config(),
        api.memories(),
        api.tools(),
      ]);
      setCurrent(configuration);
      setMemories(items);
      setTools(result.tools);
    } catch (reason) {
      setError(String(reason));
    }
  }
  async function refreshModels() {
    setError("");
    setNotice("");
    try {
      await api.refreshModels();
      await refresh();
      setNotice("模型目录信息已更新");
    } catch (reason) {
      setError(String(reason));
    }
  }
  useEffect(() => {
    void refresh();
  }, []);
  async function save(memory: Memory) {
    try {
      const value = await api.editMemory(memory.id, content);
      setMemories((items) =>
        items.map((item) => (item.id === value.id ? value : item)),
      );
      setEditing(null);
    } catch (reason) {
      setError(String(reason));
    }
  }
  async function remove(memory: Memory) {
    try {
      await api.deleteMemory(memory.id);
      setMemories((items) => items.filter((item) => item.id !== memory.id));
    } catch (reason) {
      setError(String(reason));
    }
  }
  async function create() {
    try {
      await api.createMemory(newKey, newContent);
      setNewKey("");
      setNewContent("");
      await refresh();
    } catch (reason) {
      setError(String(reason));
    }
  }
  return (
    <main className="settings-page">
      <span className="eyebrow">WORKSPACE SETTINGS</span>
      <h1>设置与记忆</h1>
      {error && <p className="inline-error">{error}</p>}
      {notice && <p className="muted">{notice}</p>}
      <section>
        <h2>模型连接</h2>
        <p className="muted">
          模型与凭据在后端配置；前端档位只调整思考强度、研究预算与委派策略。
        </p>
        <button className="subtle" onClick={() => void refreshModels()}>
          <RefreshCw size={13} />
          更新模型目录
        </button>
        {current?.models.map((model) => (
          <div className="setting-row" key={model.id}>
            <div>
              <strong>{model.model}</strong>
              <small>
                {Math.round(model.context_window / 1000)}k 上下文 ·
                {Math.round(model.max_output_tokens_supported / 1000)}k 输出 ·
                {model.capability_source}
              </small>
            </div>
            <span className={model.configured ? "badge good" : "badge"}>
              {model.configured ? "凭据已配置" : "缺少凭据"}
            </span>
          </div>
        ))}
        <p className="muted">
          全文与记忆检索：
          {current?.embedding_enabled
            ? "关键词 + 向量"
            : "关键词模式，向量模型未启用"}
        </p>
      </section>
      <section>
        <h2>工具与 MCP 连接</h2>
        <button className="subtle" onClick={() => void refresh()}>
          <RefreshCw size={13} />
          刷新状态
        </button>
        {!current?.mcp.length && (
          <p className="muted">
            尚未配置 MCP 服务。配置连接和工具启用列表后，重启后端即可发现工具。
          </p>
        )}
        {current?.mcp.map((server) => (
          <div className="setting-row" key={server.name}>
            <div>
              <strong>{server.name}</strong>
              <small>
                {server.tools.length} 个工具启用
                {server.error ? ` · ${server.error}` : ""}
              </small>
            </div>
            <span className="badge">{server.status}</span>
          </div>
        ))}
        {tools.map((tool) => (
          <details className="schema-card" key={tool.name}>
            <summary>{tool.name}</summary>
            <p>{tool.description}</p>
            <pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
            {tool.output_schema && (
              <pre>{JSON.stringify(tool.output_schema, null, 2)}</pre>
            )}
          </details>
        ))}
      </section>
      <section>
        <h2>确认的记忆</h2>
        <p className="muted">可修改或删除。删除的记忆不会从旧摘要自动恢复。</p>
        <div className="note-composer">
          <input
            aria-label="笔记名称"
            placeholder="新笔记名称"
            value={newKey}
            maxLength={100}
            onChange={(event) => setNewKey(event.target.value)}
          />
          <textarea
            aria-label="笔记内容"
            placeholder="写下希望跨会话保留的研究笔记…"
            value={newContent}
            maxLength={5000}
            onChange={(event) => setNewContent(event.target.value)}
          />
          <button
            className="subtle"
            disabled={!newKey.trim() || !newContent.trim()}
            onClick={() => void create()}
          >
            <Plus size={14} />
            保存研究笔记
          </button>
        </div>
        {!memories.length && (
          <div className="empty-page compact">尚未保存偏好或研究笔记。</div>
        )}
        {memories.map((memory) => (
          <article className="memory-card" key={memory.id}>
            <header>
              <strong>{memory.key}</strong>
              <span className="badge">
                {memory.scope === "global"
                  ? "跨会话"
                  : `会话 ${memory.session_id?.slice(0, 6)}`}
              </span>
              <div>
                {editing === memory.id ? (
                  <button
                    className="icon-button"
                    title="保存记忆"
                    onClick={() => void save(memory)}
                  >
                    <Check size={16} />
                  </button>
                ) : (
                  <button
                    className="icon-button"
                    title="编辑记忆"
                    onClick={() => {
                      setEditing(memory.id);
                      setContent(memory.content);
                    }}
                  >
                    <Pencil size={15} />
                  </button>
                )}
                <button
                  className="icon-button"
                  title="重新建立索引"
                  onClick={() =>
                    void api
                      .reindexMemory(memory.id)
                      .then(() => setNotice("索引任务已排队"))
                      .catch((reason) => setError(String(reason)))
                  }
                >
                  <RefreshCw size={14} />
                </button>
                <button
                  className="icon-button"
                  title="删除记忆"
                  onClick={() => void remove(memory)}
                >
                  <Trash2 size={15} />
                </button>
              </div>
            </header>
            {editing === memory.id ? (
              <textarea
                value={content}
                onChange={(event) => setContent(event.target.value)}
              />
            ) : (
              <p>{memory.content}</p>
            )}
          </article>
        ))}
      </section>
    </main>
  );
}
