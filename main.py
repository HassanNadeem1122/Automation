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
from collections import Counter
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "sent_log.json"
SUPPRESS_FILE = Path(__file__).parent / "unsubscribe_list.json"
# Qualified companies with no email in their post — you hit these on LinkedIn.
MANUAL_FILE = Path(__file__).parent / "manual_leads.json"

FOLLOW_UP_DAYS = 3         # first follow-up: 3 days after the initial email
SECOND_FOLLOW_UP_DAYS = 7  # second/final follow-up: 7 days after the initial email

HN_API = "https://hn.algolia.com/api/v1"
HN_MAX_PAGES = 5           # each page is up to 1000 comments
GITHUB_API = "https://api.github.com"
REPO_SCAN_LIMIT = 40
COMMITS_PER_REPO = 30


# GitHub Actions injects an *unset* secret as an empty string "" (not absent),
# and os.environ.get() only falls back to the default when a key is missing.
# Treat empty as missing so a secret you never set uses the default below.
def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


BEDROCK_MODEL_ID = env("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = env("BEDROCK_REGION", "us-east-1")

# One month's thread yields only a handful of emailable leads, so scan further
# back. A company that posted 6 months ago is usually still mid-migration —
# these leads age far better than a normal job ad, and real companies beat the
# GitHub hobbyist filler every time. Raise via the HN_MONTHS repo variable.
HN_MONTHS = int(env("HN_MONTHS", "8"))

# Daily send caps. Set the MAX_NEW_PER_RUN repo variable to ramp Gmail up as it
# warms (e.g. 5 -> 10 -> 15 -> 20) without touching code. ~20/day is the
# realistic ceiling for cold email from a free Gmail; the stop-on-limit
# safeguard halts the run if Google pushes back before then.
MAX_NEW_PER_RUN = int(env("MAX_NEW_PER_RUN", "10"))
MAX_FOLLOWUPS_PER_RUN = int(env("MAX_FOLLOWUPS_PER_RUN", "5"))

# GitHub filler leads: Rails maintainers. Lower intent than the HN companies,
# but they keep the daily quota full while HN only yields a handful a month.
SEARCH_QUERY = env("SEARCH_QUERY", "rails language:Ruby stars:50..300")
USE_GITHUB_FILLER = env("USE_GITHUB_FILLER", "true").lower() in ("1", "true", "yes")

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


def save_manual_leads(items: list) -> None:
    MANUAL_FILE.write_text(json.dumps(items, indent=2, default=str))


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
    """Grade a job post as a Rails-migration prospect.

    Requiring Python AND Rails AND an email in the same post turned out to match
    ~1 post in 1,475 — far too strict to be useful. The workable signal is just
    "they run Ruby/Rails and they're hiring": that means a funded company with an
    engineering team and a budget, which is who buys a migration. Posts that also
    mention Python/FastAPI or an explicit rewrite are graded 'strong' and get
    emailed first.
    """
    low = text.lower()
    if not any(s in low for s in RUBY_SIGNALS):
        return None
    has_py = any(s in low for s in PY_SIGNALS)
    if has_py or any(s in low for s in STRONG_SIGNALS):
        return "strong"
    return "ok"


# A pipe-segment containing any of these is a job title / employment type /
# location — not a company name. Posts don't reliably lead with the company,
# so we skip these segments rather than address an email to "hey Full-Time,".
JOB_TITLE_WORDS = (
    "engineer", "developer", "designer", "manager", "scientist", "architect",
    "full stack", "fullstack", "back end", "backend", "front end", "frontend",
    "devops", " sre", "intern", "recruiter", "analyst", "researcher",
    "full-time", "full time", "part-time", "part time", "contract", "freelance",
    "remote", "onsite", "on-site", "hybrid", "visa", "senior", "junior",
)


def clean_segment(segment: str) -> str:
    """Strip leading decoration (*bold*, emoji, dashes) and trailing parens."""
    seg = re.sub(r"^[^A-Za-z0-9]+", "", segment).strip()
    return re.sub(r"\s*\(.*?\)\s*$", "", seg).strip()


def parse_company(text: str) -> str:
    """HN posts usually start 'Acme Corp | SF | Senior Engineer | ...' — but not
    always. Some lead with the role ('Senior Engineer | Acme | Remote'), which
    made us address emails to "Senior Full Stack Engineer". Walk the first few
    segments and take the first one that doesn't read like a job title.
    """
    first_line = next((l for l in text.strip().split("\n") if l.strip()), "")
    segments = [s for s in (clean_segment(p) for p in first_line.split("|")) if s]
    for seg in segments[:3]:
        if not any(w in seg.lower() for w in JOB_TITLE_WORDS):
            return seg[:80]
    return ""  # every segment looked like a title — caller falls back to the domain


def company_from_email(email: str) -> str:
    """Last-resort company name: the email's domain.

    Some posts lead with nothing but job titles ('Head of Engineering | ... '),
    so there's no company to parse. The sending domain is a reliable stand-in —
    jointheteam@fetlife.com -> "Fetlife".
    """
    try:
        domain = email.split("@")[-1].split(".")[0]
        return domain.replace("-", " ").title()[:80] or "there"
    except Exception:
        return "there"


URL_RE = re.compile(r"https?://[^\s|)>\"]+")


def find_hn_leads() -> tuple:
    """Returns (emailable_leads, manual_leads).

    Most qualifying HN posts link to a careers page instead of an email — we
    can't cold-email those, but they're still strong prospects, so they go to
    manual_leads.json for LinkedIn outreach instead of being thrown away.
    """
    threads = fetch_recent_hiring_threads()
    if not threads:
        log("  ❌ Couldn't find any 'Who is hiring?' threads.")
        return [], []

    leads, manual, seen = [], [], set()
    total_posts = 0
    for thread in threads:
        posts = fetch_job_posts(thread["id"])
        total_posts += len(posts)
        log(f"  📋 {thread['title']} — {len(posts)} job posts")
        for post in posts:
            tier = qualify(post)
            if not tier:
                continue
            company = parse_company(post)
            email = extract_email(post)
            if email:
                if email in seen:
                    continue
                seen.add(email)
                leads.append({
                    "email": email,
                    "company": company or company_from_email(email),
                    "tier": tier,
                    "source": "hn_whoishiring",
                    "snippet": " ".join(post.split())[:700],
                })
            else:
                url = URL_RE.search(post)
                manual.append({
                    "company": company or "(see note)",
                    "tier": tier,
                    "link": url.group(0) if url else "",
                    "note": " ".join(post.split())[:300],
                })
    log(f"  📄 Scanned {total_posts} job posts across {len(threads)} months")
    leads.sort(key=lambda l: 0 if l["tier"] == "strong" else 1)
    strong = sum(1 for l in leads if l["tier"] == "strong")
    log(f"  🎯 HN: {len(leads)} emailable ({strong} strong) + {len(manual)} for manual LinkedIn")
    return leads, manual


def find_github_leads(github_token: str, needed: int) -> list:
    """Filler: real maintainers of active Rails repos, via the commits API.

    Lower intent than a company that's hiring, but there are plenty of them —
    they keep the daily quota full so the machine isn't idle between HN threads.
    """
    if needed <= 0 or not github_token:
        return []
    headers = {"Authorization": f"token {github_token}",
               "Accept": "application/vnd.github.v3+json"}
    leads, seen = [], set()
    try:
        resp = requests.get(
            f"{GITHUB_API}/search/repositories", headers=headers, timeout=20,
            params={"q": SEARCH_QUERY, "sort": "updated",
                    "order": "desc", "per_page": REPO_SCAN_LIMIT},
        )
        if resp.status_code != 200:
            log(f"  ⚠️ GitHub search returned {resp.status_code}")
            return []
        repos = [i.get("full_name") for i in resp.json().get("items", []) if i.get("full_name")]
    except Exception as e:
        log(f"  ⚠️ GitHub search failed: {e}")
        return []

    for full_name in repos:
        if len(leads) >= needed:
            break
        try:
            r = requests.get(f"{GITHUB_API}/repos/{full_name}/commits", headers=headers,
                             params={"per_page": COMMITS_PER_REPO}, timeout=20)
            if r.status_code != 200:
                continue
            tally, names = Counter(), {}
            for c in r.json():
                author = (c.get("commit") or {}).get("author") or {}
                email = (author.get("email") or "").strip().lower()
                name = (author.get("name") or "").strip()
                if not is_valid_lead_email(email) or "bot" in name.lower():
                    continue
                tally[email] += 2 if c.get("author") else 1
                names.setdefault(email, name)
            if not tally:
                continue
            best = tally.most_common(1)[0][0]
            if best in seen:
                continue
            seen.add(best)
            leads.append({
                "email": best,
                "company": names.get(best) or full_name.split("/")[0],
                "tier": "github",
                "source": "github_commits",
                "snippet": f"maintainer of the {full_name} ruby/rails repo on github",
            })
        except Exception:
            continue
    log(f"  🐙 GitHub filler: {len(leads)} maintainer leads")
    return leads


def find_leads(github_token: str) -> list:
    leads, manual = find_hn_leads()
    if manual:
        save_manual_leads(manual)
        log(f"  📝 Wrote {len(manual)} manual LinkedIn leads -> manual_leads.json")
    if USE_GITHUB_FILLER and len(leads) < MAX_NEW_PER_RUN:
        # Dedup across sources — the same person can surface in an HN post and
        # as a repo maintainer, and emailing them twice looks like spam.
        have = {l["email"] for l in leads}
        for gl in find_github_leads(github_token, MAX_NEW_PER_RUN - len(leads)):
            if gl["email"] not in have:
                have.add(gl["email"])
                leads.append(gl)
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

def run_new_outreach(sent_log, github_token, current_time) -> None:
    log("🔍 Phase 2: New outreach — companies hiring FastAPI/Python while on Rails...")
    suppressed = load_suppression()
    leads = find_leads(github_token)
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
                    "source": lead.get("source", "hn_whoishiring"),
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
    github_token = os.environ.get("GITHUB_TOKEN", "")

    sent_log = load_json_list(LOG_FILE)
    current_time = datetime.now(timezone.utc)

    if DRY_RUN:
        log("🧪 DRY RUN — no emails will be sent, no state will be written.")

    try:
        run_followups(sent_log, gmail_user, gmail_pass, current_time)
        run_new_outreach(sent_log, github_token, current_time)
    except DailyLimitReached as e:
        log("🛑 Gmail daily sending limit hit — stopping this run to protect the "
            "account. It resets in ~24h; the next scheduled run will continue where "
            f"we left off. (Gmail said: {e})")

    log("─" * 50)
    log("🏁 Cycle complete.")


if __name__ == "__main__":
    main()
