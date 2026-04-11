"""
Google Sheets integration for storing and retrieving tracked bets.

Sheet layout ("Tracked Bets" tab):
Date | Team | Player | Market | O/U | Line | Odds | CS% | Delta | BP | Delta% | Result | Profit
"""

import logging
import gspread
from google.oauth2.service_account import Credentials

import config

logger = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Date", "Team", "Player", "Market", "O/U", "Line",
    "Odds", "CS%", "Delta", "BP", "Delta%", "Exp Profit",
    "Result", "Actual Profit",
]

# Column indices in the sheet (1-based for gspread)
COL_EXP_PROFIT = 12
COL_RESULT = 13
COL_PROFIT = 14


def get_sheet():
    """Authenticate and return the Tracked Bets worksheet."""
    creds = Credentials.from_service_account_file(
        config.GOOGLE_CREDENTIALS_FILE, scopes=SCOPES
    )
    client = gspread.authorize(creds)
    spreadsheet = client.open_by_key(config.GOOGLE_SHEETS_ID)

    # Get or create the Tracked Bets tab
    try:
        worksheet = spreadsheet.worksheet(config.TRACKED_BETS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=config.TRACKED_BETS_TAB, rows=1000, cols=len(HEADERS)
        )
        worksheet.append_row(HEADERS)
        logger.info(f"Created '{config.TRACKED_BETS_TAB}' tab with headers.")

    # Ensure headers exist
    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(HEADERS)

    return worksheet


def bet_to_row(bet):
    """Convert a bet dict to a sheet row list."""
    return [
        bet["date"],
        bet["team"],
        bet["player"],
        bet["market"],
        bet["over_under"],
        bet["line"],
        bet["odds"],
        bet["cs_pct"],
        bet["delta"],
        bet["bp"],
        bet["delta_pct"],
        bet.get("exp_profit", ""),
        bet.get("result", ""),
        bet.get("profit", ""),
    ]


def row_to_bet(row):
    """Convert a sheet row list back to a bet dict."""
    # Pad row if it's shorter than expected
    padded = row + [""] * (len(HEADERS) - len(row))
    return {
        "date": padded[0],
        "team": padded[1],
        "player": padded[2],
        "market": padded[3],
        "over_under": padded[4],
        "line": padded[5],
        "odds": padded[6],
        "cs_pct": padded[7],
        "delta": padded[8],
        "bp": padded[9],
        "delta_pct": padded[10],
        "exp_profit": padded[11],
        "result": padded[12],
        "profit": padded[13],
    }


def _make_key(bet):
    """Create a dedup key from a bet dict."""
    return (
        str(bet["date"]),
        str(bet["player"]),
        str(bet["market"]),
        str(bet["over_under"]),
        str(bet["line"]),
    )


def append_bets(bets):
    """
    Append new bets to the sheet, skipping duplicates.

    Deduplicates by (date, player, market, over_under, line).

    Returns the number of new bets actually appended.
    """
    worksheet = get_sheet()
    existing_rows = worksheet.get_all_values()[1:]  # skip header

    existing_keys = set()
    for row in existing_rows:
        b = row_to_bet(row)
        existing_keys.add(_make_key(b))

    new_rows = []
    for bet in bets:
        key = _make_key(bet)
        if key not in existing_keys:
            new_rows.append(bet_to_row(bet))
            existing_keys.add(key)

    if new_rows:
        worksheet.append_rows(new_rows, value_input_option="USER_ENTERED")
        logger.info(f"Appended {len(new_rows)} new bets to sheet.")
    else:
        logger.info("No new bets to append (all duplicates).")

    return len(new_rows)


def get_pending_bets():
    """
    Get all bets with no result yet.

    Returns list of (row_number, bet_dict) tuples.
    row_number is 1-based (matching Google Sheets).
    """
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    pending = []
    for i, row in enumerate(all_rows[1:], start=2):  # row 1 is header
        bet = row_to_bet(row)
        if not bet["result"]:
            pending.append((i, bet))

    return pending


def update_result(worksheet, row_number, result, profit):
    """Update the Result and Profit columns for a specific row."""
    worksheet.update_cell(row_number, COL_RESULT, result)
    worksheet.update_cell(row_number, COL_PROFIT, profit)


def batch_update_results(updates):
    """
    Batch update results for multiple bets.

    Args:
        updates: list of (row_number, result_str, profit_float) tuples.
    """
    if not updates:
        return

    worksheet = get_sheet()

    # Build batch update cells
    cells = []
    for row_num, result, profit in updates:
        cells.append(gspread.Cell(row_num, COL_RESULT, result))
        cells.append(gspread.Cell(row_num, COL_PROFIT, f"{profit:+.2f}"))

    worksheet.update_cells(cells)
    logger.info(f"Updated results for {len(updates)} bets.")


def get_all_completed_bets():
    """Get all bets that have a result (W, L, or P)."""
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    completed = []
    for row in all_rows[1:]:
        bet = row_to_bet(row)
        if bet["result"] in ("W", "L", "P"):
            completed.append(bet)

    return completed


def get_bets_in_range(start_date, end_date):
    """Get all bets within a date range (inclusive)."""
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    in_range = []
    for row in all_rows[1:]:
        bet = row_to_bet(row)
        if start_date <= bet["date"] <= end_date:
            in_range.append(bet)

    return in_range
