import os
import re
import base64
import hmac
import hashlib
import psycopg2
import resend
from datetime import datetime, timezone, timedelta
from urllib.parse import quote, unquote
from psycopg2.extras import RealDictCursor
from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, render_template, request, jsonify, redirect, Response

app = Flask(__name__)

VALID_SOURCES = {"Direct", "Google Search", "Social Media", "Referral", "Email Campaign", "Other"}

# 1x1 transparent GIF — returned by the tracking pixel endpoint
TRACKING_PIXEL = base64.b64decode(
    "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
)

# ── Env ───────────────────────────────────────────────────────────────────────

def load_env():
    # On Vercel (and any server): real environment variables take priority
    if os.environ.get("DATABASE_URL"):
        return {
            "DATABASE_URL":   os.environ["DATABASE_URL"],
            "RESEND_API_KEY": os.environ.get("RESEND_API_KEY", ""),
            "BASE_URL":       os.environ.get("BASE_URL", "http://127.0.0.1:5000"),
            "SECRET_KEY":     os.environ.get("SECRET_KEY", "brks-crm-default-secret"),
            "CRON_SECRET":    os.environ.get("CRON_SECRET", ""),
        }
    # Local development: fall back to file.env
    env_path = os.path.join(os.path.dirname(__file__), "file.env")
    result = {}
    try:
        with open(env_path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, _, v = line.partition("=")
                    result[k.strip()] = v.strip()
                elif line.startswith(("postgresql://", "postgres://")):
                    result["DATABASE_URL"] = line
    except FileNotFoundError:
        pass
    return result

_env         = load_env()
DATABASE_URL = _env.get("DATABASE_URL") or next(
    (v for v in _env.values() if v.startswith("postgresql://")), None
)
if not DATABASE_URL:
    raise RuntimeError("No PostgreSQL URL found in environment or file.env")

resend.api_key = _env.get("RESEND_API_KEY", "")
BASE_URL       = _env.get("BASE_URL", "http://127.0.0.1:5000").rstrip("/")
SECRET_KEY     = _env.get("SECRET_KEY", "brks-crm-default-secret")
CRON_SECRET    = _env.get("CRON_SECRET", "")

# ── Database ──────────────────────────────────────────────────────────────────

def get_db():
    return psycopg2.connect(DATABASE_URL, cursor_factory=RealDictCursor)


DEFAULT_TEMPLATES = [
    {
        "name": "Welcome", "delay_days": 0,
        "subject": "Thanks for reaching out, {{first_name}}!",
        "html_body": """<div style="font-family:'Segoe UI',system-ui,sans-serif;max-width:580px;margin:0 auto;padding:40px 24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:14px;padding:36px;text-align:center;margin-bottom:32px">
    <h1 style="color:#fff;margin:0;font-size:1.7rem;font-weight:700">Welcome, {{first_name}}! 👋</h1>
    <p style="color:rgba(255,255,255,0.85);margin:10px 0 0">We're glad you reached out.</p>
  </div>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">Hi {{first_name}},</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">Thanks for getting in touch! We've received your details and someone from our team will be in touch shortly.</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:32px">Feel free to reply to this email if you have any questions.</p>
  <div style="background:#f9fafb;border-radius:10px;padding:20px;margin-bottom:32px;font-size:0.9rem;color:#6b7280;line-height:1.8">
    <strong style="color:#374151;display:block;margin-bottom:6px">Your details on file:</strong>
    Name — {{first_name}} {{last_name}}<br/>Email — {{email}}<br/>Phone — {{phone}}
  </div>
  <p style="font-size:0.8rem;color:#9ca3af;text-align:center;border-top:1px solid #f3f4f6;padding-top:20px">Brks Education · You received this because you submitted our contact form.</p>
</div>""",
    },
    {
        "name": "Follow-up Day 3", "delay_days": 3,
        "subject": "Quick check-in, {{first_name}} 👋",
        "html_body": """<div style="font-family:'Segoe UI',system-ui,sans-serif;max-width:580px;margin:0 auto;padding:40px 24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:14px;padding:36px;text-align:center;margin-bottom:32px">
    <h1 style="color:#fff;margin:0;font-size:1.7rem;font-weight:700">Just checking in 🙌</h1>
  </div>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">Hi {{first_name}},</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">It's been a couple of days since you reached out. I wanted to follow up and see if you have any questions or if there's anything we can help with.</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:32px">Just reply to this email and we'll get back to you right away.</p>
  <p style="font-size:0.8rem;color:#9ca3af;text-align:center;border-top:1px solid #f3f4f6;padding-top:20px">Brks Education · Reply to unsubscribe.</p>
</div>""",
    },
    {
        "name": "Follow-up Day 7", "delay_days": 7,
        "subject": "One last thing, {{first_name}}",
        "html_body": """<div style="font-family:'Segoe UI',system-ui,sans-serif;max-width:580px;margin:0 auto;padding:40px 24px;color:#1f2937">
  <div style="background:linear-gradient(135deg,#667eea,#764ba2);border-radius:14px;padding:36px;text-align:center;margin-bottom:32px">
    <h1 style="color:#fff;margin:0;font-size:1.7rem;font-weight:700">Still here for you 💬</h1>
  </div>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">Hi {{first_name}},</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:16px">This is our final follow-up. We don't want to crowd your inbox, but we want to make sure you have everything you need.</p>
  <p style="font-size:1rem;line-height:1.75;margin-bottom:32px">If now isn't the right time, no worries — we'll be here whenever you're ready. Just reply to this email.</p>
  <p style="font-size:0.8rem;color:#9ca3af;text-align:center;border-top:1px solid #f3f4f6;padding-top:20px">Brks Education · Reply to unsubscribe.</p>
</div>""",
    },
]


def init_db():
    with get_db() as conn:
        with conn.cursor() as cur:
            # contacts
            cur.execute("""CREATE TABLE IF NOT EXISTS contacts (
                id SERIAL PRIMARY KEY, first_name TEXT NOT NULL, last_name TEXT NOT NULL,
                email TEXT NOT NULL, phone TEXT NOT NULL, source TEXT DEFAULT 'Direct',
                utm_source TEXT, utm_medium TEXT, utm_campaign TEXT,
                utm_term TEXT, utm_content TEXT, created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
            for col, dfn in [("source","TEXT DEFAULT 'Direct'"),("utm_source","TEXT"),
                ("utm_medium","TEXT"),("utm_campaign","TEXT"),("utm_term","TEXT"),("utm_content","TEXT"),
                ("institution_type","TEXT"),("status","TEXT DEFAULT 'Lead'"),("tags","TEXT DEFAULT ''")]:
                cur.execute(f"ALTER TABLE contacts ADD COLUMN IF NOT EXISTS {col} {dfn}")

            # email_templates
            cur.execute("""CREATE TABLE IF NOT EXISTS email_templates (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, subject TEXT NOT NULL,
                html_body TEXT NOT NULL, delay_days INTEGER NOT NULL DEFAULT 0,
                active BOOLEAN DEFAULT TRUE, updated_at TIMESTAMPTZ DEFAULT NOW()
            )""")

            # email_queue
            cur.execute("""CREATE TABLE IF NOT EXISTS email_queue (
                id SERIAL PRIMARY KEY, contact_id INTEGER NOT NULL, template_id INTEGER NOT NULL,
                scheduled_at TIMESTAMPTZ NOT NULL, sent_at TIMESTAMPTZ,
                status TEXT DEFAULT 'pending', error TEXT,
                open_count INTEGER DEFAULT 0, click_count INTEGER DEFAULT 0,
                opened_at TIMESTAMPTZ, clicked_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")
            for col, dfn in [("open_count","INTEGER DEFAULT 0"),("click_count","INTEGER DEFAULT 0"),
                ("opened_at","TIMESTAMPTZ"),("clicked_at","TIMESTAMPTZ")]:
                cur.execute(f"ALTER TABLE email_queue ADD COLUMN IF NOT EXISTS {col} {dfn}")

            # email_events
            cur.execute("""CREATE TABLE IF NOT EXISTS email_events (
                id SERIAL PRIMARY KEY, queue_id INTEGER NOT NULL,
                event_type TEXT NOT NULL, url TEXT, ip TEXT, user_agent TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )""")

            # sequences
            cur.execute("""CREATE TABLE IF NOT EXISTS sequences (
                id SERIAL PRIMARY KEY, name TEXT NOT NULL, description TEXT DEFAULT '',
                steps JSONB DEFAULT '[]', created_at TIMESTAMPTZ DEFAULT NOW()
            )""")

            # seed default templates
            cur.execute("SELECT COUNT(*) AS n FROM email_templates")
            if cur.fetchone()["n"] == 0:
                for t in DEFAULT_TEMPLATES:
                    cur.execute(
                        "INSERT INTO email_templates (name,subject,html_body,delay_days) VALUES (%s,%s,%s,%s)",
                        (t["name"], t["subject"], t["html_body"], t["delay_days"]),
                    )
                print("Seeded 3 default email templates.")
        conn.commit()
    print("Database ready.")


# ── Tracking helpers ──────────────────────────────────────────────────────────

def make_token(queue_id: int) -> str:
    return hmac.new(SECRET_KEY.encode(), str(queue_id).encode(), hashlib.sha256).hexdigest()[:24]


def inject_tracking(html: str, queue_id: int) -> str:
    """Replace hrefs with click-tracking URLs and append an open-tracking pixel."""
    token = make_token(queue_id)

    def replace_href(m):
        url = m.group(1)
        if url.startswith(("mailto:", "tel:", "#", "{{")):
            return m.group(0)
        encoded = quote(url, safe="")
        return f'href="{BASE_URL}/track/click/{queue_id}/{token}?u={encoded}"'

    html = re.sub(r'href="([^"]*)"', replace_href, html)

    pixel = (
        f'<img src="{BASE_URL}/track/open/{queue_id}/{token}" '
        f'width="1" height="1" alt="" style="display:none;border:0" />'
    )
    html = html.replace("</body>", pixel + "</body>") if "</body>" in html else html + pixel
    return html


# ── Email send ────────────────────────────────────────────────────────────────

def render_vars(text: str, contact: dict) -> str:
    for k in ("first_name", "last_name", "email", "phone"):
        text = text.replace("{{" + k + "}}", contact.get(k) or "")
    return text


def send_email(to_email: str, subject: str, html: str):
    if not resend.api_key:
        print("WARNING: RESEND_API_KEY not set — skipping email.")
        return
    resend.Emails.send({"from": "onboarding@resend.dev", "to": [to_email],
                        "subject": subject, "html": html})


def enqueue_sequence(contact_id: int, created_at: datetime):
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, delay_days FROM email_templates WHERE active=TRUE ORDER BY delay_days")
            for t in cur.fetchall():
                scheduled_at = created_at + timedelta(days=t["delay_days"])
                cur.execute(
                    "INSERT INTO email_queue (contact_id,template_id,scheduled_at) VALUES (%s,%s,%s)",
                    (contact_id, t["id"], scheduled_at),
                )
        conn.commit()


def process_email_queue():
    try:
        with get_db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT q.id, q.contact_id, q.template_id,
                           c.first_name, c.last_name, c.email, c.phone,
                           t.subject, t.html_body
                    FROM   email_queue q
                    JOIN   contacts        c ON c.id = q.contact_id
                    JOIN   email_templates t ON t.id = q.template_id
                    WHERE  q.status='pending' AND q.scheduled_at <= NOW()
                    ORDER  BY q.scheduled_at
                """)
                due = cur.fetchall()

            for row in due:
                r        = dict(row)
                qid      = r["id"]
                subject  = render_vars(r["subject"],   r)
                html     = render_vars(r["html_body"], r)
                html     = inject_tracking(html, qid)

                try:
                    send_email(r["email"], subject, html)
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE email_queue SET status='sent', sent_at=NOW() WHERE id=%s",
                            (qid,),
                        )
                    print(f"[queue] Sent '{subject}' → {r['email']}")
                except Exception as e:
                    with conn.cursor() as cur:
                        cur.execute(
                            "UPDATE email_queue SET status='failed', error=%s WHERE id=%s",
                            (str(e), qid),
                        )
                    print(f"[queue] Failed {r['email']}: {e}")
            conn.commit()
    except Exception as e:
        print(f"[queue] Scheduler error: {e}")


# ── Validation ────────────────────────────────────────────────────────────────

def validate(data):
    errors = {}
    if not data.get("first_name", "").strip(): errors["first_name"] = "First name is required."
    if not data.get("last_name",  "").strip(): errors["last_name"]  = "Last name is required."
    email = data.get("email", "").strip()
    if not email: errors["email"] = "Email is required."
    elif not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email): errors["email"] = "Enter a valid email address."
    phone = data.get("phone", "").strip()
    if not phone: errors["phone"] = "Phone number is required."
    elif not re.match(r"^\+?[\d\s\-().]{7,20}$", phone): errors["phone"] = "Enter a valid phone number."
    return errors

def clean_utm(v):
    return (v or "").strip()[:255] or None


# ── Page routes ───────────────────────────────────────────────────────────────

@app.route("/")
def index():      return render_template("index.html")

@app.route("/dashboard")
def dashboard():  return render_template("dashboard.html")

@app.route("/sequences")
def sequences():  return render_template("sequences.html")

@app.route("/analytics")
def analytics():  return render_template("analytics.html")


# ── Tracking routes ───────────────────────────────────────────────────────────

@app.route("/track/open/<int:queue_id>/<token>")
def track_open(queue_id, token):
    if hmac.compare_digest(make_token(queue_id), token):
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO email_events (queue_id,event_type,ip,user_agent) VALUES (%s,'open',%s,%s)",
                        (queue_id, request.remote_addr, request.user_agent.string[:500]),
                    )
                    cur.execute(
                        "UPDATE email_queue SET open_count=open_count+1, opened_at=COALESCE(opened_at,NOW()) WHERE id=%s",
                        (queue_id,),
                    )
                conn.commit()
        except Exception as e:
            print(f"[track open] {e}")
    return Response(TRACKING_PIXEL, mimetype="image/gif",
                    headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.route("/track/click/<int:queue_id>/<token>")
def track_click(queue_id, token):
    dest = unquote(request.args.get("u", "/"))
    if hmac.compare_digest(make_token(queue_id), token):
        try:
            with get_db() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        "INSERT INTO email_events (queue_id,event_type,url,ip,user_agent) VALUES (%s,'click',%s,%s,%s)",
                        (queue_id, dest, request.remote_addr, request.user_agent.string[:500]),
                    )
                    cur.execute(
                        "UPDATE email_queue SET click_count=click_count+1, clicked_at=COALESCE(clicked_at,NOW()) WHERE id=%s",
                        (queue_id,),
                    )
                conn.commit()
        except Exception as e:
            print(f"[track click] {e}")
    return redirect(dest)


# ── API: contacts ─────────────────────────────────────────────────────────────

@app.route("/api/contacts")
def api_contacts():
    q = request.args.get("q","").strip()
    date_from = request.args.get("from","").strip()
    date_to   = request.args.get("to","").strip()

    sql = """
        SELECT c.*,
               (SELECT COUNT(*) FROM email_queue q WHERE q.contact_id=c.id AND q.status='sent')   AS emails_sent,
               (SELECT COUNT(*) FROM email_queue q WHERE q.contact_id=c.id AND q.open_count>0)     AS emails_opened,
               (SELECT COUNT(*) FROM email_queue q WHERE q.contact_id=c.id AND q.click_count>0)    AS emails_clicked
        FROM contacts c WHERE 1=1
    """
    params = []
    if q:
        sql += " AND (c.first_name ILIKE %s OR c.last_name ILIKE %s OR c.email ILIKE %s OR c.phone ILIKE %s OR c.utm_source ILIKE %s OR c.utm_campaign ILIKE %s)"
        params.extend([f"%{q}%"] * 6)
    if date_from: sql += " AND c.created_at >= %s"; params.append(date_from)
    if date_to:   sql += " AND c.created_at <= %s"; params.append(date_to + " 23:59:59")
    sql += " ORDER BY c.created_at DESC"

    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            rows = cur.fetchall()

    contacts = []
    for r in rows:
        c = dict(r)
        if c.get("created_at"): c["created_at"] = c["created_at"].strftime("%Y-%m-%d %H:%M")
        contacts.append(c)
    return jsonify(contacts)


# ── API: contacts update / export ────────────────────────────────────────────

@app.route("/api/contacts/<int:cid>", methods=["PUT"])
def api_contact_update(cid):
    body = request.get_json() or {}
    allowed = {"status", "tags"}
    sets, params = [], []
    for k in allowed:
        if k in body:
            sets.append(f"{k}=%s")
            params.append(body[k])
    if not sets:
        return jsonify({"ok": False, "error": "Nothing to update"}), 422
    params.append(cid)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(f"UPDATE contacts SET {','.join(sets)} WHERE id=%s", params)
        conn.commit()
    return jsonify({"ok": True})


@app.route("/api/contacts/export")
def api_contacts_export():
    import csv, io
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""SELECT first_name,last_name,email,phone,source,institution_type,
                               status,tags,utm_source,utm_medium,utm_campaign,created_at
                           FROM contacts ORDER BY created_at DESC""")
            rows = cur.fetchall()
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(["First Name","Last Name","Email","Phone","Source","Institution","Status",
                "Tags","UTM Source","UTM Medium","UTM Campaign","Signed Up"])
    for r in rows:
        w.writerow([r["first_name"],r["last_name"],r["email"],r["phone"],
                    r["source"] or "",r["institution_type"] or "",r["status"] or "Lead",
                    r["tags"] or "",r["utm_source"] or "",r["utm_medium"] or "",
                    r["utm_campaign"] or "",
                    r["created_at"].strftime("%Y-%m-%d %H:%M") if r["created_at"] else ""])
    out.seek(0)
    return Response(out.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=contacts.csv"})


# ── API: templates ────────────────────────────────────────────────────────────

@app.route("/api/templates")
def api_templates_list():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM email_templates ORDER BY delay_days")
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("updated_at"): r["updated_at"] = r["updated_at"].strftime("%Y-%m-%d %H:%M")
    return jsonify(rows)


@app.route("/api/templates/<int:tid>", methods=["POST"])
def api_template_update(tid):
    body = request.get_json()
    subject   = (body.get("subject")   or "").strip()
    html_body = (body.get("html_body") or "").strip()
    active    = bool(body.get("active", True))
    if not subject or not html_body:
        return jsonify({"ok": False, "error": "Subject and body are required."}), 422
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE email_templates SET subject=%s,html_body=%s,active=%s,updated_at=NOW() WHERE id=%s",
                (subject, html_body, active, tid),
            )
        conn.commit()
    return jsonify({"ok": True})


# ── API: queue ────────────────────────────────────────────────────────────────

@app.route("/api/queue")
def api_queue():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT q.id, q.status, q.scheduled_at, q.sent_at,
                       q.open_count, q.click_count, q.opened_at, q.clicked_at, q.error,
                       c.first_name, c.last_name, c.email,
                       t.name AS template_name, t.delay_days
                FROM   email_queue q
                JOIN   contacts        c ON c.id = q.contact_id
                JOIN   email_templates t ON t.id = q.template_id
                ORDER  BY q.scheduled_at DESC LIMIT 200
            """)
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        for f in ("scheduled_at","sent_at","opened_at","clicked_at"):
            if r.get(f): r[f] = r[f].strftime("%Y-%m-%d %H:%M")
    return jsonify(rows)


@app.route("/api/queue/stats")
def api_queue_stats():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT status, COUNT(*) AS n FROM email_queue GROUP BY status")
            rows = {r["status"]: r["n"] for r in cur.fetchall()}
    return jsonify({"pending": rows.get("pending",0), "sent": rows.get("sent",0), "failed": rows.get("failed",0)})


# ── API: analytics ────────────────────────────────────────────────────────────

@app.route("/api/analytics")
def api_analytics():
    with get_db() as conn:
        with conn.cursor() as cur:
            # overall
            cur.execute("""
                SELECT COUNT(*) AS total_sent,
                       COUNT(CASE WHEN open_count>0  THEN 1 END) AS total_opened,
                       COUNT(CASE WHEN click_count>0 THEN 1 END) AS total_clicked
                FROM   email_queue WHERE status='sent'
            """)
            overall = dict(cur.fetchone())

            # per template
            cur.execute("""
                SELECT t.id, t.name, t.delay_days, t.active,
                       COUNT(q.id) FILTER (WHERE q.status='sent')            AS sent,
                       COUNT(q.id) FILTER (WHERE q.open_count>0)             AS opened,
                       COUNT(q.id) FILTER (WHERE q.click_count>0)            AS clicked
                FROM   email_templates t
                LEFT JOIN email_queue q ON q.template_id = t.id
                GROUP  BY t.id, t.name, t.delay_days, t.active
                ORDER  BY t.delay_days
            """)
            by_template = [dict(r) for r in cur.fetchall()]

            # emails sent per day (last 30 days)
            cur.execute("""
                SELECT DATE(sent_at) AS day, COUNT(*) AS n
                FROM   email_queue
                WHERE  status='sent' AND sent_at >= NOW() - INTERVAL '30 days'
                GROUP  BY DATE(sent_at) ORDER BY day
            """)
            by_day = [{"day": str(r["day"]), "n": r["n"]} for r in cur.fetchall()]

            # recent events
            cur.execute("""
                SELECT e.event_type, e.url, e.created_at,
                       c.first_name, c.last_name, c.email,
                       t.name AS template_name
                FROM   email_events e
                JOIN   email_queue      q ON q.id = e.queue_id
                JOIN   contacts         c ON c.id = q.contact_id
                JOIN   email_templates  t ON t.id = q.template_id
                ORDER  BY e.created_at DESC LIMIT 30
            """)
            events = [dict(r) for r in cur.fetchall()]
            for ev in events:
                if ev.get("created_at"): ev["created_at"] = ev["created_at"].strftime("%Y-%m-%d %H:%M")

    return jsonify({"overall": overall, "by_template": by_template,
                    "by_day": by_day, "recent_events": events})


# ── API: sequences ────────────────────────────────────────────────────────────

@app.route("/api/sequences", methods=["GET"])
def api_sequences_list():
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, name, description, steps, created_at FROM sequences ORDER BY created_at DESC")
            rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if r.get("created_at"): r["created_at"] = r["created_at"].strftime("%Y-%m-%d %H:%M")
        if r.get("steps") is None: r["steps"] = []
    return jsonify(rows)


@app.route("/api/sequences", methods=["POST"])
def api_sequences_create():
    import json
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 422
    description = (body.get("description") or "").strip()
    steps = body.get("steps") or []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO sequences (name, description, steps) VALUES (%s, %s, %s) RETURNING id",
                (name, description, json.dumps(steps)),
            )
            new_id = cur.fetchone()["id"]
        conn.commit()
    return jsonify({"ok": True, "id": new_id})


@app.route("/api/sequences/<int:sid>", methods=["PUT"])
def api_sequences_update(sid):
    import json
    body = request.get_json() or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"ok": False, "error": "Name is required"}), 422
    description = (body.get("description") or "").strip()
    steps = body.get("steps") or []
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE sequences SET name=%s, description=%s, steps=%s WHERE id=%s",
                (name, description, json.dumps(steps), sid),
            )
        conn.commit()
    return jsonify({"ok": True})


# ── Submit ────────────────────────────────────────────────────────────────────

@app.route("/submit", methods=["POST"])
def submit():
    source = request.form.get("source","Direct").strip()
    if source not in VALID_SOURCES: source = "Direct"
    data = {
        "first_name":   request.form.get("first_name","").strip(),
        "last_name":    request.form.get("last_name","").strip(),
        "email":        request.form.get("email","").strip(),
        "phone":        request.form.get("phone","").strip(),
        "source":       source,
        "utm_source":   clean_utm(request.form.get("utm_source")),
        "utm_medium":   clean_utm(request.form.get("utm_medium")),
        "utm_campaign": clean_utm(request.form.get("utm_campaign")),
        "utm_term":     clean_utm(request.form.get("utm_term")),
        "utm_content":      clean_utm(request.form.get("utm_content")),
        "institution_type": request.form.get("institution_type","").strip() or None,
    }
    errors = validate(data)
    if errors: return jsonify({"ok": False, "errors": errors}), 422

    now = datetime.now(timezone.utc)
    with get_db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO contacts (first_name,last_name,email,phone,source,
                       utm_source,utm_medium,utm_campaign,utm_term,utm_content,
                       institution_type,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id""",
                (data["first_name"],data["last_name"],data["email"],data["phone"],data["source"],
                 data["utm_source"],data["utm_medium"],data["utm_campaign"],data["utm_term"],
                 data["utm_content"],data["institution_type"],now),
            )
            contact_id = cur.fetchone()["id"]
        conn.commit()

    try:
        enqueue_sequence(contact_id, now)
    except Exception as e:
        print(f"[enqueue] {e}")
    return jsonify({"ok": True})


# ── Cron endpoint (called by Vercel Cron or an external scheduler) ────────────

@app.route("/api/cron", methods=["GET", "POST"])
def api_cron():
    if CRON_SECRET:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {CRON_SECRET}":
            return jsonify({"error": "Unauthorized"}), 401
    process_email_queue()
    return jsonify({"ok": True})


# ── Scheduler (local dev only) ────────────────────────────────────────────────

def start_scheduler():
    scheduler = BackgroundScheduler(timezone="UTC")
    scheduler.add_job(process_email_queue, "interval", minutes=1, id="email_queue")
    scheduler.start()
    print("[scheduler] Email queue processor started (every 60s).")


# DB setup — runs on every cold start (Vercel) and on local startup
try:
    init_db()
except Exception as e:
    print(f"WARNING: DB init failed: {e}")

if __name__ == "__main__":
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_scheduler()
    print("Running at http://127.0.0.1:5000")
    app.run(debug=True)
