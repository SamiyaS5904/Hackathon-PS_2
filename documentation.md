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
