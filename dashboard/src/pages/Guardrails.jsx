// Guardrails: what the rules protect right now, and what they caught.
// Circuit breaker gauges, offer budgets (daily / monthly / closest to the per-customer cap), the three layers of
// checks per rule (blocked in the menu, AI proposal sent back, safe repair), why the gatekeeper skipped and why the
// free rules were used, the limits in force, and the latest harness results (the proof).
import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import { AlertTriangle, CheckCircle2, ShieldCheck, XCircle } from "lucide-react";
import { api } from "../api";
import { inr, ist } from "../format";
import { Card, Empty, ErrorBox, Meter, Spinner, Stat } from "../components/ui";

// Three layers of checks. Validated as a set on the dark surface (all-pairs CVD and contrast pass); kept apart
// from the decider colours (blue / orange / aqua) so no colour means two things in this dashboard.
const LAYERS = [
  { key: "menu_blocked", label: "Blocked in the menu", color: "#9085e9" },
  { key: "ai_rejected", label: "AI proposal sent back", color: "#d55181" },
  { key: "repaired", label: "Safely repaired", color: "#c98500" },
];
const tooltipStyle = {
  contentStyle: { background: "#232322", border: "1px solid #383835", borderRadius: 8, fontSize: 12 },
  labelStyle: { color: "#f0efec" },
  itemStyle: { color: "#c3c2b7" },
};

function BreakerCard({ breaker, daily }) {
  const pauseAt = Math.floor(breaker.window * breaker.blocked_rate_max);
  return (
    <Card title="Circuit breaker" subtitle="Pauses every offer if something looks wrong, before money is lost">
      <div className={`mb-5 flex items-center gap-3 rounded-lg p-3 text-sm ${breaker.paused ? "bg-critical/10 text-[#f08a8a]" : "bg-good/10 text-[#5fd35f]"}`}>
        {breaker.paused ? <AlertTriangle size={18} /> : <CheckCircle2 size={18} />}
        <span>{breaker.paused ? <><b>Offers paused.</b> {breaker.paused}</> : <b>Running normally</b>}</span>
      </div>
      <div className="space-y-4">
        <Meter label="Offer budget used today" value={daily.used_rupees} limit={daily.limit_rupees} format={(v) => inr(v)} />
        <Meter label={`Blocked in the last ${breaker.window} decisions (pauses above ${pauseAt})`} value={breaker.blocked_recent} limit={pauseAt} />
      </div>
      <p className="mt-3 text-xs text-muted">
        {breaker.recent_count < breaker.window
          ? `Only ${breaker.recent_count} decisions so far: the blocked-rate check starts after ${breaker.window}.`
          : "A high blocked rate means the AI keeps proposing offers the rules refuse: stop and look."}
      </p>
    </Card>
  );
}

function BudgetCard({ budgets }) {
  return (
    <Card title="Offer budgets" subtitle="Full cost if every offer is used. Limits are set on the Rules page.">
      <div className="space-y-4">
        <Meter label="Today" value={budgets.daily.used_rupees} limit={budgets.daily.limit_rupees} format={(v) => inr(v)} />
        <Meter label="This month" value={budgets.monthly.used_rupees} limit={budgets.monthly.limit_rupees} format={(v) => inr(v)} />
      </div>
      <div className="mt-5 text-xs text-muted">Closest to the per-customer cap ({inr(budgets.per_customer_cap_rupees)} a month)</div>
      {budgets.top_customers.length ? (
        <div className="mt-2 space-y-3">
          {budgets.top_customers.map((c) => (
            <Meter key={c.customer_id} label={`${c.first_name} #${c.customer_id}`} value={c.used_rupees} limit={budgets.per_customer_cap_rupees} format={(v) => inr(v)} />
          ))}
        </div>
      ) : <p className="mt-2 text-sm text-muted">No offer money committed this month.</p>}
    </Card>
  );
}

function ProofCard({ harness }) {
  const { pytest, simulate_profit: sim, eval_agent_scenarios: evalAi } = harness;
  const policies = sim ? Object.entries(sim.totals_rupees) : [];
  const max = Math.max(1, ...policies.map(([, v]) => v));
  const Line = ({ ok, children }) => (
    <div className="flex items-start gap-2 text-sm text-ink">
      {ok ? <CheckCircle2 size={16} className="mt-0.5 shrink-0 text-[#5fd35f]" /> : <XCircle size={16} className="mt-0.5 shrink-0 text-[#f08a8a]" />}
      <span>{children}</span>
    </div>
  );
  return (
    <Card title="Proof (harness)" subtitle="Automatic checks that the rules hold. Re-run with pytest harness/">
      <div className="space-y-2.5">
        {pytest ? <Line ok={pytest.ok}>{pytest.passed} of {pytest.passed + pytest.failed} tests pass: every rule, thousands of random offers, attacks, AI failures</Line>
                : <p className="text-sm text-muted">Tests not run yet on this machine.</p>}
        {evalAi && <Line ok={evalAi.ok}>AI behaved sensibly in {evalAi.passed} of {evalAi.total} golden scenarios (bar {evalAi.pass_bar})</Line>}
        {sim && <Line ok={sim.ok}>True profit on {sim.customers} customers, judged by the hidden truth:</Line>}
      </div>
      {sim && (
        <div className="mt-3 space-y-2">
          {policies.map(([name, value]) => (
            <div key={name} className="text-xs">
              <div className="flex justify-between">
                <span className={name === "AI + rules" ? "font-semibold text-ink" : "text-ink2"}>{name}</span>
                <span className="tabular text-ink">{inr(value)}</span>
              </div>
              <div className="mt-1 h-2 rounded-full bg-raised">
                <div className="h-2 rounded-full" style={{ width: `${Math.max(0, value) / max * 100}%`, background: name === "AI + rules" ? "#3987e5" : "#5c5b57" }} />
              </div>
            </div>
          ))}
        </div>
      )}
      {(sim || pytest) && <p className="mt-3 text-xs text-muted">Last run {ist((sim ?? pytest).ran_at)}</p>}
    </Card>
  );
}

function RuleChart({ rows }) {
  const data = rows.slice(0, 10);
  return (
    <ResponsiveContainer width="100%" height={data.length * 34 + 56}>
      <BarChart data={data} layout="vertical" margin={{ top: 4, right: 12, bottom: 0, left: 0 }}>
        <CartesianGrid horizontal={false} stroke="#2c2c2a" />
        <XAxis type="number" allowDecimals={false} tick={{ fill: "#898781", fontSize: 12 }} axisLine={{ stroke: "#383835" }} tickLine={false} />
        <YAxis type="category" dataKey="label" width={190} tick={{ fill: "#c3c2b7", fontSize: 12 }} axisLine={false} tickLine={false} />
        <Tooltip cursor={{ fill: "rgba(255,255,255,0.04)" }} {...tooltipStyle}
                 formatter={(v, key) => [v, LAYERS.find((l) => l.key === key)?.label ?? key]} />
        <Legend formatter={(key) => <span style={{ color: "#c3c2b7", fontSize: 12 }}>{LAYERS.find((l) => l.key === key)?.label}</span>} iconType="circle" iconSize={8} />
        {LAYERS.map((l, i) => (
          <Bar key={l.key} dataKey={l.key} stackId="r" fill={l.color} barSize={18} stroke="#1a1a19" strokeWidth={2}
               radius={i === LAYERS.length - 1 ? [0, 4, 4, 0] : 0} />
        ))}
      </BarChart>
    </ResponsiveContainer>
  );
}

function ReasonList({ items, empty }) {
  if (!items.length) return <p className="text-sm text-muted">{empty}</p>;
  return (
    <div className="divide-y divide-hairline">
      {items.map((r) => (
        <div key={r.reason} className="flex items-center justify-between gap-4 py-2 text-sm">
          <span className="text-ink2">{r.reason}</span>
          <span className="tabular text-ink">{r.count}</span>
        </div>
      ))}
    </div>
  );
}

export default function Guardrails() {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  useEffect(() => { api.guardrails().then(setData).catch(setError); }, []);

  if (error) return <ErrorBox error={error} />;
  if (!data) return <Spinner />;
  const a = data.activity;
  const share = (n, of) => (of ? ` of ${of}` : "");

  return (
    <div className="space-y-6">
      <div>
        <h1 className="flex items-center gap-2 text-2xl font-semibold"><ShieldCheck size={24} className="text-accent" /> Guardrails</h1>
        <p className="mt-1 text-sm text-muted">Every offer, from the AI or the free rules, passes these checks in code before it can be sent · rules v{data.rules_version}</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-3">
        <BreakerCard breaker={data.breaker} daily={data.budgets.daily} />
        <BudgetCard budgets={data.budgets} />
        <ProofCard harness={data.harness} />
      </div>

      <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
        <Stat label={`Decisions, last ${a.days} days`} value={a.decisions}
              sub={`${a.by_decider.agent ?? 0} AI · ${a.by_decider.fallback ?? 0} free rules · ${a.by_decider.gatekeeper ?? 0} skipped`} />
        <Stat label="Options blocked before the AI saw them" value={a.menu_blocked} sub={`${share(a.menu_blocked, a.menu_options).trim() || "no"} options code priced`} />
        <Stat label="AI proposals sent back" value={a.ai_rejected} sub={`out of ${a.ai_proposals} proposals, with the reason`} />
        <Stat label="Safe repairs" value={a.repairs} sub="e.g. send time moved out of quiet hours" />
      </div>

      <Card title="What each rule caught" subtitle={`Last ${a.days} days. The AI never sees a blocked option as allowed, and nothing it proposes skips the checks.`}>
        {a.by_rule.length ? <RuleChart rows={a.by_rule} /> : <Empty>No rule has had to step in yet.</Empty>}
      </Card>

      <div className="grid gap-6 lg:grid-cols-3">
        <Card title="Why the gatekeeper skipped" subtitle="Customers no offer could reach: no AI call, no cost">
          <ReasonList items={a.skip_reasons} empty="No skips yet." />
        </Card>
        <Card title="Why the free rules decided" subtitle="The AI was not used or failed; the fallback is checked too">
          <ReasonList items={a.fallback_reasons} empty="The AI handled every decision." />
        </Card>
        <Card title="Limits in force" subtitle="Changed only on the Rules page, as a new version" action={<Link to="/rules" className="text-xs text-accent hover:underline">Edit</Link>}>
          <div className="divide-y divide-hairline">
            {data.limits.map((l) => (
              <div key={l.label} className="flex items-center justify-between gap-4 py-2 text-sm">
                <span className="text-ink2">{l.label}</span>
                <span className="whitespace-nowrap text-ink">{l.value}</span>
              </div>
            ))}
          </div>
        </Card>
      </div>
    </div>
  );
}
