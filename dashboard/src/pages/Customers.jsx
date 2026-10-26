// Customers: find a customer (search, or "live moments" = customers with a fresh trigger right now),
// see their profile as the AI sees it, and act: run the AI agent, use the free rules, or simulate a live event.
import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { Bot, Eye, RefreshCw, Search, Send, ShoppingCart, Sparkles, Zap } from "lucide-react";
import { api } from "../api";
import { inr, ist, pct, TRIGGER } from "../format";
import DecisionDrawer from "../components/DecisionDrawer";
import { Badge, Button, Card, DeciderBadge, Empty, ErrorBox, Input, Spinner, StatusBadge } from "../components/ui";

function Fact({ label, value }) {
  return (
    <div className="rounded-lg bg-raised/60 px-3 py-2.5">
      <div className="text-[11px] text-muted">{label}</div>
      <div className="mt-0.5 text-sm text-ink">{value}</div>
    </div>
  );
}

// What just happened, in one line the manager can read during a demo: offer, profit, where it goes, status.
function SendResult({ d, onOpen, onSend, onDiscard, busy }) {
  const sent = d.status === "sent";
  const draft = d.status === "review";
  const onItsWay = d.status === "queued" || d.status === "scheduled";
  const tone = sent ? "border-good/40 bg-good/10" : onItsWay || draft ? "border-accent/40 bg-accent/10" : "border-hairline bg-raised/40";
  return (
    <div className={`mt-4 rounded-xl border p-4 text-sm ${tone}`}>
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="font-medium text-ink">{d.offer ?? "No offer"}</div>
        <StatusBadge status={d.status} />
      </div>
      <div className="mt-1.5 text-xs text-ink2">
        {draft && <>Ready. Check the message below, then send it or discard it.</>}
        {sent && <>Delivered to Telegram at {ist(d.sent_at)}.</>}
        {d.status === "queued" && <>On its way: the bot sends it within about 15 seconds…</>}
        {d.status === "scheduled" && <>Scheduled for {ist(d.send_at)} IST (quiet hours end).</>}
        {d.status === "would_send" && <>Not sent: no Telegram is linked (link yours on the Testers page).</>}
        {d.status === "cancelled" && <>Discarded. Nothing was sent.</>}
        {["nothing", "waiting", "blocked"].includes(d.status) && <>{d.summary}</>}
        {d.expected_profit_rupees != null && <> Expected profit {inr(d.expected_profit_rupees)}, offer cost {inr(d.offer_cost_rupees)}.</>}
      </div>
      {d.message && <div className="mt-2 rounded-lg bg-[#1c3556] px-3 py-2 text-xs text-ink">{d.message}</div>}
      <div className="mt-3 flex flex-wrap items-center gap-2">
        {draft && <Button onClick={onSend} loading={busy}><Send size={14} /> Send to Telegram</Button>}
        {draft && <Button variant="secondary" onClick={onDiscard} disabled={busy}>Discard</Button>}
        <button onClick={onOpen} className="text-xs text-[#86b6ef] hover:underline">See why (full reasoning)</button>
      </div>
    </div>
  );
}

function Profile({ id, onDecision }) {
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(null);
  const [query, setQuery] = useState("lipstick");
  const [error, setError] = useState(null);
  const [lastEvent, setLastEvent] = useState(null);
  const [testers, setTesters] = useState([]);
  const [result, setResult] = useState(null);   // the decision just made, refreshed until it is sent

  const load = () => api.customer(id).then(setData).catch(setError);
  useEffect(() => { setData(null); setError(null); setLastEvent(null); setResult(null); load(); }, [id]);
  useEffect(() => { api.testers().then(setTesters).catch(() => {}); }, [id]);

  // Follow the decision until the bot has sent it (the bot checks every 15 seconds)
  useEffect(() => {
    if (!result || !["queued", "scheduled"].includes(result.status)) return;
    if (result.status === "scheduled" && new Date(result.send_at) - Date.now() > 60000) return;
    const t = setTimeout(() => api.decision(result.id).then(setResult).catch(() => {}), 4000);
    return () => clearTimeout(t);
  }, [result]);

  const act = async (key, fn) => {
    setBusy(key);
    setError(null);
    try {
      const res = await fn();
      const decision = res.decision ?? res;
      if (res.event) setLastEvent(res.event);
      await load();
      if (decision?.id) setResult(await api.decision(decision.id));   // shown in the box below, with Send
    } catch (e) {
      setError(e);
    } finally {
      setBusy(null);
    }
  };

  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  const p = data.profile;
  const sendDraft = async (fn) => {                            // Send / Discard on the draft shown below
    setBusy("send");
    setError(null);
    try { setResult(await fn(result.id)); await load(); } catch (e) { setError(e); } finally { setBusy(null); }
  };

  return (
    <div className="space-y-5">
      <Card>
        <div className="flex flex-wrap items-start justify-between gap-4">
          <div>
            <div className="text-xl font-semibold">{p.first_name} <span className="text-sm font-normal text-muted">#{p.customer_id} · {p.city} · age {p.age}</span></div>
            <div className="mt-1 flex flex-wrap gap-2">
              <Badge tone={p.habit_status.startsWith("on track") ? "good" : p.habit_status.startsWith("due") ? "accent" : "warning"}>{p.habit_status}</Badge>
              {p.is_loyal && <Badge tone="accent">Loyal ({p.total_orders} orders)</Badge>}
              {!p.marketing_consent && <Badge tone="critical">No marketing consent</Badge>}
              {data.tester && <Badge tone="good">Linked to @{data.tester} on Telegram</Badge>}
            </div>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button onClick={() => act("ai", () => api.runAgent(id, true, true))} loading={busy === "ai"}><Bot size={15} /> Make offer (AI)</Button>
            <Button variant="secondary" onClick={() => act("rules", () => api.runAgent(id, false, true))} loading={busy === "rules"}>Make offer (free rules)</Button>
          </div>
        </div>
        <p className="mt-3 text-xs text-muted">
          "Make offer" lets the AI (or the free rules) decide and the rules check it. Nothing is sent until you click
          {" "}<span className="text-ink2">Send to Telegram</span>
          {data.tester ? <>: it then goes to <span className="text-ink2">@{data.tester}</span> within about 15 seconds.</> : <> (no Telegram is linked yet: link one on the Testers page).</>}
          {" "}During quiet hours (22:00–08:00) a sent offer waits until 08:00.
        </p>
        <div className="mt-4"><ErrorBox error={error} /></div>
        {result && <SendResult d={result} onOpen={() => onDecision(result.id)} busy={busy === "send"}
                               onSend={() => sendDraft(api.sendDecision)} onDiscard={() => sendDraft(api.discardDecision)} />}

        <div className="mt-4 grid grid-cols-2 gap-2.5 sm:grid-cols-4">
          <Fact label="Usual order gap" value={p.usual_gap_days ? `every ${p.usual_gap_days} days` : "–"} />
          <Fact label="Last order" value={p.days_since_last_order != null ? `${Math.round(p.days_since_last_order)} days ago` : "never"} />
          <Fact label="Buys anyway (48h, no offer)" value={pct(p.p_buy_48h_no_offer, 1)} />
          <Fact label="Deal seeker" value={`${Math.round(p.deal_seeker_score * 100)}/100`} />
          <Fact label="Lifetime spend" value={inr(p.total_spend_rupees)} />
          <Fact label="Average order" value={inr(p.avg_order_value_rupees)} />
          <Fact label="Wallet" value={inr(p.wallet_balance_rupees)} />
          <Fact label="Points" value={`${p.points_balance}${p.points_expiring_7d ? ` (${p.points_expiring_7d} expiring)` : ""}`} />
        </div>
      </Card>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="What they love and want" subtitle="Favourites (ordered 3+ times) and fresh interest">
          <div className="space-y-2 text-sm">
            {p.favourite_items.slice(0, 3).map((f) => (
              <div key={f.product_id} className="flex justify-between gap-2"><span className="text-ink2">♥ {f.name}</span><span className="text-xs text-muted">{f.times_ordered}× · {inr(f.price_rupees)}</span></div>
            ))}
            {p.intent_products.map((i) => (
              <div key={i.product_id} className="flex justify-between gap-2"><span className="text-ink2">✦ {i.name}</span><span className="text-right text-xs text-muted">{i.signals.join(", ")}</span></div>
            ))}
            {!p.favourite_items.length && !p.intent_products.length && <Empty>No favourites or fresh interest yet.</Empty>}
            <div className="pt-1 text-xs text-muted">Favourite categories: {p.favourite_categories.join(", ") || "–"}</div>
          </div>
        </Card>

        <Card title="Live moments" subtitle="Triggers detected right now, or simulate a new one">
          <div className="space-y-2">
            {data.triggers.length ? data.triggers.map((t) => (
              <div key={t.type} className="flex items-start gap-2 text-sm">
                <Zap size={15} className="mt-0.5 shrink-0 text-warning" />
                <div><span className="text-ink">{t.label}</span> <span className="text-xs text-muted">{Object.entries(t.detail).map(([k, v]) => `${k.replaceAll("_", " ")}: ${Array.isArray(v) ? v.join(", ") : v}`).join(" · ")}</span></div>
              </div>
            )) : <p className="text-sm text-muted">Nothing happening for this customer right now.</p>}
          </div>
          <div className="mt-4 border-t border-hairline pt-4">
            <div className="mb-2 text-xs text-muted">Simulate a live event, then let the AI react:</div>
            <div className="flex flex-wrap gap-2">
              <Button variant="secondary" onClick={() => act("cart", () => api.fireEvent(id, { event: "cart" }))} loading={busy === "cart"}><ShoppingCart size={15} /> Left a cart</Button>
              <Button variant="secondary" onClick={() => act("view", () => api.fireEvent(id, { event: "view" }))} loading={busy === "view"}><Eye size={15} /> Viewed 3×</Button>
              <div className="flex gap-2">
                <Input value={query} onChange={(e) => setQuery(e.target.value)} className="w-28" placeholder="search…" />
                <Button variant="secondary" onClick={() => act("search", () => api.fireEvent(id, { event: "search", query }))} loading={busy === "search"}><Search size={15} /> Searched</Button>
              </div>
            </div>
            {lastEvent && <p className="mt-2 text-xs text-ink2">Event created: {Object.values(lastEvent).join(" · ")}</p>}
          </div>
        </Card>
      </div>

      <div className="grid gap-5 lg:grid-cols-2">
        <Card title="Decisions for this customer">
          {data.decisions.length ? (
            <div className="divide-y divide-hairline">
              {data.decisions.map((d) => (
                <button key={d.id} onClick={() => onDecision(d.id)} className="flex w-full items-center gap-3 py-2.5 text-left hover:bg-raised/40">
                  <div className="w-24 shrink-0 text-xs text-muted">{ist(d.created_at)}</div>
                  <div className="min-w-0 flex-1 truncate text-sm text-ink2">{d.offer ?? d.summary}</div>
                  <DeciderBadge decider={d.decided_by} />
                  <StatusBadge status={d.status} />
                </button>
              ))}
            </div>
          ) : <Empty>No decisions yet. Click "Run AI agent".</Empty>}
        </Card>
        <Card title="Recent orders">
          <div className="space-y-2.5">
            {data.orders.map((o, i) => (
              <div key={i} className="text-sm">
                <div className="flex justify-between"><span className="text-ink2">{o.date}</span><span className="tabular text-ink">{inr(o.paid_rupees)}{o.discount_rupees ? <span className="text-xs text-muted"> (−{inr(o.discount_rupees)})</span> : null}</span></div>
                <div className="truncate text-xs text-muted">{o.items.join(", ")} · {o.status}</div>
              </div>
            ))}
            {!data.orders.length && <Empty>No orders yet.</Empty>}
          </div>
        </Card>
      </div>
    </div>
  );
}

export default function Customers() {
  const [params, setParams] = useSearchParams();
  const selected = params.get("id") ? Number(params.get("id")) : null;
  const [search, setSearch] = useState("");
  const [results, setResults] = useState([]);
  const [suggested, setSuggested] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const [openId, setOpenId] = useState(null);

  const loadSuggested = (refresh = false) => {
    setRefreshing(true);
    api.suggested(refresh).then(setSuggested).catch(() => setSuggested([])).finally(() => setRefreshing(false));
  };
  useEffect(() => loadSuggested(), []);
  useEffect(() => {
    if (!search.trim()) return setResults([]);
    const t = setTimeout(() => api.customers(search).then(setResults).catch(() => {}), 250);
    return () => clearTimeout(t);
  }, [search]);

  const pick = (id) => setParams({ id: String(id) });

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Customers</h1>
        <p className="mt-1 text-sm text-muted">See a customer the way the AI sees them, then let it decide.</p>
      </div>
      <div className="grid gap-6 lg:grid-cols-[300px_minmax(0,1fr)]">
        <div className="space-y-5">
          <Card>
            <Input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Search by name, city or #id" className="w-full" />
            {results.length > 0 && (
              <div className="mt-3 max-h-64 space-y-1 overflow-y-auto">
                {results.map((c) => (
                  <button key={c.id} onClick={() => pick(c.id)} className={`flex w-full justify-between rounded-lg px-3 py-2 text-left text-sm hover:bg-raised ${selected === c.id ? "bg-raised" : ""}`}>
                    <span className="text-ink">{c.first_name} {c.last_name}</span><span className="text-xs text-muted">#{c.id} · {c.city}</span>
                  </button>
                ))}
              </div>
            )}
          </Card>
          <Card title="Live moments" subtitle="Customers with a fresh trigger right now"
                action={<button onClick={() => loadSuggested(true)} className="text-muted hover:text-ink" aria-label="Refresh"><RefreshCw size={15} className={refreshing ? "animate-spin" : ""} /></button>}>
            {!suggested ? <Spinner /> : suggested.length === 0 ? <Empty>No live moments right now.</Empty> : (
              <div className="max-h-[480px] space-y-1 overflow-y-auto">
                {suggested.map((s) => (
                  <button key={s.id} onClick={() => pick(s.id)} className={`w-full rounded-lg px-3 py-2 text-left hover:bg-raised ${selected === s.id ? "bg-raised" : ""}`}>
                    <div className="flex items-center justify-between text-sm"><span className="text-ink">{s.name} <span className="text-muted">#{s.id}</span></span><Sparkles size={13} className="text-warning" /></div>
                    <div className="text-xs text-ink2">{TRIGGER[s.trigger] ?? s.trigger_label}{s.detail?.product ? `: ${s.detail.product}` : ""}</div>
                  </button>
                ))}
              </div>
            )}
          </Card>
        </div>
        <div>{selected ? <Profile id={selected} onDecision={setOpenId} /> : <Empty>Pick a customer on the left: try one of the live moments.</Empty>}</div>
      </div>
      <DecisionDrawer decisionId={openId} onClose={() => setOpenId(null)} />
    </div>
  );
}
