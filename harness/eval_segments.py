"""
Check (no API): are the Testers page's demo profiles really what they say? Scored against the hidden personas.

engine/segments.py picks "about-to-leave VIP", "loyal regular", ... using only signals the company can see. This
script is allowed to read customer_traits (the harness may; the app never does) and reports, per segment, how many
of the picks really have that hidden persona. Pass bar: at least 70% overall, so a re-link demo shows what it claims.
Run: python -m harness.eval_segments
"""

from __future__ import annotations

import sys

from config.settings import settings
from db.store import connect
from engine.catalog import load_catalog
from engine.segments import demo_profiles
from harness.agent_runs import DEFAULT_NOW

PERSONA_OF_SEGMENT = {"about_to_leave_vip": "vip_at_risk", "loyal_regular": "loyal_regular",
                      "deal_hunter": "deal_hunter", "cart_abandoner": "cart_abandoner",
                      "points_collector": "points_hoarder", "new_customer": "new_user"}


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    conn = connect(settings.db_path)
    truth = {r[0]: r[1] for r in conn.execute("SELECT customer_id, persona FROM customer_traits")}
    segments = demo_profiles(conn, load_catalog(conn), DEFAULT_NOW, per_segment=10)
    hits = total = 0
    print("\nDemo profiles vs hidden personas (10 picks per segment)\n")
    for seg in segments:
        ids = [c["id"] for c in seg["customers"]]
        good = sum(truth[i] == PERSONA_OF_SEGMENT[seg["key"]] for i in ids)
        hits, total = hits + good, total + len(ids)
        misses = ", ".join(f"#{i} {truth[i]}" for i in ids if truth[i] != PERSONA_OF_SEGMENT[seg["key"]])
        print(f"  {seg['label']:<22} {good}/{len(ids)}" + (f"   (others: {misses})" if misses else ""))
    share = hits / total if total else 0
    print(f"\n  Overall {hits}/{total} = {share:.0%} (pass bar 70%)  RESULT: {'PASS' if share >= 0.7 else 'FAIL'}")


if __name__ == "__main__":
    main()
