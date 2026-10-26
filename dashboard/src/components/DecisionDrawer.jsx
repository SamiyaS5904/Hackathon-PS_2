// The "why" screen: one decision with everything a marketing manager needs to trust it.
// Offer + message preview, the AI's reasoning, the menu of options code prepared, every rule check, tools, cost.
import { useEffect, useState } from "react";
import { Bot, Send } from "lucide-react";
import { api } from "../api";
import { inr, ist, pct, TRIGGER } from "../format";
import { Badge, DeciderBadge, Drawer, ErrorBox, RuleResult, Spinner, StatusBadge } from "./ui";

function Section({ title, children }) {
  return (
    <section>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-muted">{title}</h3>
      {children}
    </section>
  );
}

export default function DecisionDrawer({ decisionId, onClose }) {
  const [d, setD] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!decisionId) return;
    setD(null);
    setError(null);
    api.decision(decisionId).then(setD).catch(setError);
  }, [decisionId]);

  // While an offer is on its way to Telegram, refresh so the status turns to "Sent on Telegram"
  useEffect(() => {
    if (!d || d.id !== decisionId || d.status !== "queued") return;
    const t = setTimeout(() => api.decision(decisionId).then(setD).catch(() => {}), 4000);
    return () => clearTimeout(t);
  }, [d, decisionId]);

  const r = d?.reasoning ?? {};
  const checks = d?.rule_checks ?? [];
  const passed = checks.filter((c) => c.result === "passed").length;
  const value = r.value;
  const winback = (value?.offer_kind ?? d?.offer_kind) === "winback";

  return (
    <Drawer open={!!decisionId} onClose={onClose} title={`Decision #${decisionId}`}>
      <ErrorBox error={error} />
      {!d && !error && <Spinner />}
      {d && (
        <>
          {/* Header: who, why now, who decided, what happened */}
          <div className="flex flex-wrap items-center gap-3">
            <div className="text-lg font-semibold">
              {d.customer.name} <span className="text-sm font-normal text-muted">#{d.customer.id} · {d.customer.city}</span>
            </div>
            <StatusBadge status={d.status} />
            <DeciderBadge decider={d.decided_by} />
            {d.offer && <Badge tone={winback ? "accent" : "neutral"}>{winback ? "Win-back offer" : "Normal offer"}</Badge>}
          </div>
          <div className="text-xs text-muted">
            Trigger: <span className="text-ink2">{TRIGGER[d.trigger_type] ?? d.trigger_type}</span> · decided {ist(d.created_at)}
            {d.sent_at && <> · sent {ist(d.sent_at)}</>} · rules v{d.rules_version}
          </div>

          {/* The offer and the exact message the customer gets */}
          {d.offer ? (
            <div className="rounded-xl border border-hairline bg-surface p-4">
              <div className="flex flex-wrap items-baseline justify-between gap-2">
                <div className="font-medium">{d.offer}</div>
                <div className="text-xs text-muted">
                  valid {d.final_offer?.expiry_hours}h{d.send_at && <> · send {ist(d.send_at)}</>}
                </div>
              </div>
              <div className="mt-3 flex gap-2">
                <Send size={16} className="mt-1 shrink-0 text-accent" />
                <div className="rounded-2xl rounded-tl-sm bg-[#1c3556] px-4 py-2.5 text-sm leading-relaxed text-ink">{d.message}</div>
              </div>
            </div>
          ) : (
            <div className="rounded-xl border border-hairline bg-surface p-4 text-sm text-ink2">
              No offer sent{d.status === "waiting" ? `: the AI chose to wait and look again at ${ist(d.send_at)}.` : "."}
            </div>
          )}

          {/* The money: before -> after, cost, expected profit */}
          {(d.expected_profit_rupees !== null || d.ai_cost_usd) && (
            <div className="grid grid-cols-3 gap-3">
              <div className="rounded-xl border border-hairline bg-surface p-3">
                <div className="text-xs text-muted">Expected profit</div>
                <div className="mt-1 text-xl font-semibold">{inr(d.expected_profit_rupees)}</div>
              </div>
              <div className="rounded-xl border border-hairline bg-surface p-3">
                <div className="text-xs text-muted">Offer cost (if used)</div>
                <div className="mt-1 text-xl font-semibold">{inr(d.offer_cost_rupees)}</div>
              </div>
              <div className="rounded-xl border border-hairline bg-surface p-3">
                <div className="text-xs text-muted">AI cost</div>
                <div className="mt-1 text-xl font-semibold">{d.ai_cost_inr ? inr(d.ai_cost_inr, 2) : "free"}</div>
              </div>
            </div>
          )}

          {/* v2: value split. Win-back offers may rely on retention value, inside their own budget */}
          {value?.late_customer && (
            <div className={`rounded-xl border p-3 text-sm ${winback ? "border-accent/40 bg-accent/10" : "border-hairline bg-surface"}`}>
              <div className="grid grid-cols-3 gap-3 tabular">
                <div><div className="text-xs text-muted">Immediate profit (48h)</div><div className="mt-0.5 font-semibold">{inr(value.immediate_profit_rupees)}</div></div>
                <div><div className="text-xs text-muted">Retention value (12 wk)</div><div className="mt-0.5 font-semibold">{inr(value.retention_value_rupees)}</div></div>
                <div><div className="text-xs text-muted">Total value</div><div className="mt-0.5 font-semibold">{inr(value.total_value_rupees)}</div></div>
              </div>
              <p className="mt-2 text-xs text-ink2">
                {winback
                  ? "Win-back offer: the customer is late, so their future orders count. Allowed on total value, inside the separate win-back budget; the margin floor still applies."
                  : "The customer is late, but this offer pays for itself on immediate profit alone (normal offer, normal budget)."}
              </p>
            </div>
          )}

          {d.variants?.length > 0 && (
            <Section title={`Message variants (${d.variants.length}, all checked by the rules)`}>
              <div className="space-y-2">
                {d.variants.map((v) => (
                  <div key={v.id} className={`rounded-lg border px-3 py-2 text-sm ${v.id === d.variant_id ? "border-accent bg-accent/10" : "border-hairline"}`}>
                    <div className="mb-1 flex items-center gap-2 text-xs text-muted">
                      <span className="font-medium text-ink2">{v.id}</span> · {v.tone}{v.language && v.language !== "english" ? ` · ${v.language}` : ""}
                      {v.id === d.variant_id && <span className="rounded-full bg-accent px-2 py-0.5 text-[10px] font-semibold text-white">SENT</span>}
                    </div>
                    <div className="text-ink2">{v.text}</div>
                  </div>
                ))}
              </div>
              {r.why_this_variant && <p className="mt-2 text-xs text-muted">Why this one: {r.why_this_variant}</p>}
            </Section>
          )}

          <Section title="Why">
            <p className="text-sm leading-relaxed text-ink">{r.summary}</p>
            {r.decided_by_note && <p className="mt-2 text-xs text-muted">{r.decided_by_note}</p>}
            {r.agent_attempt && <p className="mt-1 text-xs text-warning">AI tried first but: {r.agent_attempt.error}</p>}
          </Section>

          {r.signals_used?.length > 0 && (
            <Section title="Facts it relied on">
              <ul className="space-y-1.5">
                {r.signals_used.map((s) => (
                  <li key={s} className="flex gap-2 text-sm text-ink2"><span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted" />{s}</li>
                ))}
              </ul>
            </Section>
          )}

          {r.alternatives_considered?.length > 0 && (
            <Section title="Options it rejected">
              <ul className="space-y-1.5">
                {r.alternatives_considered.map((a) => (
                  <li key={a} className="flex gap-2 text-sm text-ink2"><span className="mt-2 h-1 w-1 shrink-0 rounded-full bg-muted" />{a}</li>
                ))}
              </ul>
            </Section>
          )}

          {(r.expected_impact || r.why_now) && (
            <Section title="Impact and timing">
              {r.expected_impact && <p className="text-sm text-ink2">{r.expected_impact}</p>}
              {r.why_now && <p className="mt-1.5 text-sm text-ink2"><span className="text-muted">Why now:</span> {r.why_now}</p>}
              {r.rule_repairs?.map((x) => <p key={x} className="mt-1.5 text-sm text-warning">Rules adjusted: {x}</p>)}
              {r.followup_result && <p className="mt-1.5 text-sm text-[#5fd35f]">{r.followup_result}</p>}
              {r.cancelled_note && <p className="mt-1.5 text-sm text-serious">{r.cancelled_note}</p>}
            </Section>
          )}

          {d.candidates?.length > 0 && (
            <Section title={`Menu prepared by code (${d.candidates.length} options, priced and rule-checked)`}>
              <div className="overflow-hidden rounded-xl border border-hairline">
                <table className="w-full text-sm">
                  <thead className="bg-surface text-left text-xs text-muted">
                    <tr><th className="px-3 py-2">Option</th><th className="px-3 py-2">Chance to buy</th><th className="px-3 py-2 text-right">Profit</th>{value?.late_customer && <th className="px-3 py-2 text-right">Total</th>}<th className="px-3 py-2">Rules</th></tr>
                  </thead>
                  <tbody className="tabular">
                    {d.candidates.map((c) => {
                      const f = d.final_offer;
                      const chosen = f && c.offer && c.offer.offer_type === f.offer_type && c.offer.amount === f.amount
                        && (c.offer.product_id ?? null) === (f.product_id ?? null) && c.offer.min_order_rupees === f.min_order_rupees;
                      return (
                      <tr key={c.id} className={`border-t border-hairline ${chosen ? "bg-accent/10" : ""}`}>
                        <td className="px-3 py-2 text-ink2">
                          {c.idea}
                          {chosen && <span className="ml-2 rounded-full bg-accent px-2 py-0.5 text-[10px] font-semibold text-white">CHOSEN</span>}
                          {c.offer_kind === "winback" && <span className="ml-2 rounded-full border border-accent/50 px-2 py-0.5 text-[10px] text-[#86b6ef]">win-back</span>}
                        </td>
                        <td className="whitespace-nowrap px-3 py-2 text-ink2">{c.chance_to_buy_48h ? `${pct(c.chance_to_buy_48h[0], 1)} → ${pct(c.chance_to_buy_48h[1], 1)}` : "–"}</td>
                        <td className="px-3 py-2 text-right">{inr(c.expected_profit_rupees)}</td>
                        {value?.late_customer && <td className="px-3 py-2 text-right">{inr(c.total_value_rupees)}</td>}
                        <td className="whitespace-nowrap px-3 py-2">{c.allowed_by_rules ? <RuleResult result="passed" /> : <span title={(c.blocked_because || []).join("; ")}><RuleResult result="failed" /></span>}</td>
                      </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </Section>
          )}

          {checks.length > 0 && (
            <Section title={`Rule checks · ${passed}/${checks.length} passed outright`}>
              <div className="divide-y divide-hairline rounded-xl border border-hairline">
                {checks.map((c, i) => (
                  <div key={i} className="flex gap-3 px-3 py-2">
                    <div className="w-24 shrink-0"><RuleResult result={c.result} /></div>
                    <div className="min-w-0">
                      <div className="text-xs font-medium text-ink2">{c.rule}</div>
                      <div className="text-xs text-muted">{c.detail}</div>
                    </div>
                  </div>
                ))}
              </div>
            </Section>
          )}

          {d.tool_trace?.length > 0 && (
            <Section title="What the AI did">
              <ol className="space-y-1">
                {d.tool_trace.map((t, i) => (
                  <li key={i} className="flex gap-2 text-xs text-ink2">
                    <Bot size={14} className="shrink-0 text-accent" />
                    <span className="font-medium text-ink">{t.tool}</span>
                    <span className="truncate text-muted">{String(t.result ?? "")}</span>
                  </li>
                ))}
              </ol>
              {d.tokens?.input && (
                <p className="mt-2 text-xs text-muted">
                  {d.tokens.input.toLocaleString()} input + {d.tokens.output.toLocaleString()} output tokens · ${d.ai_cost_usd?.toFixed(4)}
                </p>
              )}
            </Section>
          )}
        </>
      )}
    </Drawer>
  );
}
