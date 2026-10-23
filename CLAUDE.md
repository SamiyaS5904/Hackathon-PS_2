# OfferPilot: Project Constitution

This is the master document. It describes the end goal, the rules every part of the code must follow, and the
order in which we build. Read this before touching any file.

| File | What it is |
|---|---|
| [PLAN.md](PLAN.md) | Full approved plan (what we build) |
| [reasoning.md](reasoning.md) | Why we build it this way, and how each part works |
| [documentation.md](documentation.md) | Running log of every step done, how to run it, decisions made |
| [TESTING.md](TESTING.md) | Automatic tests + real-tester guide |

---

## 🚦 Approval gate (read first)
- **No code is written until the user approves this CLAUDE.md.** Status: **APPROVED (2026-10-22)**.
- Open discussion items (see "Open decisions" at the bottom) must be settled with the user first.
- When approved, change the status to `APPROVED (<date>)` and log it in documentation.md.
- Any later change to this file also needs the user's approval before it is followed.

---

## 1. What we are building (end goal)
An **agentic offer-decision system for an e-commerce company**.

The company has years of customer data: shopping history, browsing, carts, wallet, reward points, and how people
reacted to past offers. OfferPilot reads that data, and an **AI agent decides which offer each customer should get,
and when**. Plain rule code checks every decision so the company **never loses profit**. Offers are delivered
through **one Telegram bot**. A **company dashboard** lets the marketing manager see every decision and the
**clear reasoning** behind it.

There is **no customer-facing UI**. Customers only receive Telegram messages.

### What "done" looks like (the final demo)
1. The manager opens the dashboard and runs the agent for a customer, or the simulator triggers it.
2. The agent studies the customer and decides: send an offer (what, how much, on what, when, which message) or
   send nothing.
3. The rules check approves, repairs or rejects it. If the agent fails, the fallback decides instead.
4. The manager opens the decision and reads **clear reasoning**: facts used, options considered and rejected,
   expected profit, rule checks.
5. If that customer is linked to a real tester, the offer arrives on the tester's Telegram.
6. The manager **re-links a friend's Telegram username to a different customer profile** (e.g. "loyal regular"
   → "about-to-leave VIP"), runs the agent again, and a **different** offer arrives on that friend's Telegram,
   with its own reasoning.
7. The harness proves: rules never break, tricks are blocked, and agent+rules earns more than simple approaches
   without ever losing money.

### Business goal: keep customers ordering, profitably
- **Grow repeat ordering and protect habits.** We want more customers ordering regularly. When someone's ordering
  habit breaks (they're late compared to their usual gap), win them back with a good offer, preferably on
  **their favourite item** (something they've ordered many times) or favourite category.
- **Never lose profit on an offer (hard rule).** Send an offer only when it creates extra profit. Never send to
  people who would buy anyway, never go beyond budget or margin limits, never spam.

Agent priorities, in order:
1. **Habit break:** customer is overdue vs their usual order gap → offer on a favourite item/category
2. **Cart abandoned:** offer tied to the item left in the cart
3. **Zero-cost nudges:** points expiring, unused wallet balance, early access
4. **Send nothing** to people who would buy anyway, or when no offer makes a profit

---

## 2. Pipeline
```
[1 DATA]       data/generate.py → SQLite (3,000 customers, 12 months, 13 tables)
[2 ANALYST]    engine/features.py → one profile per customer
[3 TRIGGER]    events/simulator.py  or  dashboard "Run agent"
[3b GATEKEEPER] engine/gatekeeper.py (no API): skip customers no offer could pass (no consent, weekly limit hit,
               24h gap, circuit breaker); rank the rest by priority; check today's Day Plan. Within the plan →
               agent. Plan used up → fallback (free).
[4 AGENT]      engine/agent.py (Claude, tool use)            ← the ONLY part that uses the API key
[5 RULES]      engine/rules.py: approve / repair / reject (reason back to agent, max 2 retries)
[5b FALLBACK]  engine/fallback.py: if agent fails → simple rule table (also checked by rules.py)
[6 DELIVERY]   bot/telegram_bot.py: linked tester → Telegram; others → "would send"
[7 DASHBOARD]  api/main.py (FastAPI) → dashboard/ (React)
[HARNESS]      harness/: proves all of the above
```

## 3. API key boundary
**The Anthropic (Claude) API key is used in exactly one place: `engine/agent.py`.** Everything else is plain code.

| Part | Uses Claude API? | Notes |
|---|---|---|
| Data generator | ❌ No | Python + random generation from hidden traits |
| Analyst (features) | ❌ No | Pure calculations |
| Profit math | ❌ No | Pure calculations, always code |
| Rules checker | ❌ No | Must be 100% predictable |
| Fallback | ❌ No | Fixed rules; messages and reasoning come from templates |
| Simulator | ❌ No | Random events from profiles |
| **Agent** | ✅ **Yes** | Decides the offer, writes the message and the reasoning |
| Telegram bot | ❌ No | Uses only `TELEGRAM_BOT_TOKEN` |
| API + dashboard | ❌ No | Reads the database |
| Harness tests 1-4 | ❌ No | Use a fake agent that returns scripted proposals |
| Harness 5-6 (agent eval, profit simulation) | ✅ Yes | Run on demand only (see Open decisions) |

**Model:** Claude Sonnet 5.5 (`CLAUDE_MODEL=claude-sonnet-5-5`), effort `medium`. Changed from Opus on 2026-10-24
with the user's approval to cut cost (~Rs 6 -> target ~Rs 1.5 per decision). Changeable in `.env`.

**One-call design:** before calling the AI, code builds and prices 6-10 **candidate offers** (`engine/candidates.py`,
each already checked by the rules except the message). They go into the AI's first message with the profile and
limits, so the AI usually picks or adjusts one and calls `propose_offer` / `propose_no_offer` in its first reply.
All 7 tools stay available for the cases where the AI wants an idea that isn't in the list.

**API cost guard:**
- The agent runs **only** when a trigger fires or the manager clicks "Run agent". Never for all 3,000 customers at once.
- **Day Plan (dashboard):** when the manager opens the dashboard they see a greeting ("Good morning! Plan your
  day") and set **how many AI decisions to allow today and tomorrow** (default 50). The planner shows the
  estimated API cost for that number. Saved in the `day_plans` table.
- `MAX_AGENT_RUNS_PER_DAY=50` in `.env` is the **hard ceiling**: the Day Plan can be lower, never higher.
- When today's plan is used up, new triggers go to the fallback (free). The dashboard shows "AI decisions used
  today: X / plan".
- The gatekeeper (no API) filters out customers who can't receive an offer anyway **before** calling the agent,
  so no API call is wasted on them.
- Every agent decision logs its real token usage and cost (`input_tokens`, `output_tokens`, `cost_usd`) in the
  `decisions` table. The dashboard shows cost per decision and today's total.
- Timeout: 30s per API call (`AGENT_TIMEOUT_SECONDS`). Max 6 tool turns per decision. (Changed from 10s on
  2026-10-23 with the user's approval: real Opus calls took up to 15s, so 10s would have failed most decisions.)
- No API key in `.env` → the whole system still works, using the fallback.
- Harness tests that use the agent (5 and 6) run only when the user asks, on a small sample, and **save the
  agent's answers** so re-running them later is free.

---

## 4. Rules (guardrails), in full detail
All values below are **starting values**.

### 4.0 The marketing team can edit the rules
- Default values live in `config/rules.yaml`. On first run they're copied into the database as **rules version 1**.
- The dashboard has a **Rules page** where the marketing team edits values (e.g. electronics cap 8% → 12% because
  electronics margins went up, or daily budget ₹5,000 → ₹8,000).
- Every save creates a **new rules version** (who, when, old → new value). Code always reads the **active version**
  through one function (`engine/rules_store.py: get_active_rules()`); no limits are hard-coded anywhere else.
- **Offers get revised when rules change:**
  - The next agent decision sees the new limits through `get_offer_limits` (e.g. it may now offer up to 12%).
  - Offers decided but **not yet sent** (e.g. waiting for quiet hours to end) are re-checked against the new
    version before sending. If they now break a rule, they are repaired or cancelled.
  - Every decision records which rules version it was checked against.
- **Safety bounds on edits (so an edit can't make the company lose money):** the Rules page refuses values like a
  margin floor below 0%, a category cap above that category's average margin minus the margin floor, a negative
  budget, or more than 7 offers per week.

### 4.1 Profit protection (most important)
| Rule | Starting value | If broken |
|---|---|---|
| Expected profit (calculated in `engine/profit.py`) | ≥ ₹20 per offer | ❌ reject |
| Order margin after offer | ≥ 10% of order value | ❌ reject (🔧 repair if lowering the amount fixes it) |
| Category discount cap | electronics 8%, fashion 30%, beauty 25%, home 20%, grocery 10% | 🔧 repair: clamp to cap |
| "Buys anyway" customer: P(buy without offer) > 0.7 | Only zero-cost perks allowed (points multiplier, early access) | ❌ reject discounts |
| Flat discount | ≤ category cap % of min order value | 🔧 repair |
| Free delivery | Cost ₹40, min order ₹299 | 🔧 repair min order |
| Cashback to wallet | ≤ ₹200 and ≤ category cap | 🔧 repair |
| Points multiplier | ≤ 3x | 🔧 repair |

**Profit formula:** `expected_profit = P(buy|offer) × margin_after_offer − P(buy|no offer) × normal_margin`

### 4.2 Budget
| Rule | Starting value | If broken |
|---|---|---|
| Daily offer budget | ₹5,000 (marketing team sets the real value on the Rules page) | ❌ reject |
| Monthly offer budget | ₹1,00,000 | ❌ reject |
| Per-customer monthly cap | ₹500 | ❌ reject |
| Circuit breaker | Pause all offers if daily budget is used up, or more than 50% of the last 20 decisions were blocked | ⏸️ pause + dashboard alert |

### 4.3 Anti-spam
| Rule | Starting value | If broken |
|---|---|---|
| Offers per customer | max 2 per 7 days | ❌ reject |
| Gap between offers | 24 hours | ❌ reject |
| Quiet hours | 22:00-08:00 IST | 🔧 repair: move send time to 08:00 |
| Marketing consent | `marketing_consent = false` → never send | ❌ reject |

### 4.4 Integrity
| Rule | If broken |
|---|---|
| Only one active offer per customer (no stacking) | ❌ reject |
| Expiry between 2 and 48 hours | 🔧 repair to nearest limit |
| Product/category must exist in the catalog | ❌ reject |
| Offer type must be one of the allowed building blocks | ❌ reject |

### 4.5 Fairness
| Rule | If broken |
|---|---|
| Loyal customer (≥5 orders) never gets a lower-value offer than a new customer got for the same category in the last 7 days | 🔧 repair up to match (if profit still ≥ min), else ❌ reject |
| No offer on a product the customer bought at full price in the last 7 days | ❌ reject |

### 4.6 Message safety
| Rule | If broken |
|---|---|
| Amount, expiry and product in the message must match the actual offer | ❌ reject |
| No fake urgency or scarcity ("only 1 left", "last chance", "hurry") | ❌ reject |
| No claims that aren't in the catalog data | ❌ reject |
| Max 300 characters, no links | ❌ reject |

### 4.7 How the checker answers
- ✅ **approve:** send as is
- 🔧 **repair:** a small, safe fix; logged as "repaired: what changed and why"
- ❌ **reject:** the reason goes back to the agent (max 2 retries), then the fallback decides
- Every check is saved in `rule_checks` (rule name, passed/repaired/failed, detail) and shown on the dashboard.

### 4.8 Fallback rules (no API)
Checked in this order; the first one that fits wins.

| # | Situation | Fallback decision | Template |
|---|---|---|---|
| 1 | Habit break (overdue ≥ 1.5× usual order gap) and has a favourite item | 10% off favourite item, within category cap | `habit_favourite` |
| 2 | Cart abandoned | Free delivery, if margin allows | `cart_free_delivery` |
| 3 | Inactive ≥ 30 days and high value | 10% comeback on favourite category, within cap | `comeback_category` |
| 4 | Points expiring in ≤ 7 days | Reminder (zero cost) | `points_expiring` |
| 5 | Wallet balance ≥ ₹200 unused for 30 days | Reminder (zero cost) | `wallet_reminder` |
| 6 | Anything else | Send nothing | - |

Used when: API error, timeout, 2 rejections, invalid agent output, no API key, or the daily agent limit is reached.
Fallback offers **also** pass the rules check. Fallback reasoning is template text naming the rule that fired.

**Fixed message templates** (in `config/templates.yaml`; `{...}` is filled by code from the real offer, so the
message always matches the offer; no fake urgency):
| Template | Message |
|---|---|
| `habit_favourite` | Hi {first_name}! Time to restock your favourite {product_name}? Get {discount} off today, valid till {expiry}. |
| `cart_free_delivery` | Hi {first_name}, your {product_name} is still in your cart. Get FREE delivery on orders above {min_order}, valid till {expiry}. |
| `comeback_category` | We miss you, {first_name}! Here's {discount} off on {category} (min order {min_order}), valid till {expiry}. |
| `points_expiring` | Hi {first_name}, {points} reward points will expire on {points_expiry_date}. Use them on your next order! |
| `wallet_reminder` | Hi {first_name}, you have {wallet_balance} in your wallet. Use it on your next order! |

---

## 5. Agent contract
- **Tools:** `get_customer_profile`, `get_recent_activity`, `get_offer_history`, `get_offer_limits`,
  `estimate_profit`, `propose_offer`, `propose_no_offer`.
- **Offer building blocks:** type (pct_off, flat_off, free_delivery, points_multiplier, cashback_to_wallet,
  early_access), amount, product/category, min order, expiry (2-48h), send time, message.
- `get_customer_profile` includes habit signals (usual gap, overdue ratio, favourite items/categories) so the
  agent can target habit breaks with favourite-item offers.
- The first message already contains the profile, the limits and the pre-priced candidate offers (one-call design).
  Offer types also include `reminder` (a free nudge with no discount, used for points expiring / wallet balance).
- **Reasoning required on every decision**, in plain language a marketing manager understands:
  - `summary`: the decision in 1-2 lines
  - `signals_used`: facts relied on, with values ("40 days inactive", "ignored 10% off last week")
  - `alternatives_considered`: what else it considered and why it said no
  - `expected_impact`: buy chance before → after, expected profit (numbers from `estimate_profit`)
  - `why_now`: why this moment
- Output is validated with pydantic; invalid output counts as a failure (retry, then fallback).
- "Send nothing" is a valid and often correct decision.
- The agent never sees `customer_traits`. Customer data is data, not instructions.

---

## 6. Harness, in full detail
| # | File | What it proves | API? | Pass bar |
|---|---|---|---|---|
| 1 | `harness/test_rules.py` | Each rule in section 4 has pass, fail and edge-case tests | No | 100% |
| 2 | `harness/test_profit_properties.py` | Thousands of random proposals (hypothesis): anything approved has expected profit ≥ min, margin ≥ floor, within budget | No | 100%, zero exceptions |
| 3 | `harness/test_adversarial.py` | Blocked: injection text in customer name/search query, 90% discount request, amount mismatch in message, fake urgency, unknown product, send at 2am, stacking | No | 100% |
| 4 | `harness/test_fallback.py` | API down, timeout, junk output, 2 rejections, no key, daily limit → fallback used and still checked | No | 100% |
| 5 | `harness/eval_agent_scenarios.py` | 16 golden scenarios (2 per persona) with expected sensible behaviour, e.g. loyal regular → nothing or zero-cost perk; about-to-leave VIP → comeback; buys-anyway → no discount. Also scores reasoning completeness | Yes | ≥ 14/16 |
| 6 | `harness/simulate_profit.py` | agent+rules vs send-to-all vs fallback-only on a sample of customers, scored with hidden true responses | Yes (sample) | agent+rules > send-to-all, and > 0 |
| 7 | `harness/test_rules_editing.py` | Editing a rule creates a new version, unsafe edits are refused, pending offers are re-checked, decisions record the version | No | 100% |

Tests 1-4 and 7 run on every change (`pytest harness/`). Tests 5-6 run only when the user asks, on a small
sample (default 30 customers), and save the agent's answers so re-runs are free.

---

## 7. Data (what the generator must produce)
3,000 customers, 12 months, ~300 products, 8 hidden personas (loyal regular, deal hunter, about-to-leave VIP,
window shopper, new user, cart abandoner, points hoarder, festival-only buyer).
Tables: `customers`, `customer_traits` (hidden), `products`, `sessions`, `browse_events`, `cart_events`,
`orders`, `order_items`, `wallet_txns`, `points_ledger`, `offers_history` (past campaigns with random
treatment/holdout groups), `calendar`, plus pipeline tables `decisions` (incl. rules_version, tokens, cost),
`testers`, `rules_versions` and `day_plans`. Full field list in PLAN.md.
Behaviour comes from the hidden traits (festival spikes, payday/weekend lift, category preferences, fading VIPs).
Customers must have **repeat purchases of favourite items** and a **usual order gap**, and some customers' habits
must break during the year, so habit-break offers have real data behind them. No phone numbers.

**Analyst signals for habits** (`engine/features.py`): usual order gap (median days between orders), days since
last order, overdue ratio (days since last order ÷ usual gap), favourite items (ordered ≥ 3 times), favourite
categories, habit strength (how regular their gaps are).

---

## 8. Testers (real Telegram users)
- A Telegram bot can't message anyone first. Each tester opens their link `t.me/<bot>?start=<token>` and presses Start once.
- Dashboard → Testers: enter username + choose customer profile → create link. The bot checks the username
  matches, then saves the chat_id.
- **Re-link:** the manager can switch a linked tester to a different customer profile anytime. The next decision
  for the new profile goes to that tester's Telegram.
- The user will provide 2-3 real Telegram usernames later.

---

## 9. Build roadmap (sessions)
Each session ends with: working code, tests where relevant, a documentation.md entry, small commits, and a quick
check with the user before moving on.

| Session | Day | What gets built | User checks |
|---|---|---|---|
| 0 | - | Approve CLAUDE.md, settle open decisions, git init, `.gitignore`, `requirements.txt`, `.env.example` | Approves CLAUDE.md |
| 1 | 1 | `config/offers.yaml`, `config/rules.yaml`, `db/schema.sql`, `data/generate.py` (full detailed dataset) | Looks at sample data and persona patterns |
| 2 | 1 | `engine/features.py` (incl. habit signals), `engine/profit.py`, `engine/rules_store.py`, `engine/rules.py`, `engine/fallback.py` + `config/templates.yaml`, `engine/gatekeeper.py` (pre-filter + Day Plan), `engine/decide.py` (fallback-only) + rule tests | Runs a fallback decision from the command line |
| 3 | 1 | `engine/agent.py` (Claude tools, reasoning, retry loop) wired into `decide.py` | Reads agent reasoning for 3-4 customers |
| 4 | 1 | `bot/telegram_bot.py` (linking, re-linking, sending) + `events/simulator.py` | Gets a real offer on their own Telegram |
| 5 | 1 | `api/main.py` + dashboard: Overview, Decisions + reasoning drawer, Customers (Run agent), Testers (link / re-link), **Rules page (edit rules)**, **Plan your day** (greeting + AI decisions for today/tomorrow + cost estimate) | **Day 1 MVP demo** end to end |
| 6 | 2 | Harness 1-6, fix everything it finds | Sees all tests green + profit report |
| 7 | 2 | Uplift model for better P(buy), Guardrails page, circuit breaker, UI polish, clearer reasoning prompts | Reviews dashboard and reasoning quality |
| 8 | 2 | Real tester rollout per TESTING.md (self → 2-3 friends, re-link demo) | **Final demo** |

---

## 10. Code style: clear files with logic and comments
The user must be able to open any Python file and understand what it does and why.
- **Every file starts with a module docstring**: what it does, where it sits in the pipeline, which files call it
  and which it calls, and the key idea behind it.
- **Every function has a docstring**: inputs, output, and the business reason it exists.
- **Comment the business logic**: explain *why* (e.g. "skip discount: this customer buys anyway, a coupon would
  only lose margin"), not just *what*. Mark planned parts with `# TODO(session N): ...`.
- Clear names (`expected_profit_paise`, not `ep`). Python 3.11+ (dev machine: 3.12), type hints, small pure
  functions in `engine/`.
- Add a harness test for every new rule.

## 11. Git workflow: small commits
- The user will share a GitHub repo. Until then, commit locally; push only after the user gives the remote URL.
- **Commit after every small working step** (one file or one feature), never one giant commit.
- Message format: `<area>: <what changed>`, e.g. `data: add customers and products generator`,
  `engine: add margin floor rule`, `docs: log session 2`.
- Each commit message ends with: `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`
- Never commit `.env`, the SQLite database file or `node_modules/` (all in `.gitignore`).
- No force push, no amending pushed commits.

## 12. Documentation workflow
- After every step, add an entry to **documentation.md**: date, session, what was built, files touched, how to run
  or check it, decisions made and why, what's next. Commit it with that step.
- When a design decision changes, update **reasoning.md** so the "why" always matches the code.

## 13. Non-negotiable rules (summary)
- Every approved offer: expected profit ≥ min and margin ≥ floor. No exceptions.
- Every offer, from agent **or** fallback, passes `engine/rules.py` before reaching `bot/`.
- Profit math is code (`engine/profit.py`), never the LLM.
- Limits are read only through `get_active_rules()` (defaults in `config/rules.yaml`, edits from the Rules page
  saved as new versions). Never hard-code a limit.
- The Claude API is used only in `engine/agent.py` (and on-demand harness 5-6).
- `customer_traits` is hidden: only `data/generate.py` and `harness/` read it.
- Every decision is logged with reasoning, rule checks, decided_by, expected profit and tool trace.
- Money in integer paise; times stored UTC, shown IST.
- Secrets only in `.env` (`ANTHROPIC_API_KEY`, `TELEGRAM_BOT_TOKEN`, `CLAUDE_MODEL`, `MAX_AGENT_RUNS_PER_DAY`).

## 14. Commands
```bash
pip install -r requirements.txt
python -m data.generate --customers 3000 --months 12
python -m bot.telegram_bot
uvicorn api.main:app --reload
cd dashboard && npm install && npm run dev
python -m events.simulator --speed 60
pytest harness/
python -m harness.eval_agent_scenarios
python -m harness.simulate_profit --sample 300
```

## 15. Scope
Out of scope: customer UI, mini shop/wallet for testers, bargaining, phone numbers, Telegram groups, extras.

---

## 16. Decisions settled with the user (2026-10-22)
1. **Agent tests and cost:** harness 5-6 run only on request, on a small sample, with saved answers for free re-runs.
2. **Daily agent limit:** set each day on the dashboard Day Plan (default 50); `MAX_AGENT_RUNS_PER_DAY=50` is the
   hard ceiling. Overflow goes to the fallback. Real cost per decision is logged and shown.
3. **Model:** Claude Sonnet 5.5 (`claude-sonnet-5-5`) with the one-call design (section 3). Was Opus until
   2026-10-24; switched by the user for cost.
4. **Fallback messages:** fixed generic templates (section 4.8).
5. **Rules:** starting values in section 4; the marketing team edits them from the dashboard Rules page (section 4.0).
6. **Business goal:** keep customers ordering and protect habits; win back lapsed customers with offers on their
   favourite items (section 1).
7. **GitHub repo:** https://github.com/SamiyaS5904/Hackathon-PS_2 (small, clear commits, pushed after each step).
8. **AI timeout (2026-10-23):** 30 seconds per API call. A timeout is only a deadline: it doesn't change the AI's
   output or the cost of a successful call; too short a timeout wastes calls and sends decisions to the fallback.
