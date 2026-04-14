"""
Scraper for Ballpark Pal's Odds Screen.

Cycles through all market dropdowns, compares BP odds to 6 sportsbooks,
calculates delta%, and flags bets with 5%+ edge.
"""

import logging
from datetime import date
from playwright.sync_api import sync_playwright
import time

import config

logger = logging.getLogger(__name__)

# All market options in the dropdown
MARKETS = [
    "Batter Home Runs",
    "Runs First Inning",
    "Team Total Runs",
    "Batter Doubles",
    "Batter Triples",
    "Batter Hits",
    "Batter Strikeouts",
    "Batter Walks",
    "Batter Runs",
    "Batter RBIs",
    "Batter Stolen Bases",
    "Batter Total Bases",
    "Pitcher Strikeouts",
    "Pitcher Outs",
    "Pitcher Walks",
    "Pitcher Hits Allowed",
    "Pitcher Earned Runs",
]

# Sportsbook columns (0-based index in the table, after TM, PLAYER, LINE, BP)
# Columns: TM | PLAYER | LINE | BP | DK | FD | NV | KA | PM | PX
BOOKS = ["DK", "FD", "NV", "KA", "PM", "PX"]
COL_TEAM = 0
COL_PLAYER = 1
COL_LINE = 2
COL_BP = 3
COL_BOOKS_START = 4  # DK=4, FD=5, NV=6, KA=7, PM=8, PX=9


def _odds_to_implied_prob(odds):
    """Convert American odds to implied probability."""
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def _calc_delta_pct(bp_odds, book_odds):
    """
    Calculate the delta% between BP's odds and a sportsbook's odds.

    Delta = BP implied probability - book implied probability.
    A positive delta means BP thinks the event is more likely than the book.
    """
    bp_prob = _odds_to_implied_prob(bp_odds)
    book_prob = _odds_to_implied_prob(book_odds)
    return round((bp_prob - book_prob) * 100, 1)


def _calc_expected_profit(bp_odds, book_odds):
    """Calculate expected profit using BP's true probability and the book's odds."""
    p_true = _odds_to_implied_prob(bp_odds)

    if book_odds < 0:
        win_amount = config.BET_SIZE * (100 / abs(book_odds))
    else:
        win_amount = config.BET_SIZE * (book_odds / 100)

    return round(p_true * win_amount - (1 - p_true) * config.BET_SIZE, 2)


def _parse_odds_cell(text):
    """Parse an odds value from a table cell. Returns int or None."""
    text = text.strip()
    if not text or text == "-" or text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def scrape_odds_screen(edge_threshold=None, headless=True):
    """
    Scrape the Odds Screen, cycling through all markets.

    Compares BP odds to each of the 6 sportsbooks.
    Returns bets where delta% >= threshold.
    """
    threshold = edge_threshold if edge_threshold is not None else config.EDGE_THRESHOLD
    today = date.today().isoformat()
    all_bets = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            # Login
            from scraper import login
            login(page)

            # Navigate to Odds Screen
            logger.info("Navigating to Odds Screen...")
            page.goto(config.BP_ODDS_URL, wait_until="networkidle")
            page.wait_for_selector("table", timeout=15000)

            # Click "Expanded" view
            try:
                page.click("text=Expanded", timeout=5000)
                time.sleep(1)
            except Exception:
                pass

            for market in MARKETS:
                logger.info(f"Scraping market: {market}")

                # Select the market from dropdown
                dropdown = page.query_selector("select")
                if dropdown:
                    dropdown.select_option(label=market)
                    time.sleep(2)  # Wait for table to update
                    page.wait_for_load_state("networkidle")

                # Parse the table
                rows = page.query_selector_all("table tbody tr")

                for row in rows:
                    cells = row.query_selector_all("td")
                    if len(cells) < 10:
                        continue

                    text = [c.inner_text().strip() for c in cells]

                    team = text[COL_TEAM]
                    player = text[COL_PLAYER]
                    line_text = text[COL_LINE]
                    bp_text = text[COL_BP]

                    bp_odds = _parse_odds_cell(bp_text)
                    if bp_odds is None:
                        continue

                    try:
                        line = float(line_text)
                    except ValueError:
                        continue

                    # Compare BP to each sportsbook
                    for i, book in enumerate(BOOKS):
                        col_idx = COL_BOOKS_START + i
                        if col_idx >= len(text):
                            continue

                        book_odds = _parse_odds_cell(text[col_idx])
                        if book_odds is None:
                            continue

                        delta_pct = _calc_delta_pct(bp_odds, book_odds)

                        if delta_pct >= threshold:
                            exp_profit = _calc_expected_profit(bp_odds, book_odds)

                            # Determine O/U based on BP vs book
                            # On the odds screen, the default view is "Over"
                            over_under = "O"

                            bet = {
                                "date": today,
                                "team": team,
                                "player": player,
                                "market": market,
                                "over_under": over_under,
                                "line": line,
                                "odds": book_odds,
                                "bp_odds": bp_odds,
                                "book": book,
                                "delta_pct": delta_pct,
                                "exp_profit": exp_profit,
                                "result": "",
                                "profit": "",
                            }
                            all_bets.append(bet)

                # Now check Under
                try:
                    page.click("text=Under", timeout=3000)
                    time.sleep(2)
                    page.wait_for_load_state("networkidle")
                except Exception:
                    continue

                rows = page.query_selector_all("table tbody tr")

                for row in rows:
                    cells = row.query_selector_all("td")
                    if len(cells) < 10:
                        continue

                    text = [c.inner_text().strip() for c in cells]

                    team = text[COL_TEAM]
                    player = text[COL_PLAYER]
                    line_text = text[COL_LINE]
                    bp_text = text[COL_BP]

                    bp_odds = _parse_odds_cell(bp_text)
                    if bp_odds is None:
                        continue

                    try:
                        line = float(line_text)
                    except ValueError:
                        continue

                    for i, book in enumerate(BOOKS):
                        col_idx = COL_BOOKS_START + i
                        if col_idx >= len(text):
                            continue

                        book_odds = _parse_odds_cell(text[col_idx])
                        if book_odds is None:
                            continue

                        delta_pct = _calc_delta_pct(bp_odds, book_odds)

                        if delta_pct >= threshold:
                            exp_profit = _calc_expected_profit(bp_odds, book_odds)

                            bet = {
                                "date": today,
                                "team": team,
                                "player": player,
                                "market": market,
                                "over_under": "U",
                                "line": line,
                                "odds": book_odds,
                                "bp_odds": bp_odds,
                                "book": book,
                                "delta_pct": delta_pct,
                                "exp_profit": exp_profit,
                                "result": "",
                                "profit": "",
                            }
                            all_bets.append(bet)

                # Switch back to Over for next market
                try:
                    page.click("text=Over", timeout=3000)
                    time.sleep(1)
                except Exception:
                    pass

            logger.info(f"Found {len(all_bets)} bets with delta% >= {threshold}% across all books.")
            return all_bets

        except Exception as e:
            logger.exception(f"Odds screen scraping failed: {e}")
            raise
        finally:
            browser.close()
