import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import { readJson } from "./api";
import { apiUrl } from "./config";
import {
  getTableauUsername,
  getUniqueUserId,
  loadExtensionContext,
  loadStoredTableauUsername,
  persistTableauUsername,
  type ExtensionContext,
} from "./tableauExtension";
import type { ToolStep, TurnTiming } from "./ToolSteps";

type Role = "user" | "assistant";

interface ChatMessage {
  role: Role;
  content: string;
  steps?: ToolStep[];
  timing?: TurnTiming;
}

type HealthInfo = {
  ok: boolean;
  healthError?: string;
  hasOpenAi?: boolean;
  chatMode?: "workbook" | "datasource";
  authMode?: string;
  requiresTableauUsername?: boolean;
  tableauSignInOk?: boolean;
  tableauHint?: string;
};

function BrandMark() {
  return (
    <svg className="brand-mark" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="8" fill="url(#brandGrad)" />
      <path d="M8 22V10h3v12H8zm5-8v8h3v-8h-3zm5 4v4h3v-4h-3zm5-6v10h3V12h-3z" fill="white" fillOpacity="0.95" />
      <defs>
        <linearGradient id="brandGrad" x1="0" y1="0" x2="32" y2="32">
          <stop stopColor="#1976d2" />
          <stop offset="1" stopColor="#0d9488" />
        </linearGradient>
      </defs>
    </svg>
  );
}

export function App() {
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [input, setInput] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [health, setHealth] = useState<HealthInfo | null>(null);
  const [extensionContext, setExtensionContext] = useState<ExtensionContext | null>(null);
  const [workbookLoading, setWorkbookLoading] = useState(false);
  const [workbookError, setWorkbookError] = useState<string | null>(null);
  const [tableauUsername, setTableauUsernameState] = useState<string>(
    () => loadStoredTableauUsername() || ""
  );
  const [usernameDraft, setUsernameDraft] = useState(() => loadStoredTableauUsername() || "");
  const [linkingUser, setLinkingUser] = useState(false);
  const [linkError, setLinkError] = useState<string | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const selectedWorkbook = extensionContext?.workbook ?? null;
  const selectedDatasources = extensionContext?.datasources ?? [];
  const needsUsername = health?.requiresTableauUsername === true;
  const hasUsername = !!tableauUsername.trim();

  useEffect(() => {
    const qs = tableauUsername.trim()
      ? `?tableauUsername=${encodeURIComponent(tableauUsername.trim())}`
      : "";
    fetch(apiUrl(`/api/health${qs}`))
      .then(async (r) => {
        try {
          return await readJson<HealthInfo>(r);
        } catch (err) {
          const message = err instanceof Error ? err.message : String(err);
          return { ok: false as const, healthError: message };
        }
      })
      .then(setHealth);
  }, [tableauUsername]);

  const reloadWorkbook = useCallback(async () => {
    setWorkbookLoading(true);
    setWorkbookError(null);
    try {
      const ctx = await loadExtensionContext();
      setExtensionContext(ctx);
      setWorkbookError(null);
    } catch (e) {
      setWorkbookError(e instanceof Error ? e.message : String(e));
      setExtensionContext(null);
    } finally {
      setWorkbookLoading(false);
    }
  }, []);

  useEffect(() => {
    if (health === null) return;
    if (needsUsername && !hasUsername) {
      setWorkbookLoading(false);
      return;
    }
    void reloadWorkbook();
  }, [health, needsUsername, hasUsername, reloadWorkbook]);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, loading]);

  const linkUsername = useCallback(async () => {
    const cleaned = usernameDraft.trim();
    if (!cleaned) {
      setLinkError("Enter your Tableau username (e.g. demoAdmin or local\\demoAdmin).");
      return;
    }
    setLinkingUser(true);
    setLinkError(null);
    try {
      const res = await fetch(
        apiUrl(`/api/auth/verify?tableauUsername=${encodeURIComponent(cleaned)}`)
      );
      const data = await readJson<{
        tableauSignInOk?: boolean;
        tableauHint?: string;
        tableauError?: string;
      }>(res);
      if (!data.tableauSignInOk) {
        throw new Error(
          data.tableauHint || data.tableauError || "Sign-in failed for that username."
        );
      }
      await persistTableauUsername(cleaned);
      setTableauUsernameState(cleaned);
    } catch (e) {
      setLinkError(e instanceof Error ? e.message : String(e));
    } finally {
      setLinkingUser(false);
    }
  }, [usernameDraft]);

  const sendMessage = useCallback(
    async (text: string) => {
      const trimmed = text.trim();
      if (!trimmed || loading) return;
      if (!selectedWorkbook) {
        setError("Connecting…");
        return;
      }
      const user = getTableauUsername() || tableauUsername.trim();
      if (needsUsername && !user) {
        setError("Link your Tableau username first.");
        return;
      }

      setError(null);
      setInput("");
      const next: ChatMessage[] = [...messages, { role: "user", content: trimmed }];
      setMessages(next);
      setLoading(true);

      try {
        const res = await fetch(apiUrl("/api/chat"), {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            messages: next.map((m) => ({ role: m.role, content: m.content })),
            selectedWorkbook,
            ...(selectedDatasources.length > 0
              ? {
                  selectedDatasources: selectedDatasources.map((d) => ({
                    id: d.id || undefined,
                    name: d.name,
                    projectName: d.projectName,
                    isPublished: d.isPublished,
                  })),
                }
              : {}),
            extensionMode: true,
            ...(user ? { tableauUsername: user } : {}),
            ...(getUniqueUserId() ? { uniqueUserId: getUniqueUserId() } : {}),
          }),
        });
        const data = await readJson<{
          reply?: string;
          steps?: ToolStep[];
          timing?: TurnTiming;
          error?: string;
        }>(res);
        if (!res.ok) throw new Error(data.error ?? res.statusText);
        setMessages([
          ...next,
          {
            role: "assistant",
            content: data.reply ?? "",
            steps: data.steps ?? [],
            timing: data.timing,
          },
        ]);
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setError(msg);
        setMessages([...next, { role: "assistant", content: msg }]);
      } finally {
        setLoading(false);
        textareaRef.current?.focus();
      }
    },
    [loading, messages, selectedWorkbook, selectedDatasources, needsUsername, tableauUsername]
  );

  const send = useCallback(() => void sendMessage(input), [input, sendMessage]);

  const clearChat = useCallback(() => {
    setMessages([]);
    setError(null);
    textareaRef.current?.focus();
  }, []);

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  };

  const workbookReady = !!selectedWorkbook;
  const canSend =
    !loading &&
    !!input.trim() &&
    health?.ok &&
    workbookReady &&
    !workbookLoading &&
    (!needsUsername || hasUsername);

  const statusMessage = (() => {
    if (health === null) return "Checking connection…";
    if (health.healthError) return health.healthError;
    if (needsUsername && !hasUsername) return "Enter your Tableau username to continue";
    if (workbookLoading) return "Connecting…";
    if (workbookError) return workbookError;
    if (!health.ok) {
      if (health.tableauSignInOk === false && health.tableauHint) return health.tableauHint;
      if (!health.hasOpenAi) return "Set OPENAI_API_KEY on the server";
      return "Set Tableau Connected App or PAT on the server";
    }
    if (selectedWorkbook && selectedDatasources.length > 0) {
      const n = selectedDatasources.length;
      const who = tableauUsername ? ` · ${tableauUsername}` : "";
      return `Connected · ${n} datasource${n === 1 ? "" : "s"}${who}`;
    }
    if (selectedWorkbook) {
      return tableauUsername ? `Connected · ${tableauUsername}` : "Connected";
    }
    return "Connected";
  })();

  const statusClass =
    health === null || workbookLoading || (needsUsername && !hasUsername)
      ? "status-pill--loading"
      : health?.healthError || workbookError || !health?.ok
        ? "status-pill--error"
        : "status-pill--ok";

  return (
    <div className="app-shell app-shell--extension">
      <div className="app app-extension">
        <header className="app-header">
          <div className="header-top">
            <div className="brand">
              <BrandMark />
              <div className="brand-text">
                <h1>Nunomics-ai</h1>
              </div>
            </div>
            <div className="header-actions">
              {messages.length > 0 && (
                <button type="button" className="btn-ghost" onClick={clearChat} disabled={loading}>
                  Clear chat
                </button>
              )}
            </div>
          </div>

          <div className={`status-pill ${statusClass}`}>
            <span className="status-dot" />
            {statusMessage}
          </div>
        </header>

        {needsUsername && !hasUsername ? (
          <div className="user-link-card">
            <h2>Link Tableau user</h2>
            <p>
              Chat runs as your Tableau account (Connected App). Enter the same username you use to
              sign in — for example <code>demoAdmin</code> or <code>local\demoAdmin</code>.
            </p>
            <div className="user-link-row">
              <input
                type="text"
                value={usernameDraft}
                onChange={(e) => setUsernameDraft(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    void linkUsername();
                  }
                }}
                placeholder="Tableau username"
                aria-label="Tableau username"
                disabled={linkingUser}
                autoComplete="username"
              />
              <button type="button" className="btn-send" onClick={() => void linkUsername()} disabled={linkingUser}>
                {linkingUser ? "…" : "Continue"}
              </button>
            </div>
            {linkError && (
              <p className="composer-error" role="alert">
                {linkError}
              </p>
            )}
          </div>
        ) : (
          <>
            <main className="messages" role="log" aria-live="polite" aria-relevant="additions">
              {messages.length === 0 &&
                !loading &&
                !selectedWorkbook &&
                health?.ok &&
                !workbookLoading && (
                  <div className="empty-state-card">
                    <p>{workbookError ? "Could not connect to this dashboard." : "Connecting…"}</p>
                  </div>
                )}

              {messages.map((m, i) => (
                <div
                  key={i}
                  className={`msg-block msg-block--${m.role}`}
                  style={{ animationDelay: `${Math.min(i, 8) * 40}ms` }}
                >
                  <div className="msg-avatar" aria-hidden="true">
                    {m.role === "user" ? "You" : "AI"}
                  </div>
                  <div className="msg-content">
                    <div
                      className={`msg msg--${m.role}${error && i === messages.length - 1 && m.role === "assistant" ? " msg--error" : ""}`}
                    >
                      {m.content}
                    </div>
                  </div>
                </div>
              ))}

              {loading && (
                <div className="msg-block msg-block--assistant">
                  <div className="msg-avatar" aria-hidden="true">
                    AI
                  </div>
                  <div className="msg msg--assistant msg--loading">
                    <span className="typing-dots" aria-hidden="true">
                      <span />
                      <span />
                      <span />
                    </span>
                    Thinking…
                  </div>
                </div>
              )}
              <div ref={bottomRef} />
            </main>

            {error && messages.length === 0 && (
              <p className="composer-error" role="alert">
                {error}
              </p>
            )}

            <footer className="composer">
              <div className="composer-inner">
                <textarea
                  ref={textareaRef}
                  value={input}
                  onChange={(e) => setInput(e.target.value)}
                  onKeyDown={onKeyDown}
                  placeholder={
                    workbookLoading
                      ? "Connecting…"
                      : !health?.ok
                        ? "Connecting…"
                        : !selectedWorkbook
                          ? "Connecting…"
                          : "Ask a question…"
                  }
                  rows={1}
                  disabled={loading || workbookLoading}
                  aria-label="Message"
                />
                <button
                  type="button"
                  className="btn-send"
                  onClick={() => void send()}
                  disabled={!canSend}
                  aria-label="Send message"
                >
                  <svg viewBox="0 0 24 24" fill="none" aria-hidden="true">
                    <path
                      d="M5 12h14M13 6l6 6-6 6"
                      stroke="currentColor"
                      strokeWidth="2"
                      strokeLinecap="round"
                      strokeLinejoin="round"
                    />
                  </svg>
                </button>
              </div>
              <p className="composer-hint">
                <kbd>Enter</kbd> to send · <kbd>Shift</kbd>+<kbd>Enter</kbd> for new line
              </p>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}
