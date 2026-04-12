"""
Google Sheets integration for storing and retrieving tracked bets.

Sheet layout ("Tracked Bets" tab):
Date | Team | Player | Market | O/U | Line | Odds | CS% | Delta | BP | Delta% | Exp Profit | Result | Actual Profit
"""

import logging
import os
import gspread

import config

logger = logging.getLogger(__name__)

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
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import json

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    with open(config.GOOGLE_TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=SCOPES,
    )

    if creds.expired:
        creds.refresh(Request())
        # Save refreshed token
        token_data["token"] = creds.token
        with open(config.GOOGLE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f)

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


def _get_spreadsheet():
    """Get the spreadsheet object (not just the worksheet)."""
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    import json

    SCOPES = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    with open(config.GOOGLE_TOKEN_FILE) as f:
        token_data = json.load(f)

    creds = Credentials(
        token=token_data["token"],
        refresh_token=token_data["refresh_token"],
        token_uri=token_data["token_uri"],
        client_id=token_data["client_id"],
        client_secret=token_data["client_secret"],
        scopes=SCOPES,
    )

    if creds.expired:
        creds.refresh(Request())
        token_data["token"] = creds.token
        with open(config.GOOGLE_TOKEN_FILE, "w") as f:
            json.dump(token_data, f)

    client = gspread.authorize(creds)
    return client.open_by_key(config.GOOGLE_SHEETS_ID)


def _calc_stats(bets):
    """Calculate stats for a list of completed bets."""
    if not bets:
        return None
    wins = sum(1 for b in bets if b["result"] == "W")
    losses = sum(1 for b in bets if b["result"] == "L")
    pushes = sum(1 for b in bets if b["result"] == "P")
    total = len(bets)
    exp = 0
    actual = 0
    for b in bets:
        try:
            exp += float(b.get("exp_profit", 0))
        except (ValueError, TypeError):
            pass
        try:
            actual += float(b.get("profit", 0))
        except (ValueError, TypeError):
            pass
    wagered = total * config.BET_SIZE
    win_pct = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    roi = (actual / wagered * 100) if wagered > 0 else 0
    return {
        "total": total, "wins": wins, "losses": losses, "pushes": pushes,
        "win_pct": win_pct, "exp": exp, "actual": actual,
        "diff": actual - exp, "wagered": wagered, "roi": roi,
    }


def update_reports_tab():
    """Write reports to a 'Reports' tab in the Google Sheet."""
    from datetime import date, timedelta

    spreadsheet = _get_spreadsheet()

    # Get or create Reports tab
    try:
        reports = spreadsheet.worksheet("Reports")
        reports.clear()
    except gspread.exceptions.WorksheetNotFound:
        reports = spreadsheet.add_worksheet(title="Reports", rows=200, cols=10)

    # Get all bets from Tracked Bets tab
    tracked = spreadsheet.worksheet(config.TRACKED_BETS_TAB)
    all_rows = tracked.get_all_values()
    all_bets = [row_to_bet(row) for row in all_rows[1:]]
    completed = [b for b in all_bets if b.get("result") in ("W", "L", "P")]

    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    today_bets = [b for b in completed if b["date"] == today.isoformat()]
    yest_bets = [b for b in completed if b["date"] == yesterday.isoformat()]
    week_bets = [b for b in completed if week_ago.isoformat() <= b["date"] <= today.isoformat()]

    rows = []
    rows.append([f"BP-Delta Reports - Updated {today.isoformat()}"])
    rows.append([])

    # Summary header
    header = ["Period", "Bets", "Record", "Win%", "Wagered",
              "Expected", "Actual", "vs Expected", "ROI"]
    rows.append(header)

    for label, bets in [
        (f"Today ({today})", today_bets),
        (f"Yesterday ({yesterday})", yest_bets),
        ("Last 7 Days", week_bets),
        ("All Time", completed),
    ]:
        s = _calc_stats(bets)
        if s:
            rows.append([
                label, s["total"], f"{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])
        else:
            rows.append([label, 0, "0-0-0", "0%", "$0", "$0", "$0", "$0", "0%"])

    # Edge bands
    rows.append([])
    rows.append(["EDGE BANDS"])
    rows.append(["Band", "Bets", "Record", "Win%", "Wagered",
                 "Expected", "Actual", "vs Expected", "ROI"])
    for low, high, label in config.EDGE_BANDS:
        band_bets = []
        for b in completed:
            try:
                dpct = float(b["delta_pct"])
            except (ValueError, TypeError):
                continue
            if low <= dpct <= high:
                band_bets.append(b)
        s = _calc_stats(band_bets)
        if s:
            rows.append([
                label, s["total"], f"{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # Market breakdown
    rows.append([])
    rows.append(["BY MARKET"])
    rows.append(["Market", "Bets", "Record", "Win%", "Wagered",
                 "Expected", "Actual", "vs Expected", "ROI"])
    markets = {}
    for b in completed:
        m = b.get("market", "Unknown")
        if m not in markets:
            markets[m] = []
        markets[m].append(b)
    for market, mbets in sorted(markets.items()):
        s = _calc_stats(mbets)
        if s:
            rows.append([
                market, s["total"], f"{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # By date
    rows.append([])
    rows.append(["BY DATE"])
    rows.append(["Date", "Bets", "Record", "Win%", "Wagered",
                 "Expected", "Actual", "vs Expected", "ROI"])
    dates = {}
    for b in completed:
        d = b["date"]
        if d not in dates:
            dates[d] = []
        dates[d].append(b)
    for d, dbets in sorted(dates.items(), reverse=True):
        s = _calc_stats(dbets)
        if s:
            rows.append([
                d, s["total"], f"{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    reports.update(f"A1:I{len(rows)}", rows, value_input_option="USER_ENTERED")
    logger.info("Reports tab updated.")
