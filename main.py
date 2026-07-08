"""
RailScout — GitHub -> Bedrock Claude -> Gmail cold outreach script.
All secrets via os.environ. No hardcoded credentials.
"""

import json
import os
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import boto3
import requests

# ── Config ────────────────────────────────────────────────────────────────
LOG_FILE = Path(__file__).parent / "sent_log.json"
MAX_EMAILS = 15
DELAY_BETWEEN_SENDS = 90  # seconds
GITHUB_API = "https://api.github.com"
BEDROCK_MODEL_ID = "us.anthropic.claude-sonnet-4-6"
BEDROCK_REGION = "us-east-1"

# Legacy/stuck signal: last pushed more than this many days ago.
# Popular + recently active repos are NOT the target — stale ones are.
STALE_DAYS = 365

# Generic/role addresses bounce more, get read less, and burn daily send
# quota without real reply potential. Skip them.
SKIP_PREFIXES = ("support@", "info@", "opensource+", "webmaster@", "contact@",
                  "hello@", "help@", "admin@", "noreply@", "no-reply@")


def log(msg: str) -> None:
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ── Persistence ───────────────────────────────────────────────────────────

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


# ── Step 1: GitHub search ─────────────────────────────────────────────────

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

                # Staleness check — this is what actually separates
                # "legacy stuck" from "popular and thriving"
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

            log(f"  page {page}: {len(orgs)} unique orgs so far")
            time.sleep(1)

        except Exception as e:
            log(f"❌ Search error: {e}")
            break

    stale_count = sum(1 for o in orgs.values() if o["is_stale"])
    log(f"✅ Found {len(orgs)} orgs total ({stale_count} look stale/legacy, {len(orgs) - stale_count} recently active)")
    return orgs


# ── Step 2: extract org email ─────────────────────────────────────────────

def get_org_email(org_login: str, github_token: str) -> str | None:
    headers = {
        "Authorization": f"token {github_token}",
        "Accept": "application/vnd.github.v3+json",
    }
    try:
        resp = requests.get(f"{GITHUB_API}/orgs/{org_login}", headers=headers, timeout=10)
        if resp.status_code != 200:
            log(f"  {org_login}: GitHub org lookup failed ({resp.status_code})")
            return None
        email = (resp.json().get("email") or "").strip()
        if not email or "@" not in email:
            return None
        if email.lower().startswith(SKIP_PREFIXES):
            return None
        return email
    except Exception as e:
        log(f"  {org_login}: error fetching org info — {e}")
        return None


# ── Step 3: generate email via Bedrock ────────────────────────────────────

def generate_email(org_data: dict) -> dict | None:
    prompt = f"""Write a short, personalized cold outreach email pitching a codebase migration service.

Org: {org_data['login']}
Repo: {org_data['repo_name']} ({org_data['stars']} stars)
Repo description: {org_data['description'] or 'no description available'}
Repo activity: {"appears stale/legacy (no recent pushes)" if org_data['is_stale'] else "actively maintained"}

Requirements:
- Open casually, reference the org name and repo naturally (e.g. "Saw {org_data['login']} is running Rails for {org_data['repo_name'].split('/')[-1]}...")
- If the repo looks stale, you can note the maintenance angle. If it looks active, don't claim it's neglected — instead frame around scale/cost of running Rails long-term.
- Pitch: migrating legacy Rails to a modern Python stack (e.g. FastAPI)
- Include this exact case study link once, verbatim: github.com/HassanNadeem1122/fat-free-crm-fastapi
- Describe the case study briefly: migrated Fat Free CRM (a 3,600-star Rails CRM) to FastAPI — full CRUD, auth, tests, same business logic
- End with a soft call-to-action (e.g. "worth a 15 min call?")
- Sign as "Hassan"
- After the sign-off, add a plain one-line opt-out: "Reply 'no thanks' and I won't follow up."
- 4-6 short sentences total, no corporate jargon, no exclamation marks, no placeholders like [Company]
- Subject line: write it like a real person typing quickly to a colleague — lowercase-first-word ok, no colons/dashes/pipe formatting, no "Migration" or "FastAPI" jargon in the subject itself, just a plain human question or observation (e.g. "quick question about your rails setup" not "Rails to FastAPI Migration for X")

Return ONLY valid JSON, no markdown fences, no preamble, no explanation, in exactly this shape:
{{"subject": "...", "body": "..."}}"""

    try:
        client = boto3.client(
            "bedrock-runtime",
            region_name=BEDROCK_REGION,
            aws_access_key_id=os.environ.get("AWS_ACCESS_KEY_ID"),
            aws_secret_access_key=os.environ.get("AWS_SECRET_ACCESS_KEY"),
        )
        response = client.invoke_model(
            modelId=BEDROCK_MODEL_ID,
            body=json.dumps(
                {
                    "anthropic_version": "bedrock-2023-05-31",
                    "max_tokens": 500,
                    "messages": [{"role": "user", "content": prompt}],
                }
            ),
        )
        result = json.loads(response["body"].read())
        text = result["content"][0]["text"].strip()

        # Strip markdown fences if the model adds them despite instructions
        if text.startswith("```"):
            text = text.strip("`")
            if text.startswith("json"):
                text = text[4:].strip()

        parsed = json.loads(text)
        if "subject" not in parsed or "body" not in parsed:
            log(f"  ⚠️  Bedrock returned JSON missing subject/body: {text[:200]}")
            return None
        return parsed

    except json.JSONDecodeError as e:
        log(f"  ⚠️  Bedrock did not return valid JSON: {e}")
        return None
    except Exception as e:
        log(f"  ❌ Bedrock error: {e}")
        return None


# ── Step 4: send via Gmail SMTP ───────────────────────────────────────────

def send_email(to_email: str, subject: str, body: str) -> bool:
    gmail_address = os.environ.get("GMAIL_ADDRESS", "")
    gmail_password = os.environ.get("GMAIL_APP_PASSWORD", "")

    if not gmail_address or not gmail_password:
        log("  ❌ Missing GMAIL_ADDRESS or GMAIL_APP_PASSWORD env vars")
        return False

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
        log(f"  ❌ SMTP error sending to {to_email}: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    github_token = os.environ.get("GITHUB_TOKEN", "")
    if not github_token:
        log("❌ GITHUB_TOKEN env var not set. Exiting.")
        return

    sent_log = load_sent_log()
    orgs = search_github_orgs(github_token)

    if not orgs:
        log("No orgs found. Exiting.")
        return

    # Prioritize stale/legacy-looking repos first — that's who the pitch fits
    ordered = sorted(orgs.values(), key=lambda o: (not o["is_stale"], -o["stars"]))

    sent_count = 0
    skipped_no_email = 0
    skipped_already_sent = 0

    for org_data in ordered:
        if sent_count >= MAX_EMAILS:
            log(f"🏁 Hit cap of {MAX_EMAILS} emails this run — stopping.")
            break

        org_login = org_data["login"]

        if already_emailed(org_login, sent_log):
            skipped_already_sent += 1
            continue

        log(f"→ {org_login} ({org_data['repo_name']}, {org_data['stars']}⭐, "
            f"{'stale' if org_data['is_stale'] else 'active'})")

        email = get_org_email(org_login, github_token)
        if not email:
            log(f"  no public email — skipping")
            skipped_no_email += 1
            continue

        log(f"  ✉️  contact found: {email} — generating email...")
        content = generate_email(org_data)
        if not content:
            log(f"  skipping (generation failed)")
            continue

        log(f"  📤 sending: \"{content['subject']}\"")
        success = send_email(email, content["subject"], content["body"])

        if success:
            sent_count += 1
            sent_log.append(
                {
                    "org": org_login,
                    "email": email,
                    "repo": org_data["repo_name"],
                    "subject": content["subject"],
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }
            )
            save_sent_log(sent_log)
            log(f"  ✅ sent ({sent_count}/{MAX_EMAILS} this run)")

            if sent_count < MAX_EMAILS:
                log(f"  ⏱  waiting {DELAY_BETWEEN_SENDS}s...")
                time.sleep(DELAY_BETWEEN_SENDS)
        else:
            log(f"  send failed, moving on")
            time.sleep(2)

    log("─" * 50)
    log(f"🏁 Done. Sent: {sent_count} | No email: {skipped_no_email} | "
        f"Already emailed: {skipped_already_sent} | Total orgs seen: {len(orgs)}")


if __name__ == "__main__":
    main()
