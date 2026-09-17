"""Seed a handful of demo patients so the workflow can be shown end to end.

Safe to re-run: existing chart numbers are skipped. No real patient data.
"""

from __future__ import annotations

import sqlite3
import uuid

from app.db import init_db, session, utcnow

DEMO_PATIENTS = [
    ("CS-1001", "Aarav", "Menon", "1991-04-12"),
    ("CS-1002", "Divya", "Nair", "1988-11-03"),
    ("CS-1003", "Rohan", "Pillai", "2005-07-27"),
    ("CS-1004", "Sara", "Thomas", "1997-01-19"),
]


def main() -> None:
    init_db()
    created = 0
    with session() as conn:
        for chart, first, last, dob in DEMO_PATIENTS:
            try:
                conn.execute(
                    "INSERT INTO patients (id, chart_number, first_name, last_name,"
                    " date_of_birth, created_at) VALUES (?,?,?,?,?,?)",
                    (str(uuid.uuid4()), chart, first, last, dob, utcnow()),
                )
                created += 1
            except sqlite3.IntegrityError:
                pass
    print(f"Seed complete: {created} patient(s) added, {len(DEMO_PATIENTS) - created} already present.")


if __name__ == "__main__":
    main()
