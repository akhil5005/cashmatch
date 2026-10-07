import { DECISION_STYLES, compactInr, pct } from "../lib/api.js";

/**
 * The four numbers a controller looks at, plus the two that qualify them.
 *
 * Auto-match rate is shown next to precision deliberately. Either alone is
 * meaningless — a system that applies everything scores 100% on the first
 * and is useless — so the layout never lets you read one without the other.
 */
export function Dashboard({ metrics, onRunMatch, running }) {
  if (!metrics) return <TileSkeleton />;

  const tiles = [
    {
      label: "Auto-match rate",
      value: pct(metrics.auto_match_rate),
      note: `${metrics.decided - metrics.review_count - metrics.unapplied_count} of ${metrics.decided} payments`,
      tone: "text-emerald-700",
    },
    {
      label: "Match precision",
      value: metrics.precision === null ? "—" : pct(metrics.precision, 2),
      note: metrics.precision === null
        ? "No answer key — not measurable here"
        : metrics.precision_basis,
      tone: "text-petrol",
    },
    {
      label: "In review",
      value: String(metrics.review_count),
      note: "waiting for an analyst",
      tone: "text-amber-700",
    },
    {
      label: "Unapplied cash",
      value: compactInr(metrics.unapplied_value.paise),
      note: `${metrics.unapplied_count} payments on account`,
      tone: "text-rose-700",
    },
  ];

  return (
    <section aria-label="Key figures" className="space-y-4">
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
        {tiles.map((tile) => (
          <div
            key={tile.label}
            className="rounded border border-slate-200 bg-white p-4 shadow-sm"
          >
            <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
              {tile.label}
            </div>
            <div className={`mt-2 font-mono text-3xl tnum ${tile.tone}`}>{tile.value}</div>
            <div className="mt-1 text-xs leading-snug text-ink-faint">{tile.note}</div>
          </div>
        ))}
      </div>

      <div className="flex flex-col gap-3 rounded border border-slate-200 bg-white p-4 shadow-sm sm:flex-row sm:items-center sm:justify-between">
        <div className="flex flex-wrap gap-2">
          {metrics.by_decision.map((row) => {
            const style = DECISION_STYLES[row.decision] || DECISION_STYLES.unapplied;
            return (
              <span
                key={row.decision}
                className={`rounded border px-2 py-1 font-mono text-[11px] ${style.chip}`}
              >
                {style.label} {row.count} · {compactInr(row.value.paise)}
              </span>
            );
          })}
        </div>

        <button
          type="button"
          onClick={onRunMatch}
          disabled={running}
          className="shrink-0 rounded bg-petrol px-4 py-2 text-sm font-medium text-white transition hover:bg-petrol/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol focus-visible:ring-offset-2 disabled:opacity-50"
        >
          {running ? "Matching…" : "Run matching"}
        </button>
      </div>

      <p className="text-xs leading-relaxed text-ink-faint">
        {metrics.open_invoice_count.toLocaleString("en-IN")} open invoices worth{" "}
        {metrics.open_receivables.display} across {metrics.customers} customers.
        {metrics.reviewed_by_humans > 0 &&
          ` ${metrics.reviewed_by_humans} item(s) reviewed by a person.`}
      </p>
    </section>
  );
}

function TileSkeleton() {
  return (
    <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 xl:grid-cols-4">
      {[0, 1, 2, 3].map((index) => (
        <div
          key={index}
          className="h-28 animate-pulse rounded border border-slate-200 bg-white"
        />
      ))}
    </div>
  );
}
