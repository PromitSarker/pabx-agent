"""One-off: import the pre-2026-11-25 token_usage rows and Asterisk CDR into calls / usage_events (flagged test + legacy)."""
import csv
import sys
from datetime import datetime, timedelta
sys.path.insert(0, "/app")
from agent.db import get_connection
from agent.usage import compute_cost

with get_connection() as conn:
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) n FROM usage_events WHERE source='legacy'")
    if cur.fetchone()["n"]:
        sys.exit("legacy rows already imported - nothing to do")

    # phone numbers recorded per session
    cur.execute("SELECT session_id, `value` FROM collected_data WHERE `key`='phone_number'")
    phones = {r["session_id"]: r["value"] for r in cur.fetchall()}

    # 1. calls from the Asterisk CDR (exact seconds)
    cdr = {}
    try:
        for r in csv.reader(open("/var/log/asterisk/cdr-csv/Master.csv", newline="", encoding="utf-8")):
            if len(r) >= 15 and r[7] == "AudioSocket":
                cdr[r[8].split(",")[0]] = r
    except FileNotFoundError:
        print("no CDR file mounted; skipping CDR import")
    for uuid, r in cdr.items():
        cid = "call-" + uuid[:8]
        start, ans, end = (datetime.strptime(x, "%Y-%m-%d %H:%M:%S") if x else None for x in (r[9], r[10], r[11]))
        cur.execute("INSERT IGNORE INTO calls (call_id, conversation_id, phone_number, status, requested_at, answered_at, ended_at, "
                    "talk_seconds, cdr_duration, cdr_billsec, cdr_disposition, end_reason, is_test, source) "
                    "VALUES (%s,%s,%s,'finished',%s,%s,%s,%s,%s,%s,%s,'legacy import',1,'legacy')",
                    (uuid, cid, phones.get(cid, "unknown"), start, ans, end, int(r[13]), int(r[12]), int(r[13]), r[14]))
    print("calls from CDR:", len(cdr))

    # 2. token_usage rows -> usage_events (+ calls for phone sessions that are not in the CDR)
    cur.execute("SELECT * FROM token_usage ORDER BY id")
    rows = cur.fetchall()
    call_by_conv = {}
    cur.execute("SELECT call_id, conversation_id FROM calls")
    for r in cur.fetchall():
        call_by_conv[r["conversation_id"]] = r["call_id"]
    made = 0
    for r in rows:
        sid, is_call = r["session_id"] or "", (r["session_id"] or "").startswith("call-")
        call_id = None
        if is_call:
            call_id = call_by_conv.get(sid)
            if not call_id:
                call_id = "legacy-" + sid
                end = r["created_at"]
                start = end - timedelta(seconds=float(r["duration_seconds"] or 0))
                cur.execute("INSERT IGNORE INTO calls (call_id, conversation_id, phone_number, status, requested_at, answered_at, ended_at, "
                            "talk_seconds, end_reason, is_test, source) VALUES (%s,%s,%s,'finished',%s,%s,%s,%s,'legacy import',1,'legacy')",
                            (call_id, sid, phones.get(sid, "unknown"), start, start, end, float(r["duration_seconds"] or 0)))
                call_by_conv[sid] = call_id
        model = (r["model_name"] or "unknown").replace("models/", "")
        # modality split was never stored: assume all input is text and (for voice) all output is audio -> flagged estimated
        tok = {"input_text": int(r["input_tokens"] or 0), "output_audio": int(r["output_tokens"] or 0)} if is_call \
            else {"input_text": int(r["input_tokens"] or 0), "output_text": int(r["output_tokens"] or 0)}
        cost, note = compute_cost(model, tok, r["created_at"].date())
        cur.execute("INSERT INTO usage_events (created_at, session_id, call_id, channel, purpose, model, input_text, output_text, output_audio, "
                    "estimated, cost_usd, price_note, is_test, source) VALUES (%s,%s,%s,%s,'legacy',%s,%s,%s,%s,1,%s,%s,1,'legacy')",
                    (r["created_at"], sid, call_id, "phone" if is_call else "web_chat", model, tok.get("input_text", 0),
                     tok.get("output_text", 0), tok.get("output_audio", 0), cost, note or "legacy: modality split unknown"))
        made += 1
    conn.commit()
    print("usage events imported:", made)
