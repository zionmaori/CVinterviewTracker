"""Job-application email sorter.

Every N minutes: pulls new Gmail messages, asks Claude whether each one is about
a job application and what stage it signals, applies a Gmail label
(Jobs/Interview, Jobs/Declined, Jobs/Not Relevant, Jobs/Under Review), and
records it in SQLite. A local dashboard is served at http://localhost:8765.
"""

import argparse
import atexit
import base64
import hmac
import socket
import html
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import anthropic
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "jobmail.db"
CREDENTIALS_PATH = ROOT / "credentials.json"  # OAuth client from Google Cloud Console
TOKEN_PATH = ROOT / "token.json"
ENV_PATH = ROOT / ".env"
DASHBOARD_PATH = ROOT / "dashboard.html"
DAILY_ALERT_STATE = ROOT / ".daily_alert"  # date the daily alert was last shown

SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]
MODEL = "claude-opus-5-5"

CATEGORIES = {
    "interview": "Jobs/Interview",
    "declined": "Jobs/Declined",
    "not_relevant": "Jobs/Not Relevant",
    "under_review": "Jobs/Under Review",
}
COMPANY_LABEL_PREFIX = "Jobs/Companies/"
CATEGORY_NAMES = {"interview": "Interview", "declined": "Declined",
                  "not_relevant": "Not relevant", "under_review": "Under review"}

# Gmail-side prefilter so we don't send every newsletter to Claude.
# Claude still makes the final call on whether a message is job-related.
GMAIL_QUERY = (
    "-in:sent -in:chats ("
    "application OR applied OR applying OR candidacy OR candidate OR interview OR "
    "recruiter OR recruiting OR hiring OR position OR role OR opportunity OR "
    '"thank you for your interest" OR "next steps" OR assessment OR '
    "greenhouse OR lever OR workday OR ashby OR smartrecruiters)"
)

# Bodies are capped so a huge HTML email can't blow up cost; job-status emails
# are short, and the status is almost always in the first few paragraphs.
MAX_BODY_CHARS = 15000

# Interview times are stored in this Mac's local time zone, e.g. "Asia/Jerusalem".
LOCAL_TZ = os.path.realpath("/etc/localtime").split("zoneinfo/")[-1]

SYSTEM_PROMPT = """You sort a job seeker's incoming email. Decide whether the email \
is about one of their own job applications, and if so which stage it signals:

- interview: an invitation to interview, schedule a call, take an assessment or \
home assignment, or any other request to move forward in the process. Also offers.
- declined: the company rejected the application or chose other candidates.
- not_relevant: the position was filled, closed, put on hold, or cancelled, or the \
candidate withdrew — the application is dead but not because of a rejection.
- under_review: application received/confirmation, "we're reviewing", status \
updates without a decision.

Set is_job_related=false for job alerts, job-board newsletters, marketing, cold \
recruiter spam about roles they never applied to, and anything unrelated. When \
is_job_related is false, set category to "under_review" (it is ignored).

company: the company's short common name without legal suffixes (e.g. "Claroty", \
not "Claroty Ltd."); if it matches one of the companies already seen, reuse that \
exact spelling. role: the job title. Use "" for either if unknown. summary: one short \
sentence a person can skim. next_step: what the candidate should do or expect \
next (e.g. "Pick an interview slot by Friday"), or "" if nothing.

interview_at: if the email states a specific scheduled date and time for an \
interview, call, or assessment, give it as "YYYY-MM-DDTHH:MM" in the candidate's \
time zone (convert if the email names another zone; resolve "tomorrow" etc. from the \
email's date). Use "" if no exact time is fixed yet (e.g. "pick a slot"). \
interview_details: for a scheduled interview, a short line like "Phone screen with \
Dana (HR), 30 min, Zoom" — otherwise ""."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_job_related": {"type": "boolean"},
        "category": {"type": "string", "enum": list(CATEGORIES)},
        "company": {"type": "string"},
        "role": {"type": "string"},
        "summary": {"type": "string"},
        "next_step": {"type": "string"},
        "interview_at": {"type": "string"},
        "interview_details": {"type": "string"},
    },
    "required": ["is_job_related", "category", "company", "role", "summary", "next_step",
                 "interview_at", "interview_details"],
    "additionalProperties": False,
}

state_lock = threading.Lock()
state = {"last_run": None, "next_run": None, "running": False, "error": None, "interval": None}
run_now = threading.Event()


def load_env():
    """Load KEY=value lines from .env into os.environ (real env vars win)."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("'\""))


# ---------- storage ----------

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as conn:
        conn.execute(
            """CREATE TABLE IF NOT EXISTS emails (
                id TEXT PRIMARY KEY,
                thread_id TEXT,
                received_at TEXT,
                sender TEXT,
                subject TEXT,
                is_job_related INTEGER,
                category TEXT,
                company TEXT,
                role TEXT,
                summary TEXT,
                next_step TEXT,
                processed_at TEXT
            )"""
        )
        conn.execute("CREATE TABLE IF NOT EXISTS company_labeled (id TEXT PRIMARY KEY)")
        columns = {r["name"] for r in conn.execute("PRAGMA table_info(emails)")}
        for col in ("interview_at", "interview_details"):
            if col not in columns:  # added after the first version; NULL = not extracted yet
                conn.execute(f"ALTER TABLE emails ADD COLUMN {col} TEXT")


def seen_ids():
    with db() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM emails")}


def save(row):
    with db() as conn:
        conn.execute(
            """INSERT OR REPLACE INTO emails
               (id, thread_id, received_at, sender, subject, is_job_related, category, company,
                role, summary, next_step, processed_at, interview_at, interview_details)
               VALUES (:id, :thread_id, :received_at, :sender, :subject, :is_job_related,
                :category, :company, :role, :summary, :next_step, :processed_at,
                :interview_at, :interview_details)""",
            row,
        )


def known_companies():
    with db() as conn:
        return sorted({r["company"] for r in conn.execute(
            "SELECT DISTINCT company FROM emails WHERE is_job_related = 1 AND company != ''"
        )})


def mark_company_labeled(msg_id):
    with db() as conn:
        conn.execute("INSERT OR IGNORE INTO company_labeled VALUES (?)", (msg_id,))


# ---------- gmail ----------

def gmail_service():
    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_PATH.exists():
                raise SystemExit(f"Missing {CREDENTIALS_PATH.name} — see README.md for Gmail setup.")
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_PATH), SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.write_text(creds.to_json())
    return build("gmail", "v1", credentials=creds, cache_discovery=False)


def ensure_labels(svc):
    # Keyed by lowercase name: Gmail label names are case-insensitive.
    labels = {l["name"].lower(): l["id"] for l in svc.users().labels().list(userId="me").execute()["labels"]}
    for name in ["Jobs", COMPANY_LABEL_PREFIX.rstrip("/"), *CATEGORIES.values()]:
        label_id(svc, name, labels)
    return labels


def label_id(svc, name, labels):
    if name.lower() not in labels:
        labels[name.lower()] = svc.users().labels().create(
            userId="me",
            body={"name": name, "labelListVisibility": "labelShow", "messageListVisibility": "show"},
        ).execute()["id"]
    return labels[name.lower()]


def company_label(company):
    # "/" would create a nested label, so replace it.
    return COMPANY_LABEL_PREFIX + company.replace("/", "-").strip()[:100]


def backfill_company_labels(svc, labels):
    """Add company labels to emails sorted before company labels existed."""
    with db() as conn:
        rows = conn.execute(
            """SELECT id, company FROM emails WHERE is_job_related = 1 AND company != ''
               AND id NOT IN (SELECT id FROM company_labeled)"""
        ).fetchall()
    for r in rows:
        svc.users().messages().modify(
            userId="me", id=r["id"],
            body={"addLabelIds": [label_id(svc, company_label(r["company"]), labels)]},
        ).execute()
        mark_company_labeled(r["id"])
    if rows:
        print(f"  added company labels to {len(rows)} earlier emails")


def list_new_message_ids(svc, days):
    query = f"{GMAIL_QUERY} newer_than:{days}d"
    ids, page = [], None
    while True:
        resp = svc.users().messages().list(userId="me", q=query, pageToken=page, maxResults=100).execute()
        ids += [m["id"] for m in resp.get("messages", [])]
        page = resp.get("nextPageToken")
        if not page:
            return ids


def _decode(data):
    return base64.urlsafe_b64decode(data.encode()).decode("utf-8", errors="replace")


def _html_to_text(s):
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</tr>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    return re.sub(r"[ \t]+", " ", html.unescape(s))


def extract_body(payload):
    plain, html_parts = [], []

    def walk(part):
        mime = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if data and mime == "text/plain":
            plain.append(_decode(data))
        elif data and mime == "text/html":
            html_parts.append(_decode(data))
        for p in part.get("parts", []):
            walk(p)

    walk(payload)
    text = "\n".join(plain) if plain else _html_to_text("\n".join(html_parts))
    text = re.sub(r"\n\s*\n+", "\n\n", text).strip()
    return text[:MAX_BODY_CHARS]


def fetch_message(svc, msg_id):
    msg = svc.users().messages().get(userId="me", id=msg_id, format="full").execute()
    headers = {h["name"].lower(): h["value"] for h in msg["payload"].get("headers", [])}
    return {
        "id": msg_id,
        "thread_id": msg["threadId"],
        "received_at": datetime.fromtimestamp(int(msg["internalDate"]) / 1000, timezone.utc).isoformat(),
        "sender": headers.get("from", ""),
        "subject": headers.get("subject", ""),
        "body": extract_body(msg["payload"]),
    }


# ---------- claude ----------

def classify(client, email, companies):
    response = client.beta.messages.create(
        model=MODEL,
        max_tokens=2048,
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        output_config={"effort": "low", "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
        system=SYSTEM_PROMPT,
        messages=[{
            "role": "user",
            "content": (
                f"Companies already seen: {', '.join(companies) or '(none yet)'}\n"
                f"Candidate's time zone: {LOCAL_TZ}\n\n"
                f"From: {email['sender']}\nSubject: {email['subject']}\n"
                f"Date: {email['received_at']}\n\n{email['body']}"
            ),
        }],
    )
    if response.stop_reason == "refusal":
        return None
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


# ---------- the sorting run ----------

def applescript_str(s):
    # AppleScript string literal; unlike json.dumps this keeps Hebrew, emoji and dashes intact.
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def notify(title, message):
    script = f"display notification {applescript_str(message)} with title {applescript_str(title)}"
    subprocess.run(["osascript", "-e", script], check=False)


# ---------- daily alert ----------

def recent_emails(hours=24):
    since = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat()
    with db() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM emails WHERE is_job_related = 1 AND received_at >= ? ORDER BY received_at DESC",
            (since,),
        )]


def show_daily_alert(url, max_lines=10):
    """Pop up a window listing the last 24 hours of job emails."""
    rows = recent_emails()
    if rows:
        counts = {}
        for r in rows:
            counts[r["category"]] = counts.get(r["category"], 0) + 1
        head = ", ".join(f"{n} {CATEGORY_NAMES[c].lower()}" for c, n in counts.items())
        lines = [f"• {CATEGORY_NAMES[r['category']]} — {r['company'] or r['sender']}: {r['summary']}"
                 for r in rows[:max_lines]]
        if len(rows) > max_lines:
            lines.append(f"…and {len(rows) - max_lines} more")
        text = f"{len(rows)} job email{'s' if len(rows) > 1 else ''}: {head}\n\n" + "\n".join(lines)
    else:
        text = "No new job emails in the last 24 hours."
    script = (f"display dialog {applescript_str(text)} with title {applescript_str('Job emails — last 24 hours')} "
              'buttons {"Close", "Open dashboard"} default button "Open dashboard"')
    out = subprocess.run(["osascript", "-e", "activate", "-e", script], capture_output=True, text=True)
    if "Open dashboard" in out.stdout:
        webbrowser.open(f"{url}/?view=recent")


def daily_alert_loop(at, url):
    hour, minute = map(int, at.split(":"))
    if not DAILY_ALERT_STATE.exists() and (datetime.now().hour, datetime.now().minute) >= (hour, minute):
        DAILY_ALERT_STATE.write_text(datetime.now().date().isoformat())  # first start after today's time: begin tomorrow
    while True:
        now = datetime.now()
        today = now.date().isoformat()
        last = DAILY_ALERT_STATE.read_text().strip() if DAILY_ALERT_STATE.exists() else ""
        # ">=" rather than "==" so an alert missed while the Mac slept shows on wake.
        if (now.hour, now.minute) >= (hour, minute) and last != today:
            DAILY_ALERT_STATE.write_text(today)
            threading.Thread(target=show_daily_alert, args=(url,), daemon=True).start()
        time.sleep(30)


def backfill_interview_times(svc, client, companies):
    """Pull interview times out of interview emails sorted before the calendar existed."""
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM emails WHERE category = 'interview' AND is_job_related = 1 AND interview_at IS NULL"
        )]
    for msg_id in ids:
        result = classify(client, fetch_message(svc, msg_id), companies) or {}
        with db() as conn:
            conn.execute(
                "UPDATE emails SET interview_at = ?, interview_details = ? WHERE id = ?",
                (result.get("interview_at", ""), result.get("interview_details", ""), msg_id),
            )
    if ids:
        print(f"  looked for interview times in {len(ids)} earlier emails")


def run_once(days):
    svc = gmail_service()
    labels = ensure_labels(svc)
    backfill_company_labels(svc, labels)
    client = anthropic.Anthropic()
    companies = known_companies()
    backfill_interview_times(svc, client, companies)

    seen = seen_ids()
    new_ids = [i for i in list_new_message_ids(svc, days) if i not in seen]
    print(f"[{datetime.now():%H:%M:%S}] {len(new_ids)} new candidate emails")

    for msg_id in new_ids:
        email = fetch_message(svc, msg_id)
        try:
            result = classify(client, email, companies)
        except anthropic.RateLimitError:
            print("  rate limited; will retry the rest next run")
            break
        except anthropic.APIStatusError as e:
            print(f"  Claude error on {email['subject']!r}: {e.status_code}; will retry next run")
            continue
        if result is None:
            result = {"is_job_related": False, "category": "under_review", "company": "",
                      "role": "", "summary": "Not classified (declined by model)", "next_step": "",
                      "interview_at": "", "interview_details": ""}

        if result["is_job_related"]:
            label = CATEGORIES[result["category"]]
            add = [label_id(svc, "Jobs", labels), label_id(svc, label, labels)]
            if result["company"]:
                add.append(label_id(svc, company_label(result["company"]), labels))
                if result["company"] not in companies:
                    companies.append(result["company"])
            svc.users().messages().modify(userId="me", id=msg_id, body={"addLabelIds": add}).execute()
            print(f"  {label:20} {result['company'] or '?'} — {email['subject']}")
            if result["category"] == "interview":
                when = result["interview_at"].replace("T", " ")
                notify(f"Interview: {result['company'] or 'new'}", f"{when} — {result['summary']}" if when else result["summary"])

        save({
            **{k: email[k] for k in ("id", "thread_id", "received_at", "sender", "subject")},
            **result,
            "is_job_related": int(result["is_job_related"]),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        })
        if result["is_job_related"] and result["company"]:
            mark_company_labeled(msg_id)


def poll_loop(interval_min, days):
    while True:
        with state_lock:
            state.update(running=True, error=None)
        try:
            run_once(days)
        except Exception as e:  # keep the loop (and dashboard) alive; surface the error in the UI
            print(f"Run failed: {e!r}")
            with state_lock:
                state["error"] = repr(e)
        now = time.time()
        with state_lock:
            state.update(running=False, last_run=now, next_run=now + interval_min * 60)
        run_now.wait(timeout=interval_min * 60)
        run_now.clear()


# ---------- dashboard ----------

def dashboard_data():
    with db() as conn:
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM emails WHERE is_job_related = 1 ORDER BY received_at DESC"
        )]
    with state_lock:
        return {"state": dict(state), "emails": rows}


def lan_ip():
    # The address other devices reach this Mac on (no packet is actually sent).
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.168.0.1", 80))
        return s.getsockname()[0]


def publish_local_name(name, port):
    """Announce <name>.local on the network via Bonjour so other devices can open it."""
    proc = subprocess.Popen(
        ["dns-sd", "-P", "Job Applications", "_http._tcp", "local", str(port), f"{name}.local", lan_ip()],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    atexit.register(proc.terminate)


class Handler(BaseHTTPRequestHandler):
    password = ""

    def _authorized(self):
        # This Mac never needs the password; other devices on the network do.
        if not self.password or self.client_address[0] in ("127.0.0.1", "::1"):
            return True
        header = self.headers.get("Authorization", "")
        if header.startswith("Basic "):
            try:
                _, _, given = base64.b64decode(header[6:]).decode().partition(":")
            except ValueError:
                return False
            return hmac.compare_digest(given, self.password)
        return False

    def _deny(self):
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Job Applications"')
        self.end_headers()

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self._authorized():
            return self._deny()
        path = urlsplit(self.path).path
        if path == "/":
            self._send(200, DASHBOARD_PATH.read_bytes(), "text/html; charset=utf-8")
        elif path == "/api/data":
            self._send(200, json.dumps(dashboard_data()).encode(), "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def do_POST(self):
        if not self._authorized():
            return self._deny()
        if self.path == "/api/run":
            run_now.set()
            self._send(202, b"{}", "application/json")
        else:
            self._send(404, b"not found", "text/plain")

    def log_message(self, *args):
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--interval", type=int, default=15, help="minutes between checks (default 15)")
    p.add_argument("--days", type=int, default=30, help="how far back to look in Gmail (default 30)")
    load_env()
    p.add_argument("--port", type=int, default=int(os.environ.get("DASHBOARD_PORT", 8765)))
    p.add_argument("--host", default=os.environ.get("DASHBOARD_HOST", "127.0.0.1"),
                   help="0.0.0.0 to allow other devices on your network")
    p.add_argument("--name", default=os.environ.get("DASHBOARD_NAME", ""),
                   help="publish the dashboard as <name>.local on your network")
    p.add_argument("--no-browser", action="store_true", help="don't open the dashboard on start")
    p.add_argument("--once", action="store_true", help="sort once and exit, no dashboard")
    p.add_argument("--alert-time", default=os.environ.get("DAILY_ALERT_TIME", "09:00"),
                   help='daily "last 24 hours" pop-up, HH:MM (empty to turn off)')
    p.add_argument("--alert-now", action="store_true", help="show the daily pop-up now and exit")
    args = p.parse_args()

    if os.environ.get("ANTHROPIC_API_KEY", "sk-ant-...") == "sk-ant-...":
        raise SystemExit(f"Set ANTHROPIC_API_KEY in {ENV_PATH} — see README.md.")
    init_db()
    if args.alert_now:
        show_daily_alert(f"http://localhost{'' if args.port == 80 else f':{args.port}'}")
        return
    if args.once:
        run_once(args.days)
        return

    gmail_service()  # do the OAuth browser flow up front, before going into the background loop
    state["interval"] = args.interval
    threading.Thread(target=poll_loop, args=(args.interval, args.days), daemon=True).start()

    Handler.password = os.environ.get("DASHBOARD_PASSWORD", "")
    on_lan = args.host != "127.0.0.1"
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    suffix = "" if args.port == 80 else f":{args.port}"
    url = f"http://localhost{suffix}"
    print(f"Dashboard: {url}  (checking Gmail every {args.interval} min)")
    if args.alert_time:
        threading.Thread(target=daily_alert_loop, args=(args.alert_time, url), daemon=True).start()
        print(f"Daily alert at {args.alert_time}")
    if on_lan:
        if args.name:
            publish_local_name(args.name, args.port)
            print(f"On your network: http://{args.name}.local{suffix}")
        print(f"On your network: http://{lan_ip()}{suffix}")
    if not args.no_browser:
        webbrowser.open(url)
    server.serve_forever()


if __name__ == "__main__":
    main()
