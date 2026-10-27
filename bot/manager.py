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


def group_chat(conn: sqlite3.Connection) -> int | None:
    """
    The public showcase group ("OfferPilot Live Offers"): every sent offer is also posted there by the same bot, so
    anyone with the group link can watch the six bots working. TELEGRAM_GROUP_CHAT_ID in .env wins; otherwise the id
    the bots recorded when they were added to a group (meta 'group_chat_id'). None = no group.
    """
    if settings.telegram_group_chat_id:
        return int(settings.telegram_group_chat_id)
    if conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'").fetchone() is None:
        return None
    row = conn.execute("SELECT value FROM meta WHERE key = 'group_chat_id'").fetchone()
    return int(row[0]) if row else None


def remember_group(conn: sqlite3.Connection, chat_id: int) -> bool:
    """Save the showcase group's id (called when a bot sees a group). Returns True if it was new."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    old = conn.execute("SELECT value FROM meta WHERE key = 'group_chat_id'").fetchone()
    conn.execute("INSERT INTO meta (key, value) VALUES ('group_chat_id', ?) ON CONFLICT(key) DO UPDATE SET "
                 "value = excluded.value", (str(chat_id),))
    conn.commit()
    return old is None or old[0] != str(chat_id)


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
