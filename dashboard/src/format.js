// Formatting helpers: money in rupees, times in IST, labels for statuses and deciders.

export const inr = (n, digits = 0) =>
  n === null || n === undefined ? "–" : `₹${Number(n).toLocaleString("en-IN", { maximumFractionDigits: digits })}`;

export const pct = (p, digits = 0) => (p === null || p === undefined ? "–" : `${(p * 100).toFixed(digits)}%`);

// Timestamps from the API are UTC ("2026-10-25T11:53:25Z"); people see IST.
export const ist = (ts, withDate = true) =>
  ts
    ? new Date(ts).toLocaleString("en-IN", {
        timeZone: "Asia/Kolkata",
        ...(withDate ? { day: "numeric", month: "short" } : {}),
        hour: "2-digit",
        minute: "2-digit",
      })
    : "–";

export const STATUS = {
  sent: { label: "Sent on Telegram", tone: "good" },
  review: { label: "Draft: not sent yet", tone: "warning" },
  queued: { label: "Queued for Telegram", tone: "accent" },
  scheduled: { label: "Scheduled", tone: "accent" },
  would_send: { label: "Would send", tone: "neutral" },
  nothing: { label: "Nothing sent", tone: "neutral" },
  waiting: { label: "Waiting", tone: "warning" },
  bought_naturally: { label: "Bought on their own", tone: "good" },
  followed_up: { label: "Followed up", tone: "neutral" },
  blocked: { label: "Blocked by rules", tone: "critical" },
  cancelled: { label: "Cancelled", tone: "serious" },
  expired: { label: "Expired", tone: "neutral" },
};

export const DECIDER = {
  agent: { label: "AI agent", color: "#3987e5" },
  fallback: { label: "Free rules", color: "#d95926" },
  gatekeeper: { label: "Gatekeeper skip", color: "#199e70" },
};

export const TRIGGER = {
  cart_abandoned: "Cart left",
  product_interest: "Product interest",
  habit_break: "Habit break",
  points_expiring: "Points expiring",
  wallet_idle: "Idle wallet",
  followup: "Follow-up",
  manual: "Manual",
};
