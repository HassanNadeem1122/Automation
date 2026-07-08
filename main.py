"""
RailScout — Production Grade Outbound Engine
Features: Bedrock AI Generation, SMTP Sending, IMAP Reply Tracking, 3-Day Follow-ups.
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
GITHUB_API = "https://api.github.com"
BEDROCK_MODEL_ID = "us.anthropic.claude-3-5-sonnet-20240620-v1:0" # Upgraded to latest Sonnet
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

# ── Inbox Sync (IMAP) ─────────────────────────────────────────────────────

def check_if_replied(to_email: str, gmail_user: str, gmail_pass: str) -> bool:
    """Logs into Gmail via IMAP to see if we received an email from this address."""
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        mail.select('inbox')
        # Search for any email FROM the prospect
        status, messages = mail.search(None, f'FROM "{to_email}"')
        mail.logout()
        
        # If the search returned message IDs, they replied
        if messages[0].split():
            return True
        return False
    except Exception as e:
        log(f"  ⚠️ IMAP sync failed for {to_email}: {e}")
        return False # Default to False so we don't break, but be careful

# ── Generation & Sending ──────────────────────────────────────────────────

def generate_initial_email(org_data: dict) -> dict | None:
    """A highly calibrated, developer-to-developer pitch."""
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

def send_email(to_email: str, subject: str, body: str, reply_to_id: str = None) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = gmail_address
    msg["To"] = to_email
    
    # If this is a follow-up, format it to thread properly (optional but good practice)
    if reply_to_id:
        msg["In-Reply-To"] = reply_to_id
        msg["References"] = reply_to_id

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

# ── Core Logic ────────────────────────────────────────────────────────────

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
            
        # Skip if they already replied or if we already sent a follow-up
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
            
            # No reply, time to send the bump
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
                time.sleep(random.randint(60, 180)) # Human delay

    if actions_taken >= MAX_ACTIONS_PER_RUN:
        log("🏁 Action limit reached during follow-ups. Exiting for the day.")
        return

    log("🔍 Phase 2: Searching GitHub for new targets...")
    # Fetch your GitHub logic here (reusing your exact search logic from previous file)
    # ... (To save space, insert your search_github_orgs and get_org_email functions here) ...
    
    # Pseudo-code for the dispatch loop:
    # for org in new_orgs:
    #     generate email
    #     send email
    #     sent_log.append({
    #         "org": org, "email": email, "repo": repo, "subject": subj,
    #         "initial_sent_at": current_time.isoformat(),
    #         "follow_up_sent_at": None,
    #         "replied": False
    #     })
    #     actions_taken += 1
    #     sleep(random delay)

if __name__ == "__main__":
    main()
