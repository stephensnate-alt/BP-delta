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
    Select a market from the dropdown using JavaScript to avoid stale elements.
    Finds the <select> that contains the market options, sets its value, and
    dispatches a change event so the page reacts.
    """
    result = page.evaluate("""(marketLabel) => {
        const selects = document.querySelectorAll('select');
        for (const sel of selects) {
            for (const opt of sel.options) {
                if (opt.text === marketLabel || opt.label === marketLabel) {
                    sel.value = opt.value;
                    sel.dispatchEvent(new Event('change', { bubbles: true }));
                    return { found: true, value: opt.value };
                }
            }
        }
        return { found: false };
    }""", market)

    if not result["found"]:
        logger.warning(f"Could not find '{market}' in any dropdown")
        return False

    logger.debug(f"Selected market: {market}")
    return True


def _click_over_under(page, target):
    """
    Click Over or Under button using JavaScript to find the exact button.
    Looks for elements whose trimmed text is exactly 'Over' or 'Under'.
    """
    clicked = page.evaluate("""(target) => {
        // Look through clickable elements for exact text match
        const candidates = document.querySelectorAll('button, a, span, div, label');
        for (const el of candidates) {
            // Only match if the element's own text (not children's combined text) matches
            // or if it's a small element with exact text
            const text = el.textContent.trim();
            if (text === target) {
                const rect = el.getBoundingClientRect();
                // Must be visible and reasonably sized (a button, not a table cell)
                if (rect.width > 0 && rect.width < 200 && rect.height > 0 && rect.height < 80) {
                    el.click();
                    return true;
                }
            }
        }
        return false;
    }""", target)

    if clicked:
        logger.debug(f"Clicked '{target}'")
    else:
        logger.warning(f"Could not find '{target}' button")
    return clicked


def _detect_columns(page):
    """Read table headers to build a column index map."""
    headers = page.evaluate("""() => {
        const ths = document.querySelectorAll('table th');
        return Array.from(ths).map(th => th.textContent.trim().toUpperCase());
    }""")

    logger.debug(f"Table headers: {headers}")

    col_map = {}
    book_names = {"DK", "FD", "NV", "KA", "PM", "PX"}
    for i, h in enumerate(headers):
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
        logger.error(f"Could not find BP column. Headers: {headers}")
        return None

    logger.info(f"Column map: {col_map}")
    return col_map


def _scrape_table(page, col_map):
    """
    Read all table data using JavaScript for speed.
    Returns list of lists (each inner list = one row's cell texts).
    """
    rows = page.evaluate("""() => {
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
    return rows


def _parse_bets_from_rows(rows, col_map, market, over_under, threshold, today):
    """Parse rows into qualifying bets."""
    bets = []
    for text in rows:
        team = text[col_map["team"]] if "team" in col_map and col_map["team"] < len(text) else ""
        player = text[col_map["player"]] if "player" in col_map and col_map["player"] < len(text) else ""

        bp_idx = col_map.get("bp")
        line_idx = col_map.get("line")
        if bp_idx is None or line_idx is None:
            continue
        if bp_idx >= len(text) or line_idx >= len(text):
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
                logger.warning("Redirected to login - retrying...")
                login(page)
                page.goto(config.BP_ODDS_URL, wait_until="networkidle", timeout=30000)

            page.screenshot(path="debug_odds_screen.png")
            logger.info(f"Page URL: {page.url}")

            # Wait for table
            page.wait_for_selector("table", timeout=30000, state="visible")
            time.sleep(2)

            # Click Expanded view
            try:
                _click_over_under(page, "Expanded")  # reuse the JS click helper
                time.sleep(2)
                page.wait_for_load_state("networkidle", timeout=10000)
                logger.info("Clicked Expanded view")
            except Exception as e:
                logger.warning(f"Could not click Expanded: {e}")

            # Detect column layout
            col_map = _detect_columns(page)
            if not col_map:
                page.screenshot(path="debug_odds_columns.png")
                raise ScraperError("Could not detect table columns")

            for i, market in enumerate(MARKETS):
                logger.info(f"Scraping market: {market} ({i+1}/{len(MARKETS)})")

                # Select market via JS (never stale)
                if not _select_market(page, market):
                    continue

                time.sleep(1.5)
                page.wait_for_load_state("networkidle", timeout=10000)

                # First market: re-detect columns and screenshot
                if i == 0:
                    page.screenshot(path="debug_first_market.png")
                    new_col_map = _detect_columns(page)
                    if new_col_map:
                        col_map = new_col_map

                # ── OVER ──
                _click_over_under(page, "Over")
                time.sleep(1)
                page.wait_for_load_state("networkidle", timeout=10000)

                rows = _scrape_table(page, col_map)
                over_bets = _parse_bets_from_rows(rows, col_map, market, "O", threshold, today)
                logger.info(f"  O: {len(rows)} rows, {len(over_bets)} qualifying bets")
                all_bets.extend(over_bets)

                # ── UNDER ──
                if _click_over_under(page, "Under"):
                    time.sleep(1)
                    page.wait_for_load_state("networkidle", timeout=10000)

                    rows = _scrape_table(page, col_map)
                    under_bets = _parse_bets_from_rows(rows, col_map, market, "U", threshold, today)
                    logger.info(f"  U: {len(rows)} rows, {len(under_bets)} qualifying bets")
                    all_bets.extend(under_bets)
                else:
                    logger.warning(f"  Could not switch to Under for {market}")

            logger.info(f"Total: {len(all_bets)} bets with delta% >= {threshold}%")
            return all_bets

        except Exception as e:
            logger.exception(f"Odds screen scraping failed: {e}")
            try:
                page.screenshot(path="debug_odds_error.png")
            except Exception:
                pass
            raise
        finally:
            browser.close()
