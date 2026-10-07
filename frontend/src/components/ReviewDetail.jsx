import { useEffect, useState } from "react";
import { DECISION_STYLES, api, pct } from "../lib/api.js";

/**
 * One review item, with everything needed to judge it on one screen.
 *
 * The explanation is the product. A reviewer who has to reconstruct why the
 * engine proposed this spends as long as they would have spent matching it
 * by hand, which erases the whole saving of routing to review. So the
 * signals, the penalties and the reasoning trail are all here — including
 * the signals that did *not* fire, because "we looked for a reference and
 * found none" is information.
 */
export function ReviewDetail({ result, onActed, onError }) {
  const [busy, setBusy] = useState(null);
  const [reviewer, setReviewer] = useState(() => localStorage.getItem("cashmatch.reviewer") || "");
  const [note, setNote] = useState("");
  const [editing, setEditing] = useState(false);

  useEffect(() => {
    setEditing(false);
    setNote("");
  }, [result?.id]);

  if (!result) {
    return (
      <div className="flex h-full min-h-[18rem] items-center justify-center rounded border border-dashed border-slate-300 bg-white p-8 text-center">
        <p className="text-sm text-ink-faint">
          Select a payment from the queue to see why the engine decided what it did.
        </p>
      </div>
    );
  }

  const reviewed = Boolean(result.reviewed_at);
  const style = DECISION_STYLES[result.decision] || DECISION_STYLES.unapplied;

  async function act(kind, payload) {
    if (!reviewer.trim()) {
      onError("Enter your name before approving or rejecting — every decision is attributed.");
      return;
    }
    localStorage.setItem("cashmatch.reviewer", reviewer.trim());
    setBusy(kind);
    try {
      const body = { reviewed_by: reviewer.trim(), note: note.trim() || null, ...payload };
      const response = await api[kind](result.id, body);
      onActed(response);
      setEditing(false);
    } catch (error) {
      onError(error.message);
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-4">
      <header className="rounded border border-slate-200 bg-white p-4 shadow-sm">
        <div className="flex flex-wrap items-start justify-between gap-3">
          <div className="min-w-0">
            <div className="font-mono text-xs text-ink-faint">{result.statement_ref}</div>
            <h2 className="mt-1 truncate text-lg font-semibold">{result.payer_name}</h2>
            {result.customer_name && (
              <p className="text-sm text-ink-soft">matched to {result.customer_name}</p>
            )}
          </div>
          <div className="text-right">
            <div className="font-mono text-2xl font-semibold tnum">{result.amount.display}</div>
            <div className="font-mono text-[11px] text-ink-faint">{result.value_date}</div>
          </div>
        </div>

        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className={`rounded border px-2 py-0.5 font-mono text-[11px] ${style.chip}`}>
            {style.label}
          </span>
          <span className="rounded border border-slate-300 bg-slate-50 px-2 py-0.5 font-mono text-[11px]">
            {pct(result.confidence, 1)} confidence
          </span>
          <span className="rounded border border-slate-300 bg-slate-50 px-2 py-0.5 font-mono text-[11px]">
            {result.strategy}
          </span>
          {reviewed && (
            <span className="rounded border border-sky-300 bg-sky-50 px-2 py-0.5 font-mono text-[11px] text-sky-800">
              {result.review_action} by {result.reviewed_by}
            </span>
          )}
        </div>

        <p className="mt-3 border-l-2 border-petrol bg-petrol-light/40 px-3 py-2 text-sm leading-relaxed">
          {result.reason_text}
        </p>

        {result.narration && (
          <p className="mt-2 break-words font-mono text-[11px] text-ink-faint">
            bank narration: {result.narration}
          </p>
        )}
      </header>

      <Allocations result={result} />
      <Signals result={result} />

      {result.remittance_text && <Advice text={result.remittance_text} />}

      {!reviewed && (
        <Actions
          result={result}
          reviewer={reviewer}
          setReviewer={setReviewer}
          note={note}
          setNote={setNote}
          busy={busy}
          act={act}
          editing={editing}
          setEditing={setEditing}
          onError={onError}
        />
      )}

      {reviewed && (
        <p className="rounded border border-slate-200 bg-slate-50 p-3 text-xs text-ink-soft">
          This item was {result.review_action} by {result.reviewed_by}. Re-run matching to
          produce a fresh suggestion if it needs revisiting.
        </p>
      )}
    </div>
  );
}

function Allocations({ result }) {
  if (result.allocations.length === 0) {
    return (
      <section className="rounded border border-slate-200 bg-white p-4 shadow-sm">
        <SectionTitle>Proposed allocation</SectionTitle>
        <p className="mt-2 text-sm text-ink-soft">
          No invoices proposed. This payment would sit as unapplied cash on the customer's
          account.
        </p>
      </section>
    );
  }

  return (
    <section className="rounded border border-slate-200 bg-white shadow-sm">
      <div className="px-4 pt-4">
        <SectionTitle>Proposed allocation</SectionTitle>
      </div>
      <div className="mt-2 overflow-x-auto">
        <table className="w-full min-w-[32rem] text-sm">
          <thead>
            <tr className="border-y border-slate-100 bg-slate-50 text-left font-mono text-[10px] uppercase tracking-wider text-ink-faint">
              <th className="px-4 py-2">Invoice</th>
              <th className="px-4 py-2">Due</th>
              <th className="px-4 py-2 text-right">Open</th>
              <th className="px-4 py-2 text-right">Applied</th>
              <th className="px-4 py-2 text-right">Deduction</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-slate-50">
            {result.allocations.map((line) => (
              <tr key={line.invoice_id}>
                <td className="px-4 py-2 font-mono text-xs">{line.invoice_number}</td>
                <td className="px-4 py-2 font-mono text-xs text-ink-faint">{line.due_date}</td>
                <td className="px-4 py-2 text-right font-mono text-xs tnum text-ink-faint">
                  {line.open_amount?.display}
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs tnum">
                  {line.allocated.display}
                </td>
                <td className="px-4 py-2 text-right font-mono text-xs tnum">
                  {line.deduction.paise > 0 ? (
                    <span className="text-rose-700">
                      {line.deduction.display}
                      {line.deduction_reason && (
                        <span className="ml-1 text-ink-faint">({line.deduction_reason})</span>
                      )}
                    </span>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {result.unapplied_amount.paise > 0 && (
        <p className="border-t border-slate-100 px-4 py-2 text-xs text-rose-700">
          {result.unapplied_amount.display} of this payment could not be placed.
        </p>
      )}
    </section>
  );
}

function Signals({ result }) {
  return (
    <section className="rounded border border-slate-200 bg-white p-4 shadow-sm">
      <SectionTitle>Why — the signals</SectionTitle>

      <ul className="mt-3 space-y-2">
        {result.signals.map((signal) => (
          <li key={signal.name} className="flex gap-3">
            <span
              className={`mt-0.5 h-4 w-4 shrink-0 rounded-full border text-center font-mono text-[9px] leading-[14px] ${
                !signal.applicable
                  ? "border-slate-300 text-slate-400"
                  : signal.fired
                    ? "border-emerald-500 bg-emerald-500 text-white"
                    : "border-rose-400 text-rose-500"
              }`}
              aria-hidden="true"
            >
              {!signal.applicable ? "–" : signal.fired ? "✓" : "✕"}
            </span>
            <div className="min-w-0 flex-1">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <span className="font-mono text-xs">{signal.name}</span>
                <span className="font-mono text-[11px] tnum text-ink-faint">
                  {signal.applicable
                    ? `${signal.value.toFixed(2)} × ${signal.weight.toFixed(2)} = ${signal.contribution.toFixed(3)}`
                    : "not applicable"}
                </span>
              </div>
              <p className="text-xs leading-snug text-ink-soft">{signal.detail}</p>
            </div>
          </li>
        ))}
      </ul>

      {result.penalties.length > 0 && (
        <div className="mt-4 space-y-2 border-t border-slate-100 pt-3">
          {result.penalties.map((penalty) => (
            <div key={penalty.name} className="rounded border-l-2 border-amber-400 bg-amber-50/60 px-3 py-2">
              <div className="font-mono text-[11px] text-amber-900">
                {penalty.name} × {penalty.multiplier.toFixed(2)}
              </div>
              <p className="text-xs leading-snug text-ink-soft">{penalty.detail}</p>
            </div>
          ))}
        </div>
      )}

      <p className="mt-3 border-t border-slate-100 pt-2 font-mono text-[11px] tnum text-ink-faint">
        base {result.base_score.toFixed(3)} ÷ applicable weight{" "}
        {result.applicable_weight.toFixed(2)} → confidence {pct(result.confidence, 1)}
        {result.excluded_signals.length > 0 &&
          ` · excluded: ${result.excluded_signals.join(", ")}`}
      </p>

      <details className="mt-3">
        <summary className="cursor-pointer font-mono text-[11px] text-petrol">
          Show the full matching trail ({result.trail.length} steps)
        </summary>
        <ul className="mt-2 space-y-1">
          {result.trail.map((step, index) => (
            <li key={index} className="flex gap-2 text-xs">
              <span className={step.fired ? "text-emerald-600" : "text-ink-faint"}>
                {step.fired ? "✓" : "–"}
              </span>
              <span className="font-mono text-[11px] text-ink-faint">{step.name}</span>
              <span className="flex-1 text-ink-soft">{step.detail}</span>
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

function Advice({ text }) {
  return (
    <section className="rounded border border-slate-200 bg-white p-4 shadow-sm">
      <SectionTitle>What the customer sent</SectionTitle>
      <pre className="mt-2 max-h-56 overflow-auto whitespace-pre-wrap break-words rounded bg-slate-50 p-3 font-mono text-[11px] leading-relaxed text-ink-soft">
        {text}
      </pre>
    </section>
  );
}

function Actions({
  result,
  reviewer,
  setReviewer,
  note,
  setNote,
  busy,
  act,
  editing,
  setEditing,
  onError,
}) {
  return (
    <section className="rounded border border-slate-200 bg-white p-4 shadow-sm">
      <SectionTitle>Your decision</SectionTitle>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            Your name
          </span>
          <input
            value={reviewer}
            onChange={(event) => setReviewer(event.target.value)}
            placeholder="e.g. akhil"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
          />
        </label>
        <label className="block">
          <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
            Note (optional)
          </span>
          <input
            value={note}
            onChange={(event) => setNote(event.target.value)}
            placeholder="why you decided this"
            className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
          />
        </label>
      </div>

      <div className="mt-3 flex flex-wrap gap-2">
        <button
          type="button"
          disabled={busy !== null || result.allocations.length === 0}
          onClick={() => act("approve", { post_cash: true })}
          className="rounded bg-emerald-700 px-4 py-2 text-sm font-medium text-white transition hover:bg-emerald-800 focus:outline-none focus-visible:ring-2 focus-visible:ring-emerald-700 focus-visible:ring-offset-2 disabled:opacity-40"
        >
          {busy === "approve" ? "Applying…" : "Approve & apply"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => act("reject", {})}
          className="rounded border border-rose-300 px-4 py-2 text-sm font-medium text-rose-800 transition hover:bg-rose-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 focus-visible:ring-offset-2 disabled:opacity-40"
        >
          {busy === "reject" ? "Rejecting…" : "Reject"}
        </button>
        <button
          type="button"
          disabled={busy !== null}
          onClick={() => setEditing((value) => !value)}
          className="rounded border border-slate-300 px-4 py-2 text-sm font-medium transition hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol focus-visible:ring-offset-2 disabled:opacity-40"
        >
          {editing ? "Cancel edit" : "Choose invoices myself"}
        </button>
      </div>

      {result.allocations.length === 0 && (
        <p className="mt-2 text-xs text-ink-faint">
          There is nothing to approve — use “Choose invoices myself” to allocate this payment.
        </p>
      )}

      {editing && (
        <Reassign
          result={result}
          busy={busy}
          onSubmit={(allocations) => act("reassign", { allocations, post_cash: true })}
          onError={onError}
        />
      )}
    </section>
  );
}

function Reassign({ result, busy, onSubmit, onError }) {
  const [search, setSearch] = useState("");
  const [invoices, setInvoices] = useState([]);
  const [lines, setLines] = useState([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    api
      .invoices({ search: search || undefined, limit: 25 })
      .then((rows) => !cancelled && setInvoices(rows))
      .catch((error) => !cancelled && onError(error.message))
      .finally(() => !cancelled && setLoading(false));
    return () => {
      cancelled = true;
    };
  }, [search, onError]);

  function addLine(invoice) {
    if (lines.some((line) => line.invoice_id === invoice.id)) return;
    setLines((current) => [
      ...current,
      {
        invoice_id: invoice.id,
        invoice_number: invoice.invoice_number,
        open: invoice.open_amount.display,
        allocated_amount: (invoice.open_amount.paise / 100).toFixed(2),
        deduction_amount: "0",
      },
    ]);
  }

  const total = lines.reduce((sum, line) => sum + (Number(line.allocated_amount) || 0), 0);
  const payment = result.amount.paise / 100;
  const over = total > payment + 0.001;

  return (
    <div className="mt-4 space-y-3 rounded border border-slate-200 bg-slate-50 p-3">
      <label className="block">
        <span className="font-mono text-[10px] uppercase tracking-wider text-ink-faint">
          Find an open invoice
        </span>
        <input
          value={search}
          onChange={(event) => setSearch(event.target.value)}
          placeholder="INV-00042"
          className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-sm focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
        />
      </label>

      <div className="max-h-40 overflow-y-auto rounded border border-slate-200 bg-white">
        {loading && <p className="p-3 text-xs text-ink-faint">Loading…</p>}
        {!loading && invoices.length === 0 && (
          <p className="p-3 text-xs text-ink-faint">No open invoices match.</p>
        )}
        <ul className="divide-y divide-slate-50">
          {invoices.map((invoice) => (
            <li key={invoice.id}>
              <button
                type="button"
                onClick={() => addLine(invoice)}
                className="flex w-full items-baseline justify-between gap-2 px-3 py-2 text-left hover:bg-slate-50 focus:outline-none focus-visible:ring-2 focus-visible:ring-inset focus-visible:ring-petrol"
              >
                <span className="min-w-0">
                  <span className="font-mono text-xs">{invoice.invoice_number}</span>
                  <span className="ml-2 truncate text-xs text-ink-faint">
                    {invoice.customer_name}
                  </span>
                </span>
                <span className="shrink-0 font-mono text-xs tnum">
                  {invoice.open_amount.display}
                </span>
              </button>
            </li>
          ))}
        </ul>
      </div>

      {lines.length > 0 && (
        <ul className="space-y-2">
          {lines.map((line, index) => (
            <li key={line.invoice_id} className="flex flex-wrap items-center gap-2">
              <span className="font-mono text-xs">{line.invoice_number}</span>
              <span className="font-mono text-[10px] text-ink-faint">open {line.open}</span>
              <input
                value={line.allocated_amount}
                onChange={(event) =>
                  setLines((current) =>
                    current.map((item, position) =>
                      position === index
                        ? { ...item, allocated_amount: event.target.value }
                        : item,
                    ),
                  )
                }
                inputMode="decimal"
                aria-label={`Amount to apply to ${line.invoice_number}`}
                className="w-28 rounded border border-slate-300 px-2 py-1 text-right font-mono text-xs tnum focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
              />
              <button
                type="button"
                onClick={() =>
                  setLines((current) => current.filter((_, position) => position !== index))
                }
                className="font-mono text-[11px] text-rose-700 hover:underline"
              >
                remove
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-200 pt-2">
        <span
          className={`font-mono text-[11px] tnum ${over ? "text-rose-700" : "text-ink-faint"}`}
        >
          allocating ₹{total.toFixed(2)} of {result.amount.display}
          {over && " — more than arrived"}
        </span>
        <button
          type="button"
          disabled={busy !== null || lines.length === 0 || over}
          onClick={() =>
            onSubmit(
              lines.map((line) => ({
                invoice_id: line.invoice_id,
                allocated_amount: line.allocated_amount,
                deduction_amount: line.deduction_amount,
              })),
            )
          }
          className="rounded bg-petrol px-4 py-2 text-sm font-medium text-white transition hover:bg-petrol/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-petrol focus-visible:ring-offset-2 disabled:opacity-40"
        >
          {busy === "reassign" ? "Applying…" : "Apply this allocation"}
        </button>
      </div>
    </div>
  );
}

function SectionTitle({ children }) {
  return (
    <h3 className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
      {children}
    </h3>
  );
}
