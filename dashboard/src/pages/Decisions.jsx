// Decisions: the full feed with filters. Click a row to open its reasoning drawer.
import { useEffect, useState } from "react";
import { api } from "../api";
import { DECIDER, inr, ist, STATUS, TRIGGER } from "../format";
import DecisionDrawer from "../components/DecisionDrawer";
import { Card, DeciderBadge, Empty, ErrorBox, Spinner, StatusBadge } from "../components/ui";

function Select({ value, onChange, options, label }) {
  return (
    <label className="flex items-center gap-2 text-xs text-muted">
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)}
              className="rounded-lg border border-baseline bg-raised px-2.5 py-1.5 text-sm text-ink focus:border-accent focus:outline-none">
        <option value="">All</option>
        {Object.entries(options).map(([k, v]) => <option key={k} value={k}>{v.label}</option>)}
      </select>
    </label>
  );
}

export default function Decisions() {
  const [rows, setRows] = useState(null);
  const [decider, setDecider] = useState("");
  const [status, setStatus] = useState("");
  const [openId, setOpenId] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    setRows(null);
    api.decisions({ limit: 200, decided_by: decider, status }).then(setRows).catch(setError);
  }, [decider, status]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Decisions</h1>
        <p className="mt-1 text-sm text-muted">Every decision, with who made it and why. Nothing reaches a customer without passing the rules.</p>
      </div>
      <Card
        title={rows ? `${rows.length} decisions` : "Decisions"}
        action={
          <div className="flex flex-wrap gap-3">
            <Select label="Decided by" value={decider} onChange={setDecider} options={DECIDER} />
            <Select label="Status" value={status} onChange={setStatus} options={STATUS} />
          </div>
        }
      >
        <ErrorBox error={error} />
        {!rows && !error && <Spinner />}
        {rows && rows.length === 0 && <Empty>No decisions match these filters.</Empty>}
        {rows && rows.length > 0 && (
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-muted">
                <tr className="border-b border-hairline">
                  <th className="py-2 pr-4">When (IST)</th><th className="py-2 pr-4">Customer</th><th className="py-2 pr-4">Trigger</th>
                  <th className="py-2 pr-4">Decision</th><th className="py-2 pr-4">Decided by</th><th className="py-2 pr-4">Status</th>
                  <th className="py-2 pr-4 text-right">Exp. profit</th><th className="py-2 text-right">AI cost</th>
                </tr>
              </thead>
              <tbody className="tabular">
                {rows.map((d) => (
                  <tr key={d.id} onClick={() => setOpenId(d.id)} className="cursor-pointer border-b border-hairline hover:bg-raised/40">
                    <td className="whitespace-nowrap py-2.5 pr-4 text-xs text-muted">{ist(d.created_at)}</td>
                    <td className="whitespace-nowrap py-2.5 pr-4 text-ink">{d.first_name} <span className="text-muted">#{d.customer_id}</span></td>
                    <td className="py-2.5 pr-4 text-ink2">{TRIGGER[d.trigger_type] ?? d.trigger_type}</td>
                    <td className="max-w-xs truncate py-2.5 pr-4 text-ink2">{d.offer ?? d.summary}</td>
                    <td className="whitespace-nowrap py-2.5 pr-4"><DeciderBadge decider={d.decided_by} /></td>
                    <td className="whitespace-nowrap py-2.5 pr-4"><StatusBadge status={d.status} /></td>
                    <td className="py-2.5 pr-4 text-right">{inr(d.expected_profit_rupees)}</td>
                    <td className="py-2.5 text-right text-ink2">{d.ai_cost_inr ? inr(d.ai_cost_inr, 2) : "free"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Card>
      <DecisionDrawer decisionId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
