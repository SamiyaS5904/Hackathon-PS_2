// Thin client for the OfferPilot API (api/main.py). Every screen gets its data through these functions.

export class ApiError extends Error {
  constructor(detail) {
    super(typeof detail === "string" ? detail : JSON.stringify(detail));
    this.detail = detail;
  }
}

async function request(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
    body: options.body ? JSON.stringify(options.body) : undefined,
  });
  const data = await res.json().catch(() => null);
  if (!res.ok) throw new ApiError(data?.detail ?? res.statusText);
  return data;
}

const query = (params) => {
  const q = new URLSearchParams(Object.entries(params).filter(([, v]) => v !== undefined && v !== ""));
  return q.toString() ? `?${q}` : "";
};

export const api = {
  health: () => request("/health"),
  dayPlan: () => request("/day-plan"),
  setDayPlan: (date, limit) => request("/day-plan", { method: "PUT", body: { date, limit } }),
  overview: () => request("/overview"),
  decisions: (params = {}) => request(`/decisions${query(params)}`),
  decision: (id) => request(`/decisions/${id}`),
  customers: (search) => request(`/customers${query({ search, limit: 25 })}`),
  suggested: (refresh = false) => request(`/customers/suggested${refresh ? "?refresh=true" : ""}`),
  customer: (id) => request(`/customers/${id}`),
  runAgent: (id, useAgent = true) => request(`/customers/${id}/run`, { method: "POST", body: { use_agent: useAgent } }),
  fireEvent: (id, body) => request(`/customers/${id}/events`, { method: "POST", body }),
  testers: () => request("/testers"),
  addTester: (username, customerId) => request("/testers", { method: "POST", body: { username, customer_id: customerId } }),
  relink: (username, customerId) =>
    request(`/testers/${encodeURIComponent(username)}/relink`, { method: "POST", body: { customer_id: customerId } }),
  rules: () => request("/rules"),
  saveRules: (changes, changedBy, note) =>
    request("/rules", { method: "PUT", body: { changes, changed_by: changedBy, note } }),
  runFollowups: () => request("/followups/run", { method: "POST" }),
};
