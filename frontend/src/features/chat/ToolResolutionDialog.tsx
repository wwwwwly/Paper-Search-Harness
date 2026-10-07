import { useState } from "react";
import type { ToolExecution } from "../../workbench/types";

export function ToolResolutionDialog({
  tool,
  onClose,
  onSave,
}: {
  tool: ToolExecution;
  onClose: () => void;
  onSave: (content: string) => Promise<void>;
}) {
  const [content, setContent] = useState("");
  const [saving, setSaving] = useState(false);
  async function save() {
    setSaving(true);
    try {
      await onSave(content);
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="modal-backdrop">
      <section
        className="modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="resolve-title"
      >
        <h2 id="resolve-title">记录中断调用的真实结果</h2>
        <p>
          工具 {tool.call.name}{" "}
          的外部副作用可能已发生。请核对后填写结果，接续执行时将使用此记录。
        </p>
        <textarea
          aria-label="已核实的调用结果"
          value={content}
          onChange={(event) => setContent(event.target.value)}
        />
        <footer>
          <button className="subtle" onClick={onClose} disabled={saving}>
            返回
          </button>
          <button
            className="new-session"
            disabled={!content.trim() || saving}
            onClick={() => void save()}
          >
            保存结果
          </button>
        </footer>
      </section>
    </div>
  );
}
