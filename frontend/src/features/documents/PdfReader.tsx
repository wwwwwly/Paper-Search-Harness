import { useEffect, useRef, useState } from "react";
import type { PDFDocumentLoadingTask, PDFDocumentProxy } from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  LoaderCircle,
} from "lucide-react";
import type { Evidence } from "../../workbench/types";
import { api } from "../../workbench/client";

export function PdfReader({
  documentId,
  evidence,
}: {
  documentId: string;
  evidence?: Evidence | null;
}) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const [pdf, setPdf] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState(evidence?.page ?? 1);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [box, setBox] = useState<{
    left: number;
    top: number;
    width: number;
    height: number;
  } | null>(null);
  useEffect(() => {
    let disposed = false,
      task: PDFDocumentLoadingTask | null = null;
    setPdf(null);
    setError("");
    setLoading(true);
    void import("pdfjs-dist")
      .then(async (library) => {
        library.GlobalWorkerOptions.workerSrc = workerUrl;
        if (disposed) return;
        task = library.getDocument({
          url: `/api/documents/${documentId}/file`,
          cMapUrl: "/pdf-assets/cmaps/",
          cMapPacked: true,
          standardFontDataUrl: "/pdf-assets/standard_fonts/",
          wasmUrl: "/pdf-assets/wasm/",
        });
        const loaded = await task.promise;
        if (!disposed) setPdf(loaded);
      })
      .catch((reason) => {
        if (!disposed) {
          setError(String(reason));
          setLoading(false);
        }
      });
    return () => {
      disposed = true;
      if (task) void task.destroy();
    };
  }, [documentId]);
  useEffect(() => {
    setPage(evidence?.page ?? 1);
  }, [documentId, evidence?.id, evidence?.page]);
  useEffect(() => {
    setBox(null);
    if (!evidence?.block_id || evidence.page !== page) return;
    let disposed = false;
    void api
      .document(documentId)
      .then((document) => {
        const block = document.parsed?.blocks.find(
          (item) => item.id === evidence.block_id,
        );
        if (!disposed && block?.bbox && block.page_width && block.page_height) {
          const [left, top, right, bottom] = block.bbox;
          setBox({
            left: (left / block.page_width) * 100,
            top: (top / block.page_height) * 100,
            width: ((right - left) / block.page_width) * 100,
            height: ((bottom - top) / block.page_height) * 100,
          });
        }
      })
      .catch((reason) => {
        if (!disposed) setError(String(reason));
      });
    return () => {
      disposed = true;
    };
  }, [documentId, evidence, page]);
  useEffect(() => {
    if (!pdf || !canvas.current) return;
    let disposed = false;
    let cancel: (() => void) | undefined;
    setLoading(true);
    void pdf
      .getPage(Math.min(page, pdf.numPages))
      .then(async (value) => {
        if (disposed || !canvas.current) return;
        const viewport = value.getViewport({ scale: 1.5 });
        canvas.current.width = viewport.width;
        canvas.current.height = viewport.height;
        const task = value.render({ canvas: canvas.current, viewport });
        cancel = () => task.cancel();
        await task.promise;
        if (!disposed) setLoading(false);
      })
      .catch((reason) => {
        if (!disposed && reason?.name !== "RenderingCancelledException") {
          setError(String(reason));
          setLoading(false);
        }
      });
    return () => {
      disposed = true;
      cancel?.();
    };
  }, [pdf, page]);
  return (
    <div className="pdf-reader">
      <div className="pdf-toolbar">
        <button
          className="icon-button"
          onClick={() => setPage((value) => Math.max(1, value - 1))}
          disabled={page <= 1}
        >
          <ChevronLeft size={17} />
        </button>
        <span>
          {page} / {pdf?.numPages ?? "—"}
        </span>
        <button
          className="icon-button"
          onClick={() =>
            setPage((value) => Math.min(pdf?.numPages ?? value, value + 1))
          }
          disabled={!pdf || page >= pdf.numPages}
        >
          <ChevronRight size={17} />
        </button>
        <a
          href={`/api/documents/${documentId}/file#page=${page}`}
          target="_blank"
          rel="noreferrer"
          title="新窗口打开"
        >
          <ExternalLink size={16} />
        </a>
      </div>
      {error && <p className="inline-error">{error}</p>}
      {loading && (
        <div className="pdf-loading">
          <LoaderCircle size={18} className="spin" />
          加载页面
        </div>
      )}
      <div className="pdf-page">
        <canvas ref={canvas} />
        {box && (
          <div
            className="evidence-highlight"
            style={{
              left: `${box.left}%`,
              top: `${box.top}%`,
              width: `${box.width}%`,
              height: `${box.height}%`,
            }}
          />
        )}
      </div>
      {evidence && (
        <blockquote className="evidence-excerpt">{evidence.excerpt}</blockquote>
      )}
    </div>
  );
}
