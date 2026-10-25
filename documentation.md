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

---

## 2026-10-23: Session 3: The AI agent
**Built:** Claude (Opus 5.5) decides offers with 7 tools; every proposal is checked by the rules inside the loop;
any failure hands over to the fallback; real tokens and cost are logged.

**Files:**
- `engine/agent.py`: system prompt (goal, priorities, message rules, reasoning format, data safety), 7 strict tools
  (`get_customer_profile`, `get_recent_activity`, `get_offer_history`, `get_offer_limits`, `estimate_profit`,
  `propose_offer`, `propose_no_offer`), the controlled loop, cost tracking
- `engine/decide.py`: agent route; approved -> agent decision (rule repairs listed); no_offer -> "nothing" by agent;
  failure -> fallback, with the AI attempt, its tokens and cost still recorded; report shows tools and cost
- `engine/gatekeeper.py`: `agent_available` switch (lets tests plug in a fake AI)
- `harness/fake_agent.py` (scripted fake Claude client), `harness/test_agent.py` (15 tests)
- `.env.example`: `AGENT_TIMEOUT_SECONDS`, `AGENT_EFFORT`, `OFFERPILOT_NOW`

**How to run / check:**
```bash
pytest harness/                                   # 82 tests, ~6 seconds
python -m engine.decide --customer 648            # uses the AI once ANTHROPIC_API_KEY is in .env
```

**Tested paths (all with the fake client, free):** approved first time; rejected -> reasons sent back -> fixed ->
approved; 2 rejections -> fallback; "send nothing"; API error; timeout (first call's cost still counted); refusal;
text without a decision; invalid proposal; 6-call limit; tools return the right data; "buys anyway" customers
only see perks; through `decide()`: failure -> fallback with cost recorded, no-offer logged as an AI decision,
used-up Day Plan never calls the AI.

**Decisions & why:**
- **Manual loop instead of the SDK tool runner:** the rules check must run inside the loop and send rejection
  reasons back; we also need hard limits (6 calls, 2 rejections) and per-call token accounting.
- **"2 rejections -> fallback"** (CLAUDE.md 4.8 and harness test 4): the AI gets its first proposal plus one fix.
- **Strict tool schemas, no forced tool choice** (forcing a tool is not allowed on Opus 5.5); the prompt says to
  finish with `propose_offer` / `propose_no_offer`, and a run without one counts as a failure.
- **Effort set explicitly** (`medium` by default; Opus 5.5 always thinks, effort controls how much).
- **Refusals are handled by our own fallback**, not the API's beta model-fallback feature: a refusal simply means
  the fallback rules decide. Simpler, and nothing to verify without a key.
- **Prompt caching** on the stable system prompt + tools, to cut the cost of the repeated prefix in each loop.
- **Timeout:** 10s per call as in CLAUDE.md, but configurable. Opus with thinking may need 20-30s per call; if
  real decisions keep timing out we should raise it (needs your OK, since it differs from CLAUDE.md).

**Not done yet:** no real Claude call has been made: there is no `.env` / API key yet. Session 3's user check
("read the agent's reasoning for 3-4 customers") needs the key.

**Next:** with the key: 4 real decisions to check reasoning quality. Then Session 4: Telegram bot + simulator
(needs a bot token from @BotFather).

---

## 2026-10-23: Session 3 check: first 4 real AI decisions (Claude Opus 5.5)
**What ran:** one customer per situation, real API, decisions saved as #1-#4. Full output:
[docs/evidence/real_agent_run_2026-10-23.txt](docs/evidence/real_agent_run_2026-10-23.txt). For this test only,
the timeout was 45s (`AGENT_TIMEOUT_SECONDS=45`) to measure real speed.

| Situation | AI decision | Expected profit | Calls / time | Cost |
|---|---|---|---|---|
| Habit broken, high value (Mansi, 4.6x overdue) | 3x points on home + wallet mention, sent tomorrow 16:00 (her usual hour) | Rs 69 | 4 / 43s | $0.089 |
| Cart left 54h ago (Aarohi, lipstick = favourite) | Free delivery, sent tomorrow 14:00 | Rs 66 | 3 / 22s | $0.086 |
| Lapsed + 302 points expiring (Neha) | 3x points on grocery, mentions expiring points | Rs 113 | 5 / 33s | $0.106 |
| Regular on track, 30% buys-anyway (Khushi) | Free wallet reminder only, **no discount** | Rs 27 | 3 / 17s | $0.065 |

**Total: $0.35 (~Rs 30), average $0.086 per decision**, matching the estimate in reasoning.md §17.

**Quality review:**
- The AI used the tools well: compared 3-6 options with `estimate_profit` before deciding, and chose the
  cheapest offer that works (points / free delivery / reminder) over discounts, as the prompt asks.
- Reasoning is specific and checkable: real values ("48.3 days vs usual 10.5", "ignored all 6 past offers",
  "searched 'bedsheet' twice", "last order was returned"), every alternative with its profit.
- It scheduled sends at each customer's usual shopping hour, to avoid starting right before quiet hours.
- Correct "no discount" for the regular customer who'd likely buy anyway.
- All 4 passed every rule check (19-20/20) with no repairs needed.

**Issues found:**
1. **Timeout:** 4 of 16 API calls took 11.5-15.1s. With the CLAUDE.md 10s limit, 3 of 4 decisions would have
   failed over to the fallback. **Proposal: 30s per call** (needs approval to change CLAUDE.md).
2. **Points multiplier looks very attractive** in the uplift heuristic (Neha: 3% -> 38% chance to buy), so the AI
   picked 3x points twice. It's within the hidden-truth range for points collectors, but the Session 7 model
   should calibrate it. Watch item, no change now.
3. For Neha the AI chose 3x points on grocery (Rs 113) over 3x points sitewide (Rs 175), following the
   "favourite category" priority literally. Defensible, but worth discussing whether profit should win ties.

**Next:** decide on the timeout, then Session 4 (Telegram bot + simulator).

---

## 2026-10-23: AI timeout raised to 30 seconds
**Changed:** timeout per AI call 10s -> **30s**, approved by the user. Updated in CLAUDE.md (section 3 and
decision 8), `engine/agent.py` default, `.env.example`, PLAN.md, and the user's local `.env`
(`AGENT_TIMEOUT_SECONDS=30`; only that line touched).

**Why:** in the real run, 4 of 16 calls took 11.5-15.1s; the longest is always the last call (writing the offer,
message and reasoning). A timeout is only a deadline: it doesn't change the AI's output or the cost of a call that
finishes. A too-short timeout throws away paid calls and sends decisions to the weaker fallback. 30s leaves room for
slower API moments.

**Also fixed:** once a real key existed, one test (`test_pipeline_without_api_key_uses_fallback`) made a real API
call (~Rs 8; it only wrote to its temporary test database). New autouse fixture `no_real_api_key` in
`harness/conftest.py` hides the key from all tests, so the harness can never call the real API. 82 tests pass in ~6s.

**Pending user decision:** optimization "give the agent the profile + limits up front" (saves ~1 call per
decision, est. 15-20% cheaper and 3-5s faster, same output). Not done yet: waiting for a yes.

---

## 2026-10-23: Agent cost optimization (user asked: cheaper, but keep output quality)
**Kept:** Claude Opus, effort `medium` (it was already medium), 30s timeout.

**Changed (`engine/agent.py`):**
- Customer profile + current rule limits are sent **in the first message** (the agent used to spend a whole API call
  fetching them). The tools still exist.
- The offer-type guide moved into the system prompt (cached and reused across customers); limits list type names only.
- The agent checks **4-6 ideas in one turn** (parallel `estimate_profit` calls) and must always include the cheap
  options (2x/3x points, reminder, free delivery) next to any discount.
- Compact JSON (no spaces, no empty fields) for the first message and tool results.
- Reasoning kept tight: summary 1-2 sentences, 3-6 signals, up to 5 alternatives, one-line why-now.
- Cache read/write tokens and API call count are now tracked per decision.

**Measured on the same 4 customers at the same moment (dry run):**
| Customer | Cost before -> after | Time | API calls | Decision |
|---|---|---|---|---|
| Mansi (habit broken) | $0.0890 -> $0.0702 (-21%) | 43s -> 18s | 4 -> 2 | same (3x points) |
| Aarohi (cart) | $0.0857 -> $0.0707 (-18%) | 22s -> 16s | 3 -> 2 | same (free delivery) |
| Neha (points expiring) | $0.1062 -> $0.0828 (-22%)* | 33s -> 26s | 5 -> 2 | same (3x points, Rs 113) |
| Khushi (buys anyway) | $0.0647 -> $0.0672 (+4%) | 17s -> 17s | 3 -> 2 | same (wallet reminder) |
| **Total** | **$0.345 -> ~$0.29 (about -17 to -19%)** | **115s -> ~77s** | **15 -> 8** | |

\* First optimized run found only a Rs 46 reminder for Neha (it checked too few ideas and skipped points). Fixed by
the "always include the cheap options, 4-6 ideas" rule; the re-run found 3x points (Rs 113) again with 6 ideas checked.
Evidence: [docs/evidence/cost_optimization_2026-10-23.txt](docs/evidence/cost_optimization_2026-10-23.txt).

**Quality check:** same decisions as the first run for all 4; reasoning is shorter but still names the key facts with
values and every alternative with its profit. Prompt caching works: ~4,800 tokens read from cache per decision.

**Cost per decision now: ~$0.07 (~Rs 6)**, vs ~$0.086 (~Rs 7.6) before. 50 AI decisions/day ~ Rs 300.

---

## 2026-10-24: Sonnet 5.5 + one-call design + "more agentic" features + uplift model
**User asked:** cut cost further (~Rs 1.5 target) but stay agentic and more personal; build A-G and train the
prediction model before the dashboard. Premium English voice, no feedback buttons, no Hinglish.

**Built:**
- **One-call design** (`engine/candidates.py`): code builds a personalised menu (reminder, 2x/3x points, free
  delivery, early access, favourite item, cart item, interest items, category %, flat, cashback), prices each one
  and runs every rule except the message. The AI usually picks/adjusts one and proposes in its first reply.
  Only the stable instructions + tools are cached (shared across customers).
- **Model switched to Claude Sonnet 5.5** (effort medium), per-model prices in `engine/agent.py`.
- **A. Interest products** (`features.py`): cart, wishlist (14d), search match + views (7d), repeat views; used in
  the menu, in triggers and in messages ("Still thinking about the Aurae Sunscreen Lite?").
- **B. Own ideas:** `estimate_profit` now prices up to 6 ideas in one call, with a rules check per idea.
- **C. Triggers** (`engine/triggers.py`): cart_abandoned, product_interest, habit_break, points_expiring,
  wallet_idle (+ followup, manual), strongest first, with suggested delays. `decide --auto-trigger`.
- **D. Premium voice** (`config/brand_voice.yaml`), loaded into the cached system prompt.
- **E. Honest deadlines:** "midnight / tonight / today" only if the real expiry matches (new rule
  `message.honest_deadline`); the AI gets `hours_until_midnight_ist`.
- **F. Verified loyalty:** "loyal customer" only if >= 5 orders (new rule `message.verified_loyalty`).
- **G. Wait first:** new tool `propose_wait` (1-48h); `engine/followups.py` resolves it: bought on their own ->
  `bought_naturally` (cost saved), else a new `followup` decision that can't wait again; the gatekeeper skips a
  customer while the AI is waiting.
- **Uplift model** (`engine/uplift.py`): T-learner on all 19,683 past offers (15,875 treatment / 3,808 holdout),
  trained in 89s. Held-out AUC: control 0.847, treated 0.713. `harness/eval_uplift.py` against the hidden truth
  (Spearman, 400 customers): formula 0.44, model 0.41, **blend 0.50** (best on 5/6 offer types) -> production uses
  the average of model and formula. "Buys anyway" (control model) beats the formula: AUC 0.847 vs 0.731, better
  calibrated (6.7% vs 8.8% predicted, 7.2% real).
- **Tests:** 108 passing (new: `test_candidates.py`, `test_agentic.py`, `test_uplift.py`); tests hide the real key
  and use the formula, never the trained model.

**Real test (Sonnet, dry run, same moment as the first Opus run):**
[docs/evidence/sonnet_agentic_run_2026-10-24.txt](docs/evidence/sonnet_agentic_run_2026-10-24.txt)
| Customer | Opus (first run) | Sonnet now | Cost |
|---|---|---|---|
| Mansi (habit broken) | 3x points | 3x points | $0.089 -> $0.027 |
| Aarohi (cart: lipstick) | free delivery | free delivery, message names the lipstick | $0.086 -> $0.018-0.028 |
| Neha (lapsed, points expiring) | 3x points | 3x points, mentions expiring points | $0.106 -> $0.016 |
| Khushi (regular, viewed atta) | wallet reminder | 3x points, mentions the atta | $0.065 -> $0.018 |
| Farhan (wishlisted sunscreen) | - | free wallet reminder around the sunscreen | $0.019 |

**Every decision used 1 API call (8-20s). Average ~Rs 1.7-1.9 per decision (Opus first run: ~Rs 7.6, -78%).**
Khushi's decision changed because the model says her "buys anyway" chance is 5.6% (formula said 30%); the
holdout check above shows the model is the more accurate of the two.

**Fixes made after review:** item offers (favourite / cart / interest) use a Rs 499 minimum order; the AI
personalises any offer around the product of interest; loyalty mentioned only when it's the point; voice examples
varied (all 3 re-run messages opened with "Still thinking about...").

**Known limits:** interest discounts on cheap items still often fail the Rs 20 minimum (honest economics); the
AI then wraps a profitable offer around that product instead. Tone effects can't be measured by the simulation,
only by real testers.

**Next:** Session 4 (Telegram bot + simulator firing the new triggers and follow-ups). Needs the bot token.

---

## 2026-10-25: Session 4: Telegram bot + event simulator
**Built:**
- `bot/testers.py`: add tester (username + customer profile -> personal link), link a chat on /start (username
  must match), re-link to another customer (the chat moves), list. CLI: `python -m bot.testers add|relink|list`.
- `bot/delivery.py`: offers due now ('queued', or 'scheduled' whose time came) for linked customers; the chat is
  looked up at send time, so after a re-link the old customer's pending offer is not sent; expired offers are never
  sent ('expired'); unlinked -> 'would_send'.
- `bot/telegram_bot.py`: python-telegram-bot 22; /start <token>, /help, delivery job every 15s. Uses only
  `TELEGRAM_BOT_TOKEN`; httpx request logging muted so the token never appears in logs.
- `events/simulator.py`: `fire cart|search|view|none --customer N` writes the event as real data (sessions,
  searches, views, cart add/abandon), detects the trigger and decides; `tick` / `loop --speed 60` run follow-ups,
  every linked tester and random background customers. Cost guard: AI only for linked testers by default.
- Settings: `TELEGRAM_BOT_USERNAME` (public) for links. Bot verified with Telegram: **@offer_pilot_samiya_bot**.
- `harness/test_bot.py`: 14 tests. **122 tests pass.**

**How to run (demo):**
```bash
python -m bot.telegram_bot                                   # terminal 1: keep running
python -m bot.testers add --username <your_telegram_username> --customer 1309
python -m events.simulator fire none --customer 1309          # decide now for this customer
python -m events.simulator fire cart --customer 1309          # or: create a cart event, then decide
python -m bot.testers relink --username <you> --customer 2942  # same person, different customer
```

**Demo customers picked from observable profiles (no prior decisions):**
#1309 Aman (VIP, habit broken: usual gap 9 days, now 71), #1201 Diya (regular on track, 18% buys-anyway),
#2942 Samiya (deal seeker 1.0, fashion/beauty).

**Decisions & why:**
- Expired offers are never delivered (found while preparing the live test: old test decisions were still
  'scheduled'); delivery marks them 'expired'.
- The bot only delivers; deciding stays in `engine/decide.py`, so every sent message passed the rules checker.

**Next:** live test on the user's Telegram (needs their Telegram username), then Session 5 (dashboard).

---

## 2026-10-25: Session 4 check: live Telegram test (real AI, real delivery)
**Linking:** the first tap on the t.me link never reached the bot (Telegram had nothing pending; the bot received
nothing). Sending `/start <code>` from the Telegram app worked at 17:22 IST. Added: every incoming update and every
handler error is now logged immediately (`bot/telegram_bot.py`), and the bot runs with `python -u` for live logs.

**Same person (@Samiya5904), three customer profiles:**
| # | Customer | Trigger | AI decision | Delivered | Cost |
|---|---|---|---|---|---|
| 5 | #1309 Aman (VIP, 71 days vs usual 9) | habit_break | 3x points, message names his favourite FreshFarm Coffee | ✅ 17:23:25 | $0.025 |
| 6 | #2942 Samiya (deal seeker) after a simulated "lipstick" search | product_interest | 10% off the exact Glowveda Lipstick Neo she viewed (min Rs 499) | ✅ ~17:24 | $0.017 |
| 7 | #1201 Diya (regular, due in ~3 days, 18% buys-anyway) | manual | **Wait 48h**, send nothing (all options below Rs 20; likely to reorder alone) | nothing sent (correct) | $0.012 |

**Total ~Rs 4.8 for 3 decisions.** Evidence: [docs/evidence/live_telegram_test_2026-10-25.txt](docs/evidence/live_telegram_test_2026-10-25.txt)

**Observations:** for Samiya the AI chose the lipstick offer (Rs 33) over generic 15% fashion (Rs 61), citing her
fresh interest: priority 2 in CLAUDE.md, explained in the reasoning, still above the minimum. Diya's decision shows
"knows when not to send" live: a follow-up will check in 48h whether she bought on her own.

**State after the test:** @samiya5904 is linked to customer #1201 (Diya). Re-link with
`python -m bot.testers relink --username Samiya5904 --customer <id>`.

**Next:** Session 5: API + dashboard (the demo is clicked, not typed).

---

## 2026-10-25: Session 5: Dashboard API + company dashboard
**Built:**
- `api/main.py` (FastAPI): `/api/health`, `/api/day-plan` (GET/PUT), `/api/overview`, `/api/decisions` (+ `/{id}`
  with reasoning, menu, rule checks, tool trace, cost), `/api/customers` (search, `suggested` live moments with
  mixed trigger types, profile, `run`, `events`), `/api/testers` (create link + `/start` code, relink, list),
  `/api/rules` (fields, margins, versions; PUT saves a new version, refuses unsafe values, re-checks pending
  offers), `/api/followups/run`. Serves the built dashboard at `/`. No decision logic in the API: it calls the engine.
- `dashboard/` (React 18 + Vite 5 + Tailwind 3 + Recharts + lucide icons + QR codes), dark premium look using the
  validated dark steps of the reference palette (3-series chart palette passes every CVD / contrast check on the
  dark surface):
  - **Today:** greeting, **Plan your day** (AI decisions today/tomorrow, ceiling, cost estimate from the real
    average of current-design decisions ~Rs 1.6), KPI tiles, offer budget meter, 7-day stacked chart
    (AI agent / free rules / gatekeeper skip, legend + tooltip), prediction model card, latest decisions.
  - **Decisions:** filterable feed; click -> **reasoning drawer**: offer + exact Telegram message preview, expected
    profit / offer cost / AI cost, why, facts used, rejected options, impact and timing, the code-prepared menu
    with the **chosen** option highlighted, every rule check (icon + label), what the AI did, tokens.
  - **Customers:** search + live moments; profile as the AI sees it (habit, buys-anyway, deal seeker, favourites,
    interest products, wallet, points); **Run AI agent**, **Free rules only**, **simulate** cart / views / search.
  - **Testers:** add tester -> personal link + **QR code** + copyable `/start` code (fallback when the link click
    doesn't reach the bot); list with **re-link**.
  - **Rules:** every editable limit grouped, category caps show "max allowed" (avg margin - floor), change
    summary, save as new version with note and name, version history.
- Engine: Day Plan cost estimate now averages only one-call-design decisions (old Opus runs inflated it to Rs 5).
- `harness/test_api.py`: 7 API tests. **129 tests pass.**

**Checked in the browser (built dashboard served by FastAPI):** every page loads with real data; drawer opens
(after a fix: it's rendered through a portal so page spacing can't shift it); product names replace "product #139";
"Free rules only" on a live-moment customer created a decision that showed up immediately. Rules editing was
verified by tests, not on the real database, to keep the demo's version history clean.

**How to run the demo:**
```bash
cd dashboard && npm install && npm run build && cd ..   # once (and after dashboard changes)
python -m uvicorn api.main:app --port 8010              # terminal 1 -> open http://127.0.0.1:8010
python -u -m bot.telegram_bot                            # terminal 2 -> Telegram delivery
```
(For dashboard development with hot reload: `cd dashboard && npm run dev` -> http://localhost:5173, API on :8000.)

**Next:** Day 1 MVP demo by the user (link via Testers page, Run AI agent, offer on Telegram). Then Day 2:
Session 6 harness (profit properties, adversarial, agent scenario eval, profit simulation).

## 2026-10-25: Session 6 (part 1): Harness 1-6, first real-AI checks, calibration
**Built:**
- `harness/test_profit_properties.py` (hypothesis): thousands of random proposals across every offer type, category,
  amount, customer and rule state. Anything approved always has expected profit >= min, margin >= floor, within
  caps and budgets. Checked that the test isn't empty: ~15% of random proposals get approved, covering all types.
- `harness/test_adversarial.py`: injection in name and search query, 90% off, sneaky amounts/claims, fake urgency,
  fake deadline, fake loyalty, links, unknown product, 2am send, stacking, huge amounts, no consent.
  **It found real holes, now fixed in `engine/rules.py` / `config/rules.yaml`:** "half price", "buy one get one",
  amounts in words ("500 rupees", "50 percent", "per cent"), rupees after the number ("999 rs"), and a regex that
  crashed on "₹,". 
- `harness/test_fallback.py`: every AI failure path (API down, timeout, junk output, 2 rejections, no key, daily
  limit) goes to the fallback, which is still rule-checked; circuit breaker pauses offers.
- Real-AI checks (run on request, answers saved in `harness/recordings/`, git-ignored): `harness/agent_runs.py`
  (temp DB copy, sample per hidden persona, saved answers), `harness/truth.py` (hidden-truth referee),
  `harness/simulate_profit.py` (harness 6), `harness/eval_agent_scenarios.py` (harness 5, 16 golden scenarios).

**First paid run (~Rs 41):** simulation **FAIL** (AI + rules Rs 771 vs send-to-all Rs 827, 24 customers);
scenarios **13/16** (pass bar 14). Reading the failures: the AI gave about-to-leave VIPs points/free delivery where
a real discount would have won them back, and gave a points hoarder Rs 200 off. The AI was following our numbers,
and our numbers were wrong: the formula rated points ~2x too high and VIP discounts too low.

**Fix: calibrated `engine/profit.py` on past campaign results** (treatment vs holdout in `offers_history`):
measured lift pct 10% +8.6 pts, flat +5.4-6.8, cashback +5.2, free delivery +3.6, early access +3.3, 2x points
+2.5, 3x +4.0. New bases follow those; 3x points adds ~60% over 2x (not 2x); the deal-seeker factor is gentler.
Free check against the hidden truth: average error 0.789 -> 0.690; ranking quality blend 0.51, formula 0.47.
Saved AI answers are now keyed by the priced menu, so changed numbers always ask the AI again.
**163 tests pass.**

**Next:** re-run the two paid checks with the calibrated numbers (~Rs 40, new questions so no saved answers), then
Session 7.

## 2026-10-25: Session 6 (part 2): Paid checks re-run with calibrated numbers (~Rs 42)
**Result: both pass.** Full output: `docs/evidence/harness_ai_checks_2026-10-25.txt`.
- **Profit simulation (harness 6): PASS.** True profit on 24 customers: AI + rules **Rs 880** vs send-to-all Rs 827,
  send-to-all + rules Rs 782, free rules only Rs 249. AI + rules sent 20 offers (send-to-all 24) and won on
  festival buyers (Rs 171 vs 110), points hoarders (Rs 67 vs -60) and new users (Rs 54 vs 24).
- **AI scenarios (harness 5): 14/16 PASS** (bar 14), reasoning complete on all 16.
- The 2 remaining fails, read honestly:
  - cart abandoner #1455: the fresh cart was a cheap snack; every discount on it was under the Rs 20 minimum, so the AI
    offered 10% off the jeans he had searched for (the most profitable allowed option) and said why. Sensible, but
    not "tied to the cart", so it counts as a fail.
  - points hoarder #1319: Rs 200 off a Rs 700 order (~29%, inside the 30% fashion cap) because it was the top-profit
    candidate. Allowed and profitable, but bigger than we want for a hoarder.
- Weak spot left: about-to-leave VIPs (AI + rules Rs 144 vs send-to-all Rs 262): the AI still prefers free delivery /
  points for them. To look at in Session 7 (prompt + how VIP comeback discounts are priced).

**Next:** Session 7 (guardrails page, circuit breaker UI, polish, reasoning prompt tuning incl. VIP comebacks).
