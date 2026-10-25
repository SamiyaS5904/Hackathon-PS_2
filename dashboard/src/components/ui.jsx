// Small UI building blocks shared by every page: cards, stat tiles, meters, badges, buttons, drawer.
import { useEffect } from "react";
import { createPortal } from "react-dom";
import { AlertTriangle, CheckCircle2, Circle, Clock, Loader2, MinusCircle, Wrench, XCircle } from "lucide-react";
import { DECIDER, STATUS } from "../format";

export function Card({ title, subtitle, action, children, className = "" }) {
  return (
    <section className={`rounded-xl border border-hairline bg-surface ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 px-5 pt-4">
          <div>
            {title && <h2 className="text-sm font-semibold text-ink">{title}</h2>}
            {subtitle && <p className="mt-0.5 text-xs text-muted">{subtitle}</p>}
          </div>
          {action}
        </header>
      )}
      <div className="p-5">{children}</div>
    </section>
  );
}

export function Stat({ label, value, sub }) {
  return (
    <div className="rounded-xl border border-hairline bg-surface px-5 py-4">
      <div className="text-xs text-muted">{label}</div>
      <div className="mt-1 text-2xl font-semibold text-ink">{value}</div>
      {sub && <div className="mt-1 text-xs text-ink2">{sub}</div>}
    </div>
  );
}

// Meter: the fill carries severity (accent -> warning -> critical); the track is a darker step behind it.
export function Meter({ value, limit, label, format = (v) => v }) {
  const ratio = limit ? Math.min(value / limit, 1) : 0;
  const color = ratio >= 1 ? "#d03b3b" : ratio >= 0.8 ? "#fab219" : "#3987e5";
  return (
    <div>
      <div className="flex justify-between text-xs">
        <span className="text-ink2">{label}</span>
        <span className="tabular text-ink">
          {format(value)} <span className="text-muted">/ {format(limit)}</span>
        </span>
      </div>
      <div className="mt-2 h-2 rounded-full bg-[#1c3556]">
        <div className="h-2 rounded-full transition-all" style={{ width: `${ratio * 100}%`, background: color }} />
      </div>
    </div>
  );
}

const TONES = {
  good: "bg-good/15 text-[#5fd35f]",
  accent: "bg-accent/15 text-[#86b6ef]",
  warning: "bg-warning/15 text-warning",
  serious: "bg-serious/15 text-serious",
  critical: "bg-critical/20 text-[#f08a8a]",
  neutral: "bg-raised text-ink2",
};

export function Badge({ tone = "neutral", children }) {
  return <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs ${TONES[tone]}`}>{children}</span>;
}

export function StatusBadge({ status }) {
  const s = STATUS[status] ?? { label: status, tone: "neutral" };
  return <Badge tone={s.tone}>{s.label}</Badge>;
}

// Identity is never color alone: a colored dot sits beside a text label.
export function DeciderBadge({ decider }) {
  const d = DECIDER[decider] ?? { label: decider, color: "#898781" };
  return (
    <span className="inline-flex items-center gap-1.5 text-xs text-ink2">
      <span className="h-2 w-2 rounded-full" style={{ background: d.color }} />
      {d.label}
    </span>
  );
}

// Rule check result: icon + label, never color alone.
export function RuleResult({ result }) {
  if (result === "passed") return <span className="inline-flex items-center gap-1 text-xs text-[#5fd35f]"><CheckCircle2 size={14} /> Passed</span>;
  if (result === "repaired") return <span className="inline-flex items-center gap-1 text-xs text-warning"><Wrench size={14} /> Repaired</span>;
  return <span className="inline-flex items-center gap-1 text-xs text-[#f08a8a]"><XCircle size={14} /> Failed</span>;
}

export function Button({ children, variant = "primary", loading, className = "", ...props }) {
  const styles = {
    primary: "bg-accent text-white hover:bg-[#2a78d6]",
    secondary: "border border-baseline bg-raised text-ink hover:border-muted",
    ghost: "text-ink2 hover:bg-raised hover:text-ink",
  };
  return (
    <button
      className={`inline-flex items-center justify-center gap-2 rounded-lg px-3.5 py-2 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-50 ${styles[variant]} ${className}`}
      disabled={loading || props.disabled}
      {...props}
    >
      {loading && <Loader2 size={15} className="animate-spin" />}
      {children}
    </button>
  );
}

export function Input({ className = "", ...props }) {
  return (
    <input
      className={`rounded-lg border border-baseline bg-raised px-3 py-2 text-sm text-ink placeholder:text-muted focus:border-accent focus:outline-none ${className}`}
      {...props}
    />
  );
}

export function Spinner({ label = "Loading" }) {
  return (
    <div className="flex items-center gap-2 p-6 text-sm text-muted">
      <Loader2 size={16} className="animate-spin" /> {label}…
    </div>
  );
}

export function Empty({ children }) {
  return <div className="rounded-lg border border-dashed border-hairline p-6 text-center text-sm text-muted">{children}</div>;
}

export function ErrorBox({ error }) {
  if (!error) return null;
  const detail = error.detail ?? error.message;
  const problems = detail?.problems;
  return (
    <div className="flex gap-2 rounded-lg border border-critical/40 bg-critical/10 p-3 text-sm text-[#f08a8a]">
      <AlertTriangle size={16} className="mt-0.5 shrink-0" />
      <div>{problems ? <ul className="list-disc pl-4">{problems.map((p) => <li key={p}>{p}</li>)}</ul> : String(detail)}</div>
    </div>
  );
}

// Rendered into <body> through a portal, so page layout (spacing, scroll position) can never shift it.
export function Drawer({ open, onClose, title, children }) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e) => e.key === "Escape" && onClose();
    document.body.style.overflow = "hidden";
    window.addEventListener("keydown", onKey);
    return () => { document.body.style.overflow = ""; window.removeEventListener("keydown", onKey); };
  }, [open, onClose]);
  if (!open) return null;
  return createPortal(
    <div className="fixed inset-0 z-40 flex justify-end">
      <div className="absolute inset-0 bg-black/60" onClick={onClose} />
      <aside className="relative z-50 h-full w-full max-w-2xl overflow-y-auto border-l border-hairline bg-page">
        <header className="sticky top-0 z-10 flex items-center justify-between border-b border-hairline bg-page/95 px-6 py-4 backdrop-blur">
          <h2 className="text-base font-semibold">{title}</h2>
          <button onClick={onClose} className="rounded-lg px-2 py-1 text-ink2 hover:bg-raised hover:text-ink" aria-label="Close">✕</button>
        </header>
        <div className="space-y-5 p-6">{children}</div>
      </aside>
    </div>,
    document.body,
  );
}

export const icons = { Circle, Clock, MinusCircle };
