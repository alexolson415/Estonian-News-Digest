# ---------------------------------------------------------------------------
# REPLACEMENT send_email FUNCTION — Gmail API version
# ---------------------------------------------------------------------------
# Instructions:
#   1. In digest.py, DELETE the old send_email function (the one using smtplib)
#   2. Paste this function in its place
#   3. DELETE the line "import smtplib" at the top of digest.py
#   4. ADD these imports at the top of digest.py instead:
#        import base64
#        from google.oauth2.credentials import Credentials
#        from google.auth.transport.requests import Request as GoogleAuthRequest
#        from googleapiclient.discovery import build
#   5. In main(), the send_email call changes — it no longer needs
#      gmail_address or app_password. Find:
#        send_email(html, subject, gmail_address, app_password, to_address)
#      Replace with:
#        send_email(html, subject, to_address)
#      And delete the gmail_address / app_password lookups and the `missing`
#      check for GMAIL_APP_PASSWORD (keep the DIGEST_TO check).
#   6. Also update check_setup(): remove the SMTP login test section, since
#      there is no SMTP login anymore.
# ---------------------------------------------------------------------------

def send_email(html_body, subject, to_address):
    """
    Send the digest via the Gmail API using OAuth credentials.

    Reads the OAuth token from either:
      - GMAIL_TOKEN_JSON environment variable (GitHub Actions), or
      - token.json file in the working directory (local runs)

    The token was generated once by generate_token.py and refreshes itself
    automatically when expired.
    """
    recipients = [addr.strip() for addr in to_address.split(",")]

    # Load token: env var first (CI), then local file
    token_json = os.environ.get("GMAIL_TOKEN_JSON")
    if token_json:
        creds = Credentials.from_authorized_user_info(json.loads(token_json))
    elif os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json")
    else:
        raise RuntimeError(
            "No Gmail token found. Set GMAIL_TOKEN_JSON env var or run "
            "generate_token.py to create token.json"
        )

    # Refresh if expired (tokens expire after ~1 hour; refresh is automatic)
    if creds.expired and creds.refresh_token:
        creds.refresh(GoogleAuthRequest())

    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["To"]      = ", ".join(recipients)
    msg.attach(MIMEText(html_body, "html"))

    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
