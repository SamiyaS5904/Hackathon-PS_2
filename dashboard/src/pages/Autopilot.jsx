// Autopilot: the home page. Six category bots send offers automatically through the day; this page shows, in plain
// words, what each bot is doing today and, for every offer, who it is for, why, the exact Telegram message and the
// money. No Send button needed: each planned offer runs at its time (the customer's usual shopping hour).
import { useEffect, useState } from "react";
import { Cpu, Gift, House, Pause, Play, Send, Shirt, ShoppingBasket, Sparkles, Zap } from "lucide-react";
import { api } from "../api";
import { inr, ist } from "../format";
import DecisionDrawer from "../components/DecisionDrawer";
import { Badge, Button, Card, ErrorBox, Spinner, StatusBadge } from "../components/ui";

const BOT_ICON = { electronics: Cpu, fashion: Shirt, beauty: Sparkles, home: House, grocery: ShoppingBasket, rewards: Gift };
const BOT_LABEL = { electronics: "Electronics", fashion: "Fashion", beauty: "Beauty", home: "Home", grocery: "Grocery", rewards: "Rewards" };

function BotTag({ bot }) {
  const Icon = BOT_ICON[bot] ?? Send;
  return <span className="inline-flex items-center gap-1.5 whitespace-nowrap text-xs text-ink2"><Icon size={13} className="text-accent" />{BOT_LABEL[bot] ?? bot}</span>;
}

function BotCard({ b, active, onClick }) {
  const Icon = BOT_ICON[b.key] ?? Send;
  return (
    <button onClick={onClick} className={`rounded-xl border p-4 text-left transition ${active ? "border-accent bg-accent/10" : "border-hairline bg-surface hover:border-baseline"}`}>
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2 text-sm font-semibold text-ink"><Icon size={16} className="text-accent" />{b.name}</div>
      </div>
      <div className="mt-3 flex items-baseline gap-1.5">
        <span className="text-2xl font-semibold text-ink tabular">{b.sent}</span>
        <span className="text-xs text-muted">sent of {b.planned} planned</span>
      </div>
      <div className="mt-1 text-xs text-ink2">Expected profit {inr(b.expected_profit_rupees)}</div>
      <div className="mt-1 text-xs text-muted">{b.next_at ? `Next at ${ist(b.next_at, false)}` : "Done for today"}</div>
      <div className="mt-2 text-[11px] text-muted">
        {b.username && <a href={b.link} target="_blank" rel="noreferrer" onClick={(e) => e.stopPropagation()} className="text-[#86b6ef] hover:underline">@{b.username}</a>}
        {" · "}{b.connected ? "connected" : "token not added yet (main bot sends)"}
      </div>
    </button>
  );
}

function Row({ r, onOpen }) {
  const planned = r.slot_status === "planned";
  return (
    <div onClick={() => r.decision_id && onOpen(r.decision_id)}
         className={`grid gap-3 border-t border-hairline py-3 sm:grid-cols-[72px_minmax(0,1fr)] ${r.decision_id ? "cursor-pointer hover:bg-raised/30" : ""}`}>
      <div className="text-sm tabular text-ink2">{ist(r.time, false)}</div>
      <div className="min-w-0 space-y-1.5">
        <div className="flex flex-wrap items-center gap-x-3 gap-y-1">
          <BotTag bot={r.bot} />
          <span className="text-sm text-ink">{r.name} <span className="text-muted">#{r.customer_id}</span></span>
          {r.group && <Badge>{r.group}</Badge>}
          {planned ? <Badge tone="accent">Planned</Badge> : <StatusBadge status={r.status} />}
          {r.offer_kind === "winback" && <Badge tone="accent">Win-back</Badge>}
        </div>
        <div className="text-xs text-muted">Reason: {r.reason}{r.decided_by && <> · decided by {r.decided_by === "agent" ? "AI" : "free rules"}</>}</div>
        {r.offer && (
          <div className="text-xs text-ink2">
            <span className="text-ink">{r.offer}</span>
            {r.expected_profit_rupees != null && <> · expected profit {inr(r.expected_profit_rupees)} · cost if used {inr(r.offer_cost_rupees)}</>}
          </div>
        )}
        {r.message && <div className="max-w-2xl rounded-2xl rounded-tl-sm bg-[#1c3556] px-3 py-2 text-sm leading-relaxed text-ink">{r.message}</div>}
        {r.why && <div className="max-w-2xl text-xs text-muted"><span className="text-ink2">Why:</span> {r.why}</div>}
      </div>
    </div>
  );
}

export default function Autopilot() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [filter, setFilter] = useState(null);
  const [busy, setBusy] = useState(null);
  const [openId, setOpenId] = useState(null);

  const load = () => api.autopilot().then(setData).catch(setError);
  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);

  const act = async (key, fn) => {
    setBusy(key);
    setError(null);
    try { await fn(); await load(); } catch (e) { setError(e); } finally { setBusy(null); }
  };

  if (error && !data) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const t = data.totals;
  const rows = data.rows.filter((r) => !filter || r.bot === filter || r.planned_bot === filter);
  const day = new Date(data.day).toLocaleDateString("en-IN", { weekday: "long", day: "numeric", month: "long" });

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Autopilot</h1>
          <p className="mt-1 text-sm text-muted">{day} · 6 category bots · each offer runs at the customer's usual shopping time; the AI decides and the rules check every one.</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Badge tone={data.enabled ? "good" : "warning"}>{data.enabled ? "Running" : "Paused"}</Badge>
          <Button variant="secondary" loading={busy === "toggle"} onClick={() => act("toggle", () => api.autopilotToggle(!data.enabled))}>
            {data.enabled ? <><Pause size={14} /> Pause</> : <><Play size={14} /> Resume</>}
          </Button>
          <Button loading={busy === "run"} onClick={() => act("run", api.autopilotRunNow)}><Zap size={14} /> Run next round now</Button>
        </div>
      </div>
      <ErrorBox error={error} />

      <div className="grid grid-cols-2 gap-3 sm:grid-cols-5">
        {[["Planned today", t.planned], ["Sent on Telegram", t.sent], ["Still to run", t.waiting], ["AI chose no offer", t.no_offer], ["Expected profit (sent)", inr(t.expected_profit_rupees)]].map(([label, value]) => (
          <div key={label} className="rounded-xl border border-hairline bg-surface px-4 py-3">
            <div className="text-xs text-muted">{label}</div>
            <div className="mt-1 text-xl font-semibold text-ink tabular">{value}</div>
          </div>
        ))}
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {data.bots.map((b) => <BotCard key={b.key} b={b} active={filter === b.key} onClick={() => setFilter(filter === b.key ? null : b.key)} />)}
      </div>

      <Card title={filter ? `Today's offers: ${BOT_LABEL[filter]}` : "Today's offers, in time order"}
            subtitle="Click an offer to see the AI's full reasoning and every rule check. Click a bot above to filter."
            action={filter && <button onClick={() => setFilter(null)} className="text-xs text-[#86b6ef] hover:underline">Show all</button>}>
        {rows.length === 0 ? <p className="text-sm text-muted">Nothing planned.</p> : rows.map((r) => <Row key={r.slot_id} r={r} onOpen={setOpenId} />)}
      </Card>

      <Card title="Customer groups" subtitle="Every customer is grouped from what they actually do (orders, gaps, carts, sales, points).">
        <div className="flex flex-wrap gap-2">
          {data.groups.map((g) => <Badge key={g.group}>{g.label}: {g.customers}</Badge>)}
        </div>
      </Card>
      <DecisionDrawer decisionId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
