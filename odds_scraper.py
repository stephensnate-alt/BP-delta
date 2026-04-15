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
    pass


def _odds_to_implied_prob(odds):
    if odds < 0:
        return abs(odds) / (abs(odds) + 100)
    else:
        return 100 / (odds + 100)


def _calc_delta_pct(bp_odds, book_odds):
    bp_prob = _odds_to_implied_prob(bp_odds)
    book_prob = _odds_to_implied_prob(book_odds)
    return round((bp_prob - book_prob) * 100, 1)


def _calc_expected_profit(bp_odds, book_odds):
    p_true = _odds_to_implied_prob(bp_odds)
    if book_odds < 0:
        win_amount = config.BET_SIZE * (100 / abs(book_odds))
    else:
        win_amount = config.BET_SIZE * (book_odds / 100)
    return round(p_true * win_amount - (1 - p_true) * config.BET_SIZE, 2)


def _parse_odds_cell(text):
    text = text.strip()
    if not text or text == "-":
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _select_market(page, market):
    """
    Select a market from the correct dropdown.
    Re-queries all <select> elements fresh each call to avoid stale handles.
    The page may do a full reload after selection - that's fine.
    """
    selects = page.query_selector_all("select")
    for sel in selects:
        try:
            html = sel.inner_text()
            if "Batter Home Runs" in html or "Batter Hits" in html:
                sel.select_option(label=market)
                return True
        except Exception:
            continue

    logger.warning(f"Could not find market dropdown for '{market}'")
    return False


def _click_button(page, label):
    """
    Click a button/link with exact text match. Uses get_by_text with exact=True
    to avoid matching partial text in player names or table cells.
    """
    try:
        page.get_by_text(label, exact=True).first.click(timeout=3000)
        return True
    except Exception:
        logger.debug(f"Could not click '{label}'")
        return False


def _detect_columns(page):
    """Read table headers to build a column map."""
    headers = page.query_selector_all("table th")
    header_texts = [h.inner_text().strip().upper() for h in headers]
    logger.debug(f"Headers: {header_texts}")

    col_map = {}
    book_names = {"DK", "FD", "NV", "KA", "PM", "PX"}
    for i, h in enumerate(header_texts):
        if h == "TM":
            col_map["team"] = i
        elif h == "PLAYER":
            col_map["player"] = i
        elif h == "LINE":
            col_map["line"] = i
        elif h == "BP":
            col_map["bp"] = i
        elif h in book_names:
            col_map[h] = i

    if "bp" not in col_map:
        logger.error(f"No BP column found. Headers: {header_texts}")
        return None

    logger.info(f"Columns: {col_map}")
    return col_map


def _scrape_table(page):
    """
    Read entire table data in one JS call for speed.
    Returns list of lists (each = one row's cell texts).
    Only called AFTER page has fully loaded (no pending navigation).
    """
    return page.evaluate("""() => {
        const table = document.querySelector('table');
        if (!table) return [];
        const rows = table.querySelectorAll('tbody tr');
        const result = [];
        for (const row of rows) {
            const cells = row.querySelectorAll('td');
            if (cells.length >= 4) {
                result.push(Array.from(cells).map(c => c.textContent.trim()));
            }
        }
        return result;
    }""")


def _parse_bets(rows, col_map, market, over_under, threshold, today):
    """Extract qualifying bets from raw row data."""
    bets = []

    bp_idx = col_map.get("bp")
    line_idx = col_map.get("line")
    team_idx = col_map.get("team")
    player_idx = col_map.get("player")

    if bp_idx is None or line_idx is None:
        return bets

    for text in rows:
        if bp_idx >= len(text) or line_idx >= len(text):
            continue

        bp_odds = _parse_odds_cell(text[bp_idx])
        if bp_odds is None:
            continue

        try:
            line = float(text[line_idx])
        except ValueError:
            continue

        team = text[team_idx] if team_idx is not None and team_idx < len(text) else ""
        player = text[player_idx] if player_idx is not None and player_idx < len(text) else ""

        for book in BOOKS:
            book_idx = col_map.get(book)
            if book_idx is None or book_idx >= len(text):
                continue

            book_odds = _parse_odds_cell(text[book_idx])
            if book_odds is None:
                continue

            delta_pct = _calc_delta_pct(bp_odds, book_odds)
            if delta_pct >= threshold:
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
                    "exp_profit": _calc_expected_profit(bp_odds, book_odds),
                    "result": "",
                    "profit": "",
                })

    return bets


def _wait_and_settle(page):
    """Wait for page to finish loading after a dropdown change or button click."""
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    time.sleep(1)


def scrape_odds_screen(edge_threshold=None, headless=True):
    threshold = edge_threshold if edge_threshold is not None else config.EDGE_THRESHOLD
    today = date.today().isoformat()
    all_bets = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            from scraper import login
            login(page)

            logger.info("Navigating to Odds Screen...")
            page.goto(config.BP_ODDS_URL, wait_until="networkidle", timeout=30000)

            if "login" in page.url.lower():
                login(page)
                page.goto(config.BP_ODDS_URL, wait_until="networkidle", timeout=30000)

            page.wait_for_selector("table", timeout=30000, state="visible")
            page.screenshot(path="debug_odds_screen.png")
            time.sleep(2)

            # Click Expanded to show all 6 book columns
            _click_button(page, "Expanded")
            _wait_and_settle(page)

            # Detect columns
            col_map = _detect_columns(page)
            if not col_map:
                page.screenshot(path="debug_no_columns.png")
                raise ScraperError("Could not detect table columns")

            for i, market in enumerate(MARKETS):
                logger.info(f"[{i+1}/{len(MARKETS)}] {market}")

                # Select market (fresh dropdown query each time)
                if not _select_market(page, market):
                    continue

                # Page may fully reload here - wait for it
                _wait_and_settle(page)
                page.wait_for_selector("table", timeout=15000, state="visible")

                # Re-detect columns after first market (Expanded may have changed layout)
                if i == 0:
                    page.screenshot(path="debug_first_market.png")
                    new_map = _detect_columns(page)
                    if new_map:
                        col_map = new_map

                # ── OVER ──
                _click_button(page, "Over")
                _wait_and_settle(page)

                rows = _scrape_table(page)
                over_bets = _parse_bets(rows, col_map, market, "O", threshold, today)
                logger.info(f"  Over: {len(rows)} rows, {len(over_bets)} bets")
                all_bets.extend(over_bets)

                # ── UNDER ──
                _click_button(page, "Under")
                _wait_and_settle(page)

                rows = _scrape_table(page)
                under_bets = _parse_bets(rows, col_map, market, "U", threshold, today)
                logger.info(f"  Under: {len(rows)} rows, {len(under_bets)} bets")
                all_bets.extend(under_bets)

            logger.info(f"DONE: {len(all_bets)} total bets with delta >= {threshold}%")
            return all_bets

        except Exception as e:
            logger.exception(f"Scraping failed: {e}")
            try:
                page.screenshot(path="debug_odds_error.png")
            except Exception:
                pass
            raise
        finally:
            browser.close()
