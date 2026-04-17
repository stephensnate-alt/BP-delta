"""
Scraper for Ballpark Pal's Odds Screen.

Cycles through all market dropdowns, compares BP odds to 6 sportsbooks,
calculates delta%, and flags bets with 5%+ edge.
"""

import logging
from datetime import date, timedelta
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

# Expanded view fixed column indices:
# TM | PLAYER | LINE | BP | DK | FD | NV | KA | PM | PX
BOOKS = ["DK", "FD", "NV", "KA", "PM", "PX"]
COL_TEAM = 0
COL_PLAYER = 1
COL_LINE = 2
COL_BP = 3
COL_BOOKS_START = 4  # DK=4, FD=5, NV=6, KA=7, PM=8, PX=9


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
    """Fresh-query all selects, find the market dropdown, select the option."""
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
    """Click a button with exact text match."""
    try:
        page.get_by_text(label, exact=True).first.click(timeout=3000)
        return True
    except Exception:
        logger.debug(f"Could not click '{label}'")
        return False


def _wait_and_settle(page):
    """Wait for page to finish loading after a click or dropdown change."""
    time.sleep(1)
    try:
        page.wait_for_load_state("load", timeout=10000)
    except Exception:
        pass
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass


def _scrape_table(page):
    """Read only VISIBLE cells from the table. Retries if page is mid-navigation."""
    for attempt in range(3):
        try:
            return page.evaluate("""() => {
                const table = document.querySelector('table');
                if (!table) return [];
                const rows = table.querySelectorAll('tbody tr');
                const result = [];
                for (const row of rows) {
                    const cells = row.querySelectorAll('td');
                    const visible = [];
                    for (const c of cells) {
                        if (c.offsetWidth > 0 && c.offsetHeight > 0) {
                            visible.push(c.innerText.trim());
                        }
                    }
                    if (visible.length >= 4) {
                        result.push(visible);
                    }
                }
                return result;
            }""")
        except Exception:
            time.sleep(2)
            try:
                page.wait_for_load_state("load", timeout=10000)
                page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass
    return []


def _parse_bets(rows, market, over_under, threshold, bet_date, debug_count=3):
    """Extract qualifying bets using fixed Expanded column indices."""
    bets = []
    logged = 0
    for text in rows:
        if len(text) < COL_BOOKS_START + 1:
            continue

        bp_odds = _parse_odds_cell(text[COL_BP])

        # Debug: show raw data for first few rows
        if logged < debug_count:
            logger.info(f"    RAW ROW: cols={len(text)} | "
                        f"[0]={text[0]} [1]={text[1]} [2]={text[2]} "
                        f"[3]={text[3]} [4]={text[4] if len(text)>4 else 'N/A'} "
                        f"[5]={text[5] if len(text)>5 else 'N/A'} "
                        f"[6]={text[6] if len(text)>6 else 'N/A'}")
            if bp_odds is not None and len(text) > COL_BOOKS_START:
                dk_text = text[COL_BOOKS_START] if COL_BOOKS_START < len(text) else "N/A"
                dk_odds = _parse_odds_cell(dk_text)
                if dk_odds is not None:
                    delta = _calc_delta_pct(bp_odds, dk_odds)
                    bp_prob = _odds_to_implied_prob(bp_odds)
                    dk_prob = _odds_to_implied_prob(dk_odds)
                    logger.info(f"    CALC: BP={bp_odds} ({bp_prob:.1%}) DK={dk_odds} ({dk_prob:.1%}) delta={delta}%")
                else:
                    logger.info(f"    CALC: BP={bp_odds}, DK text='{dk_text}' (unparseable)")
            logged += 1

        if bp_odds is None:
            continue

        try:
            line = float(text[COL_LINE])
        except ValueError:
            continue

        team = text[COL_TEAM]
        player = text[COL_PLAYER]

        for i, book in enumerate(BOOKS):
            col_idx = COL_BOOKS_START + i
            if col_idx >= len(text):
                continue

            book_odds = _parse_odds_cell(text[col_idx])
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


def scrape_odds_screen(edge_threshold=None, headless=True, target_date=None):
    threshold = edge_threshold if edge_threshold is not None else config.EDGE_THRESHOLD
    if target_date:
        bet_date = target_date
    else:
        bet_date = date.today().isoformat()
    all_bets = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            from scraper import login
            login(page)

            odds_url = f"{config.BP_ODDS_URL}?date={bet_date}"
            logger.info(f"Navigating to Odds Screen for {bet_date}...")
            page.goto(odds_url, wait_until="networkidle", timeout=30000)

            if "login" in page.url.lower():
                login(page)
                page.goto(odds_url, wait_until="networkidle", timeout=30000)

            page.wait_for_selector("table", timeout=30000, state="visible")
            time.sleep(2)

            # Click Expanded once (stays across market changes)
            _click_button(page, "Expanded")
            time.sleep(2)
            page.wait_for_load_state("networkidle", timeout=10000)

            # Debug: log first row to verify columns
            test_rows = _scrape_table(page)
            if test_rows:
                logger.info(f"First row sample: {test_rows[0][:6]}")

            for i, market in enumerate(MARKETS):
                logger.info(f"[{i+1}/{len(MARKETS)}] {market}")

                try:
                    if not _select_market(page, market):
                        continue

                    time.sleep(2)
                    try:
                        page.wait_for_load_state("load", timeout=15000)
                        page.wait_for_load_state("networkidle", timeout=10000)
                    except Exception:
                        pass
                    try:
                        page.wait_for_selector("table", timeout=10000, state="visible")
                    except Exception:
                        logger.info(f"  No table, skipping")
                        continue

                    # ── OVER ──
                    _click_button(page, "Over")
                    _wait_and_settle(page)

                    rows = _scrape_table(page)
                    over_bets = _parse_bets(rows, market, "O", threshold, bet_date)
                    logger.info(f"  Over: {len(rows)} rows, {len(over_bets)} bets")
                    all_bets.extend(over_bets)

                    # ── UNDER ──
                    _click_button(page, "Under")
                    _wait_and_settle(page)

                    rows = _scrape_table(page)
                    under_bets = _parse_bets(rows, market, "U", threshold, bet_date)
                    logger.info(f"  Under: {len(rows)} rows, {len(under_bets)} bets")
                    all_bets.extend(under_bets)

                except Exception as e:
                    logger.warning(f"  Error on {market}, skipping: {e}")
                    continue

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
