#!/usr/bin/env python3
"""
One-time Gmail API token generator.

Run this ONCE on your laptop:
    python generate_token.py

It will:
  1. Open a browser window asking you to sign in and click Allow
  2. Save the resulting token to token.json
  3. Print the token contents for you to paste into GitHub Secrets

Requires credentials.json (downloaded from Google Cloud Console) in the
same folder.

SECURITY NOTE: token.json and credentials.json both grant access to send
email from your account. Never commit either to git. Add both to .gitignore.
"""

import json
import os
import sys

from google_auth_oauthlib.flow import InstalledAppFlow

# gmail.send is the narrowest scope that allows sending -- the token cannot
# read your inbox or do anything else.
SCOPES = ["https://www.googleapis.com/auth/gmail.send"]


def main():
    if not os.path.exists("credentials.json"):
        sys.exit(
            "ERROR: credentials.json not found in this folder.\n"
            "Download it from Google Cloud Console -> APIs & Services -> "
            "Credentials -> your OAuth client -> Download JSON, and save it "
            "here as credentials.json"
        )

    flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
    creds = flow.run_local_server(port=0)

    with open("token.json", "w") as f:
        f.write(creds.to_json())

    print("\n" + "=" * 60)
    print("token.json saved.")
    print("=" * 60)
    print("\nNext steps:")
    print("1. Add token.json and credentials.json to .gitignore (do NOT commit them)")
    print("2. Copy the ENTIRE contents below into a GitHub Secret named GMAIL_TOKEN_JSON:\n")
    with open("token.json") as f:
        print(f.read())


if __name__ == "__main__":
    main()
