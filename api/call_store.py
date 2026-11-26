"""Persistent record of every phone call (queued, answered, finished, failed). Sync helpers: call via run_in_threadpool."""
import csv
import os
from datetime import datetime
from typing import Optional

from agent.db import get_connection
from agent.usage import is_test_number

CDR_PATH = os.getenv("SIP_CDR_PATH", "/var/log/asterisk/cdr-csv/Master.csv")


def _run(sql: str, args: tuple = ()) -> int:
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            n = cur.rowcount
        conn.commit()
    return n


def create_call(call_id: str, conversation_id: str, number: str, name: str = "", client_id: Optional[str] = None) -> None:
    _run("INSERT INTO calls (call_id, conversation_id, phone_number, contact_name, client_id, status, is_test) "
         "VALUES (%s, %s, %s, %s, %s, 'queued', %s)",
         (call_id, conversation_id, number, name or None, client_id, 1 if is_test_number(number) else 0))


def mark_answered(call_id: str) -> None:
    _run("UPDATE calls SET status='answered', answered_at=CURRENT_TIMESTAMP(6) WHERE call_id=%s", (call_id,))


def finish_call(call_id: str, talk_seconds: float, end_reason: str, outcome: Optional[str]) -> None:
    _run("UPDATE calls SET status='finished', ended_at=CURRENT_TIMESTAMP(6), talk_seconds=%s, end_reason=%s, outcome=%s "
         "WHERE call_id=%s", (round(talk_seconds, 2), (end_reason or "")[:100], outcome, call_id))


def mark_failed(call_id: str, detail: str) -> None:
    _run("UPDATE calls SET status='failed', ended_at=CURRENT_TIMESTAMP(6), fail_detail=%s "
         "WHERE call_id=%s AND status IN ('queued','answered')", ((detail or "")[:255], call_id))


def sweep_orphans() -> int:
    """At bridge start: calls left queued/answered belong to a process that no longer exists (a restart ends live calls)."""
    return _run("UPDATE calls SET status='lost', ended_at=COALESCE(ended_at, CURRENT_TIMESTAMP(6)), "
                "fail_detail='bridge restarted while the call was open' WHERE status IN ('queued','answered')")


def sync_cdr(path: str = CDR_PATH) -> int:
    """Copy Asterisk's duration/billsec/disposition (keyed by the call UUID in the CDR's lastdata) onto the calls table."""
    if not os.path.exists(path):
        return 0
    updated = 0
    with open(path, newline="", encoding="utf-8", errors="replace") as f, get_connection() as conn:
        with conn.cursor() as cur:
            for r in csv.reader(f):
                if len(r) < 15 or r[7] != "AudioSocket":
                    continue
                call_id = r[8].split(",")[0].strip()
                try:
                    dur, bill = int(r[12]), int(r[13])
                except ValueError:
                    continue
                cur.execute("UPDATE calls SET cdr_duration=%s, cdr_billsec=%s, cdr_disposition=%s "
                            "WHERE call_id=%s AND (cdr_billsec IS NULL OR cdr_billsec <> %s)", (dur, bill, r[14], call_id, bill))
                updated += cur.rowcount
        conn.commit()
    return updated
