"""Run this once to authorize Google Sheets access."""

import json
import webbrowser
from urllib.parse import urlparse, parse_qs
from google_auth_oauthlib.flow import Flow

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

flow = Flow.from_client_secrets_file(
    "credentials.json",
    scopes=SCOPES,
    redirect_uri="http://localhost:1"
)

auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")

print("Opening your browser...")
webbrowser.open(auth_url)

print()
print("After you sign in and click Allow, the browser will show an error page.")
print("That is NORMAL. Look at the address bar - it will have a long URL.")
print("Copy the ENTIRE URL from the address bar and paste it below.")
print()

redirect_url = input("Paste the URL here: ").strip()

code = parse_qs(urlparse(redirect_url).query)["code"][0]
flow.fetch_token(code=code)
creds = flow.credentials

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
