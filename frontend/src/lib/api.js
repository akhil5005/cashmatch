/**
 * The only place that talks to the backend.
 *
 * Every amount arrives as `{ paise, display }`. The UI renders `display` and
 * never does arithmetic on money — the integer is authoritative and any
 * client-side maths on it would reintroduce exactly the float problem the
 * API is shaped to avoid.
 */

const BASE = "/api";

class ApiError extends Error {
  constructor(message, status, code) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.code = code;
  }
}

async function request(path, options = {}) {
  let response;
  try {
    response = await fetch(`${BASE}${path}`, {
      headers: options.body ? { "Content-Type": "application/json" } : {},
      ...options,
    });
  } catch {
    // A network failure is a different problem from a rejected request, and
    // the user needs to be told which one it is.
    throw new ApiError(
      "Could not reach the CashMatch API. Is the backend running?",
      0,
      "network",
    );
  }

  if (response.status === 204) return null;

  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }

  if (!response.ok) {
    const detail = body?.error?.message || body?.detail;
    throw new ApiError(
      detail || `The request failed with status ${response.status}.`,
      response.status,
      body?.error?.code || "http_error",
    );
  }

  return body;
}

function query(params) {
  const search = new URLSearchParams();
  Object.entries(params || {}).forEach(([key, value]) => {
    if (value !== undefined && value !== null && value !== "") {
      search.append(key, value);
    }
  });
  const text = search.toString();
  return text ? `?${text}` : "";
}

export const api = {
  metrics: () => request("/metrics"),

  roi: (assumptions) => request(`/roi${query(assumptions)}`),

  results: (filters) => request(`/results${query(filters)}`),

  result: (id) => request(`/results/${id}`),

  approve: (id, payload) =>
    request(`/results/${id}/approve`, { method: "POST", body: JSON.stringify(payload) }),

  reject: (id, payload) =>
    request(`/results/${id}/reject`, { method: "POST", body: JSON.stringify(payload) }),

  reassign: (id, payload) =>
    request(`/results/${id}/reassign`, { method: "POST", body: JSON.stringify(payload) }),

  invoices: (filters) => request(`/invoices${query(filters)}`),

  customers: (filters) => request(`/customers${query(filters)}`),

  runMatch: () => request("/pipeline/match", { method: "POST" }),

  upload: async (kind, file) => {
    const form = new FormData();
    form.append("file", file);
    const response = await fetch(`${BASE}/uploads/${kind}`, { method: "POST", body: form });
    const body = await response.json().catch(() => null);
    if (!response.ok) {
      throw new ApiError(
        body?.error?.message || `Upload failed with status ${response.status}.`,
        response.status,
        body?.error?.code || "upload_failed",
      );
    }
    return body;
  },
};

export { ApiError };

/** Percentages, rendered the way a finance screen expects them. */
export function pct(value, places = 1) {
  if (value === null || value === undefined) return "—";
  return `${(value * 100).toFixed(places)}%`;
}

/** Short money, for tiles where the full figure will not fit. */
export function compactInr(paise) {
  const rupees = paise / 100;
  if (Math.abs(rupees) >= 1e7) return `₹${(rupees / 1e7).toFixed(2)} cr`;
  if (Math.abs(rupees) >= 1e5) return `₹${(rupees / 1e5).toFixed(2)} L`;
  return `₹${rupees.toLocaleString("en-IN", { maximumFractionDigits: 0 })}`;
}

export const DECISION_STYLES = {
  auto_applied: { label: "Auto-applied", chip: "bg-emerald-50 text-emerald-800 border-emerald-300" },
  needs_review: { label: "Needs review", chip: "bg-amber-50 text-amber-900 border-amber-300" },
  unapplied: { label: "Unapplied", chip: "bg-rose-50 text-rose-800 border-rose-300" },
  manually_applied: { label: "Applied by human", chip: "bg-sky-50 text-sky-800 border-sky-300" },
  rejected: { label: "Rejected", chip: "bg-slate-100 text-slate-700 border-slate-300" },
};
