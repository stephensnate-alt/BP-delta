"""
Scraper for Ballpark Pal's Positive EV page.

Uses Playwright to log in and parse the table at:
https://www.ballparkpal.com/Positive-EV.php

Filters for DraftKings (DK) bets with Delta% >= configured threshold.
"""

import logging
from datetime import date
from playwright.sync_api import sync_playwright

import config

logger = logging.getLogger(__name__)

# ============================================================
# SELECTOR REGISTRY
# Update these if BP changes their HTML structure.
# ============================================================
SELECTORS = {
    # Login page elements
    "email_input": 'input[name="email"]',
    "password_input": 'input[name="password"]',
    "login_button": 'button[type="submit"]',

    # Positive EV table
    "data_table": "table",
    "data_rows": "table tbody tr",
}

# Column indices in the Positive EV table (0-based)
# From screenshot: Tm | Player | Bk | Market | O/U | Line | Odds | CS | Δ | BP | Δ%
COL_TEAM = 0
COL_PLAYER = 1
COL_BOOK = 2
COL_MARKET = 3
COL_OVER_UNDER = 4
COL_LINE = 5
COL_ODDS = 6
COL_CS = 7
COL_DELTA = 8
COL_BP = 9
COL_DELTA_PCT = 10


class ScraperError(Exception):
    """Raised when scraping fails."""


def login(page):
    """Log into Ballpark Pal."""
    logger.info("Navigating to BP login page...")
    page.goto(config.BP_LOGIN_URL, wait_until="networkidle")

    page.fill(SELECTORS["email_input"], config.BP_EMAIL)
    page.fill(SELECTORS["password_input"], config.BP_PASSWORD)
    page.click(SELECTORS["login_button"])

    # Wait for navigation after login
    page.wait_for_load_state("networkidle", timeout=15000)
    logger.info("Login successful.")


def parse_positive_ev(page, edge_threshold=None, target_date=None):
    """
    Parse the Positive EV table, filtering for DK book and edge threshold.

    Returns a list of dicts, one per qualifying bet.
    """
    threshold = edge_threshold if edge_threshold is not None else config.EDGE_THRESHOLD

    bet_date = target_date or date.today().isoformat()
    ev_url = f"{config.BP_POSITIVE_EV_URL}?date={bet_date}"
    logger.info(f"Navigating to Positive EV page for {bet_date}...")
    page.goto(ev_url, wait_until="networkidle")
    page.wait_for_selector(SELECTORS["data_table"], timeout=15000)

    rows = page.query_selector_all(SELECTORS["data_rows"])
    logger.info(f"Found {len(rows)} total rows in table.")

    bets = []

    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 11:
            continue

        text = [c.inner_text().strip() for c in cells]

        # Filter: only DraftKings
        book = text[COL_BOOK].upper()
        if book != "DK":
            continue

        # Parse delta percentage (last column) - strip % sign
        delta_pct_raw = text[COL_DELTA_PCT].replace("%", "").strip()
        try:
            delta_pct = float(delta_pct_raw)
        except ValueError:
            logger.warning(f"Could not parse delta%: {delta_pct_raw!r}")
            continue

        # Filter: edge threshold
        if delta_pct < threshold:
            continue

        # Parse odds
        try:
            odds = int(text[COL_ODDS])
        except ValueError:
            odds_str = text[COL_ODDS]
            logger.warning(f"Could not parse odds: {odds_str!r}")
            continue

        # Parse line
        try:
            line = float(text[COL_LINE])
        except ValueError:
            line_str = text[COL_LINE]
            logger.warning(f"Could not parse line: {line_str!r}")
            continue

        # Calculate expected profit using true probability and odds
        # 1. Get implied probability from the odds
        if odds < 0:
            p_implied = abs(odds) / (abs(odds) + 100)
            win_amount = config.BET_SIZE * (100 / abs(odds))
        else:
            p_implied = 100 / (odds + 100)
            win_amount = config.BET_SIZE * (odds / 100)

        # 2. True probability = implied + delta edge
        p_true = p_implied + (delta_pct / 100)
        if p_true > 1:
            p_true = 0.99

        # 3. EV = (true prob * win) - (1 - true prob) * stake
        exp_profit = round(p_true * win_amount - (1 - p_true) * config.BET_SIZE, 2)

        bet = {
            "date": bet_date,
            "team": text[COL_TEAM],
            "player": text[COL_PLAYER],
            "market": text[COL_MARKET],
            "over_under": text[COL_OVER_UNDER],
            "line": line,
            "odds": odds,
            "cs_pct": text[COL_CS],
            "delta": text[COL_DELTA],
            "bp": text[COL_BP],
            "delta_pct": delta_pct,
            "exp_profit": exp_profit,
            "result": "",
            "profit": "",
        }
        bets.append(bet)

    logger.info(f"Found {len(bets)} DK bets with delta% >= {threshold}%.")
    return bets


def scrape_bets(edge_threshold=None, headless=True, target_date=None):
    """
    Top-level entry point. Launches browser, logs in, scrapes, filters.

    Args:
        edge_threshold: Minimum delta% to include. Defaults to config.EDGE_THRESHOLD.
        headless: Run browser in headless mode. Set False for debugging.
        target_date: Date to scrape (YYYY-MM-DD). Defaults to today.

    Returns:
        List of bet dicts passing the edge filter.
    """
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()
        try:
            login(page)
            bets = parse_positive_ev(page, edge_threshold, target_date)
            return bets
        except Exception as e:
            logger.exception(f"Scraping failed: {e}")
            raise ScraperError(str(e)) from e
        finally:
            browser.close()
