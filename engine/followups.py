"""
Follow-ups: what happens after the AI decided to WAIT instead of sending an offer.

Where it sits: after decide.py. The simulator / bot loop (Session 4) calls run_due_followups() regularly; it can
also be run by hand:  python -m engine.followups

Why it exists (the "knows when not to send" feature): an offer to someone who would have bought anyway is lost
margin. When a customer may well buy on their own (a cart left an hour ago, a regular who is due any day), the AI
can choose `propose_wait`. When the follow-up time comes:
  - they bought meanwhile        -> status 'bought_naturally': no offer was needed, its cost was saved
  - they didn't buy              -> status 'followed_up', and a new decision is made with trigger 'followup'
                                    (in that decision the AI may not wait again, so it never loops)
"""

from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime

from db.store import connect
from engine.catalog import Catalog, load_catalog
from engine.clock import days_between, now_utc, parse_ts, to_ts


def run_due_followups(conn: sqlite3.Connection, now: datetime | None = None, agent_client=None,
                      catalog: Catalog | None = None) -> list[dict]:
    """Resolve every 'waiting' decision whose follow-up time has come. Returns a report line per decision."""
    from engine.decide import decide  # imported here: decide.py imports the agent, which is heavy

    now = now or now_utc()
    catalog = catalog or load_catalog(conn)
    report = []
    due = conn.execute("SELECT * FROM decisions WHERE status = 'waiting' AND send_at <= ? ORDER BY send_at",
                       (to_ts(now),)).fetchall()
    for row in due:
        reasoning = json.loads(row["reasoning"]) if row["reasoning"] else {}
        waited = round(days_between(parse_ts(row["created_at"]), now) * 24, 1)
        order = conn.execute("""SELECT id, ts, net_paise FROM orders WHERE customer_id = ? AND ts > ? AND ts <= ?
                                AND status != 'cancelled' ORDER BY ts LIMIT 1""",
                             (row["customer_id"], row["created_at"], to_ts(now))).fetchone()
        if order:
            reasoning["followup_result"] = (f"Bought on their own {order['ts'][:16].replace('T', ' ')} UTC "
                                            f"(order #{order['id']}, Rs {order['net_paise'] // 100:,}) while we waited "
                                            f"{waited:g}h: no offer was needed, so its cost was saved.")
            conn.execute("UPDATE decisions SET status = 'bought_naturally', reasoning = ? WHERE id = ?",
                         (json.dumps(reasoning), row["id"]))
            conn.commit()
            report.append({"decision_id": row["id"], "customer_id": row["customer_id"], "result": "bought_naturally",
                           "order_id": order["id"]})
            continue

        conn.execute("UPDATE decisions SET status = 'followed_up' WHERE id = ?", (row["id"],))
        conn.commit()
        outcome = decide(conn, row["customer_id"], "followup",
                         {"after_waiting_decision": row["id"], "original_trigger": row["trigger_type"],
                          "waited_hours": waited, "why_we_waited": reasoning.get("summary", "")},
                         now=now, catalog=catalog, agent_client=agent_client)
        reasoning["followup_result"] = (f"No order after waiting {waited:g}h: follow-up decision "
                                        f"#{outcome.decision_id} ({outcome.status}).")
        conn.execute("UPDATE decisions SET reasoning = ? WHERE id = ?", (json.dumps(reasoning), row["id"]))
        conn.commit()
        report.append({"decision_id": row["id"], "customer_id": row["customer_id"], "result": "followed_up",
                       "new_decision_id": outcome.decision_id, "new_status": outcome.status})
    return report


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = connect()
    report = run_due_followups(conn)
    print(f"{len(report)} follow-up(s) processed")
    for line in report:
        print(" ", line)


if __name__ == "__main__":
    main()
