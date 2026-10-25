# OfferPilot

**An AI agent decides which offer each e-commerce customer gets, and when. Plain code makes sure the company never
loses money on it.** Offers are delivered on Telegram; a company dashboard shows every decision and exactly why.

Built for a hackathon problem statement: years of customer data (orders, browsing, carts, wallet, reward points,
past campaigns) → the right offer for the right person at the right moment, with hard limits the AI can't cross.

## Results (scored against hidden ground truth)

| Check | Result |
|---|---|
| True profit, 24 customers across 8 hidden personas | **AI + rules ₹1,039** vs send-an-offer-to-everyone ₹827, free rules only ₹341 |
| Offers sent | 20 of 24 (send-to-all: 24) — the AI also knows when to send nothing |
| AI behaviour in 16 golden scenarios | 14/16 (pass bar 14), reasoning complete in all 16 |
| Automatic tests | 168 pass: every rule, thousands of random offers (hypothesis), attacks, every AI failure path |
| Approved offers that lose money | **0**: expected profit ≥ ₹20 and margin ≥ 10% on every approved offer, by construction |
| Cost per AI decision | ~₹1.6–2.4 (Claude Sonnet 5.5, one call in most cases) |

Full outputs: [docs/evidence/](docs/evidence/).

## How it decides

```
SQLite data (3,000 customers, 12 months)          past campaigns with random holdout groups
        │                                                    │
        ▼                                                    ▼
 Analyst profile (habit, interests, wallet, points)   Uplift model: who buys BECAUSE of an offer
        │
 Trigger: habit break / cart left / product interest / points expiring / idle wallet / manager clicks "Run agent"
        │
 Menu: code builds and prices 6-10 personalised offers, each already checked by the rules
        │
 Gatekeeper (free): skip anyone no offer could reach; check today's AI budget ("Plan your day")
        │
 AI agent (Claude): picks / adapts an offer, or sends nothing, or WAITS to see if they buy on their own;
        │            writes the message in the brand voice and the reasoning for the manager
        ▼
 Rules checker (plain code): approve / repair / reject → 2 rejections or any AI failure → free fallback rules
        │
 Telegram bot (linked testers) or "would send" ── follow-ups resolve every "wait" decision
        │
 Dashboard: decisions + reasoning, customers, testers, guardrails, editable rules (versioned)
```

Key ideas:
- **Profit is maths, not the AI.** `expected_profit = P(buy | offer) × margin_after_offer − P(buy | no offer) × normal_margin`,
  computed in [engine/profit.py](engine/profit.py) and calibrated on past campaigns. Discounts to people who'd buy
  anyway count as a loss, so they're refused.
- **The AI only chooses inside the limits.** The Claude API key is used in exactly one file, [engine/agent.py](engine/agent.py).
  Everything else — data, profit, rules, fallback, bot, dashboard — is plain code.
- **Every decision is explained:** summary, facts used, options rejected (with their numbers), expected impact, why
  now, every rule check, the priced menu, tokens and cost.
- **Honest messages:** a "valid till midnight" must match the real expiry, "loyal customer" must be true, no fake
  urgency or unbacked claims, the amount in the text must match the offer.
- **Marketing edits the rules** on the dashboard; every save is a new version, unsafe values are refused, and offers
  not yet sent are re-checked.

Why each part works this way: [reasoning.md](reasoning.md). The full rulebook: [CLAUDE.md](CLAUDE.md).

## Run it

Needs Python 3.11+ and Node 18+.

```bash
pip install -r requirements.txt
cp .env.example .env          # add ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN (both optional, see below)
python -m data.generate --customers 3000 --months 12    # ~170 MB SQLite database, a few minutes
python -m engine.uplift --train                         # uplift model, ~90 s, no API
cd dashboard && npm install && npm run build && cd ..
python -m uvicorn api.main:app --port 8010              # dashboard at http://127.0.0.1:8010
python -u -m bot.telegram_bot                            # second terminal: Telegram delivery
```

- **No Claude key?** Everything still works: the free fallback rules decide, and are checked the same way.
- **No Telegram token?** Decisions are shown as "would send" on the dashboard.

Tests:
```bash
pytest harness/                          # 168 tests, no API, ~1 minute
python -m harness.eval_uplift            # free: uplift model vs formula vs blend, against the hidden truth
python -m harness.eval_segments          # free: do the Testers page profiles match the hidden personas?
python -m harness.simulate_profit        # real AI on 24 customers (saved answers make re-runs free)
python -m harness.eval_agent_scenarios   # real AI, 16 golden scenarios
```

## Demo in 5 minutes

1. **Today**: greeting, *Plan your day* (how many AI decisions to allow, and what that costs).
2. **Testers** → *Pick a profile to play* → About-to-leave VIP → link your Telegram (QR code).
3. **Customers** → that customer → **Run AI agent** → the offer arrives on Telegram.
4. **Decisions** → open it: why this offer, why now, what else was considered, every rule check.
5. **Testers** → re-link the same Telegram to a *Loyal regular* → run again → a different decision (often nothing:
   they'd buy anyway).
6. **Guardrails**: what each rule caught, budgets, circuit breaker, and the proof from the harness.
7. **Rules**: change the electronics cap 8% → 12%; it's saved as a new version and the next decision uses it.

Real-tester guide: [TESTING.md](TESTING.md).

## Repository map

| Folder | What's in it |
|---|---|
| `data/` | synthetic data generator (8 hidden personas, festivals, paydays, habits that break) |
| `engine/` | profile, triggers, candidate menu, profit, uplift model, gatekeeper, agent, rules, fallback, follow-ups |
| `bot/` | Telegram bot, tester linking / re-linking, delivery |
| `api/` | FastAPI for the dashboard (no decision logic of its own) |
| `dashboard/` | React + Vite + Tailwind + Recharts |
| `harness/` | tests and AI checks (the only code besides the generator that may read the hidden truth) |
| `config/` | rules, offer types, message templates, brand voice |
| `docs/evidence/` | saved outputs of real runs |

Build log, step by step: [documentation.md](documentation.md).

Made by Samiya Sehgal (2302662).
