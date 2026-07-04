#!/usr/bin/env python3
"""
Estonia-US Defense News Digest
--------------------------------
Pulls RSS feeds from Estonian news sources, uses the Gemini API (free tier)
to filter for Estonia-US defense and security relevance, summarizes each
relevant article in both English and Estonian, and emails the result.

Scope of "relevant":
  - Direct US-Estonia bilateral defense activity (exercises, troop rotations,
    visits by US officials, arms/equipment transfers, defense agreements)
  - NATO posture in the Baltic region as it relates to US interests and
    commitments (US-led battlegroups, Baltic air policing, summit decisions)
  - Security threats to Estonia bearing on US/NATO interests (Russian military
    activity near Estonia, hybrid threats, Baltic Sea infrastructure incidents,
    espionage with a US/NATO angle)
  - Estonian defense industry/tech developments involving US companies or
    relevant to the transatlantic alliance

Run modes:
    python digest.py --check-setup   validate credentials and feeds, no email sent
    python digest.py --test          fetch feeds and print candidates, no API/email
    python digest.py --dry-run       full pipeline, writes digest_preview.html instead of emailing
    python digest.py                 full pipeline, sends the email

Setup:
    pip install -r requirements.txt
    cp .env.example .env && nano .env   # fill in your four credentials

Cron (daily at 07:00 local time):
    0 7 * * * cd /path/to/estonia_digest && venv/bin/python3 digest.py >> digest.log 2>&1
"""

import argparse
import json
import os
import smtplib
import sys
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import feedparser


# ---------------------------------------------------------------------------
# .env loader (no external dependency)
# ---------------------------------------------------------------------------

def load_dotenv(path=".env"):
    """Read key=value pairs from a .env file into os.environ."""
    if not os.path.exists(path):
        return
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            os.environ.setdefault(key, value)

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# RSS feeds to pull from.
# ERR News and Postimees are confirmed stable endpoints.
# ICDS is Estonia's leading defense/security think tank -- verify icds.ee/feed/
# resolves from your network before relying on it (their site was recently
# redesigned and the feed path may have changed).
FEEDS = {
    "ERR News (EN)":  "https://news.err.ee/rss",
    "Postimees (EN)": "https://news.postimees.ee/rss",
    "ICDS":           "https://icds.ee/en/feed/",
}

# How far back to look for articles.
LOOKBACK_HOURS = 24

# Gemini model to use. gemini-2.5-flash is on the free tier as of mid-2026.
# Check https://ai.google.dev/gemini-api/docs/pricing if this stops working.
GEMINI_MODEL = "gemini-2.5-flash"

# Coarse keyword pre-filter applied before any API calls.
# An article must match at least one of these (case-insensitive, title + summary)
# to be sent to Gemini for relevance judgment.
# Purpose: cut down API usage by dropping obvious non-defense stories
# (sports, weather, culture) before spending a free-tier request on them.
# The LLM step still makes the real relevance decision -- this is just noise reduction.
PREFILTER_KEYWORDS = [
    # Direct defense terms
    "defense", "defence", "military", "nato", "troops", "soldier", "army",
    "navy", "air force", "battalion", "brigade", "regiment",
    # Equipment and operations
    "exercise", "drill", "rotation", "deployment", "weapon", "missile",
    "drone", "ammunition", "radar", "surveillance",
    # Key actors
    "pentagon", "us embassy", "ambassador", "lockheed", "raytheon",
    # Threats and security
    "russia", "putin", "hybrid", "sabotage", "espionage", "cyberattack",
    "cable", "pipeline", "border", "intelligence", "security threat",
    # Regional / alliance
    "baltic", "tapa", "ämari", "nato flank", "deterrence", "DIANA"
    # Wisconsin National Guard has a state partnership with Estonia
    "wisconsin",
]


# ---------------------------------------------------------------------------
# Step 1: Fetch RSS feeds
# ---------------------------------------------------------------------------

def fetch_recent_entries():
    """
    Pull all configured RSS feeds and return entries published within
    LOOKBACK_HOURS. Entries with no parseable date are included rather than
    silently dropped.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)
    all_entries = []

    for source, url in FEEDS.items():
        print(f"Fetching {source} ...", file=sys.stderr)
        try:
            feed = feedparser.parse(url)
        except Exception as exc:
            print(f"  ERROR: {exc}", file=sys.stderr)
            continue

        if feed.bozo and not feed.entries:
            print(f"  WARNING: could not parse feed ({feed.bozo_exception})", file=sys.stderr)
            continue

        count = 0
        for entry in feed.entries:
            # Resolve publication time
            published = None
            for field in ("published_parsed", "updated_parsed"):
                raw = getattr(entry, field, None)
                if raw:
                    published = datetime(*raw[:6], tzinfo=timezone.utc)
                    break

            if published is not None and published < cutoff:
                continue

            all_entries.append({
                "source":    source,
                "title":     entry.get("title", "(no title)"),
                "summary":   entry.get("summary", entry.get("description", "")),
                "link":      entry.get("link", ""),
                "published": published,
            })
            count += 1

        print(f"  -> {count} entries within lookback window", file=sys.stderr)

    return all_entries


# ---------------------------------------------------------------------------
# Step 2: Keyword pre-filter
# ---------------------------------------------------------------------------

def prefilter(entries):
    """
    Keep only entries where the title or summary contains at least one
    keyword from PREFILTER_KEYWORDS. Fast, free, no API call needed.
    """
    kept = []
    for e in entries:
        text = f"{e['title']} {e['summary']}".lower()
        if any(kw in text for kw in PREFILTER_KEYWORDS):
            kept.append(e)
    dropped = len(entries) - len(kept)
    print(f"Keyword pre-filter: kept {len(kept)}, dropped {dropped}", file=sys.stderr)
    return kept


# ---------------------------------------------------------------------------
# Step 3: LLM relevance filter + bilingual summary
# ---------------------------------------------------------------------------

def build_prompt(batch):
    """
    Construct the Gemini prompt for a batch of articles.
    Returns a string.
    """
    articles_block = "\n\n".join(
        f"[{i}] SOURCE: {e['source']}\n"
        f"TITLE: {e['title']}\n"
        f"TEXT: {e['summary'][:600]}"
        for i, e in enumerate(batch)
    )

    return f"""You are a defense analyst specializing in Baltic security and US-Estonia relations.

Below are {len(batch)} Estonian news article snippets, each tagged [0], [1], etc.

TASK: For each article, decide whether it is relevant to ESTONIA-US DEFENSE AND SECURITY, defined as:
  - Direct US-Estonia bilateral defense activity: joint exercises, US troop rotations or presence
    at Estonian bases (Tapa, Ämari), US officials visiting Estonia on defense matters, US arms
    or equipment transfers to Estonia, bilateral defense agreements or treaties
  - NATO posture in the Baltic region as it involves or is meaningful to US commitments: US-led
    Enhanced Forward Presence, Baltic air policing contributions by US aircraft, NATO summit
    decisions materially affecting the Baltic flank
  - Security threats to Estonia that bear on US/NATO strategic interests: Russian military activity
    near or against Estonia, hybrid warfare, sabotage of Baltic undersea cables or pipelines,
    espionage cases with a US or NATO angle, disinformation campaigns targeting NATO credibility
  - Estonian defense industry or technology with a US or transatlantic dimension: deals with US
    companies, NATO-funded programs, dual-use technology relevant to allied defense

NOT relevant: purely domestic Estonian politics, cultural news, sports, economic stories with no
security angle, or defense items with zero US/NATO/transatlantic dimension.

When genuinely uncertain, err toward INCLUDING the article and flag it as uncertain.

For EACH article produce a JSON object with:
  "relevant": true or false
  "en_summary": (only if relevant=true) one concise sentence in English, your own words
  "en_detail":  (only if relevant=true) a paragraph expanding on the summary — key actors,
                what was decided or happened, and why it matters for US-Estonia defense relations
  "tag":        (only if relevant=true) 3-6 word phrase naming the specific angle,
Respond with ONLY a JSON array of {len(batch)} objects in order. No preamble, no markdown fences.

Articles:
{articles_block}"""


def gemini_filter_and_summarize(entries, api_key):
    """
    Send batches of pre-filtered entries to Gemini. Keep only those judged
    relevant. Returns a list of entries enriched with en_summary,
    and tag fields.
    """
    from google import genai

    client = genai.Client(api_key=api_key)
    relevant = []
    batch_size = 8

    for batch_start in range(0, len(entries), batch_size):
        batch = entries[batch_start : batch_start + batch_size]
        prompt = build_prompt(batch)

        parsed_response = None
        max_retries = 4

        for attempt in range(max_retries):
            try:
                response = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=prompt,
                )
                text = response.text.strip()
                # Strip any markdown fences Gemini may add despite instructions
                text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
                parsed_response = json.loads(text)
                break

            except Exception as exc:
                msg = str(exc)
                is_rate_limit = "429" in msg or "RESOURCE_EXHAUSTED" in msg
                if is_rate_limit and attempt < max_retries - 1:
                    wait = 2 ** (attempt + 2)  # 4s, 8s, 16s, 32s
                    print(f"  Rate limited — waiting {wait}s ...", file=sys.stderr)
                    time.sleep(wait)
                    continue
                print(f"  ERROR on batch at index {batch_start}: {exc}", file=sys.stderr)
                break

        if parsed_response is None:
            # If the entire batch failed, skip rather than guess
            print(f"  Skipping {len(batch)} articles (batch failed entirely)", file=sys.stderr)
        else:
            for entry, result in zip(batch, parsed_response):
                if result.get("relevant"):
                    entry["en_summary"] = result.get("en_summary", "")
                    entry["tag"]        = result.get("tag", "")
                    relevant.append(entry)

        # Polite pause between batches — free tier is ~10-15 RPM
        time.sleep(2)

    print(f"LLM filter: {len(relevant)} relevant article(s) kept", file=sys.stderr)
    return relevant


# ---------------------------------------------------------------------------
# Step 4: Build HTML email
# ---------------------------------------------------------------------------

def build_html(entries, digest_date):
    """Render filtered, summarized entries as a clean HTML email body."""

    # Sort most-recent first; entries without a date sort to the bottom
    sorted_entries = sorted(
        entries,
        key=lambda e: e["published"] or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )

    lines = [f"""<html>
<body style="font-family: -apple-system, Arial, sans-serif; max-width: 680px;
             margin: 0 auto; color: #222;">
<h1 style="font-size: 20px; border-bottom: 2px solid #1a5276; padding-bottom: 8px;">
  Estonia-US Defense Digest &mdash; {digest_date}
</h1>
<p style="color: #666; font-size: 13px;">
  {len(sorted_entries)} relevant article(s) from the past {LOOKBACK_HOURS} hours.
</p>"""]

    if not sorted_entries:
        lines.append(
            '<p style="color:#888;">No Estonia-US defense-relevant articles found today.</p>'
        )

    for e in sorted_entries:
        pub_str = ""
        if e["published"]:
            pub_str = e["published"].strftime("%Y-%m-%d %H:%M UTC") + " &nbsp;&middot;&nbsp; "

        lines.append(f"""
<div style="margin-bottom: 22px; padding-bottom: 16px; border-bottom: 1px solid #e8e8e8;">
  <a href="{e['link']}"
     style="font-size: 15px; font-weight: bold; color: #154360; text-decoration: none;">
    {e['title']}
  </a>
  <div style="font-size: 12px; color: #888; margin: 3px 0 8px;">
    {pub_str}{e['source']}
    &nbsp;&middot;&nbsp;
    <span style="background: #eaf2f8; color: #1a5276;
                 padding: 2px 7px; border-radius: 3px; font-size: 11px;">
      {e.get('tag', '')}
    </span>
  </div>
  <div>{e.get('en_summary', '')}</div>
  </div>""")

    lines.append("</body></html>")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Step 5: Send email via Gmail SMTP
# ---------------------------------------------------------------------------

def send_email(html_body, subject, gmail_address, app_password, to_address):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = gmail_address
    msg["To"]      = to_address
    msg.attach(MIMEText(html_body, "html"))

    with smtplib.SMTP("smtp.gmail.com", 587) as server:
        server.starttls()
        server.login(gmail_address, app_password)
        server.sendmail(gmail_address, [to_address], msg.as_string())


# ---------------------------------------------------------------------------
# Setup check
# ---------------------------------------------------------------------------

def check_setup():
    """
    Validate all four environment variables, test Gmail SMTP login, test the
    Gemini API key with a minimal call, and test RSS feed connectivity.
    Does not send any email or process any articles.
    """
    print("=== 1. Environment variables ===")
    required = ["GMAIL_ADDRESS", "GMAIL_APP_PASSWORD", "DIGEST_TO", "GEMINI_API_KEY"]
    all_present = True
    for var in required:
        val = os.environ.get(var)
        if val:
            display = val if var in ("GMAIL_ADDRESS", "DIGEST_TO") else val[:4] + "..." + val[-2:]
            print(f"  [OK]      {var} = {display}")
        else:
            print(f"  [MISSING] {var}")
            all_present = False

    if not all_present:
        print("\nFill in missing values in .env before continuing.")
        return

    print("\n=== 2. Gmail SMTP login ===")
    try:
        with smtplib.SMTP("smtp.gmail.com", 587) as server:
            server.starttls()
            server.login(os.environ["GMAIL_ADDRESS"], os.environ["GMAIL_APP_PASSWORD"])
        print("  [OK] Login succeeded")
    except smtplib.SMTPAuthenticationError:
        print("  [FAILED] Authentication error. Verify that:")
        print("    - 2-Step Verification is ON for the Google account")
        print("    - GMAIL_APP_PASSWORD is an app password, not your login password")
        print("    - GMAIL_ADDRESS matches the account the app password belongs to")
        return
    except Exception as exc:
        print(f"  [FAILED] {exc}")
        return

    print("\n=== 3. Gemini API key ===")
    try:
        from google import genai
        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        client.models.generate_content(model=GEMINI_MODEL, contents="Reply with the single word OK.")
        print("  [OK] API key accepted")
    except Exception as exc:
        print(f"  [FAILED] {exc}")
        return

    print("\n=== 4. RSS feed connectivity ===")
    for source, url in FEEDS.items():
        feed = feedparser.parse(url)
        if feed.entries:
            print(f"  [OK]      {source}: {len(feed.entries)} entries")
        else:
            bozo = f" ({feed.bozo_exception})" if feed.bozo else ""
            print(f"  [WARNING] {source}: 0 entries{bozo}")
            print(f"            URL: {url}")
            print("            Check this URL in a browser — feed path may have changed.")

    print("\nAll checks done. If all [OK], you are ready for --dry-run.")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Estonia-US defense news digest emailer"
    )
    parser.add_argument(
        "--check-setup",
        action="store_true",
        help="Validate credentials and feed URLs. No email sent.",
    )
    parser.add_argument(
        "--test",
        action="store_true",
        help="Fetch feeds and print keyword-matched candidates. No API calls, no email.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Run full pipeline including Gemini calls. Write digest_preview.html instead of emailing.",
    )
    args = parser.parse_args()

    # --- check-setup mode ---
    if args.check_setup:
        check_setup()
        return

    # --- fetch + pre-filter (used by all remaining modes) ---
    raw_entries = fetch_recent_entries()
    print(f"Total fetched: {len(raw_entries)}", file=sys.stderr)

    candidates = prefilter(raw_entries)

    # --- test mode: stop here ---
    if args.test:
        print(f"\n{len(candidates)} keyword-matched candidate(s):\n")
        for e in candidates:
            pub = e["published"].strftime("%Y-%m-%d %H:%M") if e["published"] else "no date"
            print(f"  [{pub}] [{e['source']}] {e['title']}")
        return

    # --- full pipeline (dry-run or live) ---
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("ERROR: GEMINI_API_KEY not set. Add it to .env or export it.")

    if not candidates:
        print("No candidates after pre-filter. Exiting without sending.", file=sys.stderr)
        return

    relevant = gemini_filter_and_summarize(candidates, api_key)

    digest_date = datetime.now().strftime("%Y-%m-%d")
    html = build_html(relevant, digest_date)

    if args.dry_run:
        preview_path = "digest_preview.html"
        with open(preview_path, "w") as f:
            f.write(html)
        print(f"Dry run complete — open {preview_path} in a browser to preview.")
        return

    # --- live send ---
    gmail_address = os.environ.get("GMAIL_ADDRESS")
    app_password  = os.environ.get("GMAIL_APP_PASSWORD")
    to_address    = os.environ.get("DIGEST_TO")

    missing = [k for k, v in [
        ("GMAIL_ADDRESS", gmail_address),
        ("GMAIL_APP_PASSWORD", app_password),
        ("DIGEST_TO", to_address),
    ] if not v]
    if missing:
        sys.exit(f"ERROR: missing environment variable(s): {', '.join(missing)}")

    subject = f"Estonia-US Defense Digest — {digest_date}"
    send_email(html, subject, gmail_address, app_password, to_address)
    print(f"Sent: {subject}")


if __name__ == "__main__":
    main()
