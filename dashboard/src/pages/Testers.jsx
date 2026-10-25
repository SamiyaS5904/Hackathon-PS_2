// Testers: real people who receive offers on Telegram, each playing one customer profile.
// Create a personal link (QR + /start code, because a plain link click doesn't always reach the bot),
// and re-link a tester to another customer to show "same person, different customer, different offer".
import { useEffect, useState } from "react";
import { QRCodeSVG } from "qrcode.react";
import { Check, Copy, Link2, Repeat } from "lucide-react";
import { Link } from "react-router-dom";
import { api } from "../api";
import { ist } from "../format";
import { Badge, Button, Card, Empty, ErrorBox, Input, Spinner } from "../components/ui";

function CopyField({ value }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    await navigator.clipboard.writeText(value);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };
  return (
    <div className="flex items-center gap-2">
      <code className="min-w-0 flex-1 truncate rounded-lg bg-raised px-3 py-2 text-xs text-ink">{value}</code>
      <Button variant="secondary" onClick={copy} className="px-2.5" aria-label="Copy">{copied ? <Check size={15} /> : <Copy size={15} />}</Button>
    </div>
  );
}

function Relink({ tester, onDone }) {
  const [customerId, setCustomerId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const go = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.relink(tester.username, Number(customerId));
      setCustomerId("");
      onDone();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div>
      <div className="flex gap-2">
        <Input type="number" placeholder="customer #" value={customerId} onChange={(e) => setCustomerId(e.target.value)} className="w-28" />
        <Button variant="secondary" onClick={go} loading={busy} disabled={!customerId}><Repeat size={14} /> Re-link</Button>
      </div>
      <div className="mt-2"><ErrorBox error={error} /></div>
    </div>
  );
}

export default function Testers() {
  const [testers, setTesters] = useState(null);
  const [username, setUsername] = useState("");
  const [customerId, setCustomerId] = useState("");
  const [created, setCreated] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = () => api.testers().then(setTesters).catch(setError);
  useEffect(() => { load(); }, []);

  const create = async () => {
    setBusy(true);
    setError(null);
    try {
      setCreated(await api.addTester(username, Number(customerId)));
      setUsername("");
      setCustomerId("");
      load();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <div>
        <h1 className="text-2xl font-semibold">Testers</h1>
        <p className="mt-1 text-sm text-muted">Classmates who receive real offers on Telegram. Each one plays a customer from the dataset.</p>
      </div>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card title="Add a tester" subtitle="A Telegram bot can't message anyone first: the tester opens their link and presses Start once.">
          <div className="flex flex-wrap gap-2">
            <Input placeholder="Telegram username" value={username} onChange={(e) => setUsername(e.target.value)} className="flex-1" />
            <Input type="number" placeholder="customer #" value={customerId} onChange={(e) => setCustomerId(e.target.value)} className="w-32" />
            <Button onClick={create} loading={busy} disabled={!username || !customerId}><Link2 size={15} /> Create link</Button>
          </div>
          <div className="mt-3"><ErrorBox error={error} /></div>
          <p className="mt-3 text-xs text-muted">The link only works for that Telegram username, so a forwarded link can't be used by someone else.</p>
        </Card>

        {created && (
          <Card title={`Personal link for @${created.username}`} subtitle={`Plays customer #${created.customer_id}`}>
            <div className="flex flex-wrap gap-5">
              <div className="rounded-xl bg-white p-3"><QRCodeSVG value={created.link} size={132} /></div>
              <div className="min-w-0 flex-1 space-y-3">
                <div>
                  <div className="mb-1 text-xs text-muted">1. Scan the QR code, or open this link on the phone:</div>
                  <CopyField value={created.link} />
                </div>
                <div>
                  <div className="mb-1 text-xs text-muted">2. If nothing happens: open @{created.bot} in Telegram and send:</div>
                  <CopyField value={created.start_command} />
                </div>
              </div>
            </div>
          </Card>
        )}
      </div>

      <Card title="Testers" subtitle="Re-link a tester to another customer: the next decision for that customer goes to the same person">
        {!testers ? <Spinner /> : testers.length === 0 ? <Empty>No testers yet.</Empty> : (
          <div className="divide-y divide-hairline">
            {testers.map((t) => (
              <div key={t.username} className="flex flex-wrap items-center gap-4 py-3">
                <div className="min-w-[180px]">
                  <div className="text-sm text-ink">@{t.username}</div>
                  <div className="text-xs text-muted">{t.linked ? `linked ${ist(t.linked_at)}` : "waiting for Start"}</div>
                </div>
                <Badge tone={t.linked ? "good" : "warning"}>{t.linked ? "Linked" : "Not linked yet"}</Badge>
                <div className="flex-1 text-sm text-ink2">
                  plays <Link to={`/customers?id=${t.customer_id}`} className="text-[#86b6ef] hover:underline">{t.customer} (#{t.customer_id})</Link>
                </div>
                <Relink tester={t} onDone={load} />
              </div>
            ))}
          </div>
        )}
      </Card>
    </div>
  );
}
