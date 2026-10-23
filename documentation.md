# OfferPilot: Build Log

Every step of the project is recorded here: what was built, how to run or check it, and decisions made.
Newest entries at the bottom.

**Entry template**
```
## <date>: Session <N>: <short title>
**Built:** ...
**Files:** ...
**How to run / check:** ...
**Decisions & why:** ...
**Commits:** ...
**Next:** ...
```

---

## 2026-10-22: Session 0: Planning and discussion
**Built:** No code yet. Planning documents only.

**Files:**
- `CLAUDE.md`: project constitution: end goal, pipeline, API key boundary, full rules, harness, session roadmap,
  code style, git and documentation workflow
- `PLAN.md`: approved plan with full data tables, profit formula, guardrails, harness
- `reasoning.md`: why and how for every part
- `TESTING.md`: automatic tests + real-tester guide
- `documentation.md`: this log

**Decisions & why:**
- **Scope is only the decide-and-send layer.** No customer UI, mini shop, wallet, bargaining, phone numbers or
  Telegram group. Keeps a 2-day solo build focused.
- **#1 goal is never losing profit.** Every offer must have positive expected profit, calculated by code.
- **Claude API key is used only by the agent.** Everything else is plain code, so it's predictable, free to run,
  and still works if the API is down (fallback).
- **3,000 synthetic customers, 12 months, 8 hidden personas.** Enough for clear patterns, fast to generate;
  hidden traits let the harness check whether the agent was right.
- **Reasoning is for the marketing manager**, shown on the dashboard for every decision.
- **One Telegram bot.** Testers link via a special link and press Start once (Telegram rule); the manager can
  re-link a tester to a different customer profile to compare offers.
- **Day 1 = MVP, Day 2 = improve + test.** Build roadmap: Sessions 0-8 in CLAUDE.md.

**Commits:** none yet (git is set up in Session 0 after CLAUDE.md approval; GitHub remote to be shared by the user).

**Next:** user reviews and approves CLAUDE.md, settles the open decisions in CLAUDE.md section 16, then Session 1.

---

## 2026-10-22: Session 0: Decisions settled, repo connected
**Built:** Planning docs updated with the user's decisions; git set up and connected to GitHub.

**Files:** `CLAUDE.md`, `reasoning.md`, `PLAN.md`, `TESTING.md`, `documentation.md`, `.gitignore`,
`requirements.txt`, `.env.example`

**Decisions & why:**
- **Model: Claude Opus 5.5**, for the strongest reasoning. ≈ $0.07-0.10 per AI decision (estimate in reasoning.md §17).
- **Plan your day:** the manager sets how many AI decisions to allow today and tomorrow (default 50; hard ceiling 50
  in `.env`). Overflow goes to the free fallback. Keeps API cost under control.
- **Gatekeeper (no API)** filters out customers who can't get an offer before calling the agent, so no call is wasted.
- **Business goal: keep the ordering habit.** Overdue customers get offers on their favourite items first.
- **Editable rules:** the marketing team edits limits on a dashboard Rules page; every save is a new version;
  unsafe values are refused; pending offers are re-checked.
- **Fallback messages:** 5 fixed generic templates (CLAUDE.md §4.8).
- **Repo:** https://github.com/SamiyaS5904/Hackathon-PS_2

**Next:** user gives final approval of CLAUDE.md → Session 1 (configs, schema, data generator).

---

## 2026-10-22: Session 1: Configs, schema and synthetic data
**Built:**
- Offer building blocks (6 offer types and their cost models) and default guardrail rules
- Settings loader (`.env`), 17-table SQLite schema, DB helper
- Synthetic data generator: product catalog (280 products, fictional brands), Indian names/cities, festival
  calendar, 8 hidden personas, day-by-day simulation of 3,000 customers over 12 months

**Files:** `config/offers.yaml`, `config/rules.yaml`, `config/settings.py`, `db/schema.sql`, `db/store.py`,
`data/catalog.py`, `data/calendar_gen.py`, `data/personas.py`, `data/generate.py`, `.gitattributes`

**How to run / check:**
```bash
python -m data.generate --customers 3000 --months 12          # add --force to rebuild
```
Takes ~80s, writes `data/offerpilot.db` (~170 MB, not committed) and prints a summary.

**Result of the full run (seed 42, window 2025-10-22 to 2026-10-21):**
| Table | Rows |
|---|---|
| customers | 3,000 |
| sessions | 277,049 |
| browse_events | 1,194,656 |
| cart_events | 325,371 |
| orders / order_items | 35,785 / 76,920 |
| wallet_txns | 9,449 |
| points_ledger | 49,739 |
| offers_history (26 past campaigns) | 19,683 |

Broken links between tables: **0**.

**Patterns verified:**
- Loyal regulars order every ~15 days; VIPs every ~12 days but **86% are now overdue** (habit broke).
- Points hoarders lose ~2,100 points each to expiry; cart abandoners abandon ~32 carts each.
- Festival days: 139 orders/day vs 88 on normal days.
- **Effect of past offers (sent vs holdout, buy within 48h):** deal hunters +12.2 pts, festival-only +8.3,
  VIPs +7.6, new users +6.5, cart abandoners +5.5, points hoarders +5.3, window shoppers +0.8,
  **loyal regulars +0.5** (they buy anyway, so offers are wasted on them). This is exactly the signal the agent
  and the profit math must use.

**Decisions & why:**
- No Faker dependency: own name/city lists (fewer installs, deterministic).
- Product margins chosen so default category caps leave room above the 10% margin floor; grocery is
  deliberately tight (18-28% margin, 10% cap) so the margin guardrail has real work to do.
- Visits per day lowered ~40% after a 300-customer trial: the first version would have made a ~400 MB database.
- Foreign keys are checked once after loading (orders and offers reference each other), result: 0 problems.

**Commits:** config, db and data modules each committed separately (see `git log`).

**Next:** Session 2: analyst features (habit signals), profit math, rules store + checker, fallback +
templates, gatekeeper + Day Plan, `decide.py` (fallback-only), rule tests.

---

## 2026-10-23: Session 2: Analyst, profit math, rules, fallback, gatekeeper, pipeline
**Built:** the whole decision pipeline except the AI. Every decision is explained and logged.

**Files:**
- `engine/clock.py` (UTC storage / IST display, `OFFERPILOT_NOW` to freeze time), `engine/catalog.py`,
  `engine/models.py` (pydantic `OfferProposal`, `RuleCheck`, `CheckResult`, `ProfitEstimate`)
- `engine/features.py`: analyst profile (habit: usual gap, overdue ratio, habit strength/status; favourites;
  shopping hour; browsing; open cart; deal-seeker score; wallet; points expiring; offer response; fatigue;
  48h "buys anyway" chance)
- `engine/profit.py`: expected profit vs sending nothing, offer costs, uplift heuristic, max amount for margin floor
- `engine/rules_store.py`: rules versions, safety bounds, diff, `get_active_rules()`
- `engine/rules.py`: the checker (approve / repair / reject) for all rule groups in CLAUDE.md section 4
- `engine/fallback.py` + `config/templates.yaml`: 5-rule fallback table with fixed templates
- `engine/gatekeeper.py`: free pre-filter, circuit breaker, Day Plan (AI decisions per day, capped by `.env`)
- `engine/decide.py`: pipeline + command-line tool + `recheck_scheduled()` after rule edits
- `harness/conftest.py`, `test_rules.py`, `test_rules_editing.py`, `test_fallback.py`: **67 tests, all passing**

**How to run / check:**
```bash
python -m engine.decide --showcase --dry-run      # one example per situation, not saved
python -m engine.decide --customer 42             # decide and save for one customer
pytest harness/                                   # 67 tests, ~8 seconds
```

**What the showcase shows (no API key, so fallback decides):**
- Habit broken, high value (48 days vs usual 10.5) -> 10% off favourite category; "favourite item" offer was tried
  first but blocked: expected profit Rs 6 < Rs 20 minimum
- Cart left 45h ago -> free delivery, expected profit Rs 145
- Points expiring -> free reminder ("302 reward points will expire on 26 Oct")
- Regular on track (26% buys-anyway chance) -> only a free wallet reminder, no discount
- New customer -> send nothing (every option below the minimum profit)

**Decisions & why:**
- **Deal-seeker score** (share of orders placed during sales) added: discount dependence alone was ~0 for
  almost everyone, so it couldn't separate deal hunters.
- **"Buys anyway" chance fades fast once a customer is 1.3x overdue**: a broken habit means a low natural chance,
  which is exactly when an offer can help.
- **New offer type `reminder`** (zero cost) in `config/offers.yaml`, for the free nudges in CLAUDE.md 4.8 / agent
  priority 3. The CLAUDE.md offer-type list should mention it (needs your OK to edit CLAUDE.md).
- **Banned claims** list added to message rules ("guaranteed", "lowest price"...).
- **Habit fallback uses a Rs 499 minimum order**: favourites are often cheap restock items; a discount on a Rs 99
  item alone can't earn the minimum profit.
- **Gatekeeper skips cost nothing**: logged as `decided_by = gatekeeper`, `status = nothing`, so the manager still
  sees why a customer got nothing.
- Uplift is still a heuristic (documented in `engine/profit.py`); a trained model replaces it in Session 7.

**Next:** Session 3: AI agent (`engine/agent.py`) with tools, structured reasoning and the retry loop.
