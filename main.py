import json
import os
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
MAX_ACTIONS_PER_RUN = 10 
FOLLOW_UP_DAYS = 3
STALE_DAYS = 365
GITHUB_API = "https://api.github.com"

# Updated to target the correct Sonnet 4.6 cross-region inference identifier
BEDROCK_MODEL_ID = "us.anthropic.claude-3-6-sonnet-20250304-v1:0"
BEDROCK_REGION = "us-east-1"

SKIP_PREFIXES = (
    "support@", "info@", "opensource+", "webmaster@", "contact@",
    "hello@", "help@", "admin@", "noreply@", "no-reply@", "dev@",
    "developer@", "oss@", "crewonslack@", "sales@", "marketing@"
)

def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

# ── Pre-Flight Check ──────────────────────────────────────────────────────

def pre_flight_check() -> bool:
    required_env = ["GITHUB_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "GMAIL_ADDRESS", "GMAIL_APP_PASSWORD"]
    missing = [var for var in required_env if not os.environ.get(var)]
    if missing:
        log(f"❌ CRITICAL ISSUE: Missing environment variables: {', '.join(missing)}")
        return False
    log("✅ System scan passed. All credentials ready.")
    return True

# ── State Management ──────────────────────────────────────────────────────

def load_sent_log() -> list[dict]:
    if LOG_FILE.exists():
        try: return json.loads(LOG_FILE.read_text())
        except Exception: return []
    return []

def save_sent_log(entries: list[dict]) -> None:
    LOG_FILE.write_text(json.dumps(entries, indent=2, default=str))

def already_emailed(org_login: str, sent_log: list[dict]) -> bool:
    return any(e["org"] == org_login for e in sent_log)

# ── Inbox Sync ────────────────────────────────────────────────────────────

def check_if_replied(to_email: str, gmail_user: str, gmail_pass: str) -> bool:
    try:
        mail = imaplib.IMAP4_SSL('imap.gmail.com')
        mail.login(gmail_user, gmail_pass)
        mail.select('inbox')
        status, messages = mail.search(None, f'FROM "{to_email}"')
        mail.logout()
        return bool(messages[0].split())
    except Exception as e:
        log(f"  ⚠️ IMAP sync failed for {to_email}: {e}")
        return False

# ── GitHub Logic ──────────────────────────────────────────────────────────

def search_github_orgs(github_token: str) -> dict[str, dict]:
    headers = {"Authorization": f"token {github_token}", "Accept": "application/vnd.github.v3+json"}
    orgs = {}
    try:
        log("🔍 Searching GitHub for active Ruby repositories...")
        resp = requests.get(f"{GITHUB_API}/search/repositories", headers=headers, 
                            params={"q": "language:Ruby stars:100..5000", "per_page": 30}, timeout=15)
        for item in resp.json().get("items", []):
            owner = item.get("owner", {})
            if owner.get("type") == "Organization":
                orgs[owner.get("login")] = {"login": owner.get("login"), "repo_name": item.get("full_name")}
    except Exception as e:
        log(f"❌ GitHub Search failed: {e}")
    return orgs

def get_org_email(org_login: str, github_token: str) -> str | None:
    headers = {"Authorization": f"token {github_token}"}
    try:
        resp = requests.get(f"{GITHUB_API}/orgs/{org_login}", headers=headers, timeout=10)
        email = resp.json().get("email", "")
        if not email or "@" not in email or email.lower().startswith(SKIP_PREFIXES): 
            return None
        return email
    except Exception:
        return None

# ── AI Generation (Bulletproofed) ─────────────────────────────────────────

def generate_initial_email(org_data: dict) -> dict | None:
    prompt = f"""Write an extremely short, developer-to-developer cold email.

Context:
Org: {org_data['login']}
Repo: {org_data['repo_name']}

STRICT RULES:
1. Tone: Informal, technical, and fast. Like a Slack message to another engineer. No sales pitch vocabulary.
2. Length: Under 60 words. No "hope you are well" or boilerplate intros.
3. Opening: "hey team," or "hey [name]," followed by "came across your {org_data['repo_name']} codebase."
4. Core Value: If their rails backend is getting heavy or server costs are creeping up, moving critical endpoints to fastapi drops latency and slashes compute costs.
5. Technical Proof: State exactly this: "i recently ported 6000 lines of ruby (fat free crm) to fastapi. you can check out the architecture here: https://github.com/HassanNadeem1122/fat-free-crm-fastapi"
6. CTA: Ask a casual, low-pressure question: "is optimization or cutting server bills on your radar this quarter?"
7. Formatting: Keep it entirely lowercase, use casual punctuation, and use simple line breaks.
8. Subject: "fastapi migration / {org_data['login']}"

Output ONLY raw JSON format starting with {{ and ending with }}: {{"subject": "...", "body": "..."}}
"""
    try:
        client = boto3.client("bedrock-runtime", region_name=BEDROCK_REGION,
                              aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
                              aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"))
        
        response = client.invoke_model(modelId=BEDROCK_MODEL_ID, body=json.dumps({
            "anthropic_version": "bedrock-2023-05-31", "max_tokens": 400,
            "messages": [{"role": "user", "content": prompt}]}))
        
        text = json.loads(response["body"].read())["content"][0]["text"]
        
        # Isolation patch to extract clean JSON
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            log("  ❌ AI did not return a valid JSON format.")
            return None
            
        clean_json = text[start:end]
        return json.loads(clean_json)
        
    except Exception as e:
        log(f"  ❌ Generation error: {e}")
        return None

# ── SMTP Sending ──────────────────────────────────────────────────────────

def send_email(to_email: str, subject: str, body: str) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD")
    
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

# ── Main Execution Loop ───────────────────────────────────────────────────

def main():
    if not pre_flight_check():
        return

    github_token = os.environ.get("GITHUB_TOKEN")
    gmail_user = os.environ.get("GMAIL_ADDRESS")
    gmail_pass = os.environ.get("GMAIL_APP_PASSWORD")
    
    sent_log = load_sent_log()
    current_time = datetime.now(timezone.utc)
    actions_taken = 0

    # Phase 1: Follow-ups
    log("🔄 Phase 1: Checking for Follow-ups and Replies...")
    for entry in sent_log:
        if actions_taken >= MAX_ACTIONS_PER_RUN:
            break
        if entry.get("replied") or entry.get("follow_up_sent_at"):
            continue

        initial_date = datetime.fromisoformat(entry["initial_sent_at"])
        if (current_time - initial_date).days >= FOLLOW_UP_DAYS:
            log(f"  🔍 Checking inbox for replies from {entry['email']}...")
            if check_if_replied(entry['email'], gmail_user, gmail_pass):
                log(f"  ✅ Prospect replied! Updating state.")
                entry["replied"] = True
                save_sent_log(sent_log)
                continue
            
            log(f"  📤 Sending 3-day follow-up to {entry['org']}...")
            bump_subject = f"Re: {entry['subject']}"
            bump_body = "hey,\n\njust floating this to the top of your inbox. let me know if migrating the backend is a priority right now, otherwise i'll stop bugging you.\n\nbest,\nhassan"
            
            if send_email(entry["email"], bump_subject, bump_body):
                entry["follow_up_sent_at"] = current_time.isoformat()
                save_sent_log(sent_log)
                actions_taken += 1
                time.sleep(random.randint(60, 120))

    if actions_taken >= MAX_ACTIONS_PER_RUN:
        log("🏁 Action limit reached during follow-ups. Exiting.")
        return

    # Phase 2: New Emails
    log("🔍 Phase 2: Searching GitHub for new targets...")
    orgs = search_github_orgs(github_token)
    
    for org in orgs.values():
        if actions_taken >= MAX_ACTIONS_PER_RUN:
            break
        if already_emailed(org["login"], sent_log):
            continue
            
        email = get_org_email(org["login"], github_token)
        if not email:
            continue
        
        log(f"  ✉️ Contact found: {email} — generating email...")
        content = generate_initial_email(org)
        if not content:
            continue
            
        log(f"  📤 Sending initial pitch: \"{content['subject']}\"")
        if send_email(email, content["subject"], content["body"]):
            actions_taken += 1
            sent_log.append({
                "org": org["login"], 
                "email": email, 
                "subject": content["subject"],
                "initial_sent_at": current_time.isoformat(),
                "follow_up_sent_at": None,
                "replied": False
            })
            save_sent_log(sent_log)
            log(f"  ✅ Sent ({actions_taken}/{MAX_ACTIONS_PER_RUN})")
            time.sleep(random.randint(120, 300))

    log("─" * 50)
    log(f"🏁 Cycle Complete. Total operations executed: {actions_taken}")

if __name__ == "__main__":
    main()
