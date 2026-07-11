import json
import os
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

# Separate budgets so new outreach isn't starved by follow-ups.
MAX_NEW_PER_RUN = 15
MAX_FOLLOWUPS_PER_RUN = 10

FOLLOW_UP_DAYS = 3         # first follow-up: 3 days after the initial email
SECOND_FOLLOW_UP_DAYS = 7  # second/final follow-up: 7 days after the initial email
GITHUB_API = "https://api.github.com"

# How many repos to scan and how many commits to inspect per repo when
# hunting for a real maintainer email.
REPO_SCAN_LIMIT = 40
COMMITS_PER_REPO = 30

# GitHub Actions injects an *unset* secret as an empty string "" (not absent),
# and os.environ.get() only falls back to the default when a key is missing.
# So an empty secret would silently override our defaults (this is exactly what
# broke the search: SEARCH_QUERY came through as "" -> 422). Treat empty as
# missing so a secret you never set just uses the sensible default below.
def env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value.strip() if value and value.strip() else default


# Bedrock model. Override with the BEDROCK_MODEL_ID secret if this ID isn't
# the one enabled in your account/region. `us.` = cross-region inference
# profile; a bare `anthropic.claude-sonnet-4-6` may be what your account needs.
BEDROCK_MODEL_ID = env("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-6")
BEDROCK_REGION = env("BEDROCK_REGION", "us-east-1")

# CAN-SPAM: a real physical mailing address is legally required in every
# commercial email. Set SENDER_ADDRESS / SENDER_NAME as GitHub secrets.
SENDER_NAME = env("SENDER_NAME", "hassan")
SENDER_ADDRESS = env("SENDER_ADDRESS", "")

# GitHub search query. Biased toward Rails so leads are actually relevant to
# the FastAPI-migration pitch, not just "any Ruby repo".
SEARCH_QUERY = env("SEARCH_QUERY", "rails language:Ruby stars:50..300")

# Emails we never contact (generic inboxes + bots).
SKIP_PREFIXES = (
    "support@", "info@", "opensource+", "webmaster@", "contact@",
    "hello@", "help@", "admin@", "noreply@", "no-reply@", "dev@",
    "developer@", "oss@", "crewonslack@", "sales@", "marketing@",
    "project@", "subscribe@", "unsubscribe@", "list@", "lists@",
    "google-groups@", "security@", "abuse@", "team@",
)

# Domains/substrings that mean "not a real human inbox".
JUNK_EMAIL_MARKERS = (
    "users.noreply.github.com", "noreply", "no-reply", "example.com",
    "localhost", "[bot]", "bot@",
)


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ── Pre-Flight Check ──────────────────────────────────────────────────────

def pre_flight_check() -> bool:
    required_env = [
        "GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
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


def save_sent_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))


def load_suppression() -> set[str]:
    # unsubscribe_list.json is a plain JSON array of email strings.
    return {e.strip().lower() for e in load_json_list(SUPPRESS_FILE) if isinstance(e, str)}


def already_contacted(email: str, sent_log: list[dict]) -> bool:
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


# ── GitHub Logic ──────────────────────────────────────────────────────────

def search_github_repos(github_token: str) -> list[dict]:
    """Return a list of {full_name, org_login} for recently-updated repos."""
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    repos: list[dict] = []
    try:
        log(f"🔍 Searching GitHub: {SEARCH_QUERY!r} (sorted by recently updated)...")
        params = {
            "q": SEARCH_QUERY,
            "sort": "updated",
            "order": "desc",
            "per_page": REPO_SCAN_LIMIT,
        }
        resp = requests.get(
            f"{GITHUB_API}/search/repositories",
            headers=headers, params=params, timeout=20,
        )
        if resp.status_code != 200:
            log(f"❌ GitHub search returned {resp.status_code}: {resp.text[:200]}")
            return repos
        for item in resp.json().get("items", []):
            owner = item.get("owner", {}) or {}
            repos.append({
                "full_name": item.get("full_name"),
                "org_login": owner.get("login"),
            })
    except Exception as e:
        log(f"❌ GitHub search failed: {e}")
    return repos


def is_valid_lead_email(email: str) -> bool:
    if not email or "@" not in email:
        return False
    low = email.lower()
    if low.startswith(SKIP_PREFIXES):
        return False
    if any(marker in low for marker in JUNK_EMAIL_MARKERS):
        return False
    return True


def get_maintainer_from_commits(full_name: str, github_token: str) -> dict | None:
    """Pull recent commit authors and return the most frequent real person.

    This is the key fix: instead of the org's public profile email (which is
    almost always empty or a generic inbox), we read actual commit authors from
    git history — real maintainers who'd care about a backend migration.
    """
    if not full_name:
        return None
    headers = {"Authorization": f"token {github_token}"}
    try:
        resp = requests.get(
            f"{GITHUB_API}/repos/{full_name}/commits",
            headers=headers, params={"per_page": COMMITS_PER_REPO}, timeout=20,
        )
        if resp.status_code != 200:
            return None
        # Count valid (email -> name) pairs, weighting by real GitHub accounts.
        tally: Counter = Counter()
        names: dict[str, str] = {}
        for c in resp.json():
            commit = (c.get("commit") or {}).get("author") or {}
            email = (commit.get("email") or "").strip()
            name = (commit.get("name") or "").strip()
            if not is_valid_lead_email(email):
                continue
            if "bot" in name.lower():
                continue
            weight = 2 if c.get("author") else 1  # linked GitHub account = real person
            tally[email.lower()] += weight
            names.setdefault(email.lower(), name)
        if not tally:
            return None
        best_email = tally.most_common(1)[0][0]
        return {"email": best_email, "name": names.get(best_email, "")}
    except Exception:
        return None


# ── AI Generation ─────────────────────────────────────────────────────────

def generate_initial_email(lead: dict) -> dict | None:
    greeting_name = lead.get("name") or "team"
    first_name = greeting_name.split()[0].lower() if greeting_name != "team" else "team"

    prompt = f"""Write an extremely short, developer-to-developer cold email.

Context:
Maintainer: {greeting_name}
Repo: {lead['repo_name']}

STRICT RULES:
1. Tone: Informal, technical, fast. Like a Slack message to another engineer. No sales vocabulary.
2. Length: Under 60 words. No "hope you are well" or boilerplate intros.
3. Opening: "hey {first_name}," followed by "came across your {lead['repo_name']} codebase."
4. Core value: If their rails backend is getting heavy or server costs are creeping up, moving critical endpoints to fastapi drops latency and slashes compute costs.
5. Technical proof, state exactly: "i recently ported 6000 lines of ruby (fat free crm) to fastapi. you can check out the architecture here: https://github.com/HassanNadeem1122/fat-free-crm-fastapi"
6. CTA: casual, low-pressure: "is optimization or cutting server bills on your radar this quarter?"
7. Formatting: entirely lowercase, casual punctuation, simple line breaks.
8. Subject: "fastapi migration / {lead['repo_name'].split('/')[-1]}"

Output ONLY raw JSON starting with {{ and ending with }}: {{"subject": "...", "body": "..."}}
"""
    client = boto3.client(
        "bedrock-runtime", region_name=BEDROCK_REGION,
        aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
    )
    response = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps({
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": 400,
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
        log(f"  ❌ SMTP error: {e}")
        return False


# ── Main Execution Loop ───────────────────────────────────────────────────

FOLLOWUP_1_BODY = (
    "hey,\n\njust floating this to the top of your inbox. let me know "
    "if migrating the backend is a priority right now, otherwise i'll "
    "stop bugging you.\n\nbest,\nhassan"
)

# Final "breakup" email — this consistently pulls the most replies of the whole
# sequence, because it's easy to say "actually, wait" when someone's walking away.
FOLLOWUP_2_BODY = (
    "hey,\n\nlast time i'll reach out — i'll assume backend performance isn't a "
    "priority right now and close this out. if that changes down the line, my "
    "fastapi migration work is here: "
    "https://github.com/HassanNadeem1122/fat-free-crm-fastapi\n\ncheers,\nhassan"
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

        log(f"  📤 Follow-up #{stage} (day {days}) to {entry.get('org') or entry['email']}...")
        bump_subject = f"Re: {entry['subject']}"
        if send_email(entry["email"], bump_subject, body):
            entry[stamp] = current_time.isoformat()
            save_sent_log(sent_log)
            sent += 1
            time.sleep(random.randint(60, 120))
    log(f"  Follow-ups sent this run: {sent}")


def run_new_outreach(sent_log, github_token, current_time) -> None:
    log("🔍 Phase 2: New outreach...")
    suppressed = load_suppression()
    repos = search_github_repos(github_token)
    if not repos:
        log("  No repos returned from search.")
        return

    sent = 0
    gen_failures = 0        # consecutive Bedrock failures -> likely misconfig
    emailed_this_run: set[str] = set()

    for repo in repos:
        if sent >= MAX_NEW_PER_RUN:
            break

        lead = get_maintainer_from_commits(repo["full_name"], github_token)
        if not lead:
            continue
        email = lead["email"].lower()

        if (email in emailed_this_run or email in suppressed
                or already_contacted(email, sent_log)):
            continue

        lead["repo_name"] = repo["full_name"]
        log(f"  ✉️ Lead: {lead.get('name') or '(no name)'} <{email}> — {repo['full_name']}")

        try:
            content = generate_initial_email(lead)
            gen_failures = 0
        except Exception as e:
            gen_failures += 1
            log(f"  ❌ Bedrock generation error ({gen_failures}): {e}")
            if gen_failures >= 3:
                log("  🛑 3 generation failures in a row — almost certainly a bad "
                    "BEDROCK_MODEL_ID or the model isn't enabled in this region. "
                    "Aborting so we don't burn through leads. Fix the model config.")
                return
            continue

        if not content:
            continue

        log(f'  📤 Sending: "{content["subject"]}"')
        if send_email(email, content["subject"], content["body"]):
            emailed_this_run.add(email)
            sent += 1
            sent_log.append({
                "org": repo.get("org_login"),
                "repo": repo["full_name"],
                "email": email,
                "name": lead.get("name"),
                "subject": content["subject"],
                "initial_sent_at": current_time.isoformat(),
                "follow_up_sent_at": None,
                "replied": False,
            })
            save_sent_log(sent_log)
            log(f"  ✅ Sent ({sent}/{MAX_NEW_PER_RUN})")
            time.sleep(random.randint(120, 300))

    log(f"  New emails sent this run: {sent}")


def main():
    if not pre_flight_check():
        return

    github_token = os.environ.get("GITHUB_TOKEN")
    gmail_user = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")

    sent_log = load_json_list(LOG_FILE)
    current_time = datetime.now(timezone.utc)

    run_followups(sent_log, gmail_user, gmail_pass, current_time)
    run_new_outreach(sent_log, github_token, current_time)

    log("─" * 50)
    log("🏁 Cycle complete.")


if __name__ == "__main__":
    main()
