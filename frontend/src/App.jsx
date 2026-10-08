import { useCallback, useEffect, useState } from "react";
import { Dashboard } from "./components/Dashboard.jsx";
import { ReviewDetail } from "./components/ReviewDetail.jsx";
import { ReviewQueue } from "./components/ReviewQueue.jsx";
import { RoiPanel } from "./components/RoiPanel.jsx";
import { api } from "./lib/api.js";

const DECISIONS = [
  { value: "needs_review", label: "Needs review" },
  { value: "auto_applied", label: "Auto-applied" },
  { value: "unapplied", label: "Unapplied" },
  { value: "manually_applied", label: "Applied by human" },
  { value: "", label: "All" },
];

export default function App() {
  // The review queue is where an analyst actually works, so it is what the
  // app opens on.
  const [filters, setFilters] = useState({ decision: "needs_review", search: "", offset: 0 });
  const [page, setPage] = useState(null);
  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [metrics, setMetrics] = useState(null);
  const [loading, setLoading] = useState(true);
  const [running, setRunning] = useState(false);
  const [banner, setBanner] = useState(null);
  const [tab, setTab] = useState("queue");

  const notify = useCallback((message, tone = "error") => {
    setBanner({ message, tone });
  }, []);

  const loadMetrics = useCallback(() => {
    api.metrics().then(setMetrics).catch((error) => notify(error.message));
  }, [notify]);

  const loadPage = useCallback(() => {
    setLoading(true);
    api
      .results({ ...filters, limit: 25 })
      .then(setPage)
      .catch((error) => notify(error.message))
      .finally(() => setLoading(false));
  }, [filters, notify]);

  useEffect(loadMetrics, [loadMetrics]);
  useEffect(loadPage, [loadPage]);

  useEffect(() => {
    if (selectedId === null) {
      setDetail(null);
      return;
    }
    let cancelled = false;
    api
      .result(selectedId)
      .then((body) => !cancelled && setDetail(body))
      .catch((error) => !cancelled && notify(error.message));
    return () => {
      cancelled = true;
    };
  }, [selectedId, notify]);

  function handleActed(response) {
    setDetail(response.result);
    notify(response.message, "success");
    loadPage();
    loadMetrics();
  }

  async function handleRunMatch() {
    setRunning(true);
    try {
      const body = await api.runMatch();
      notify(body.message, "success");
      loadPage();
      loadMetrics();
    } catch (error) {
      notify(error.message);
    } finally {
      setRunning(false);
    }
  }

  async function handleUpload(event) {
    const file = event.target.files?.[0];
    if (!file) return;
    const kind = file.name.toLowerCase().endsWith(".csv") ? "bank-statement" : "remittance";
    try {
      const body = await api.upload(kind, file);
      notify(body.message, "success");
      loadPage();
      loadMetrics();
    } catch (error) {
      notify(error.message);
    } finally {
      event.target.value = "";
    }
  }

  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center justify-between gap-3 px-4 py-3">
          <div>
            <h1 className="font-mono text-lg font-semibold tracking-tight">CashMatch</h1>
            <p className="text-xs text-ink-faint">Cash application review</p>
          </div>
          <label className="cursor-pointer rounded border border-slate-300 px-3 py-1.5 text-sm transition hover:bg-slate-50 focus-within:ring-2 focus-within:ring-petrol">
            Upload statement or advice
            <input
              type="file"
              accept=".csv,.txt,.pdf"
              onChange={handleUpload}
              className="sr-only"
            />
          </label>
        </div>
      </header>

      <main className="mx-auto max-w-7xl space-y-5 px-4 py-5">
        {banner && (
          <div
            role="status"
            className={`flex items-start justify-between gap-3 rounded border px-3 py-2 text-sm ${
              banner.tone === "success"
                ? "border-emerald-300 bg-emerald-50 text-emerald-900"
                : "border-rose-300 bg-rose-50 text-rose-900"
            }`}
          >
            <span className="min-w-0">{banner.message}</span>
            <button
              type="button"
              onClick={() => setBanner(null)}
              aria-label="Dismiss"
              className="shrink-0 font-mono text-xs opacity-60 hover:opacity-100"
            >
              ✕
            </button>
          </div>
        )}

        <Dashboard metrics={metrics} onRunMatch={handleRunMatch} running={running} />

        <nav className="flex gap-1 border-b border-slate-200" aria-label="Views">
          {[
            { id: "queue", label: "Review queue" },
            { id: "roi", label: "What it is worth" },
          ].map((view) => (
            <button
              key={view.id}
              type="button"
              onClick={() => setTab(view.id)}
              aria-current={tab === view.id ? "page" : undefined}
              className={`-mb-px border-b-2 px-3 py-2 text-sm transition focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol ${
                tab === view.id
                  ? "border-petrol font-medium text-petrol"
                  : "border-transparent text-ink-faint hover:text-ink"
              }`}
            >
              {view.label}
            </button>
          ))}
        </nav>

        {tab === "roi" && (
          <RoiPanel defaultVolume={metrics?.decided} onError={notify} />
        )}

        {tab === "queue" && (
        <section className="grid gap-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
          <div className="space-y-3">
            <div className="flex flex-wrap gap-2">
              {DECISIONS.map((option) => (
                <button
                  key={option.value || "all"}
                  type="button"
                  onClick={() =>
                    setFilters((current) => ({ ...current, decision: option.value, offset: 0 }))
                  }
                  aria-pressed={filters.decision === option.value}
                  className={`rounded border px-2.5 py-1 font-mono text-[11px] transition focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol ${
                    filters.decision === option.value
                      ? "border-petrol bg-petrol text-white"
                      : "border-slate-300 bg-white hover:bg-slate-50"
                  }`}
                >
                  {option.label}
                </button>
              ))}
            </div>

            <input
              value={filters.search}
              onChange={(event) =>
                setFilters((current) => ({ ...current, search: event.target.value, offset: 0 }))
              }
              placeholder="Search reference, payer or narration"
              aria-label="Search the queue"
              className="w-full rounded border border-slate-300 px-3 py-2 text-sm focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
            />

            <ReviewQueue
              page={page}
              loading={loading}
              selectedId={selectedId}
              onSelect={setSelectedId}
              onPage={(offset) => setFilters((current) => ({ ...current, offset }))}
            />
          </div>

          <ReviewDetail result={detail} onActed={handleActed} onError={notify} />
        </section>
        )}
      </main>

      <footer className="mx-auto max-w-7xl px-4 pb-8 pt-2">
        <p className="font-mono text-[11px] text-ink-faint">
          Every amount is an integer count of paise. Every decision carries the signals that
          produced it.
        </p>
      </footer>
    </div>
  );
}
