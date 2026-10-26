// Today: the first screen the marketing manager sees.
// Greeting + "Plan your day" (how many AI decisions to allow, and what that costs), today's numbers,
// decisions over the last 7 days, and the latest decisions (click one to see why).
import { useEffect, useState } from "react";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, BrainCircuit, Send } from "lucide-react";
import { api } from "../api";
import { DECIDER, inr, ist, TRIGGER } from "../format";
import DecisionDrawer from "../components/DecisionDrawer";
import { Button, Card, DeciderBadge, ErrorBox, Input, Meter, Spinner, Stat, StatusBadge } from "../components/ui";

function PlanCard({ plan, onSaved }) {
  const [today, setToday] = useState(plan.today.limit);
  const [tomorrow, setTomorrow] = useState(plan.tomorrow.limit);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const per = plan.today.estimate.per_decision_inr;

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.setDayPlan(plan.today.date, Number(today));
      await api.setDayPlan(plan.tomorrow.date, Number(tomorrow));
      onSaved();
    } catch (e) {
      setError(e);
    } finally {
      setSaving(false);
    }
  };

  const prettyDate = (d) => new Date(d).toLocaleDateString("en-IN", { weekday: "short", day: "numeric", month: "short" });
  const row = (label, value, set, date) => (
    <div className="flex items-center justify-between gap-4 rounded-lg bg-raised/60 px-4 py-3">
      <div className="whitespace-nowrap">
        <div className="text-sm text-ink">{label}</div>
        <div className="text-xs text-muted">{prettyDate(date)}</div>
      </div>
      <div className="flex items-center gap-3">
        <Input type="number" min={0} max={plan.ceiling} value={value} onChange={(e) => set(e.target.value)} className="w-20 text-right tabular" />
        <div className="w-24 text-right text-xs text-ink2">≈ {inr(Math.round(Number(value || 0) * per))}</div>
      </div>
    </div>
  );

  return (
    <Card
      title="Plan your day"
      subtitle={`How many AI decisions to allow. Beyond the plan, the free rules decide. Hard ceiling: ${plan.ceiling}/day.`}
    >
      <div className="space-y-2.5">
        {row("Today", today, setToday, plan.today.date)}
        {row("Tomorrow", tomorrow, setTomorrow, plan.tomorrow.date)}
      </div>
      <div className="mt-4">
        <Meter label="AI decisions used today" value={plan.today.used} limit={plan.today.limit} />
      </div>
      <div className="mt-4 flex items-center justify-between">
        <span className="text-xs text-muted">≈ {inr(per, 2)} per AI decision (average of recent real decisions)</span>
        <Button onClick={save} loading={saving}>Save plan</Button>
      </div>
      <div className="mt-3"><ErrorBox error={error} /></div>
    </Card>
  );
}

// Who gets which offer, when, through which channel, and what it should earn. Refreshes every 15 seconds.
function Schedule({ onOpen }) {
  const [data, setData] = useState(null);
  const [busyId, setBusyId] = useState(null);
  const load = () => api.outbox().then(setData).catch(() => {});
  useEffect(() => {
    load();
    const t = setInterval(load, 10000);
    return () => clearInterval(t);
  }, []);
  const act = async (e, fn, id) => {   // Send / Discard a draft without opening the drawer
    e.stopPropagation();
    setBusyId(id);
    try { await fn(id); } catch { /* the status in the list shows what happened */ } finally { setBusyId(null); load(); }
  };
  if (!data) return <Card title="Offer schedule"><Spinner /></Card>;
  const t = data.totals;
  return (
    <Card title="Offer schedule: who gets what, when"
          subtitle={`${t.offers} offers · ${t.on_telegram} sent or on the way · drafts wait for your Send · expected profit ${inr(t.expected_profit_rupees)} · cost if all used ${inr(t.offer_cost_rupees)}`}>
      {data.items.length === 0 ? <p className="text-sm text-muted">No offers yet. Open a customer and click "Make offer (AI)".</p> : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[640px] text-sm">
            <thead className="text-left text-xs text-muted">
              <tr><th className="py-2 pr-3">When (IST)</th><th className="py-2 pr-3">Customer</th><th className="py-2 pr-3">Offer</th><th className="py-2 pr-3">Goes to</th><th className="py-2 pr-3">Status</th><th className="py-2 pr-3 text-right">Exp. profit</th><th className="py-2"></th></tr>
            </thead>
            <tbody className="tabular">
              {data.items.map((d) => (
                <tr key={d.id} onClick={() => onOpen(d.id)} className="cursor-pointer border-t border-hairline hover:bg-raised/40">
                  <td className="whitespace-nowrap py-2 pr-3 text-ink2">{ist(d.goes_at)}</td>
                  <td className="py-2 pr-3 text-ink">{d.first_name} <span className="text-muted">#{d.customer_id}</span></td>
                  <td className="py-2 pr-3 text-ink2">{d.offer}{d.offer_kind === "winback" && <span className="ml-2 rounded-full border border-accent/50 px-2 py-0.5 text-[10px] text-[#86b6ef]">win-back</span>}</td>
                  <td className="whitespace-nowrap py-2 pr-3 text-ink2">{d.channel.startsWith("Telegram") && <Send size={12} className="mr-1 inline text-accent" />}{d.channel}</td>
                  <td className="py-2 pr-3"><StatusBadge status={d.status} /></td>
                  <td className="py-2 pr-3 text-right text-ink">{inr(d.expected_profit_rupees)}</td>
                  <td className="whitespace-nowrap py-2 text-right">
                    {d.status === "review" && <>
                      <Button className="px-2.5 py-1 text-xs" loading={busyId === d.id} onClick={(e) => act(e, api.sendDecision, d.id)}><Send size={12} /> Send</Button>
                      <button className="ml-2 text-xs text-muted hover:text-ink" onClick={(e) => act(e, api.discardDecision, d.id)}>Discard</button>
                    </>}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </Card>
  );
}

function WeekChart({ week }) {
  const data = week.map((d) => ({ ...d, day: new Date(d.date).toLocaleDateString("en-IN", { weekday: "short", day: "numeric" }) }));
  const series = ["agent", "fallback", "gatekeeper"];
  return (
    <ResponsiveContainer width="100%" height={240}>
      <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: -16 }}>
        <CartesianGrid vertical={false} stroke="#2c2c2a" />
        <XAxis dataKey="day" tick={{ fill: "#898781", fontSize: 12 }} axisLine={{ stroke: "#383835" }} tickLine={false} />
        <YAxis allowDecimals={false} tick={{ fill: "#898781", fontSize: 12 }} axisLine={false} tickLine={false} />
        <Tooltip
          cursor={{ fill: "rgba(255,255,255,0.04)" }}
          contentStyle={{ background: "#232322", border: "1px solid #383835", borderRadius: 8, fontSize: 12 }}
          labelStyle={{ color: "#f0efec" }}
          itemStyle={{ color: "#c3c2b7" }}
          formatter={(v, name) => [v, DECIDER[name]?.label ?? name]}
        />
        <Legend formatter={(name) => <span style={{ color: "#c3c2b7", fontSize: 12 }}>{DECIDER[name]?.label}</span>} iconType="circle" iconSize={8} />
        {series.map((s, i) => (
          <Bar key={s} dataKey={s} stackId="d" fill={DECIDER[s].color} barSize={24}
               stroke="#1a1a19" strokeWidth={2} radius={i === series.length - 1 ? [4, 4, 0, 0] : 0} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

export default function Today() {
  const [plan, setPlan] = useState(null);
  const [overview, setOverview] = useState(null);
  const [recent, setRecent] = useState([]);
  const [openId, setOpenId] = useState(null);
  const [error, setError] = useState(null);

  const load = () => {
    Promise.all([api.dayPlan(), api.overview(), api.decisions({ limit: 8 })])
      .then(([p, o, r]) => { setPlan(p); setOverview(o); setRecent(r); })
      .catch(setError);
  };
  useEffect(load, []);

  if (error) return <ErrorBox error={error} />;
  if (!plan || !overview) return <Spinner />;
  const t = overview.today;

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">{plan.greeting} 👋</h1>
        <p className="mt-1 text-sm text-muted">{plan.now_ist} IST · rules v{overview.rules_version}</p>
      </div>

      {overview.circuit_breaker && (
        <div className="flex items-center gap-3 rounded-xl border border-critical/40 bg-critical/10 p-4 text-sm text-[#f08a8a]">
          <AlertTriangle size={18} /> <span><b>Offers paused (circuit breaker):</b> {overview.circuit_breaker}</span>
        </div>
      )}

      <Schedule onOpen={setOpenId} />

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <PlanCard plan={plan} onSaved={load} />
        <div className="grid grid-cols-2 gap-4 sm:grid-cols-3">
          <Stat label="Decisions today" value={t.decisions}
                sub={`${t.by_decider.agent} AI · ${t.by_decider.fallback} rules · ${t.by_decider.gatekeeper} skipped`} />
          <Stat label="Offers going out" value={t.offers_out} sub={`${t.offers_delivered_telegram} delivered on Telegram`} />
          <Stat label="Expected profit" value={inr(t.expected_profit_rupees)} sub="from today's offers, vs sending nothing" />
          <Stat label="AI cost today" value={inr(t.ai_cost_inr, 2)} sub={`${plan.today.remaining} AI decisions left`} />
          <div className="col-span-2 rounded-xl border border-hairline bg-surface px-5 py-4">
            <Meter label="Offer budget used today" value={t.offer_budget.used_rupees} limit={t.offer_budget.limit_rupees} format={(v) => inr(v)} />
            <p className="mt-2 text-xs text-muted">Full cost if every offer is used. Set on the Rules page.</p>
          </div>
        </div>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.4fr)_minmax(0,1fr)]">
        <Card title="Decisions, last 7 days" subtitle="Who decided: the AI agent, the free rules, or a skip before any cost">
          <WeekChart week={overview.week} />
        </Card>
        <Card title="Prediction model" subtitle="Learned from past campaigns with random holdout groups">
          {overview.uplift_model ? (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-2 text-ink"><BrainCircuit size={18} className="text-accent" /> Uplift model active</div>
              <div className="grid grid-cols-2 gap-3 text-xs">
                <div className="rounded-lg bg-raised/60 p-3"><div className="text-muted">Trained on</div><div className="mt-1 text-base text-ink">{(overview.uplift_model.rows_control + overview.uplift_model.rows_treated).toLocaleString()} offers</div></div>
                <div className="rounded-lg bg-raised/60 p-3"><div className="text-muted">"Buys anyway" accuracy</div><div className="mt-1 text-base text-ink">AUC {overview.uplift_model.auc_control}</div></div>
              </div>
              <p className="text-xs leading-relaxed text-muted">Predicts each customer's chance to buy with and without an offer. Blended with the expert formula, it ranked customers best against the hidden truth.</p>
            </div>
          ) : <p className="text-sm text-muted">Not trained yet: run <code>python -m engine.uplift --train</code>.</p>}
        </Card>
      </div>

      <Card title="Latest decisions" subtitle="Click one to see exactly why">
        <div className="divide-y divide-hairline">
          {recent.map((d) => (
            <button key={d.id} onClick={() => setOpenId(d.id)} className="flex w-full items-center gap-4 py-3 text-left hover:bg-raised/40">
              <div className="w-28 shrink-0 text-xs text-muted">{ist(d.created_at)}</div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-sm text-ink">{d.first_name} <span className="text-muted">#{d.customer_id} · {TRIGGER[d.trigger_type] ?? d.trigger_type}</span></div>
                <div className="truncate text-xs text-ink2">{d.offer ?? d.summary}</div>
              </div>
              <DeciderBadge decider={d.decided_by} />
              <StatusBadge status={d.status} />
            </button>
          ))}
        </div>
      </Card>
      <DecisionDrawer decisionId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
