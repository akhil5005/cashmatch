import { useCallback, useEffect, useState } from "react";
import { api, pct } from "../lib/api.js";

/**
 * What the automation is worth, with the assumptions exposed.
 *
 * The inputs are editable on purpose. A business case built on someone
 * else's averages is not a business case, and a client who can change the
 * numbers and watch the answer move trusts the answer far more than one
 * handed a single figure.
 */
const FIELDS = [
  {
    key: "monthly_payments",
    label: "Payments a month",
    step: 50,
    min: 1,
    hint: "your volume",
  },
  {
    key: "minutes_per_payment",
    label: "Minutes to match by hand",
    step: 0.5,
    min: 0.5,
    hint: "start to finish",
  },
  {
    key: "review_minutes",
    label: "Minutes to clear a review item",
    step: 0.5,
    min: 0,
    hint: "the suggestion is already on screen",
  },
  {
    key: "hourly_cost",
    label: "Analyst cost, ₹ per hour",
    step: 50,
    min: 1,
    hint: "fully loaded",
  },
  {
    key: "error_hours",
    label: "Hours to unwind one error",
    step: 0.5,
    min: 0,
    hint: "reversal, approval, the customer call",
  },
];

export function RoiPanel({ defaultVolume, onError }) {
  const [inputs, setInputs] = useState({
    monthly_payments: defaultVolume || 800,
    minutes_per_payment: 4,
    review_minutes: 2,
    hourly_cost: 600,
    error_hours: 3,
  });
  const [result, setResult] = useState(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    api
      .roi(inputs)
      .then(setResult)
      .catch((error) => onError(error.message))
      .finally(() => setLoading(false));
  }, [inputs, onError]);

  useEffect(() => {
    const timer = setTimeout(load, 250);
    return () => clearTimeout(timer);
  }, [load]);

  return (
    <section className="space-y-4" aria-label="Return on investment">
      <div className="grid gap-4 lg:grid-cols-[minmax(0,20rem)_minmax(0,1fr)]">
        <div className="space-y-3 rounded border border-slate-200 bg-white p-4 shadow-sm">
          <h2 className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
            Your assumptions
          </h2>
          <p className="text-xs leading-snug text-ink-faint">
            Every default below is a starting point, not a benchmark. Replace them with
            your own figures and the answer changes with them.
          </p>

          {FIELDS.map((field) => (
            <label key={field.key} className="block">
              <span className="text-xs font-medium">{field.label}</span>
              <input
                type="number"
                inputMode="decimal"
                min={field.min}
                step={field.step}
                value={inputs[field.key]}
                onChange={(event) =>
                  setInputs((current) => ({
                    ...current,
                    [field.key]: Number(event.target.value),
                  }))
                }
                className="mt-1 w-full rounded border border-slate-300 px-2 py-1.5 text-right font-mono text-sm tnum focus:border-petrol focus:outline-none focus:ring-1 focus:ring-petrol"
              />
              <span className="text-[11px] text-ink-faint">{field.hint}</span>
            </label>
          ))}
        </div>

        <div className="space-y-4">
          {result ? (
            <Outcome result={result} loading={loading} />
          ) : (
            <div className="h-64 animate-pulse rounded border border-slate-200 bg-white" />
          )}
        </div>
      </div>
    </section>
  );
}

function Outcome({ result, loading }) {
  const { hours, money, measured } = result;

  return (
    <div className={loading ? "opacity-60 transition" : "transition"}>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-3">
        <Tile
          label="Hours saved"
          value={`${hours.saved.toLocaleString("en-IN")}`}
          unit="/ month"
          note={`${pct(hours.effort_reduction, 0)} of the manual effort · ${hours.analyst_days_saved} analyst-days`}
          tone="text-emerald-700"
        />
        <Tile
          label="Net saving"
          value={money.net_saving.display}
          unit="/ month"
          note={`${money.net_saving_per_year.display} a year`}
          tone="text-emerald-700"
        />
        <Tile
          label="Cost of errors"
          value={money.expected_error_cost.display}
          unit="/ month"
          note={
            measured.precision === null
              ? "precision not measurable here"
              : `at ${pct(measured.precision, 2)} precision`
          }
          tone={money.expected_error_cost.paise > 0 ? "text-rose-700" : "text-ink-faint"}
        />
      </div>

      <div className="mt-3 overflow-x-auto rounded border border-slate-200 bg-white shadow-sm">
        <table className="w-full min-w-[26rem] text-sm">
          <tbody className="divide-y divide-slate-50">
            <Row label="Effort today" value={`${hours.baseline} hours`} />
            <Row label="Effort with CashMatch" value={`${hours.remaining} hours`} />
            <Row label="Cost today" value={money.baseline_cost.display} />
            <Row label="Cost with CashMatch" value={money.remaining_cost.display} />
            <Row label="Gross saving" value={money.gross_saving.display} />
            <Row
              label="Less: expected cost of wrong auto-matches"
              value={money.expected_error_cost.display}
              tone="text-rose-700"
            />
            <Row label="Net saving" value={money.net_saving.display} strong />
          </tbody>
        </table>
      </div>

      <div className="mt-3 rounded border-l-2 border-petrol bg-petrol-light/40 px-3 py-2">
        <p className="text-xs leading-relaxed">
          Measured at {pct(measured.auto_match_rate)} auto-match,{" "}
          {pct(measured.review_rate)} review, {pct(measured.unapplied_rate)} unapplied.{" "}
          {result.note}
        </p>
      </div>
    </div>
  );
}

function Tile({ label, value, unit, note, tone }) {
  return (
    <div className="rounded border border-slate-200 bg-white p-4 shadow-sm">
      <div className="font-mono text-[10px] uppercase tracking-[0.14em] text-ink-faint">
        {label}
      </div>
      <div className={`mt-2 font-mono text-2xl tnum ${tone}`}>
        {value}
        <span className="ml-1 text-sm text-ink-faint">{unit}</span>
      </div>
      <div className="mt-1 text-xs leading-snug text-ink-faint">{note}</div>
    </div>
  );
}

function Row({ label, value, strong, tone }) {
  return (
    <tr className={strong ? "bg-slate-50" : undefined}>
      <td className={`px-4 py-2 ${strong ? "font-semibold" : ""}`}>{label}</td>
      <td
        className={`px-4 py-2 text-right font-mono tnum ${strong ? "font-semibold" : ""} ${tone || ""}`}
      >
        {value}
      </td>
    </tr>
  );
}
