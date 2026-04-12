import os
from dotenv import load_dotenv

load_dotenv()

# Ballpark Pal
BP_LOGIN_URL = "https://www.ballparkpal.com/login.php"
BP_POSITIVE_EV_URL = "https://www.ballparkpal.com/Positive-EV.php"
BP_EMAIL = os.getenv("BP_EMAIL", "")
BP_PASSWORD = os.getenv("BP_PASSWORD", "")

# Google Sheets
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID", "")
GOOGLE_CREDENTIALS_FILE = os.getenv("GOOGLE_CREDENTIALS_FILE", "credentials.json")
GOOGLE_TOKEN_FILE = os.getenv("GOOGLE_TOKEN_FILE", "token.json")
TRACKED_BETS_TAB = "Tracked Bets"

# Edge filtering
EDGE_THRESHOLD = float(os.getenv("EDGE_THRESHOLD", "5.0"))

# Bet sizing
BET_SIZE = float(os.getenv("BET_SIZE", "100"))

# MLB Stats API (free, no key needed)
MLB_API_BASE = "https://statsapi.mlb.com/api/v1"

# Edge bands for reporting
EDGE_BANDS = [
    (5.0, 9.9, "5.0-9.9%"),
    (10.0, 14.9, "10.0-14.9%"),
    (15.0, 19.9, "15.0-19.9%"),
    (20.0, 24.9, "20.0-24.9%"),
    (25.0, 100.0, "25.0%+"),
]

# Odds bands for reporting (American odds)
ODDS_BANDS = [
    (0, -199, "0 to -199"),
    (-200, -399, "-200 to -399"),
    (-400, -9999, "-400+"),
    (100, 199, "+100 to +199"),
    (200, 399, "+200 to +399"),
    (400, 9999, "+400+"),
]
