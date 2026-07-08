"""
RailScout — Production Grade Outbound Engine
Features: Bedrock AI Generation, SMTP Sending, IMAP Reply Tracking, 3-Day Follow-ups.
No omitted code blocks. Fully production ready.
"""

import json
import os
import smtplib
import imaplib
import time
import random
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path
import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "sent_log.json"
MAX_ACTIONS_PER_RUN = 10  # Max combination of new emails + follow-ups per day
FOLLOW_UP_DAYS = 3
STALE_DAYS = 365
GITHUB_API = "https://api.github.com"
BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = "us-east-1"

SKIP_PREFIXES = (
    "support@", "info@", "opensource+", "webmaster@", "contact@",
    "hello@", "help@", "admin@", "noreply@", "no-reply@", "dev@",
    "developer@", "oss@", "crewonslack@", "sales@", "marketing@"
)

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ── State Management ──────────────────────────────────────────────────────

def load_sent_log() -> list[dict]:
    if LOG_FILE.exists():
        try:
            return json.loads(LOG_FILE.read_text())
        except Exception:
            return []
    return []

def save_sent_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))

def already_emailed(org_login: str, sent_log: list[dict]) -> bool:
    return any(e["org"] == org_login for e in sent_log)

# ── Inbox Sync (IMAP) ─────────────────────────────────────────────────────

def check_if_replied(to_email: str, gmail_user: str, gmail_pass: str) -> bool:
    """Logs into Gmail via IMAP to see if we received an email from this address."""
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        mail.select('inbox')
        status, messages = mail.search(None, f'FROM "{to_email}"')
        mail.logout()
        
        if messages[0].split():
            return True
        return False
    except Exception as e:
        log(f"  ⚠️ IMAP sync failed for {to_email}: {e}")
        return False

# ── GitHub Targeting Logic ────────────────────────────────────────────────

def search_github_orgs(github_token: str) -> dict[str, dict]:
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    orgs: dict[str, dict] = {}
    cutoff = datetime.now(timezone.utc)

    log("🔍 Searching GitHub for Ruby on Rails organizations...")
    for page in range(1, 8):
        try:
            resp = requests.get(
                f"{GITHUB_API}/search/repositories",
                headers=headers,
                params={
                    "q": "language:Ruby stars:100..5000 archived:false",
                    "sort": "stars",
                    "per_page": 30,
                    "page": page,
                },
                timeout=15,
            )
            if resp.status_code == 403:
                log("⚠️  Rate limited, waiting 60s...")
                time.sleep(60)
                continue
            if resp.status_code != 200:
                log(f"❌ GitHub error {resp.status_code}: {resp.text[:200]}")
                break

            items = resp.json().get("items", [])
            if not items:
                break

            for item in items:
                owner = item.get("owner", {})
                if owner.get("type") != "Organization":
                    continue
                login = owner.get("login", "")
                if not login or login in orgs:
                    continue

                pushed_at = item.get("pushed_at")
                is_stale = False
                if pushed_at:
                    pushed_dt = datetime.strptime(pushed_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                        tzinfo=timezone.utc
                    )
                    is_stale = (cutoff - pushed_dt).days >= STALE_DAYS

                orgs[login] = {
                    "login": login,
                    "repo_name": item.get("full_name", ""),
                    "repo_url": item.get("html_url", ""),
                    "stars": item.get("stargazers_count", 0),
                    "description": item.get("description") or "",
                    "pushed_at": pushed_at,
                    "is_stale": is_stale,
                }
            time.sleep(1)
        except Exception as e:
            log(f"❌ Search error: {e}")
            break
    return orgs

def get_org_email(org_login: str, github_token: str) -> str | None:
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = requests.get(f"{GITHUB_API}/orgs/{org_login}", headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        email = (resp.json().get("email") or "").strip()
        
        if not email or "@" not in email:
            return None
        if email.lower().startswith(SKIP_PREFIXES) or "+" in email:
            return None
            
        return email
    except Exception:
        return None

# ── Generation & Sending ──────────────────────────────────────────────────

def generate_initial_email(org_data: dict) -> dict | None:
    prompt = f"""Write a highly personalized, casual cold email to a software engineering team.

Context:
Org: {org_data['login']}
Repo: {org_data['repo_name']} ({org_data['stars']} stars)
Description: {org_data['description'] or 'N/A'}
Status: {"Legacy/No recent updates" if org_data['is_stale'] else "Actively maintained"}

Rules for a high-converting B2B pitch:
- Tone: Peer-to-peer. Direct, relaxed, and concise. No marketing jargon. 
- Opening: Mention coming across their specific repo organically.
- The Hook: Acknowledge that running Rails at scale (or maintaining legacy Rails) can become a bottleneck for compute costs or dev speed.
- The Pitch: You specialize in migrating Rails backends to lightweight Python architectures (FastAPI) to cut server costs and modernize the stack.
- The Ask: "Is migrating off Rails something on your radar right now?"
- Sign off as "Hassan".
- Include a plain text opt-out at the very bottom: "Reply 'no thanks' to opt out."
- Format: Return ONLY raw JSON in this format: {{"subject": "...", "body": "..."}}. 
- Subject line must be lowercase, 3-5 words, reading like an internal team ping.

Do not include markdown blocks or any other text. Just the JSON."""

    try:
        client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 400,
                "messages": [{"role": "user", "content": prompt}],
            }),
        )
        text = json.loads(response["body"].read())["content"][0]["text"].strip()
        if text.startswith("```"):
            text = text.strip("`").replace("json\n", "", 1)
        
        return json.loads(text)
    except Exception as e:
        log(f"  ❌ Generation error: {e}")
        return None

def send_email(to_email: str, subject: str, body: str) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email
    msg.attach(MIMEText(body, "plain"))

    try:
        with smtplib.SMTP("smtp.gmail.com", 587, timeout=15) as server:
            server.starttls()
            server.login(gmail_address, gmail_password)
            server.sendmail(gmail_address, to_email, msg.as_string())
        return True
    except Exception as e:
        log(f"  ❌ SMTP error: {e}")
        return False

# ── Core Engine Lifecycle ─────────────────────────────────────────────────

def main():
    github_token = os.environ.get("GITHUB_TOKEN", "")
    gmail_user = os.environ.get("GMAIL_ADDRESS", "")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD", "")
    
    if not all([github_token, gmail_user, gmail_pass]):
        log("❌ Missing core environment variables. Exiting.")
        return

    sent_log = load_sent_log()
    current_time = datetime.now(timezone.utc)
    actions_taken = 0

    log("🔄 Phase 1: Checking for Follow-ups and Replies...")
    for entry in sent_log:
        if actions_taken >= MAX_ACTIONS_PER_RUN:
            break
            
        if entry.get("replied") or entry.get("follow_up_sent_at"):
            continue

        initial_date = datetime.fromisoformat(entry["initial_sent_at"])
        days_since_initial = (current_time - initial_date).days

        if days_since_initial >= FOLLOW_UP_DAYS:
            log(f"  🔍 Checking inbox for replies from {entry['email']}...")
            if check_if_replied(entry['email'], gmail_user, gmail_pass):
                log(f"  ✅ Prospect replied! Updating state.")
                entry["replied"] = True
                save_sent_log(sent_log)
                continue
            
            log(f"  📤 Sending 3-day follow-up to {entry['org']}...")
            bump_subject = f"Re: {entry['subject']}"
            bump_body = (
                f"Hey,\n\n"
                f"Just floating this to the top of your inbox. Let me know if migrating the {entry['repo']} backend is a priority right now, otherwise I'll stop bugging you.\n\n"
                f"Best,\nHassan"
            )
            
            if send_email(entry["email"], bump_subject, bump_body):
                entry["follow_up_sent_at"] = current_time.isoformat()
                save_sent_log(sent_log)
                actions_taken += 1
                delay = random.randint(120, 300)
                log(f"  ⏱  waiting {delay}s...")
                time.sleep(delay)

    if actions_taken >= MAX_ACTIONS_PER_RUN:
        log("🏁 Action limit reached during follow-ups. Exiting.")
        return

    log("🔍 Phase 2: Searching GitHub for new targets...")
    orgs = search_github_orgs(github_token)
    if not orgs:
        log("No orgs found via API search.")
        return

    ordered = sorted(orgs.values(), key=lambda o: (not o["is_stale"], -o["stars"]))

    for org_data in ordered:
        if actions_taken >= MAX_ACTIONS_PER_RUN:
            log(f"🏁 Hit cap of {MAX_ACTIONS_PER_RUN} actions — stopping.")
            break

        org_login = org_data["login"]
        if already_emailed(org_login, sent_log):
            continue

        log(f"→ Processing {org_login} ({org_data['repo_name']})...")
        email = get_org_email(org_login, github_token)
        if not email:
            continue

        log(f"  ✉️  contact found: {email} — generating email...")
        content = generate_initial_email(org_data)
        if not content:
            continue

        log(f"  📤 sending initial pitch: \"{content['subject']}\"")
        if send_email(email, content["subject"], content["body"]):
            actions_taken += 1
            sent_log.append({
                "org": org_login,
                "email": email,
                "repo": org_data["repo_name"],
                "subject": content["subject"],
                "initial_sent_at": current_time.isoformat(),
                "follow_up_sent_at": None,
                "replied": False
            })
            save_sent_log(sent_log)
            log(f"  ✅ sent ({actions_taken}/{MAX_ACTIONS_PER_RUN})")

            if actions_taken < MAX_ACTIONS_PER_RUN:
                delay = random.randint(120, 300)
                log(f"  ⏱  waiting {delay}s to look human...")
                time.sleep(delay)
        else:
            time.sleep(5)

    log("─" * 50)
    log(f"🏁 Cycle Complete. Total operations executed: {actions_taken}")

if __name__ == "__main__":
    main()
