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

# All market options in the dropdown (exact text from BP)
MARKETS = [
    "Batter Singles",
    "Batter Doubles",
    "Batter Triples",
    "Batter Home Runs",
    "Batter Strikeouts",
    "Batter Walks",
    "Batter Hits",
    "Batter Bases",
    "Batter Stolen Bases",
    "Batter RBIs",
    "Batter Runs",
    "Batter H+R+RBI",
    "Pitcher Walks",
    "Pitcher Strikeouts",
    "Pitcher Earned Runs",
    "Pitcher To Record Win",
    "Pitcher Hits Allowed",
    "Pitcher Outs",
]

BOOKS = ["DK", "FD", "NV", "KA", "PM", "PX"]


class ScraperError(Exception):
    """Raised when scraping fails."""


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


def _find_market_dropdown(page):
    """Find the market dropdown specifically (the one containing 'Batter Home Runs')."""
    selects = page.query_selector_all("select")
    for s in selects:
        options_text = s.inner_text()
        if "Batter Home Runs" in options_text or "Batter Hits" in options_text:
            logger.debug("Found market dropdown")
            return s
    # Fallback: if only one select, use it
    if len(selects) == 1:
        return selects[0]
    logger.error(f"Could not identify market dropdown among {len(selects)} selects")
    return None


def _click_over_under(page, target):
    """Click the Over or Under button specifically."""
    try:
        # Target the exact button - BP uses styled buttons/links for Over/Under
        btn = page.locator(f"button:text-is('{target}'), a:text-is('{target}')").first
        btn.click(timeout=3000)
        logger.debug(f"Clicked '{target}' button")
        return True
    except Exception:
        pass
    try:
        # Fallback: try any clickable element with exact text
        btn = page.locator(f"text='{target}'").first
        btn.click(timeout=3000)
        logger.debug(f"Clicked '{target}' via text fallback")
        return True
    except Exception:
        logger.warning(f"Could not find '{target}' button")
        return False


def _get_first_player(page):
    """Get the first player name from the table for change detection."""
    rows = page.query_selector_all("table tbody tr, table tr")
    for row in rows:
        cells = row.query_selector_all("td")
        if len(cells) >= 4:
            return cells[1].inner_text().strip()
    return ""


def _detect_columns(page):
    """Detect column layout from table headers."""
    headers = page.query_selector_all("table th, table thead td")
    header_texts = [h.inner_text().strip().upper() for h in headers]
    logger.debug(f"Table headers: {header_texts}")

    col_map = {}
    for i, h in enumerate(header_texts):
        if h == "TM":
            col_map["team"] = i
        elif h == "PLAYER":
            col_map["player"] = i
        elif h == "LINE":
            col_map["line"] = i
        elif h == "BP":
            col_map["bp"] = i
        elif h == "DK":
            col_map["DK"] = i
        elif h == "FD":
            col_map["FD"] = i
        elif h == "NV":
            col_map["NV"] = i
        elif h == "KA":
            col_map["KA"] = i
        elif h == "PM":
            col_map["PM"] = i
        elif h == "PX":
            col_map["PX"] = i

    if "bp" not in col_map:
        logger.error(f"Could not find BP column in headers: {header_texts}")
        return None

    logger.info(f"Detected columns: {col_map}")
    return col_map


def _wait_for_table_change(page, old_first_player, timeout=10):
    """Wait until the first player in the table changes (or timeout)."""
    for _ in range(timeout * 2):
        new_first = _get_first_player(page)
        if new_first and new_first != old_first_player:
            return True
        time.sleep(0.5)
    return False


def _parse_table_bets(page, col_map, market, over_under, threshold, today):
    """Parse visible table rows and return qualifying bets."""
    bets = []
    rows = page.query_selector_all("table tbody tr, table tr")
    data_rows = []
    for r in rows:
        cells = r.query_selector_all("td")
        if len(cells) >= 4:
            data_rows.append(r)

    logger.info(f"  {over_under}: Found {len(data_rows)} data rows")

    for row in data_rows:
        cells = row.query_selector_all("td")
        text = [c.inner_text().strip() for c in cells]

        team = text[col_map["team"]] if "team" in col_map else ""
        player = text[col_map["player"]] if "player" in col_map else ""

        line_idx = col_map.get("line")
        bp_idx = col_map.get("bp")
        if line_idx is None or bp_idx is None:
            continue
        if line_idx >= len(text) or bp_idx >= len(text):
            continue

        bp_odds = _parse_odds_cell(text[bp_idx])
        if bp_odds is None:
            continue

        try:
            line = float(text[line_idx])
        except ValueError:
            continue

        for book in BOOKS:
            book_idx = col_map.get(book)
            if book_idx is None or book_idx >= len(text):
                continue

            book_odds = _parse_odds_cell(text[book_idx])
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

            # Wait for table to appear
            page.wait_for_selector("table", timeout=30000, state="visible")
            time.sleep(2)

            # Click "Expanded" view to get all book columns
            try:
                expanded_btn = page.locator("button:has-text('Expanded'), a:has-text('Expanded'), span:has-text('Expanded')")
                if expanded_btn.count() > 0:
                    expanded_btn.first.click()
                    time.sleep(3)
                    logger.info("Clicked Expanded view")
                else:
                    # Try clicking by exact text
                    page.click("text=Expanded", timeout=5000)
                    time.sleep(3)
            except Exception as e:
                logger.warning(f"Could not click Expanded: {e}")

            # Detect column layout from headers
            col_map = _detect_columns(page)
            if not col_map:
                page.screenshot(path="debug_odds_columns.png")
                raise ScraperError("Could not detect table columns")

            # Find the market dropdown
            market_dropdown = _find_market_dropdown(page)
            if not market_dropdown:
                page.screenshot(path="debug_odds_dropdown.png")
                raise ScraperError("Could not find market dropdown")

            for i, market in enumerate(MARKETS):
                logger.info(f"Scraping market: {market}")

                # Select the market
                try:
                    market_dropdown.select_option(label=market)
                except Exception as e:
                    logger.warning(f"Could not select '{market}': {e}")
                    continue

                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=10000)

                # First market: take debug screenshot and detect columns
                if i == 0:
                    page.screenshot(path="debug_first_market.png")
                    new_col_map = _detect_columns(page)
                    if new_col_map:
                        col_map = new_col_map

                # Make sure we're on Over
                _click_over_under(page, "Over")
                time.sleep(1)

                # Parse Over bets
                all_bets.extend(_parse_table_bets(page, col_map, market, "O", threshold, today))

                # Switch to Under
                if _click_over_under(page, "Under"):
                    time.sleep(1.5)
                    page.wait_for_load_state("networkidle", timeout=10000)

                    all_bets.extend(_parse_table_bets(page, col_map, market, "U", threshold, today))

                    # Switch back to Over for next market
                    _click_over_under(page, "Over")
                    time.sleep(0.5)
                else:
                    logger.warning(f"Could not switch to Under for {market}")

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
