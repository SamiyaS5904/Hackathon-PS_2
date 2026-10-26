"""
Manager mode: every offer goes to ONE Telegram (the marketing manager's), and the dashboard is the control panel.

Where it sits: used by engine/decide.py (where an approved offer is headed), bot/delivery.py (which chat to send
to) and api/main.py (Send / Discard buttons). Switched on by TELEGRAM_MANAGER_USERNAME in .env; that username must
have linked their chat with the bot once (Testers page link or /start code). Change the username any time.

How the manager works with it:
  - Customers page -> "Make offer (AI)": the decision is made and checked, then saved as a DRAFT ('review').
    Nothing is sent yet; the dashboard shows the offer, its profit and the exact message.
  - "Send to my Telegram": the draft is released (approve) and the bot delivers it within ~15 seconds. In quiet
    hours it waits until they end. "Discard" cancels it (it then stops counting against budgets and anti-spam).
  - Automatic triggers (simulator, follow-ups) still go out on their own, to the same Telegram.
Only the customer's message is sent to Telegram; who it was for, the offer, profit and why are on the dashboard.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from config.settings import settings
from engine.clock import parse_ts


def manager_chat(conn: sqlite3.Connection) -> tuple[str, int] | None:
    """(username, chat_id) of the manager, or None when manager mode is off or they haven't linked their chat yet."""
    name = settings.telegram_manager_username.lower()
    if not name:
        return None
    row = conn.execute("SELECT telegram_username, chat_id FROM testers WHERE lower(telegram_username) = ? "
                       "AND chat_id IS NOT NULL", (name,)).fetchone()
    return (row[0], row[1]) if row else None


def approve(conn: sqlite3.Connection, decision_id: int, now: datetime) -> str | None:
    """
    Send a draft: 'queued' (the bot sends it within seconds), or 'scheduled' if its send time is still ahead
    (quiet hours). Returns the new status, or None if it wasn't a draft.
    """
    row = conn.execute("SELECT send_at FROM decisions WHERE id = ? AND status = 'review'", (decision_id,)).fetchone()
    if row is None:
        return None
    status = "scheduled" if row[0] and parse_ts(row[0]) > now else "queued"
    conn.execute("UPDATE decisions SET status = ? WHERE id = ?", (status, decision_id))
    conn.commit()
    return status


def discard(conn: sqlite3.Connection, decision_id: int) -> bool:
    """Discard a draft: cancelled, with a note for the reasoning drawer."""
    row = conn.execute("SELECT reasoning FROM decisions WHERE id = ? AND status = 'review'", (decision_id,)).fetchone()
    if row is None:
        return False
    reasoning = json.loads(row[0]) if row[0] else {}
    reasoning["cancelled_note"] = "Discarded by the manager on the dashboard before sending."
    conn.execute("UPDATE decisions SET status = 'cancelled', reasoning = ? WHERE id = ?",
                 (json.dumps(reasoning), decision_id))
    conn.commit()
    return True


def offer_card(conn: sqlite3.Connection, decision_id: int) -> str:
    """What the manager reads on Telegram for one offer: who, what, money, why, then the customer's message."""
    r = conn.execute("""SELECT d.id, d.customer_id, d.message, d.final_offer, d.reasoning, d.expected_profit_paise,
                               d.offer_cost_paise, d.offer_kind, d.decided_by, c.first_name
                        FROM decisions d JOIN customers c ON c.id = d.customer_id WHERE d.id = ?""",
                     (decision_id,)).fetchone()
    reasoning = json.loads(r["reasoning"]) if r["reasoning"] else {}
    from engine.models import OfferProposal  # local import keeps the bot light
    offer = OfferProposal.model_validate_json(r["final_offer"]).describe() if r["final_offer"] else "no offer"
    kind = "Win-back offer" if r["offer_kind"] == "winback" else "Offer"
    money = ""
    if r["expected_profit_paise"] is not None:
        money = (f"\nExpected profit Rs {r['expected_profit_paise'] // 100:,} · cost if used "
                 f"Rs {(r['offer_cost_paise'] or 0) // 100:,}")
    return (f"{kind} for {r['first_name']} (customer #{r['customer_id']}) · decision #{r['id']} · "
            f"{'AI' if r['decided_by'] == 'agent' else 'free rules'}\n{offer}{money}\n"
            f"Why: {reasoning.get('summary', '')}\n\nMessage to the customer:\n{r['message']}")
