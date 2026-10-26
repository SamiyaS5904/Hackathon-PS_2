# OfferPilot

**An AI agent decides which offer each e-commerce customer gets, and when. Plain code makes sure every offer is
worth it, today and over the coming weeks, without teaching full-price customers to wait for coupons.** Offers are
delivered on Telegram; a company dashboard shows every decision and exactly why.

Built for a hackathon problem statement: years of customer data (orders, browsing, carts, wallet, reward points,
past campaigns) → the right offer for the right person at the right moment, with hard limits the AI can't cross.

## Results (scored against hidden ground truth)

| Check | Result |
|---|---|
| True 12-week profit, 24 customers across 8 hidden personas (v2 data and referee) | AI + rules **₹1,515** (v1 engine ₹1,709), free rules ₹388, send an offer to everyone **₹2,692**: send-to-all still wins over 12 weeks on this referee (see "v2 results" in [documentation.md](documentation.md)) |
| Full-price share of orders in the next 12 weeks | AI + rules 59% (v1 engine 59%), send to all 55%, no offers 65% |
| Offers sent | AI + rules 21 of 24 (v1 engine 22) |
| AI behaviour in 16 golden scenarios | 14/16 (pass bar 14), reasoning complete in all 16 |
| Automatic tests | 193 pass: every rule, thousands of random offers (hypothesis), attacks, every AI failure path, the CSV importer |
| Approved offers with negative total expected value | **0** (total = immediate profit + retention value, by construction); the **10% margin floor holds on every offer**. Normal offers also need ≥ ₹20 immediate profit; win-back offers may rely on retention value, inside their own smaller budget |
| Cost per AI decision | ~₹1.6–2.4 (Claude Sonnet 5.5, one call in most cases) |

Full outputs: [docs/evidence/](docs/evidence/).

## What this is and is not

- **For:** small D2C brands (a few thousand to a few lakh customers) who send offers by hand or by simple rules
  and want every offer to pay for itself, with the reasoning written down.
- **How it differs from CleverTap / MoEngage-style tools:** those are excellent at *sending* (segments, journeys,
  channels, A/B tests); the marketer still decides who gets what. OfferPilot decides the offer per customer, prices
  it with profit maths (including what a won-back customer is worth over 12 weeks), and refuses anything that breaks
  margin, budget, anti-spam, habit-protection or honesty rules. It could sit in front of such a tool.
- **Limits, honestly:** the results come from **synthetic data** (3,000 customers, 12 months, a hidden "true
  behaviour" we wrote ourselves); there is **no store**, so nobody actually redeems anything; the AI checks use small
  samples (24 customers) because each AI decision costs money. The CSV importer makes it run on a real export, but it
  has not been proven on one yet.

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
- **Long-term value (win-back):** for a customer whose habit is slipping, the value of an offer includes what they
  are worth over the next 12 weeks if they come back (`retention_value`). Offers that rely on it are *win-back
  offers*: shown with their own badge, inside a separate budget, margin floor unchanged.
- **Habit protection:** customers who mostly pay full price get no cash discounts (only early access, bonus points or
  free delivery) unless it's a win-back; coupons would teach them to wait for deals. The simulation measures the
  full-price share of orders over 12 weeks for every approach.
- **Message variants:** the AI writes 2-3 variants in different tones (and Hinglish for Hindi-speaking customers);
  every variant is checked, the sent one is recorded so reply rates can be compared later. Price anchors
  ("₹799 → ₹719") are allowed only with the catalog's real numbers.
- **Real data:** `python -m data.import_csv` loads a brand's customers, products (with cost) and orders into a new
  database; without past campaigns it runs in learning mode (no model, conservative estimates, a 10% holdout).
  Guide: [docs/csv_import/README.md](docs/csv_import/README.md).

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
pytest harness/                          # 193 tests, no API, ~2 minutes
python -m harness.eval_uplift            # free: uplift model vs formula vs blend, against the hidden truth
python -m harness.eval_segments          # free: do the Testers page profiles match the hidden personas?
python -m harness.simulate_profit        # real AI on 24 customers, 48h + 12-week profit, full-price share
python -m harness.simulate_profit --skip-ai   # free: the same without the AI column
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
| `data/` | synthetic data generator (8 hidden personas, festivals, paydays, habits that break, discount training) and the CSV importer |
| `engine/` | profile, triggers, candidate menu, profit, uplift model, gatekeeper, agent, rules, fallback, follow-ups |
| `bot/` | Telegram bot, tester linking / re-linking, delivery |
| `api/` | FastAPI for the dashboard (no decision logic of its own) |
| `dashboard/` | React + Vite + Tailwind + Recharts |
| `harness/` | tests and AI checks (the only code besides the generator that may read the hidden truth) |
| `config/` | rules, offer types, message templates, brand voice |
| `docs/evidence/` | saved outputs of real runs |
| `docs/csv_import/` | column mapping example, sample brand export, import guide |

Build log, step by step: [documentation.md](documentation.md).

Made by Samiya Sehgal (2302662).
