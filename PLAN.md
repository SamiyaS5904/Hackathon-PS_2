# OfferPilot: Agentic Offer Decision Pipeline for E-commerce

## Context
Hackathon brief: an e-commerce company has years of customer data (shopping, browsing, cart, wallet, reward
points, past offers). Decide **which offer each customer should get, and when**, deliver it through **one
Telegram bot**, and give the company a **dashboard**. Rules set hard limits; an **AI agent (Claude)** decides
offers dynamically within them; **every AI choice is checked by rule code before sending**; a small rule table
is the backup. There is no customer-facing UI: the only UI is the **company dashboard** (marketing manager).

**#1 goal: the company must never lose profit on an offer.** Send an offer only when it creates extra profit,
never to people who would buy anyway, never beyond budget or margin limits, never spam.

Constraints: solo, 2 days (Day 1 = working MVP, Day 2 = improve + test), Python, Claude API, laptop,
empty folder `D:\Hackathon PS_2`. No phone numbers. No Telegram group. No bargain mode. No extras.

## Pipeline (flow)
```
[1 DATA] synthetic company data (3,000 customers, 12 months, 13 tables) ──► SQLite
     ▼
[2 ANALYST] per-customer profile: churn risk, category affinity, active hours, discount dependence,
            "buys anyway" chance, wallet balance, points expiring, offer fatigue
     ▼
[3 SIMULATOR] "today" clock creates live moments: cart abandoned, repeat product views, points expiring,
              inactive N days, wallet balance idle, payday, festival start
     │        + manager clicks "Run agent" on dashboard for one customer / a segment
     ▼
[4 AGENT] Claude + tools → SEND (type, amount, product/category, expiry, min order, send time, message)
          or SEND NOTHING; writes structured reasoning for the manager
     ▼
[5 RULES CHECKER] pure Python: ✅ approve / 🔧 repair / ❌ reject → reason back to agent (max 2 retries)
                  API error / timeout >30s / 2 rejections / bad output → [5b FALLBACK RULES]
     ▼
[6 DELIVERY] customer linked to a real tester? → Telegram message
             not linked → saved as "would send" (decision still fully logged)
     ▼
[7 DASHBOARD] every decision + WHY + rule checks + profit estimate + agent/fallback, budget, guardrail stats
```

## 1. Synthetic data (very detailed): `data/generate.py`
3,000 customers, 12 months (covers Diwali / Big Billion Days / end-of-season sales), ~300 products.
Personas (hidden, 8): loyal regular, deal hunter, about-to-leave VIP, window shopper, new user, cart abandoner,
points hoarder, festival-only buyer.

| Table | Key fields |
|---|---|
| `customers` | id, name, gender, age, city, city_tier, signup_date, device (android/ios/web), language, marketing_consent, telegram_chat_id (null unless a real tester is linked) |
| `customer_traits` (hidden, never shown to agent) | persona, price_sensitivity, churn_propensity, deal_affinity, spam_tolerance, category weights, active hours, true response per offer type (for the harness) |
| `products` | id, name, category, subcategory, brand, price, cost, margin_pct |
| `sessions` | id, customer_id, start, end, device, source (organic/notification/ad) |
| `browse_events` | session_id, customer_id, ts, type (view/search/wishlist_add), product_id, query |
| `cart_events` | customer_id, ts, type (add/remove/checkout/abandon), product_id, qty, cart_value |
| `orders` / `order_items` | id, customer_id, ts, gross, discount, net, offer_id, payment_method (wallet/UPI/card/COD), status (delivered/cancelled/returned) / product, qty, price |
| `wallet_txns` | customer_id, ts, type (topup/spend/refund/cashback), amount, balance_after |
| `points_ledger` | customer_id, ts, type (earn/redeem/expire), points, expires_at |
| `offers_history` (past campaigns) | id, customer_id, offer_type, value, category, sent_at, opened_at, clicked_at, redeemed_at, order_id, group (treatment/holdout, randomly assigned) |
| `calendar` | date, is_festival, festival_name, is_payday, is_weekend |
| `decisions` (written by pipeline) | id, customer_id, trigger, decided_by (agent/fallback), proposal, final_offer, status (sent/would_send/blocked/nothing), reasoning JSON, rule_checks JSON, expected_profit, tool_trace, ts |
| `testers` | telegram_username, link_token, customer_id, chat_id, linked_at |

Behaviour is generated from hidden traits, so patterns are real: deal hunters spike in sales, VIPs fade before
churning, cart abandoners leave carts, hoarders let points pile up, payday/weekend lift, category preferences.
Money stored as integer paise.

## 2. Analyst: `engine/features.py`
recency, frequency, monetary, avg order value, category affinity, active hours/weekdays, cart abandon rate,
discount dependence (% orders with discount), past offer response by type, churn risk, wallet balance + idle days,
points expiring in 7 days, offers received in last 7/30 days (fatigue), estimated P(buy) without offer.

## 3. Simulator: `events/simulator.py`
Advances a simulated clock; for real testers and random dataset customers it emits events consistent with their
profile. Dashboard can also fire a chosen event for any customer.

## 4. Agent: `engine/agent.py` (Claude tool use, latest Sonnet, model in config)
**This is the only part of the system that uses the Anthropic API key.** Data, features, profit, rules, fallback,
simulator, bot, dashboard and harness tests 1-4 are plain code (see CLAUDE.md section 3).
Tools: `get_customer_profile`, `get_recent_activity`, `get_offer_history`, `get_offer_limits` (what rules allow
for this customer now), `estimate_profit(offer)`, `propose_offer(reasoning, offer, message, send_at)` or
`propose_no_offer(reasoning)`.
Offer = building blocks: type (pct_off, flat_off, free_delivery, points_multiplier, cashback_to_wallet,
early_access), amount, product/category, min order, expiry (2-48h), send time.
**Reasoning (required, for the manager):** `summary`, `signals_used` (with values), `alternatives_considered`
(+ why rejected), `expected_impact` (P(buy) before → after, expected profit), `why_now`.
Customer data passed to the agent is treated as data; names/fields can't inject instructions.

## 5. Goal, profit math and guardrails: `config/rules.yaml` + `engine/rules.py`
**Goal:** maximise total expected incremental profit; every single offer must have positive expected profit.

**Profit formula (computed by code, not the AI):**
`expected_profit = P(buy|offer) × margin_after_offer − P(buy|no offer) × normal_margin`
(The second term counts the loss from giving discounts to people who would buy anyway.)

**Guardrails (hard, the agent can't override):**
| Group | Rules |
|---|---|
| Profit protection | expected_profit ≥ ₹20 minimum; order margin after offer ≥ 10%; category caps (electronics 8%, fashion 30%, beauty 25%, home 20%, grocery 10%); min order value per offer type; no discount if P(buy without offer) > 0.7 (only zero-cost perks like points/early access) |
| Budget | daily offer budget, monthly budget, per-customer monthly cap; auto-pause (circuit breaker) when budget hit or blocked rate spikes |
| Anti-spam | max 2 offers/week, 24h gap, quiet hours 22:00-08:00 (customer local), respect marketing_consent=false |
| Integrity | one active offer per customer (no stacking), expiry 2-48h, product must exist and be in stock category |
| Fairness | loyal customers never get a worse offer than a new customer for the same item; no offer on an item bought at full price in last 7 days |
| Message safety | amount/expiry in text must match the offer; no fake urgency/scarcity; no made-up claims; length limit |

Outcomes: approve, **repair** (e.g. clamp 25% → cap, move send time out of quiet hours), reject (reason fed back).

**Fallback (`engine/fallback.py`)**: conservative table, still validated: cart abandoned → free delivery if
margin allows; inactive 30d + high value → 10% comeback within caps; points expiring → reminder (zero cost);
else nothing.

## 6. Delivery: `bot/telegram_bot.py` (one bot)
Dashboard Testers page: enter Telegram username + pick customer profile → link `t.me/<bot>?start=<token>`.
Tester taps link, presses Start; bot checks username matches, saves chat_id. Offers for linked customers arrive
as clean formatted messages. Everyone else: "would send" in the dashboard.
**Re-link:** the manager can switch a linked tester to a different customer profile anytime; the next decision
for the new profile goes to that tester's Telegram (used to compare offers across personas on a friend's phone).

## 7. Company dashboard: `dashboard/` (React + Vite + Tailwind, dark product look) + `api/main.py` (FastAPI)
- **Overview:** offers decided / sent / blocked / nothing, spend vs budget, total expected profit, agent vs fallback
- **Decisions feed:** card per decision → drawer with reasoning (summary, signals, alternatives, expected
  impact, why now), rule checks ✅🔧❌, profit math, tool trace
- **Customers:** search, profile + scores + history timeline, **Run agent** button, fire event
- **Guardrails:** block/repair reasons chart, budget gauge, circuit breaker status
- **Testers:** create link, see linked testers, **re-link a tester to a different customer profile**
- **Rules:** marketing team edits rule values (versioned, safety-bounded; pending offers re-checked)
- **Plan your day:** greeting + number of AI decisions for today/tomorrow + estimated API cost

> Later additions (habit goal, editable rules, Day Plan, gatekeeper, Opus model) are described in full in
> CLAUDE.md, which is the master document.

## 8. Harness: `harness/` (pytest)
1. **Rule tests:** pass/fail/edge for every guardrail
2. **Profit property tests (hypothesis):** thousands of random proposals → anything approved always has
   margin ≥ floor, expected_profit ≥ min, within budget. **Zero exceptions allowed.**
3. **Adversarial:** injection in customer name/search query, agent asks 90%, amount mismatch in message,
   fake urgency, invalid product, quiet-hour send → all blocked or repaired
4. **Fallback:** API down / timeout / malformed output / 2 rejections → fallback used, still validated
5. **Agent scenario eval:** ~16 golden scenarios (2 per persona) with expected behaviour (e.g. loyal regular
   → no discount; about-to-leave VIP → comeback; buys-anyway → nothing) → score report
6. **Profit simulation:** run agent+rules vs send-to-all vs fallback-only on a sample of customers, scored with
   hidden true responses → total profit per policy (agent+rules must not lose money)

## Project layout
```
CLAUDE.md, TESTING.md, .env.example, requirements.txt
config/offers.yaml, config/rules.yaml, config/settings.py
data/generate.py
db/schema.sql, db/store.py
engine/features.py, engine/profit.py, engine/rules.py, engine/agent.py, engine/fallback.py, engine/decide.py
events/simulator.py
bot/telegram_bot.py
api/main.py
dashboard/  (React app)
harness/test_rules.py, test_profit_properties.py, test_adversarial.py, test_fallback.py,
        eval_agent_scenarios.py, simulate_profit.py
```

## Day 1: MVP (end-to-end working)
1. `CLAUDE.md`, `requirements.txt`, `.env.example`, configs
2. Schema + detailed data generator (3,000 customers, 12 months)
3. Analyst features + profit formula (heuristic P(buy) from features + past offer responses)
4. Rules validator + fallback + `decide.py`
5. Agent with tools + structured reasoning + retry loop
6. Simulator + Telegram bot with tester linking
7. FastAPI + dashboard: Overview, Decisions feed + reasoning drawer, Customers (Run agent), Testers
✅ Done when: run agent on a customer from the dashboard → decision + reasoning shown → linked tester gets
the Telegram message.

## Day 2: Improve + test
1. Harness 1-6 above, fix everything they find
2. Better P(buy) estimates: train simple uplift model (sklearn, two-model) on `offers_history` treatment vs holdout
3. Guardrails page, circuit breaker, budget gauge, UI polish
4. Prompt tuning for clearer manager reasoning
5. Real tester rollout per `TESTING.md`

## CLAUDE.md (first file created after approval)
```markdown
# OfferPilot
Agentic offer-decision pipeline for e-commerce. Company dashboard + one Telegram bot. No customer UI.

## Pipeline
data (SQLite) → engine/features (analyst) → events/simulator (triggers) → engine/agent (Claude tools)
→ engine/rules (validator) → engine/fallback (backup) → bot (Telegram) / "would send" → api + dashboard

## Non-negotiable rules
- The company must never lose profit: every approved offer has expected_profit ≥ min and margin ≥ floor.
- Every offer (agent OR fallback) passes engine/rules.py. Nothing reaches bot/ without a validator Decision.
- Profit math lives in engine/profit.py (code), never computed by the LLM.
- Guardrail values live only in config/rules.yaml; no hard-coded limits elsewhere.
- customer_traits is hidden ground truth: only data/generate.py and harness/ may read it. Never the agent.
- Every decision is logged in `decisions` with reasoning, rule_checks, decided_by, expected_profit.
- Money is integer paise. Times are stored UTC, shown IST.
- Secrets only in .env (ANTHROPIC_API_KEY, TELEGRAM_BOT_TOKEN, CLAUDE_MODEL).

## Commands
python -m data.generate --customers 3000 --months 12
python -m bot.telegram_bot
uvicorn api.main:app --reload
cd dashboard && npm run dev
python -m events.simulator --speed 60
pytest harness/
python -m harness.eval_agent_scenarios
python -m harness.simulate_profit

## Conventions
Python 3.11, type hints, small pure functions in engine/. Agent output is validated with pydantic.
Tests for every new guardrail. Keep scope: no customer UI, no bargaining, no extras.
```

## TESTING.md (created after approval): real tester guide
1. Create the bot with @BotFather → put token in `.env`
2. Self test: dashboard → Testers → your username + pick "about-to-leave VIP" → open link → Start → Run agent →
   message arrives; check reasoning on dashboard
3. 2-3 friends: each gets a different persona (deal hunter, loyal regular, cart abandoner) → fire the same
   event for all → confirm **different** decisions (loyal regular should often get nothing or a zero-cost perk)
4. Guardrail checks live: fire 3 events in one hour for one tester → only 1 offer; fire at 23:00 → send time moved
   to 08:00; set a tiny daily budget → circuit breaker pauses
5. Kill the API key → fallback message still arrives, shown as `decided_by: fallback`
6. Record results in a simple table (tester, persona, event, decision, correct?)

## Verification
- `pytest harness/` all green, including zero-exception profit property tests
- `eval_agent_scenarios` ≥ 14/16 scenarios behave as expected
- `simulate_profit`: agent+rules total profit > send-to-all and > 0
- Live: Run agent from dashboard → reasoning drawer complete → linked tester receives Telegram offer
