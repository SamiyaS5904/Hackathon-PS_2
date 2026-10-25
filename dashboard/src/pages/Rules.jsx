// Rules: the marketing team edits the business limits here (e.g. electronics cap 8% -> 12%).
// Every save is a new version; unsafe values are refused with the reason; offers not yet sent are re-checked.
import { useEffect, useMemo, useState } from "react";
import { History, ShieldCheck } from "lucide-react";
import { api } from "../api";
import { ist } from "../format";
import { Button, Card, ErrorBox, Input, Spinner } from "../components/ui";

export default function Rules() {
  const [data, setData] = useState(null);
  const [values, setValues] = useState({});
  const [note, setNote] = useState("");
  const [by, setBy] = useState("marketing team");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [result, setResult] = useState(null);

  const load = () =>
    api.rules().then((d) => {
      setData(d);
      setValues(Object.fromEntries(d.fields.map((f) => [f.path, f.value])));
    }).catch(setError);
  useEffect(() => { load(); }, []);

  const changes = useMemo(() => {
    if (!data) return {};
    const out = {};
    for (const f of data.fields) {
      const v = values[f.path];
      const typed = typeof f.value === "number" ? Number(v) : v;
      if (typed !== f.value) out[f.path] = typed;
    }
    return out;
  }, [data, values]);

  const save = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await api.saveRules(changes, by, note || null);
      setResult(res);
      setNote("");
      load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  if (!data) return error ? <ErrorBox error={error} /> : <Spinner />;
  const groups = [...new Set(data.fields.map((f) => f.group))];
  const floor = Number(values["profit.margin_floor_pct"]);

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Rules</h1>
        <p className="mt-1 text-sm text-muted">
          Version {data.version} · saved {ist(data.created_at)} by {data.created_by}. The AI can never break these: every offer is checked by code.
        </p>
      </div>

      <div className="grid gap-6 lg:grid-cols-[minmax(0,1.5fr)_minmax(0,1fr)]">
        <div className="space-y-6">
          {groups.map((g) => (
            <Card key={g} title={g}>
              <div className="grid gap-3 sm:grid-cols-2">
                {data.fields.filter((f) => f.group === g).map((f) => {
                  const category = f.path.startsWith("profit.category_cap_pct.") ? f.path.split(".").pop() : null;
                  const maxCap = category ? (data.category_margins[category] - floor).toFixed(1) : null;
                  const changed = f.path in changes;
                  return (
                    <label key={f.path} className={`rounded-lg border px-3 py-2.5 ${changed ? "border-accent bg-accent/5" : "border-hairline"}`}>
                      <div className="text-xs text-ink2">{f.label}</div>
                      <div className="mt-1.5 flex items-center gap-2">
                        <Input value={values[f.path] ?? ""} onChange={(e) => setValues({ ...values, [f.path]: e.target.value })}
                               className="w-full tabular" type={typeof f.value === "number" ? "number" : "text"} step="any" />
                        {f.unit && <span className="w-4 text-xs text-muted">{f.unit}</span>}
                      </div>
                      {category && <div className="mt-1 text-[11px] text-muted">avg margin {data.category_margins[category]}% · max allowed {maxCap}%</div>}
                    </label>
                  );
                })}
              </div>
            </Card>
          ))}
        </div>

        <div className="space-y-6">
          <Card title="Save changes" subtitle="Saved as a new version. Offers decided but not yet sent are re-checked.">
            {Object.keys(changes).length === 0 ? (
              <p className="text-sm text-muted">Edit a value on the left to see the change here.</p>
            ) : (
              <ul className="mb-3 space-y-1 text-sm">
                {Object.entries(changes).map(([path, v]) => {
                  const f = data.fields.find((x) => x.path === path);
                  return <li key={path} className="text-ink2">{f.label}: <span className="text-muted line-through">{f.value}</span> → <span className="text-ink">{v}</span></li>;
                })}
              </ul>
            )}
            <div className="space-y-2">
              <Input placeholder="Why? (e.g. electronics margins went up)" value={note} onChange={(e) => setNote(e.target.value)} className="w-full" />
              <Input placeholder="Your name" value={by} onChange={(e) => setBy(e.target.value)} className="w-full" />
              <Button onClick={save} loading={busy} disabled={!Object.keys(changes).length} className="w-full"><ShieldCheck size={15} /> Save as version {data.version + 1}</Button>
            </div>
            <div className="mt-3"><ErrorBox error={error} /></div>
            {result && (
              <div className="mt-3 rounded-lg border border-good/40 bg-good/10 p-3 text-sm text-[#5fd35f]">
                Saved as version {result.version}. {result.rechecked.length ? `${result.rechecked.length} pending offer(s) re-checked.` : "No pending offers needed re-checking."}
              </div>
            )}
          </Card>

          <Card title="Version history" subtitle="Who changed what, and when">
            <div className="space-y-3">
              {data.versions.map((v) => (
                <div key={v.version} className="text-sm">
                  <div className="flex items-center gap-2 text-ink"><History size={14} className="text-muted" /> v{v.version} {v.is_active && <span className="text-xs text-[#5fd35f]">active</span>}</div>
                  <div className="text-xs text-muted">{ist(v.created_at)} · {v.created_by}{v.note ? ` · ${v.note}` : ""}</div>
                  {v.changes.map((c) => <div key={c.path} className="text-xs text-ink2">{c.path}: {String(c.old)} → {String(c.new)}</div>)}
                </div>
              ))}
            </div>
          </Card>
        </div>
      </div>
    </div>
  );
}
