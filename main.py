"""railscout — finds companies actively migrating off Rails and pitches them.

Lead source: Hacker News "Ask HN: Who is hiring?" (free, public, no API key).
A company hiring Python/FastAPI engineers while running Ruby/Rails is a company
migrating *right now* — they have budget approved and the pain is live. That's a
far higher-intent lead than a random Rails maintainer on GitHub.
"""

import html
import json
import os
import re
import smtplib
import imaplib
import time
import random
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "sent_log.json"
SUPPRESS_FILE = Path(__file__).parent / "unsubscribe_list.json"

# Kept low on purpose: a personal Gmail can't cold-send much before Google
# throttles or locks it. The stop-on-limit safeguard halts the run the moment
# Gmail refuses, so a bad day doesn't hammer the account into a longer lock.
MAX_NEW_PER_RUN = 10
MAX_FOLLOWUPS_PER_RUN = 5

FOLLOW_UP_DAYS = 3         # first follow-up: 3 days after the initial email
SECOND_FOLLOW_UP_DAYS = 7  # second/final follow-up: 7 days after the initial email

HN_API = "https://hn.algolia.com/api/v1"
HN_MAX_PAGES = 5           # each page is up to 1000 comments


# GitHub Actions injects an *unset* secret as an empty string "" (not absent),
# and os.environ.get() only falls back to the default when a key is missing.
# Treat empty as missing so a secret you never set uses the default below.
def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


BEDROCK_MODEL_ID = env("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = env("BEDROCK_REGION", "us-east-1")

# One month's thread yields only ~5 qualified leads, so scan a few months back.
HN_MONTHS = int(env("HN_MONTHS", "3"))

# Dry run: find leads and generate the real emails, but send nothing and never
# open an SMTP connection. Lets us test the pipeline without touching Gmail
# (important while the account is throttled) and without writing to sent_log.
DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

# CAN-SPAM: a real physical mailing address is legally required in every
# commercial email. Set SENDER_ADDRESS / SENDER_NAME as GitHub secrets.
SENDER_NAME = env("SENDER_NAME", "hassan")
SENDER_ADDRESS = env("SENDER_ADDRESS", "")

# Your proof-of-work. This is the single most persuasive thing in the email.
PROOF_URL = env("PROOF_URL", "https://github.com/HassanNadeem1122/fat-free-crm-fastapi")

# ── Lead qualification ────────────────────────────────────────────────────
# A post must show BOTH a Python-side signal and a Ruby-side signal — that
# combination is what says "polyglot shop mid-migration", not just "a job".
PY_SIGNALS = ("fastapi", "python")
RUBY_SIGNALS = ("rails", "ruby")
# Presence of these upgrades a lead to "strong" (explicitly moving/rewriting).
STRONG_SIGNALS = ("fastapi", "migrat", "rewrit", "porting", "moving off", "legacy")

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Emails we never contact (generic/no-reply inboxes + bots).
SKIP_PREFIXES = (
    "noreply@", "no-reply@", "donotreply@", "postmaster@", "abuse@",
    "webmaster@", "unsubscribe@", "subscribe@", "list@", "lists@",
    "google-groups@", "mailer-daemon@",
)
JUNK_EMAIL_MARKERS = (
    "users.noreply.github.com", "noreply", "no-reply", "example.com",
    "example.org", "yourcompany.com", "domain.com", "localhost", "[bot]",
    "sentry.io", ".png", ".jpg",
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Pre-Flight Check ──────────────────────────────────────────────────────

def pre_flight_check() -> bool:
    # NOTE: no GITHUB_TOKEN needed anymore — the HN API is public/keyless.
    required_env = [
        "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
        "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD",
    ]
    missing = [var for var in required_env if not os.environ.get(var)]
    if missing:
        log(f"❌ CRITICAL: Missing environment variables: {', '.join(missing)}")
        return False
    if not SENDER_ADDRESS:
        log("⚠️  SENDER_ADDRESS is not set. A physical mailing address is legally "
            "required by CAN-SPAM in every commercial email. Set it as a secret.")
    log("✅ System scan passed. All credentials ready.")
    return True


# ── State Management ──────────────────────────────────────────────────────

def load_json_list(path: Path) -> list:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []


def save_sent_log(entries: list) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))


def load_suppression() -> set:
    # unsubscribe_list.json is a plain JSON array of email strings.
    return {e.strip().lower() for e in load_json_list(SUPPRESS_FILE) if isinstance(e, str)}


def already_contacted(email: str, sent_log: list) -> bool:
    email = email.lower()
    return any((e.get("email") or "").lower() == email for e in sent_log)


# ── Inbox Sync ────────────────────────────────────────────────────────────

def check_if_replied(to_email: str, gmail_user: str, gmail_pass: str) -> bool:
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(gmail_user, gmail_pass)
        mail.select("inbox")
        status, messages = mail.search(None, f'FROM "{to_email}"')
        mail.logout()
        return bool(messages[0].split())
    except Exception as e:
        log(f"  ⚠️ IMAP sync failed for {to_email}: {e}")
        return False


# ── Lead Sourcing: HN "Who is hiring?" ────────────────────────────────────

def fetch_recent_hiring_threads(months: int = HN_MONTHS) -> list:
    """The most recent N 'Ask HN: Who is hiring?' stories (newest first).

    One month's thread only yields a handful of qualified leads, so we scan a
    few months back. A company that posted 2 months ago is usually still mid
    migration — these leads stay warm longer than a typical job ad.
    """
    threads = []
    try:
        resp = requests.get(
            f"{HN_API}/search_by_date",
            params={"tags": "story,author_whoishiring", "hitsPerPage": 30},
            timeout=20,
        )
        for hit in resp.json().get("hits", []):
            title = (hit.get("title") or "")
            if "who is hiring" in title.lower():
                threads.append({"id": hit["objectID"], "title": title})
            if len(threads) >= months:
                break
    except Exception as e:
        log(f"❌ HN thread lookup failed: {e}")
    return threads


def strip_html(raw: str) -> str:
    text = re.sub(r"<p>", "\n", raw)
    text = re.sub(r"<[^>]+>", " ", text)
    return html.unescape(text)


def fetch_job_posts(story_id: str) -> list:
    """Every top-level comment on the thread is one company's job post."""
    posts = []
    for page in range(HN_MAX_PAGES):
        try:
            resp = requests.get(
                f"{HN_API}/search",
                params={"tags": f"comment,story_{story_id}",
                        "hitsPerPage": 1000, "page": page},
                timeout=30,
            )
            data = resp.json()
        except Exception as e:
            log(f"  ⚠️ HN page {page} failed: {e}")
            break
        hits = data.get("hits", [])
        if not hits:
            break
        for h in hits:
            raw = h.get("comment_text")
            if raw:
                posts.append(strip_html(raw))
        if page >= data.get("nbPages", 1) - 1:
            break
    return posts


def is_valid_lead_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    low = email.lower()
    if low.startswith(SKIP_PREFIXES):
        return False
    if any(marker in low for marker in JUNK_EMAIL_MARKERS):
        return False
    return True


def extract_email(text: str) -> str | None:
    for match in EMAIL_RE.findall(text):
        if is_valid_lead_email(match):
            return match.lower()
    return None


def qualify(text: str) -> str | None:
    """Return 'strong' / 'ok' if this post smells like a Rails->Python migration."""
    low = text.lower()
    has_py = any(s in low for s in PY_SIGNALS)
    has_ruby = any(s in low for s in RUBY_SIGNALS)
    if not (has_py and has_ruby):
        return None
    if any(s in low for s in STRONG_SIGNALS):
        return "strong"
    return "ok"


def parse_company(text: str) -> str:
    """HN posts start like: 'Acme Corp | SF | Senior Engineer | ...'"""
    first_line = next((l for l in text.strip().split("\n") if l.strip()), "")
    company = first_line.split("|")[0].strip()
    return company[:80] if company else "there"


def find_leads() -> list:
    threads = fetch_recent_hiring_threads()
    if not threads:
        log("  ❌ Couldn't find any 'Who is hiring?' threads.")
        return []

    leads, seen = [], set()
    total_posts = 0
    for thread in threads:
        posts = fetch_job_posts(thread["id"])
        total_posts += len(posts)
        log(f"  📋 {thread['title']} — {len(posts)} job posts")
        for post in posts:
            tier = qualify(post)
            if not tier:
                continue
            email = extract_email(post)
            if not email or email in seen:
                continue
            seen.add(email)
            leads.append({
                "email": email,
                "company": parse_company(post),
                "tier": tier,
                "snippet": " ".join(post.split())[:700],
            })
    log(f"  📄 Scanned {total_posts} job posts across {len(threads)} months")
    # Strongest signals first — explicit "migrating"/"fastapi" posts get emailed
    # before generic polyglot shops.
    leads.sort(key=lambda l: 0 if l["tier"] == "strong" else 1)
    strong = sum(1 for l in leads if l["tier"] == "strong")
    log(f"  🎯 {len(leads)} migration-intent leads with a contact email ({strong} strong)")
    return leads


# ── AI Generation ─────────────────────────────────────────────────────────

def generate_initial_email(lead: dict) -> dict | None:
    prompt = f"""Write a short, developer-to-developer email to a company that just posted a job on Hacker News.

Their job post (excerpt):
\"\"\"{lead['snippet']}\"\"\"

Company: {lead['company']}

About me (the sender):
- I recently ported ~6,000 lines of Ruby on Rails (Fat Free CRM) to FastAPI, end to end.
- Architecture and code: {PROOF_URL}

STRICT RULES:
1. Tone: informal, technical, direct — like messaging another engineer. NO sales vocabulary, no "hope you're well", no "I came across your company and was impressed".
2. Length: under 80 words.
3. Open by referencing something SPECIFIC and real from their job post (the role, the stack, or what they're building). Do not invent facts that aren't in the post.
4. Make the connection: they're bringing on Python/FastAPI while running Ruby/Rails — that's a migration, and I've already done exactly that one.
5. Include this link exactly once: {PROOF_URL}
6. CTA: low-pressure. Offer to help de-risk the migration or take a piece of it off their plate. Ask a simple question, don't demand a call.
7. Formatting: entirely lowercase, casual punctuation, simple line breaks.
8. Subject: short, lowercase, mention the migration and their company.

Output ONLY raw JSON starting with {{ and ending with }}: {{"subject": "...", "body": "..."}}
"""
    client = boto3.client(
        "bedrock-runtime", region_name=BEDROCK_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    response = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 500,
        "messages": [{"role": "user", "content": prompt}],
    }))
    text = json.loads(response["body"].read())["content"][0]["text"]

    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        log("  ❌ AI did not return valid JSON.")
        return None
    return json.loads(text[start:end])


# ── SMTP Sending ──────────────────────────────────────────────────────────

class DailyLimitReached(Exception):
    """Raised when Gmail refuses further sends for the day (550 5.4.5)."""


def build_footer() -> str:
    lines = ["\n\n—", SENDER_NAME,
             'not relevant? just reply "unsubscribe" and i won\'t reach out again.']
    if SENDER_ADDRESS:
        lines.append(SENDER_ADDRESS)
    return "\n".join(lines)


def send_email(to_email: str, subject: str, body: str, add_footer: bool = True) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")

    if add_footer:
        body = body.rstrip() + build_footer()

    if DRY_RUN:
        log(f"  🧪 DRY RUN — would send to {to_email}")
        log(f"       subject: {subject}")
        for line in body.strip().split("\n"):
            log(f"       | {line}")
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, to_email, msg.as_string())
        return True
    except Exception as e:
        text = str(e).lower()
        # Gmail's daily cap — stop the whole run so we don't keep hammering a
        # throttled account (that only makes the lock last longer).
        if "5.4.5" in text or "sending limit exceeded" in text or "5.7.1" in text:
            raise DailyLimitReached(str(e))
        log(f"  ❌ SMTP error: {e}")
        return False


# ── Follow-up sequence ────────────────────────────────────────────────────

FOLLOWUP_1_BODY = (
    "hey,\n\njust floating this to the top of your inbox. if the migration is on "
    "the roadmap i'd be happy to take a piece of it — otherwise i'll stop bugging "
    "you.\n\nbest,\nhassan"
)

# Final "breakup" email — this consistently pulls the most replies of the whole
# sequence, because it's easy to say "actually, wait" when someone's walking away.
FOLLOWUP_2_BODY = (
    "hey,\n\nlast time i'll reach out — i'll assume the migration isn't a priority "
    "right now and close this out. if that changes, my rails -> fastapi work is "
    f"here: {PROOF_URL}\n\ncheers,\nhassan"
)


def run_followups(sent_log, gmail_user, gmail_pass, current_time) -> None:
    log("🔄 Phase 1: Follow-ups and reply checks...")
    sent = 0
    for entry in sent_log:
        if sent >= MAX_FOLLOWUPS_PER_RUN:
            break
        if entry.get("replied") or entry.get("second_follow_up_sent_at"):
            continue  # replied, or already got both follow-ups — nothing left to do
        try:
            initial_date = datetime.fromisoformat(entry["initial_sent_at"])
        except Exception:
            continue
        days = (current_time - initial_date).days

        # Decide which follow-up (if any) is due for this contact.
        if not entry.get("follow_up_sent_at"):
            if days < FOLLOW_UP_DAYS:
                continue
            stage, body, stamp = 1, FOLLOWUP_1_BODY, "follow_up_sent_at"
        else:
            if days < SECOND_FOLLOW_UP_DAYS:
                continue
            stage, body, stamp = 2, FOLLOWUP_2_BODY, "second_follow_up_sent_at"

        log(f"  🔍 Checking replies from {entry['email']}...")
        if check_if_replied(entry["email"], gmail_user, gmail_pass):
            log("  ✅ Prospect replied! Marking as replied.")
            entry["replied"] = True
            save_sent_log(sent_log)
            continue

        target = entry.get("company") or entry.get("org") or entry["email"]
        log(f"  📤 Follow-up #{stage} (day {days}) to {target}...")
        if send_email(entry["email"], f"Re: {entry['subject']}", body):
            sent += 1
            if not DRY_RUN:
                entry[stamp] = current_time.isoformat()
                save_sent_log(sent_log)
                time.sleep(random.randint(60, 120))
    log(f"  Follow-ups sent this run: {sent}")


# ── New outreach ──────────────────────────────────────────────────────────

def run_new_outreach(sent_log, current_time) -> None:
    log("🔍 Phase 2: New outreach — companies hiring FastAPI/Python while on Rails...")
    suppressed = load_suppression()
    leads = find_leads()
    if not leads:
        log("  No qualifying leads found this run.")
        return

    sent = 0
    gen_failures = 0  # consecutive Bedrock failures -> likely misconfig

    for lead in leads:
        if sent >= MAX_NEW_PER_RUN:
            break
        email = lead["email"]
        if email in suppressed or already_contacted(email, sent_log):
            continue

        log(f"  ✉️ [{lead['tier']}] {lead['company']} <{email}>")

        try:
            content = generate_initial_email(lead)
            gen_failures = 0
        except Exception as e:
            gen_failures += 1
            log(f"  ❌ Bedrock generation error ({gen_failures}): {e}")
            if gen_failures >= 3:
                log("  🛑 3 generation failures in a row — almost certainly a bad "
                    "BEDROCK_MODEL_ID or the model isn't enabled in this region. "
                    "Aborting so we don't burn through leads.")
                return
            continue

        if not content:
            continue

        log(f'  📤 Sending: "{content["subject"]}"')
        if send_email(email, content["subject"], content["body"]):
            sent += 1
            log(f"  ✅ Sent ({sent}/{MAX_NEW_PER_RUN})")
            if not DRY_RUN:
                sent_log.append({
                    "company": lead["company"],
                    "email": email,
                    "tier": lead["tier"],
                    "source": "hn_whoishiring",
                    "subject": content["subject"],
                    "initial_sent_at": current_time.isoformat(),
                    "follow_up_sent_at": None,
                    "replied": False,
                })
                save_sent_log(sent_log)
                time.sleep(random.randint(120, 300))

    log(f"  New emails sent this run: {sent}")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if not pre_flight_check():
        return

    gmail_user = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")

    sent_log = load_json_list(LOG_FILE)
    current_time = datetime.now(timezone.utc)

    try:
        run_followups(sent_log, gmail_user, gmail_pass, current_time)
        run_new_outreach(sent_log, current_time)
    except DailyLimitReached as e:
        log("🛑 Gmail daily sending limit hit — stopping this run to protect the "
            "account. It resets in ~24h; the next scheduled run will continue where "
            f"we left off. (Gmail said: {e})")

    log("─" * 50)
    log("🏁 Cycle complete.")


if __name__ == "__main__":
    main()
