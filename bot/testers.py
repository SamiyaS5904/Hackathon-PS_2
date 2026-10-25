"""
Testers: real people who receive offers on Telegram, each linked to one customer profile from the dataset.

Where it sits: used by the Telegram bot (/start links a chat) and, in Session 5, the dashboard Testers page.
Command line (until the dashboard exists):
    python -m bot.testers add --username samiya --customer 648      # prints the personal link
    python -m bot.testers relink --username samiya --customer 1790  # switch to another customer profile
    python -m bot.testers list

How linking works (CLAUDE.md section 8):
  1. The manager adds a tester: Telegram username + which customer profile they will "play".
     A random token makes a personal link: t.me/<bot>?start=<token>
  2. A Telegram bot can't message anyone first, so the tester taps the link and presses Start once.
  3. The bot checks the Telegram username matches the one the manager entered (so a forwarded link doesn't
     work for someone else), then saves the chat id on the tester AND on the customer profile.
  4. From then on, offers decided for that customer are delivered to that chat.
Re-linking moves the chat to another customer profile: the next decision for the new profile goes to the same
person, which is how the demo shows "same friend, different customer, different offer".
"""

from __future__ import annotations

import argparse
import secrets
import sqlite3
import sys
from datetime import datetime

from config.settings import settings
from db.store import connect
from engine.clock import now_utc, to_ts


def normalize_username(username: str) -> str:
    """'@Samiya_S ' -> 'samiya_s' (Telegram usernames are case-insensitive)."""
    return username.strip().lstrip("@").lower()


def tester_link(token: str) -> str:
    bot = settings.telegram_bot_username or "<your_bot_username>"
    return f"https://t.me/{bot}?start={token}"


def _customer_name(conn: sqlite3.Connection, customer_id: int) -> str:
    row = conn.execute("SELECT first_name, last_name FROM customers WHERE id = ?", (customer_id,)).fetchone()
    if row is None:
        raise ValueError(f"Customer #{customer_id} does not exist")
    return f"{row['first_name']} {row['last_name']}"


def add_tester(conn: sqlite3.Connection, username: str, customer_id: int, now: datetime | None = None) -> dict:
    """Create a tester (or re-point an existing one) and return their personal link."""
    username = normalize_username(username)
    if not username:
        raise ValueError("Telegram username is required")
    _customer_name(conn, customer_id)  # fails clearly if the customer doesn't exist
    row = conn.execute("SELECT * FROM testers WHERE telegram_username = ?", (username,)).fetchone()
    if row:
        if row["customer_id"] != customer_id:
            relink_tester(conn, username, customer_id)
        token = row["link_token"]
    else:
        token = secrets.token_urlsafe(9)
        conn.execute("INSERT INTO testers (telegram_username, link_token, customer_id, created_at) VALUES (?, ?, ?, ?)",
                     (username, token, customer_id, to_ts(now or now_utc())))
        conn.commit()
    return {"username": username, "customer_id": customer_id, "link": tester_link(token), "token": token}


def link_chat(conn: sqlite3.Connection, token: str | None, telegram_username: str | None, chat_id: int,
              now: datetime | None = None) -> tuple[bool, str]:
    """
    Called by the bot when someone presses Start on a personal link. Returns (linked?, message for the user).
    The username must match what the manager entered, so a forwarded link can't be used by someone else.
    """
    if not token:
        return False, ("Welcome to OfferPilot. To receive personalised offers, please open the personal link "
                       "shared with you by the OfferPilot team.")
    row = conn.execute("SELECT * FROM testers WHERE link_token = ?", (token,)).fetchone()
    if row is None:
        return False, "This link is not valid any more. Please ask the OfferPilot team for a new one."
    if normalize_username(telegram_username or "") != row["telegram_username"]:
        return False, ("This personal link was created for a different Telegram account. "
                       "Please ask the OfferPilot team for your own link.")
    # One chat can only stand for one customer at a time
    conn.execute("UPDATE customers SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (chat_id,))
    conn.execute("UPDATE testers SET chat_id = ?, linked_at = ? WHERE id = ?", (chat_id, to_ts(now or now_utc()), row["id"]))
    conn.execute("UPDATE customers SET telegram_chat_id = ? WHERE id = ?", (chat_id, row["customer_id"]))
    conn.commit()
    customer = conn.execute("SELECT first_name FROM customers WHERE id = ?", (row["customer_id"],)).fetchone()
    return True, (f"You're all set. For this OfferPilot demo you'll receive the personalised offers our AI decides "
                  f"for customer {customer['first_name']} (#{row['customer_id']}). They'll arrive right here.")


def relink_tester(conn: sqlite3.Connection, username: str, customer_id: int) -> dict:
    """Switch a tester to another customer profile; if they're already linked, their chat moves along."""
    username = normalize_username(username)
    row = conn.execute("SELECT * FROM testers WHERE telegram_username = ?", (username,)).fetchone()
    if row is None:
        raise ValueError(f"No tester with username @{username}")
    _customer_name(conn, customer_id)
    if row["chat_id"] is not None:
        conn.execute("UPDATE customers SET telegram_chat_id = NULL WHERE telegram_chat_id = ?", (row["chat_id"],))
        conn.execute("UPDATE customers SET telegram_chat_id = ? WHERE id = ?", (row["chat_id"], customer_id))
    conn.execute("UPDATE testers SET customer_id = ? WHERE id = ?", (customer_id, row["id"]))
    conn.commit()
    return {"username": username, "old_customer_id": row["customer_id"], "customer_id": customer_id,
            "linked": row["chat_id"] is not None}


def list_testers(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("""SELECT t.*, c.first_name, c.last_name FROM testers t
                           LEFT JOIN customers c ON c.id = t.customer_id ORDER BY t.id""").fetchall()
    return [{"username": r["telegram_username"], "customer_id": r["customer_id"],
             "customer": f"{r['first_name']} {r['last_name']}" if r["first_name"] else None,
             "linked": r["chat_id"] is not None, "linked_at": r["linked_at"], "link": tester_link(r["link_token"])}
            for r in rows]


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="Manage OfferPilot testers (Telegram users linked to customers).")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("add", "relink"):
        p = sub.add_parser(name)
        p.add_argument("--username", required=True)
        p.add_argument("--customer", type=int, required=True)
    sub.add_parser("list")
    args = parser.parse_args()
    conn = connect()
    if args.command == "add":
        t = add_tester(conn, args.username, args.customer)
        print(f"@{t['username']} -> customer #{t['customer_id']} ({_customer_name(conn, t['customer_id'])})")
        print(f"Personal link (open it in Telegram and press Start): {t['link']}")
    elif args.command == "relink":
        r = relink_tester(conn, args.username, args.customer)
        print(f"@{r['username']}: customer #{r['old_customer_id']} -> #{r['customer_id']} "
              f"({_customer_name(conn, r['customer_id'])}); chat {'moved' if r['linked'] else 'not linked yet'}")
    else:
        for t in list_testers(conn):
            print(t)


if __name__ == "__main__":
    main()
