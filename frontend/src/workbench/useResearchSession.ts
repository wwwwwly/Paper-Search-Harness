import { useEffect, useReducer, useRef, useState } from "react";
import { api, ApiError, subscribe } from "./client";
import { reducer } from "./state";
import type { Configuration, Session, Effort, SessionEvent } from "./types";

/** Own server state and session transport; components own visual interactions. */
export function useResearchSession() {
  const [config, setConfig] = useState<Configuration | null>(null);
  const [sessions, setSessions] = useState<Session[]>([]);
  const [selected, setSelected] = useState<string | null>(() =>
    localStorage.getItem("research.session"),
  );
  const [effort, setEffort] = useState<Effort>("quick");
  const [snapshot, dispatch] = useReducer(reducer, null);
  const [connected, setConnected] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const attachments = useRef<{ session: string; ids: string[] } | null>(null);
  const draft = useRef<{
    text: string;
    id: string;
    session: string;
    documents: string[];
    effort: Effort;
  } | null>(null);
  const visible = snapshot?.session.id === selected ? snapshot : null;
  // Also protects a new-session upload/send from creating two sessions concurrently.
  const creating = useRef<Promise<string> | null>(null);
  const selection = useRef(selected);
  const eventQueue = useRef<SessionEvent[]>([]);
  const eventFrame = useRef<number | null>(null);
  selection.current = selected;

  useEffect(() => {
    let alive = true;
    void Promise.all([api.config(), api.sessions()])
      .then(([value, items]) => {
        if (!alive) return;
        setConfig(value);
        setEffort(value.default_effort);
        setSessions(items);
      })
      .catch((reason) => {
        if (alive) setError(String(reason));
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => {
    setConnected(false);
    if (!selected) {
      localStorage.removeItem("research.session");
      return;
    }
    localStorage.setItem("research.session", selected);
    let dispose: (() => void) | undefined,
      alive = true;
    void api
      .snapshot(selected)
      .then((value) => {
        if (!alive) return;
        dispatch({ type: "snapshot", value });
        setEffort(value.session.effort);
        dispose = subscribe(
          selected,
          value.event_cursor,
          (event) => {
            if (!alive) return;
            eventQueue.current.push(event);
            if (eventFrame.current === null)
              eventFrame.current = requestAnimationFrame(() => {
                const events = eventQueue.current;
                eventQueue.current = [];
                eventFrame.current = null;
                dispatch({ type: "events", value: events });
              });
            if (event.type === "session.updated") {
              setSessions((items) =>
                items.map((item) =>
                  item.id === event.data.session.id ? event.data.session : item,
                ),
              );
            }
          },
          (value) => {
            if (alive) setConnected(value);
          },
        );
      })
      .catch((reason) => {
        if (!alive) return;
        if (reason instanceof ApiError && reason.status === 404) {
          if (selection.current === selected) {
            localStorage.removeItem("research.session");
            selectSession(null);
            setSessions((items) =>
              items.filter((item) => item.id !== selected),
            );
            setError("原会话已不存在，已切换到新研究。");
          }
          return;
        }
        setError(String(reason));
      });
    return () => {
      alive = false;
      if (eventFrame.current !== null) cancelAnimationFrame(eventFrame.current);
      eventFrame.current = null;
      eventQueue.current = [];
      dispose?.();
    };
  }, [selected]);

  async function ensureSession() {
    if (selection.current) return selection.current;
    if (!creating.current) {
      creating.current = api
        .create(effort)
        .then((session) => {
          setSessions((items) => [session, ...items]);
          selection.current = session.id;
          setSelected(session.id);
          return session.id;
        })
        .finally(() => {
          creating.current = null;
        });
    }
    return creating.current;
  }

  async function refreshSnapshot(id: string) {
    const value = await api.snapshot(id);
    if (selection.current === id) dispatch({ type: "snapshot", value });
  }

  function selectSession(id: string | null) {
    selection.current = id;
    setSelected(id);
    attachments.current = null;
    draft.current = null;
    setError("");
  }

  async function send(text: string) {
    setSending(true);
    setError("");
    let submitted = false;
    try {
      const id = await ensureSession();
      const documents =
        attachments.current?.session === id ? [...attachments.current.ids] : [];
      if (
        draft.current?.text !== text ||
        draft.current.session !== id ||
        draft.current.effort !== effort ||
        JSON.stringify(draft.current.documents) !== JSON.stringify(documents)
      ) {
        draft.current = {
          text,
          session: id,
          documents,
          effort,
          id: crypto.randomUUID(),
        };
      }
      await api.send(id, {
        text,
        client_id: draft.current.id,
        document_ids: documents,
        effort,
      });
      submitted = true;
      if (attachments.current?.session === id) attachments.current = null;
      draft.current = null;
      await refreshSnapshot(id);
      setSessions(await api.sessions());
    } catch (reason) {
      setError(String(reason));
      if (!submitted) throw reason;
    } finally {
      setSending(false);
    }
  }

  async function upload(file: File) {
    const id = await ensureSession();
    const document = await api.upload(id, file);
    if (selection.current === id) {
      const existing =
        attachments.current?.session === id ? attachments.current.ids : [];
      attachments.current = { session: id, ids: [...existing, document.id] };
    }
    await refreshSnapshot(id);
  }

  async function history() {
    if (!visible?.messages[0]) return;
    const id = visible.session.id;
    const messages = await api.messages(id, visible.messages[0].id);
    if (selection.current === id)
      dispatch({ type: "history", value: messages });
  }

  async function act(work: () => Promise<unknown>) {
    setError("");
    try {
      await work();
    } catch (reason) {
      setError(String(reason));
    }
  }

  return {
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
  };
}
