"""
Delivery: which decided offers should go out to Telegram right now, and recording that they went out.

Where it sits: STEP 6. The bot (bot/telegram_bot.py) calls these every few seconds; the actual sending is done by
the bot, so this file has no Telegram code and is easy to test.

Rules of delivery:
  - 'queued' decisions go out immediately; 'scheduled' ones when their send time arrives (e.g. after quiet hours).
  - The chat is looked up at SEND time from the customer profile. So if a tester was re-linked to another customer
    in the meantime, the old customer's pending offer is NOT sent to them; it's recorded as 'would_send' instead.
  - Only messages that passed the rules checker are ever in these statuses (decide.py guarantees that).
  - An offer whose validity has already run out (send time + expiry is in the past) is marked 'expired' and never
    sent, e.g. after the bot was switched off for a while.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta

from bot.manager import manager_chat
from engine.clock import parse_ts, to_ts


def due_deliveries(conn: sqlite3.Connection, now: datetime) -> list[dict]:
    """
    Offers to send now: [{decision_id, chat_id, text, customer_id}]. Unlinked ones are marked 'would_send'.
    Manager mode (bot/manager.py): every offer goes to the manager's chat. Only the customer's message is sent, exactly
    as the customer would see it; who it was for and why is on the dashboard (Today -> Offer schedule).
    """
    rows = conn.execute("""
        SELECT d.id, d.customer_id, d.message, d.final_offer, COALESCE(d.send_at, d.created_at) start,
               c.telegram_chat_id
        FROM decisions d JOIN customers c ON c.id = d.customer_id
        WHERE d.status = 'queued' OR (d.status = 'scheduled' AND d.send_at <= ?)
        ORDER BY d.id""", (to_ts(now),)).fetchall()
    due = []
    manager = manager_chat(conn)   # manager mode: everything goes to the manager's chat, with a header
    for r in rows:
        expiry_hours = json.loads(r["final_offer"]).get("expiry_hours", 24) if r["final_offer"] else 24
        if parse_ts(r["start"]) + timedelta(hours=expiry_hours) <= now:
            # Its validity already ran out (e.g. the bot wasn't running): never send an expired offer
            conn.execute("UPDATE decisions SET status = 'expired' WHERE id = ?", (r["id"],))
            continue
        if manager and r["message"]:
            due.append({"decision_id": r["id"], "chat_id": manager[1], "text": r["message"],
                        "customer_id": r["customer_id"]})
            continue
        if r["telegram_chat_id"] is None or not r["message"]:
            conn.execute("UPDATE decisions SET status = 'would_send' WHERE id = ?", (r["id"],))
            continue
        due.append({"decision_id": r["id"], "chat_id": r["telegram_chat_id"], "text": r["message"],
                    "customer_id": r["customer_id"]})
    conn.commit()
    return due


def mark_sent(conn: sqlite3.Connection, decision_id: int, telegram_message_id: int, now: datetime) -> None:
    conn.execute("UPDATE decisions SET status = 'sent', sent_at = ?, telegram_message_id = ? WHERE id = ?",
                 (to_ts(now), telegram_message_id, decision_id))
    conn.commit()


def mark_failed(conn: sqlite3.Connection, decision_id: int, error: str) -> None:
    """Telegram refused (e.g. the tester blocked the bot): keep the decision, record why, don't retry forever."""
    conn.execute("UPDATE decisions SET status = 'would_send', reasoning = json_set(COALESCE(reasoning, '{}'), "
                 "'$.delivery_error', ?) WHERE id = ?", (error[:300], decision_id))
    conn.commit()
