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

# ── Sending: Amazon SES ──────────────────────────────────────────────────
# We send through Amazon SES from your DKIM/SPF/DMARC-authenticated domain, not
# Gmail (Gmail throttles/blocks cold mail). boto3 reuses the AWS keys already in
# the environment, so no SMTP credentials are needed. Replies go to REPLY_TO so
# you actually see them.
EMAIL_PROVIDER = env("EMAIL_PROVIDER", "ses").lower()   # "ses" or "gmail"
SES_REGION = env("SES_REGION", "us-east-1")
FROM_NAME = env("FROM_NAME", "Hassan Nadeem")
FROM_EMAIL = env("FROM_EMAIL", "hello@hassandevs.online")  # must be on the verified domain
# Replies land here. Defaults to your Gmail so the follow-up reply-detector
# (which reads that Gmail over IMAP) keeps working — keep them the same inbox.
REPLY_TO = env("REPLY_TO", "") or os.environ.get("GMAIL_ADDRESS", "")

# ── Warmup + automatic ramp ──────────────────────────────────────────────
# A brand-new sending domain must ramp slowly or it torches its own reputation.
# The daily cap grows automatically by days since WARMUP_START_DATE, so you
# never touch it. Days 0..PRIME_DAYS-1 = "prime": send only to seed inboxes YOU
# control (open + reply + mark-not-spam) to earn early positive signals. After
# that, real cold sends, ramping.
WARMUP_START_DATE = env("WARMUP_START_DATE", "")   # "YYYY-MM-DD" — set when you go live
# 14 days of pure warmup (send only to seed inboxes you engage with) before any
# real cold sends. A brand-new domain needs this — especially after we saw Gmail
# silently drop the first email. Real outreach starts on day 14.
PRIME_DAYS = int(env("PRIME_DAYS", "14"))
# Inboxes you control, comma-separated. Defaults to your Gmail; add more (a
# second Gmail, Outlook, a friend's) as a repo variable for a stronger warmup.
_seed_raw = env("SEED_EMAILS", "") or os.environ.get("GMAIL_ADDRESS", "")
SEED_EMAILS = [e.strip() for e in _seed_raw.split(",") if e.strip()]

# (day_number, emails_per_day) — cap for a given day is the last row whose
# day_number <= days elapsed. Gentle, deliverability-safe ramp.
RAMP_SCHEDULE = [(0, 3), (3, 5), (7, 8), (11, 11), (15, 14), (21, 18), (28, 22)]

# Warmup is only meant to prove the domain sends wanted mail — a couple of
# messages a day is plenty. Anything more just floods the seed inboxes (and made
# the earlier bounce storm much worse), so it's capped independently of the
# cold-outreach ramp above.
WARMUP_PER_DAY = int(env("WARMUP_PER_DAY", "2"))

MAX_FOLLOWUPS_PER_RUN = int(env("MAX_FOLLOWUPS_PER_RUN", "5"))


def _days_since_start(now: datetime) -> int:
    if not WARMUP_START_DATE:
        return 0
    try:
        start = datetime.fromisoformat(WARMUP_START_DATE).date()
        return max(0, (now.date() - start).days)
    except Exception:
        return 0


def todays_cap(now: datetime) -> int:
    override = env("MAX_NEW_PER_RUN", "")   # pin a fixed number if you ever want to
    if override:
        return int(override)
    days = _days_since_start(now)
    cap = RAMP_SCHEDULE[0][1]
    for day, c in RAMP_SCHEDULE:
        if days >= day:
            cap = c
    return cap


def in_prime_phase(now: datetime) -> bool:
    return _days_since_start(now) < PRIME_DAYS


# GitHub filler leads: Rails maintainers. Lower intent than the HN companies,
# but they keep the daily quota full while HN only yields a handful a month.
SEARCH_QUERY = env("SEARCH_QUERY", "rails language:Ruby stars:50..300")
USE_GITHUB_FILLER = env("USE_GITHUB_FILLER", "true").lower() in ("1", "true", "yes")

# Dry run: find leads and generate the real emails, but send nothing. Lets us
# test the whole pipeline without sending a single email.
DRY_RUN = env("DRY_RUN", "false").lower() in ("1", "true", "yes")

# CAN-SPAM: a real physical mailing address is legally required in every
# commercial email. Set SENDER_ADDRESS as a repo variable/secret.
SENDER_NAME = env("SENDER_NAME", "Hassan")
SENDER_ADDRESS = env("SENDER_ADDRESS", "")

# Your proof-of-work. The single most persuasive thing in the email.
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


# Scraping free text throws up two kinds of false positive: fragments of URLs
# that look like addresses ("architect@heroku.https"), and placeholders people
# type as examples ("username@gmail.com"). Both waste sends and look careless.
REAL_TLDS = {
    "com", "net", "org", "io", "dev", "co", "me", "ai", "app", "xyz", "tech",
    "edu", "gov", "info", "biz", "online", "site", "sh", "ly", "cc", "tv", "fm",
    "gg", "pro", "cloud", "space", "email", "systems", "digital", "software",
    "uk", "de", "fr", "ca", "au", "us", "in", "nl", "se", "no", "fi", "dk",
    "es", "it", "ch", "at", "be", "pl", "cz", "ru", "br", "jp", "cn", "kr",
    "sg", "hk", "nz", "za", "ie", "pt", "gr", "il", "mx", "ar", "cl", "eu",
    "is", "lt", "lv", "ee", "ro", "hu", "bg", "hr", "si", "sk", "ua", "tr",
}

PLACEHOLDER_LOCALS = {
    "username", "user", "name", "yourname", "your", "email", "youremail",
    "someone", "somebody", "example", "test", "foo", "bar", "me@example",
    "firstname", "lastname", "address", "more",
}

# Profile/social links, not mailboxes.
NON_CONTACT_DOMAINS = {
    "twitter.com", "x.com", "github.com", "linkedin.com", "facebook.com",
    "instagram.com", "youtube.com", "reddit.com", "medium.com", "t.co",
    "news.ycombinator.com", "ycombinator.com", "mastodon.social", "bsky.app",
}


def is_valid_lead_email(email: str) -> bool:
    if not email or email.count("@") != 1:
        return False
    low = email.lower().strip().strip(".,;:)")
    if low.startswith(SKIP_PREFIXES):
        return False
    if any(marker in low for marker in JUNK_EMAIL_MARKERS):
        return False

    local, _, domain = low.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if local in PLACEHOLDER_LOCALS:
        return False
    if domain in NON_CONTACT_DOMAINS:
        return False
    # A bare "heroku.https" or "enveritas.also" is a mangled URL, not a domain.
    if domain.rsplit(".", 1)[-1] not in REAL_TLDS:
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


def find_github_leads(github_token: str, needed: int, exclude: set | None = None) -> list:
    """Filler: real maintainers of active Rails repos, via the commits API.

    Lower intent than a company that's hiring, but there are plenty of them —
    they keep the daily quota full so the machine isn't idle between HN threads.
    """
    if needed <= 0:
        return []
    if not github_token:
        log("  ⚠️ GitHub filler skipped — GITHUB_TOKEN/GH_TOKEN is empty.")
        return []
    headers = {"Authorization": f"token {github_token}",
               "Accept": "application/vnd.github.v3+json"}
    leads, seen = [], set(exclude or ())
    try:
        resp = requests.get(
            f"{GITHUB_API}/search/repositories", headers=headers, timeout=20,
            params={"q": SEARCH_QUERY, "sort": "updated",
                    "order": "desc", "per_page": REPO_SCAN_LIMIT},
        )
        if resp.status_code != 200:
            log(f"  ⚠️ GitHub search returned {resp.status_code}")
            return []
        items = [i for i in resp.json().get("items", []) if i.get("full_name")]
        # Company-owned repos first. A maintainer at an Organization is far more
        # likely to have a budget and an actual migration problem than a solo
        # hobbyist, and hobbyists were most of what this filler was returning.
        orgs = [i["full_name"] for i in items
                if ((i.get("owner") or {}).get("type") == "Organization")]
        users = [i["full_name"] for i in items
                 if ((i.get("owner") or {}).get("type") != "Organization")]
        repos = orgs + users
        log(f"  🏢 GitHub repos: {len(orgs)} org-owned prioritised, {len(users)} personal")
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


# HN users routinely obfuscate the address in their profile ("me at foo dot com")
# to dodge scrapers, so undo the common spellings before matching.
_OBFUSCATED = re.compile(
    r"([a-zA-Z0-9._%+-]+)\s*(?:@|\[at\]|\(at\)|\s+at\s+)\s*"
    r"([a-zA-Z0-9.-]+)\s*(?:\.|\[dot\]|\(dot\)|\s+dot\s+)\s*([a-zA-Z]{2,})",
    re.IGNORECASE,
)

_hn_profile_cache: dict = {}


def hn_profile_email(username: str) -> str | None:
    """Pull a contact address off a Hacker News profile.

    The Algolia search index gives us the commenter's username but no contact
    details. HN's own public API exposes each user's "about" text, and a good
    number of developers list an email there. This is what makes a comment about
    Rails pain actually actionable instead of just interesting.
    """
    if not username:
        return None
    if username in _hn_profile_cache:
        return _hn_profile_cache[username]

    email = None
    try:
        r = requests.get(
            f"https://hacker-news.firebaseio.com/v0/user/{username}.json", timeout=15
        )
        if r.status_code == 200 and r.json():
            about = html.unescape(strip_html(r.json().get("about") or ""))
            direct = extract_email(about)
            if direct:
                email = direct
            else:
                m = _OBFUSCATED.search(about)
                if m:
                    candidate = f"{m.group(1)}@{m.group(2)}.{m.group(3)}".lower()
                    if is_valid_lead_email(candidate):
                        email = candidate
    except Exception:
        email = None

    _hn_profile_cache[username] = email
    return email


def find_hn_search_leads(exclude: set, limit: int = 40) -> list:
    """Mine HN beyond the monthly hiring thread.

    The "Who is hiring" thread only refreshes once a month and we exhaust it in
    days. HN's Algolia index is free and covers every comment ever posted, so we
    search for people actively describing Rails pain or a Python move. Someone
    writing about their own migration is a warmer lead than a job ad.
    """
    queries = [
        "rails to python migration",
        "migrating off rails",
        "rails to fastapi",
        "replacing rails backend",
        "rails performance problems scaling",
        "rewriting our rails app",
        "moving away from ruby on rails",
        "rails monolith slow",
        "rails hiring python engineers",
        "legacy rails codebase",
        "rails technical debt rewrite",
        "porting rails to python",
    ]
    leads = []
    for q in queries:
        if len(leads) >= limit:
            break
        try:
            resp = requests.get(
                f"{HN_API}/search",
                params={"query": q, "tags": "comment", "hitsPerPage": 60},
                timeout=25,
            )
            if resp.status_code != 200:
                continue
            hits = resp.json().get("hits", [])
        except Exception as e:
            log(f"  ⚠️ HN search '{q}' failed: {e}")
            continue

        for h in hits:
            if len(leads) >= limit:
                break
            text = strip_html(h.get("comment_text") or "")
            if not qualify(text):
                continue
            author = h.get("author") or ""
            # Prefer an address in the comment itself; otherwise fall back to the
            # commenter's HN profile, which is where most people actually keep it.
            email = extract_email(text) or hn_profile_email(author)
            if not email or email in exclude:
                continue
            exclude.add(email)
            leads.append({
                "email": email,
                "company": author[:80] or "there",
                "tier": "strong" if any(s in text.lower() for s in STRONG_SIGNALS) else "ok",
                "source": "hn_search",
                "hn_user": author,
                "snippet": " ".join(text.split())[:700],
            })
    log(f"  🔎 HN search: {len(leads)} leads")
    return leads


def find_remoteok_leads(exclude: set) -> list:
    """RemoteOK publishes its whole board as free JSON — no key, no scraping.

    Companies hiring remote Python/backend roles that also mention Ruby/Rails
    are the same migration-intent signal we look for on HN, and the board
    refreshes constantly, so it doesn't dry up between monthly HN threads.
    """
    leads = []
    try:
        resp = requests.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "Mozilla/5.0 (compatible; railscout/1.0)"},
            timeout=25,
        )
        if resp.status_code != 200:
            log(f"  ⚠️ RemoteOK returned {resp.status_code}")
            return []
        rows = resp.json()
    except Exception as e:
        log(f"  ⚠️ RemoteOK fetch failed: {e}")
        return []

    for row in rows:
        if not isinstance(row, dict) or not row.get("company"):
            continue
        blob = " ".join(str(row.get(k, "")) for k in
                        ("position", "description", "tags", "company")).lower()
        if not qualify(blob):
            continue
        email = extract_email(blob) or extract_email(str(row.get("apply_url", "")))
        if not email or email in exclude:
            continue
        exclude.add(email)
        leads.append({
            "email": email,
            "company": str(row.get("company"))[:80],
            "tier": "strong" if any(s in blob for s in STRONG_SIGNALS) else "ok",
            "source": "remoteok",
            "snippet": f"{row.get('position','')} — {str(row.get('description',''))[:500]}".strip(),
        })
    log(f"  🌐 RemoteOK: {len(leads)} leads")
    return leads


def find_leads(github_token: str, cap: int, already: set) -> list:
    """`already` = emails in sent_log + suppression list.

    Dedup has to happen *here*, before we decide whether to top up from other
    sources. Counting raw HN hits meant 22 leads looked like plenty against a
    cap of 14 — even when 19 were already-contacted duplicates and only 3 were
    actually sendable, which silently starved the run.
    """
    leads, manual = find_hn_leads()
    if manual:
        save_manual_leads(manual)
        log(f"  📝 Wrote {len(manual)} manual LinkedIn leads -> manual_leads.json")

    seen = set(already)
    fresh = []
    for l in leads:
        if l["email"] not in seen:
            seen.add(l["email"])
            fresh.append(l)
    log(f"  ♻️ HN fresh after dedup: {len(fresh)} (of {len(leads)} found)")

    # Order matters: people describing their own Rails pain convert far better
    # than a repo maintainer who never asked for anything.
    if len(fresh) < cap:
        fresh += find_hn_search_leads(seen)

    if len(fresh) < cap:
        fresh += find_remoteok_leads(seen)

    if USE_GITHUB_FILLER and len(fresh) < cap:
        fresh += find_github_leads(github_token, cap - len(fresh), seen)

    return fresh


# ── AI Generation ─────────────────────────────────────────────────────────

def de_ai(text: str) -> str:
    """Strip the punctuation and vocabulary that make text read as machine-written."""
    out = text
    # Em/en dash used as a clause break becomes a comma or a full stop.
    out = re.sub(r"\s*[—–]\s*", ", ", out)
    out = out.replace("->", "to").replace("=>", "to").replace("→", "to")
    out = out.replace(";", ".")
    for word, plain in (
        ("leverage", "use"), ("streamline", "simplify"), ("robust", "solid"),
        ("seamless", "clean"), ("utilize", "use"), ("reach out", "get in touch"),
        ("circle back", "follow up"), ("cutting-edge", "modern"),
    ):
        out = re.sub(word, plain, out, flags=re.IGNORECASE)
    out = re.sub(r",\s*,", ",", out)
    out = re.sub(r"\s+([.,])", r"\1", out)
    return out.strip()


def generate_initial_email(lead: dict) -> dict | None:
    # The prompt has to describe where this lead ACTUALLY came from. It used to
    # say "posted a job on Hacker News" for every lead, so GitHub-sourced leads
    # got emails opening with "saw your post on hn" about a post that never
    # existed — an instant credibility kill if the recipient notices.
    if lead.get("source") == "hn_search":
        # These are the best leads we get: the person literally wrote about the
        # problem. The email should prove it was read, not pattern-matched.
        context_line = (
            "Write a short, developer-to-developer email to someone who posted a "
            "comment on Hacker News about their own backend stack or migration."
        )
        context_label = "The comment they actually wrote"
        specific_rule = (
            "3. Open by referring to the SPECIFIC point they made in their comment, "
            "in your own words, so it is obvious you read it. Quote at most a few "
            "words. Do not say 'I saw your post' generically, and do not claim they "
            "are hiring or that you know their company.\n"
            "3b. The name given below is only their Hacker News username. Never "
            "treat it as a company name and never write phrases like 'for <name>'. "
            "Address them as a person or not at all."
        )
    elif lead.get("source") == "github_commits":
        context_line = (
            "Write a short, developer-to-developer email to the maintainer of an "
            "open-source Ruby/Rails project you found on GitHub."
        )
        context_label = "What I know about them (from GitHub)"
        specific_rule = (
            "3. Open by referencing their actual repository by name. They are an "
            "open-source maintainer, NOT a company that posted a job. Never claim "
            "you saw a job post, a Hacker News post, or that they are hiring — "
            "none of that is true here and inventing it destroys credibility."
        )
    else:
        context_line = (
            "Write a short, developer-to-developer email to a company that just "
            "posted a job on Hacker News."
        )
        context_label = "Their job post (excerpt)"
        specific_rule = (
            "3. Open by referencing something SPECIFIC and real from their job post "
            "(the role, the stack, or what they're building). Do not invent facts "
            "that aren't in the post."
        )

    prompt = f"""{context_line}

{context_label}:
\"\"\"{lead['snippet']}\"\"\"

Company / project: {lead['company']}

About me (the sender):
- I recently ported ~6,000 lines of Ruby on Rails (Fat Free CRM) to FastAPI, end to end.
- Architecture and code: {PROOF_URL}

STRICT RULES:
1. Tone: informal, technical, direct — like messaging another engineer. NO sales vocabulary, no "hope you're well", no "I came across your company and was impressed".
2. Length: under 80 words.
{specific_rule}
4. Make the connection to a possible Rails -> FastAPI/Python migration, and that I've already done exactly that one. Do not assert as fact that they are migrating, hiring, or have a problem — frame it as "if/when", since I don't actually know.
5. Include this link exactly once: {PROOF_URL}
6. CTA: low-pressure. Offer to help de-risk the migration or take a piece of it off their plate. Ask a simple question, don't demand a call.
7. Formatting: entirely lowercase, simple line breaks.
8. Subject: short, lowercase, plain words only. No arrows, no colons stacking, no clever punctuation.
9. WRITE LIKE A TIRED HUMAN TYPING QUICKLY, NOT LIKE AN ASSISTANT. Specifically banned:
   - em dashes and en dashes (do not use the characters "—" or "–"). Use a comma, a full stop, or start a new sentence.
   - arrows of any kind ("->", "=>", the arrow character)
   - semicolons, and any sentence with more than one comma
   - the words: leverage, streamline, robust, seamless, dive in, reach out, circle back, elevate, unlock, empower, cutting-edge, game-changer, delighted, excited to, hope this finds you
   - rhetorical setups like "the thing is," "here's the kicker," "let's be honest"
   - three-part lists (a, b, and c). Say one thing.
10. Short sentences. Plain words. It should read like a message from one dev to another, not marketing copy.

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
    content = json.loads(text[start:end])
    # Belt and braces: the model still slips an em dash or an arrow in now and
    # then, and those are the tells that make a cold email look generated.
    for field in ("subject", "body"):
        if isinstance(content.get(field), str):
            content[field] = de_ai(content[field])
    return content


# ── SMTP Sending ──────────────────────────────────────────────────────────

class DailyLimitReached(Exception):
    """Raised when the email provider refuses further sends for the day/period."""


def build_footer() -> str:
    # Plain "thanks," sign-off. The old em-dash separator was one of the louder
    # tells that a message was machine-written.
    lines = ["\n\nthanks,", SENDER_NAME,
             '', 'not relevant? reply "unsubscribe" and i\'ll stop emailing.']
    if SENDER_ADDRESS:
        lines.append(SENDER_ADDRESS)
    return "\n".join(lines)


def _send_via_ses(to_email: str, subject: str, body: str) -> bool:
    client = boto3.client(
        "ses", region_name=SES_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    # Build a full MIME message so we can include a plain+HTML alternative AND a
    # List-Unsubscribe header — both improve deliverability and are increasingly
    # required by Gmail/Outlook. (send_email can't set custom headers.)
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{FROM_NAME} <{FROM_EMAIL}>"
    msg["To"] = to_email
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg["List-Unsubscribe"] = f"<mailto:{FROM_EMAIL}?subject=unsubscribe>"
    msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"
    # Ties this send to the config set that reports bounce/complaint/delivery
    # events back via SNS -> the reply-dash Worker, so failures are visible
    # (check /api/events) instead of us having to guess why something bounced.
    msg["X-SES-CONFIGURATION-SET"] = "hassandevs-tracked"
    html_body = html.escape(body)
    html_body = re.sub(r"(https?://[^\s<]+)", r'<a href="\1">\1</a>', html_body)
    html_body = ('<html><body style="font-family:Arial,sans-serif;font-size:14px">'
                 + html_body.replace("\n", "<br>") + "</body></html>")
    msg.attach(MIMEText(body, "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        client.send_raw_email(RawMessage={"Data": msg.as_string()})
        return True
    except Exception as e:
        text = str(e).lower()
        # Hit the SES sending quota/rate — stop the run cleanly, resume tomorrow.
        if ("throttl" in text or "maximum sending rate" in text
                or "daily message quota" in text or "sending quota" in text
                or "limitexceeded" in text):
            raise DailyLimitReached(str(e))
        if "not verified" in text or "messagerejected" in text:
            log(f"  ⏭️ SES rejected (recipient not verified): {to_email}")
            return False
        log(f"  ❌ SES error: {e}")
        return False


def _send_via_gmail(to_email: str, subject: str, body: str) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email
    if REPLY_TO:
        msg["Reply-To"] = REPLY_TO
    msg.attach(MIMEText(body, "plain"))
    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=20) as server:
            server.starttls()
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, to_email, msg.as_string())
        return True
    except Exception as e:
        text = str(e).lower()
        if "5.4.5" in text or "sending limit exceeded" in text or "5.7.1" in text:
            raise DailyLimitReached(str(e))
        log(f"  ❌ SMTP error: {e}")
        return False


def send_email(to_email: str, subject: str, body: str, add_footer: bool = True) -> bool:
    if add_footer:
        body = body.rstrip() + build_footer()

    if DRY_RUN:
        log(f"  🧪 DRY RUN — would send to {to_email}")
        log(f"       subject: {subject}")
        for line in body.strip().split("\n"):
            log(f"       | {line}")
        return True

    if EMAIL_PROVIDER == "ses":
        return _send_via_ses(to_email, subject, body)
    return _send_via_gmail(to_email, subject, body)


# ── Follow-up sequence ────────────────────────────────────────────────────

FOLLOWUP_1_BODY = (
    "hey,\n\njust bumping this in case it got buried. if moving any of it to "
    "python is something you're thinking about, happy to help with a piece of "
    "it. if not, no worries at all.\n\nbest,\nhassan"
)

# Final "breakup" email — this consistently pulls the most replies of the whole
# sequence, because it's easy to say "actually, wait" when someone's walking away.
FOLLOWUP_2_BODY = (
    "hey,\n\nlast note from me on this. sounds like it's not a priority right "
    "now, which is fair. if that changes, my rails to fastapi work is here: "
    f"{PROOF_URL}\n\ngood luck with it,\nhassan"
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

def run_new_outreach(sent_log, github_token, current_time, cap) -> None:
    log(f"🔍 Phase 2: New outreach — companies hiring FastAPI/Python on Rails (cap {cap})...")
    suppressed = load_suppression()
    already = set(suppressed) | {
        (e.get("email") or "").lower() for e in sent_log if e.get("email")
    }
    leads = find_leads(github_token, cap, already)
    if not leads:
        log("  No qualifying leads found this run.")
        return

    sent = 0
    gen_failures = 0  # consecutive Bedrock failures -> likely misconfig

    for lead in leads:
        if sent >= cap:
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
            log(f"  ✅ Sent ({sent}/{cap})")
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


# ── Warmup (prime phase) ──────────────────────────────────────────────────

WARMUP_TEMPLATES = [
    ("setting up my inbox", "hey — getting my new work inbox warmed up. mind hitting reply with a quick 'got it'? appreciate it."),
    ("quick deliverability test", "hi! just making sure mail from this address lands fine. a one-word reply helps a ton — thanks."),
    ("hello from hassan", "hey, testing my domain's deliverability. could you reply 'yep' so i know it arrived? cheers."),
    ("warmup ping", "hi — priming this inbox for outreach. a quick reply (anything works) helps build reputation. thanks!"),
    ("mail check", "hey! confirming this inbox sends cleanly. drop me a one-liner back when you see this? much appreciated."),
]


def run_warmup(current_time, cap) -> None:
    """Prime phase: send to seed inboxes YOU control. Open + reply + mark
    not-spam on each one — that positive engagement is what actually warms the
    domain. Deliberately tiny volume (WARMUP_PER_DAY), not the cold-outreach ramp."""
    cap = min(cap, WARMUP_PER_DAY)
    log(f"🔥 Phase 2: WARMUP (prime) — seeding {len(SEED_EMAILS)} inbox(es), {cap}/day...")
    if not SEED_EMAILS:
        log("  ⚠️ No SEED_EMAILS set — skipping warmup. Add your own inbox(es) to "
            "the SEED_EMAILS repo variable (comma-separated).")
        return
    sent = 0
    i = 0
    while sent < cap:
        seed = SEED_EMAILS[i % len(SEED_EMAILS)]
        subject, body = random.choice(WARMUP_TEMPLATES)
        log(f"  📤 warmup -> {seed}")
        if send_email(seed, subject, body, add_footer=False):
            sent += 1
            if not DRY_RUN:
                time.sleep(random.randint(60, 180))
        else:
            break
        i += 1
    log(f"  Warmup emails sent: {sent}  (open + reply + 'not spam' on each!)")


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    if not pre_flight_check():
        return

    gmail_user = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    github_token = os.environ.get("GITHUB_TOKEN", "")

    sent_log = load_json_list(LOG_FILE)
    current_time = datetime.now(timezone.utc)

    cap = todays_cap(current_time)
    day = _days_since_start(current_time)
    prime = in_prime_phase(current_time)
    log(f"📮 Provider: {EMAIL_PROVIDER} | from: {FROM_EMAIL} | reply-to: {REPLY_TO or '(none)'}")
    log(f"📈 Warmup day {day} | phase: {'PRIME/warmup' if prime else 'cold outreach'} | today's cap: {cap}")
    if DRY_RUN:
        log("🧪 DRY RUN — no emails will be sent, no state will be written.")

    try:
        if prime:
            # Warmup phase: ONLY send to seed inboxes. No follow-ups, no cold
            # sends — the domain isn't trusted yet, so real recipients would
            # just hurt reputation.
            run_warmup(current_time, cap)
        else:
            run_followups(sent_log, gmail_user, gmail_pass, current_time)
            run_new_outreach(sent_log, github_token, current_time, cap)
    except DailyLimitReached as e:
        log("🛑 Sending limit hit — stopping this run to protect the domain's "
            "reputation. It resets on its own; the next scheduled run continues "
            f"where we left off. (provider said: {e})")

    log("─" * 50)
    log("🏁 Cycle complete.")


if __name__ == "__main__":
    main()
