"""Run this once to authorize Google Sheets access. Opens your browser."""

import json
from google_auth_oauthlib.flow import InstalledAppFlow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

print("Opening your browser to sign in with Google...")
print("Sign in, click Allow, then wait for it to redirect.")
print()

flow = InstalledAppFlow.from_client_secrets_file("credentials.json", SCOPES)
creds = flow.run_local_server(port=8090, open_browser=True, timeout_seconds=300)

token_data = {
    "token": creds.token,
    "refresh_token": creds.refresh_token,
    "token_uri": creds.token_uri,
    "client_id": creds.client_id,
    "client_secret": creds.client_secret,
    "scopes": list(creds.scopes),
}

with open("token.json", "w") as f:
    json.dump(token_data, f)

print("Done! Google Sheets access authorized.")
