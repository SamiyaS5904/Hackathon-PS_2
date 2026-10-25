// App shell: sidebar navigation + top bar (AI usage today, model, bot) + the pages.
import { useEffect, useState } from "react";
import { NavLink, Route, Routes } from "react-router-dom";
import { CalendarClock, ListChecks, Scale, Send, ShieldCheck, Users } from "lucide-react";
import { api } from "./api";
import Today from "./pages/Today";
import Decisions from "./pages/Decisions";
import Customers from "./pages/Customers";
import Testers from "./pages/Testers";
import Rules from "./pages/Rules";
import Guardrails from "./pages/Guardrails";

const NAV = [
  { to: "/", label: "Today", icon: CalendarClock },
  { to: "/decisions", label: "Decisions", icon: ListChecks },
  { to: "/customers", label: "Customers", icon: Users },
  { to: "/testers", label: "Testers", icon: Send },
  { to: "/guardrails", label: "Guardrails", icon: ShieldCheck },
  { to: "/rules", label: "Rules", icon: Scale },
];

function TopBar() {
  const [health, setHealth] = useState(null);
  const [plan, setPlan] = useState(null);
  useEffect(() => {
    const load = () => {
      api.health().then(setHealth).catch(() => setHealth({ ok: false }));
      api.dayPlan().then(setPlan).catch(() => {});
    };
    load();
    const t = setInterval(load, 20000);
    return () => clearInterval(t);
  }, []);
  return (
    <div className="flex flex-wrap items-center justify-end gap-x-6 gap-y-1 border-b border-hairline px-8 py-3 text-xs text-muted">
      {plan && (
        <span>AI decisions today <span className="tabular text-ink">{plan.today.used} / {plan.today.limit}</span> · spent <span className="text-ink">₹{plan.today.spent_inr}</span></span>
      )}
      {health && (
        <>
          <span>Model <span className="text-ink2">{health.ai_enabled ? health.model : "no AI key (free rules only)"}</span></span>
          <span>Bot <span className="text-ink2">@{health.bot}</span></span>
          <span className="inline-flex items-center gap-1.5">
            <span className={`h-2 w-2 rounded-full ${health.ok ? "bg-good" : "bg-critical"}`} />
            {health.ok ? "API online" : "API offline"}
          </span>
        </>
      )}
    </div>
  );
}

export default function App() {
  return (
    <div className="flex min-h-screen">
      <aside className="sticky top-0 hidden h-screen w-56 shrink-0 flex-col border-r border-hairline bg-surface px-3 py-5 md:flex">
        <div className="mb-8 flex items-center gap-2.5 px-3">
          <div className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-sm font-bold text-white">OP</div>
          <div>
            <div className="text-sm font-semibold">OfferPilot</div>
            <div className="text-[11px] text-muted">Agentic offers</div>
          </div>
        </div>
        <nav className="space-y-1">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition ${isActive ? "bg-raised text-ink" : "text-ink2 hover:bg-raised hover:text-ink"}`
              }
            >
              <Icon size={17} /> {label}
            </NavLink>
          ))}
        </nav>
        <div className="mt-auto px-3 text-[11px] leading-relaxed text-muted">
          Every offer is checked by code before it's sent. The company never loses money on an offer.
        </div>
      </aside>
      <main className="min-w-0 flex-1">
        <TopBar />
        <div className="mx-auto max-w-7xl px-4 py-6 md:px-8">
          <Routes>
            <Route path="/" element={<Today />} />
            <Route path="/decisions" element={<Decisions />} />
            <Route path="/customers" element={<Customers />} />
            <Route path="/testers" element={<Testers />} />
            <Route path="/guardrails" element={<Guardrails />} />
            <Route path="/rules" element={<Rules />} />
          </Routes>
        </div>
      </main>
    </div>
  );
}
