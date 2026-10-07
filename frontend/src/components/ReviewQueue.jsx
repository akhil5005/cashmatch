import { DECISION_STYLES, pct } from "../lib/api.js";

/**
 * The queue. One row per payment, scannable in a column.
 *
 * Confidence is shown as a bar as well as a number: an analyst working a
 * backlog needs to see at a glance which items are marginal, and a column of
 * four-digit percentages does not give you that.
 */
export function ReviewQueue({ page, loading, selectedId, onSelect, onPage }) {
  if (loading && !page) return <QueueSkeleton />;

  if (page && page.items.length === 0) {
    return (
      <div className="rounded border border-slate-200 bg-white p-8 text-center">
        <p className="text-sm text-ink-soft">Nothing matches these filters.</p>
        <p className="mt-1 text-xs text-ink-faint">
          Try clearing the search box, or widening the decision filter.
        </p>
      </div>
    );
  }

  return (
    <div className="overflow-hidden rounded border border-slate-200 bg-white shadow-sm">
      <ul className="divide-y divide-slate-100">
        {page?.items.map((item) => {
          const style = DECISION_STYLES[item.decision] || DECISION_STYLES.unapplied;
          const selected = item.id === selectedId;
          return (
            <li key={item.id}>
              <button
                type="button"
                onClick={() => onSelect(item.id)}
                aria-current={selected ? "true" : undefined}
                className={`w-full px-4 py-3 text-left transition focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-petrol ${
                  selected ? "bg-petrol-light" : "hover:bg-slate-50"
                }`}
              >
                <div className="flex flex-wrap items-baseline justify-between gap-x-3 gap-y-1">
                  <span className="font-mono text-xs text-ink-faint">{item.statement_ref}</span>
                  <span className="font-mono text-sm font-semibold tnum">
                    {item.amount.display}
                  </span>
                </div>

                <div className="mt-1 truncate text-sm font-medium">{item.payer_name}</div>

                <div className="mt-1 flex flex-wrap items-center gap-2">
                  <span className={`rounded border px-1.5 py-0.5 font-mono text-[10px] ${style.chip}`}>
                    {style.label}
                  </span>
                  {item.reviewed_by && (
                    <span className="rounded border border-sky-300 bg-sky-50 px-1.5 py-0.5 font-mono text-[10px] text-sky-800">
                      {item.reviewed_by}
                    </span>
                  )}
                  {item.has_deduction && (
                    <span className="rounded border border-slate-300 bg-slate-50 px-1.5 py-0.5 font-mono text-[10px] text-slate-700">
                      claim
                    </span>
                  )}
                  <span className="font-mono text-[10px] text-ink-faint">
                    {item.invoice_count > 0
                      ? item.invoice_numbers.slice(0, 2).join(", ") +
                        (item.invoice_count > 2 ? ` +${item.invoice_count - 2}` : "")
                      : "no invoices proposed"}
                  </span>
                </div>

                <Confidence value={item.confidence} />
              </button>
            </li>
          );
        })}
      </ul>

      {page && (
        <div className="flex items-center justify-between border-t border-slate-100 px-4 py-2">
          <span className="font-mono text-[11px] text-ink-faint">
            {page.offset + 1}–{Math.min(page.offset + page.items.length, page.total)} of{" "}
            {page.total}
          </span>
          <div className="flex gap-2">
            <PageButton
              disabled={page.offset === 0}
              onClick={() => onPage(Math.max(0, page.offset - page.limit))}
            >
              Previous
            </PageButton>
            <PageButton
              disabled={page.offset + page.items.length >= page.total}
              onClick={() => onPage(page.offset + page.limit)}
            >
              Next
            </PageButton>
          </div>
        </div>
      )}
    </div>
  );
}

function Confidence({ value }) {
  // Green above the auto threshold, amber in the review band, rose below.
  const tone = value >= 0.9 ? "bg-emerald-500" : value >= 0.45 ? "bg-amber-500" : "bg-rose-500";
  return (
    <div className="mt-2 flex items-center gap-2">
      <div
        className="h-1 flex-1 overflow-hidden rounded bg-slate-200"
        role="meter"
        aria-valuenow={Math.round(value * 100)}
        aria-valuemin={0}
        aria-valuemax={100}
        aria-label="Confidence"
      >
        <div className={`h-full ${tone}`} style={{ width: `${Math.max(2, value * 100)}%` }} />
      </div>
      <span className="w-10 text-right font-mono text-[11px] tnum text-ink-faint">
        {pct(value, 0)}
      </span>
    </div>
  );
}

function PageButton({ children, ...props }) {
  return (
    <button
      type="button"
      {...props}
      className="rounded border border-slate-300 px-2 py-1 font-mono text-[11px] transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol disabled:opacity-40 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  );
}

function QueueSkeleton() {
  return (
    <div className="space-y-px overflow-hidden rounded border border-slate-200 bg-white">
      {[0, 1, 2, 3, 4, 5].map((index) => (
        <div key={index} className="h-24 animate-pulse bg-slate-50" />
      ))}
    </div>
  );
}
