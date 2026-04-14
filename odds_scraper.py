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
    "Batter Singles",
    "Batter Strikeouts",
    "Batter Walks",
    "Batter Runs",
    "Batter RBIs",
    "Batter Stolen Bases",
    "Batter Total Bases",
    "Batter Hits+Runs+RBIs",
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


# Selectors to try for finding the data table (in priority order)
TABLE_SELECTORS = [
    "table.pointed",          # BP often uses class="pointed"
    "table.odds-table",
    "table.data-table",
    "#oddsTable",
    ".odds-screen table",
    "#odds-container table",
    "div.table-responsive table",
    "table",                  # fallback to any table
]

ROW_SELECTORS = [
    "tbody tr",
    "tr",
]


def _wait_for_table(page, timeout=30000):
    """Wait for the data table to appear, trying multiple selectors."""
    for selector in TABLE_SELECTORS:
        try:
            page.wait_for_selector(selector, timeout=timeout, state="visible")
            logger.debug(f"Found table with selector: {selector}")
            return selector
        except Exception:
            continue

    # If no selector worked, dump the page for debugging
    logger.error("Could not find table. Page title: %s", page.title())
    logger.error("Page URL: %s", page.url)
    # Check if we got redirected to login
    if "login" in page.url.lower():
        raise ScraperError("Got redirected to login page - session may have expired")
    raise ScraperError("Could not find data table on Odds Screen page")


def _find_table_rows(page):
    """Find table data rows using multiple selector strategies."""
    for table_sel in TABLE_SELECTORS:
        table = page.query_selector(table_sel)
        if not table:
            continue
        for row_sel in ROW_SELECTORS:
            rows = table.query_selector_all(row_sel)
            # Filter out header rows
            data_rows = []
            for r in rows:
                cells = r.query_selector_all("td")
                if len(cells) >= 4:  # At least TM, Player, Line, BP
                    data_rows.append(r)
            if data_rows:
                logger.debug(f"Found {len(data_rows)} data rows with {table_sel} > {row_sel}")
                return data_rows
    return []


class ScraperError(Exception):
    """Raised when scraping fails."""


def _parse_odds_cell(text):
    """Parse an odds value from a table cell. Returns int or None."""
    text = text.strip()
    if not text or text == "-" or text == "":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _parse_table_bets(page, market, over_under, threshold, today):
    """Parse visible table rows and return qualifying bets."""
    bets = []
    rows = _find_table_rows(page)
    logger.info(f"  {over_under}: Found {len(rows)} data rows")

    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) < 4:
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
                bets.append({
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
                })

    return bets


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
            page.goto(config.BP_ODDS_URL, wait_until="networkidle", timeout=30000)

            # Check if we got redirected to login
            if "login" in page.url.lower():
                logger.warning("Redirected to login - retrying login...")
                login(page)
                page.goto(config.BP_ODDS_URL, wait_until="networkidle", timeout=30000)

            # Take a debug screenshot
            page.screenshot(path="debug_odds_screen.png")
            logger.info(f"Page URL: {page.url}")
            logger.info(f"Page title: {page.title()}")

            # Wait for the page content to fully render
            _wait_for_table(page)

            # Click "Expanded" view
            try:
                expanded_btn = page.locator("text=Expanded")
                if expanded_btn.count() > 0:
                    expanded_btn.first.click()
                    time.sleep(2)
                    _wait_for_table(page)
            except Exception as e:
                logger.warning(f"Could not click Expanded: {e}")

            for market in MARKETS:
                logger.info(f"Scraping market: {market}")

                # Select the market from dropdown
                dropdown = page.query_selector("select")
                if dropdown:
                    dropdown.select_option(label=market)
                    time.sleep(3)  # Wait for table to update
                    page.wait_for_load_state("networkidle", timeout=15000)
                    _wait_for_table(page)

                # Parse Over bets
                all_bets.extend(_parse_table_bets(page, market, "O", threshold, today))

                # Now check Under
                try:
                    under_btn = page.locator("text=Under")
                    if under_btn.count() > 0:
                        under_btn.first.click()
                        time.sleep(3)
                        page.wait_for_load_state("networkidle", timeout=15000)
                        _wait_for_table(page)
                except Exception:
                    logger.warning(f"Could not switch to Under for {market}")
                    continue

                all_bets.extend(_parse_table_bets(page, market, "U", threshold, today))

                # Switch back to Over for next market
                try:
                    over_btn = page.locator("text=Over")
                    if over_btn.count() > 0:
                        over_btn.first.click()
                        time.sleep(2)
                except Exception:
                    pass

            logger.info(f"Found {len(all_bets)} bets with delta% >= {threshold}% across all books.")
            return all_bets

        except Exception as e:
            logger.exception(f"Odds screen scraping failed: {e}")
            try:
                page.screenshot(path="debug_odds_error.png")
                logger.info("Error screenshot saved to debug_odds_error.png")
            except Exception:
                pass
            raise
        finally:
            browser.close()
