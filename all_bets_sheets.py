"""
Google Sheets integration for All Bets (odds screen scraper).
Separate sheet from the DK-only Positive EV tracker.
"""

import logging
import json
import gspread
from datetime import date, timedelta

import config

logger = logging.getLogger(__name__)

HEADERS = [
    "Date", "Team", "Player", "Market", "O/U", "Line",
    "Book Odds", "BP Odds", "Book", "Delta%", "Exp Profit",
    "Result", "Actual Profit",
]

COL_EXP_PROFIT = 11
COL_RESULT = 12
COL_PROFIT = 13


def _get_client():
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request

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

    return gspread.authorize(creds)


def get_sheet():
    client = _get_client()
    spreadsheet = client.open_by_key(config.ALL_BETS_SHEET_ID)

    try:
        worksheet = spreadsheet.worksheet(config.ALL_BETS_TAB)
    except gspread.exceptions.WorksheetNotFound:
        worksheet = spreadsheet.add_worksheet(
            title=config.ALL_BETS_TAB, rows=5000, cols=len(HEADERS)
        )
        worksheet.append_row(HEADERS)

    first_row = worksheet.row_values(1)
    if not first_row:
        worksheet.append_row(HEADERS)

    return worksheet


def bet_to_row(bet):
    return [
        bet["date"],
        bet["team"],
        bet["player"],
        bet["market"],
        bet["over_under"],
        bet["line"],
        bet["odds"],
        bet["bp_odds"],
        bet["book"],
        bet["delta_pct"],
        bet.get("exp_profit", ""),
        bet.get("result", ""),
        bet.get("profit", ""),
    ]


def row_to_bet(row):
    padded = row + [""] * (len(HEADERS) - len(row))
    return {
        "date": padded[0],
        "team": padded[1],
        "player": padded[2],
        "market": padded[3],
        "over_under": padded[4],
        "line": padded[5],
        "odds": padded[6],
        "bp_odds": padded[7],
        "book": padded[8],
        "delta_pct": padded[9],
        "exp_profit": padded[10],
        "result": padded[11],
        "profit": padded[12],
    }


def _make_key(bet):
    return (
        str(bet["date"]),
        str(bet["player"]),
        str(bet["market"]),
        str(bet["over_under"]),
        str(bet["line"]),
        str(bet["book"]),
    )


def append_bets(bets):
    worksheet = get_sheet()
    existing_rows = worksheet.get_all_values()[1:]

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
        logger.info(f"Appended {len(new_rows)} new bets to All Bets sheet.")
    else:
        logger.info("No new bets to append (all duplicates).")

    return len(new_rows)


def get_pending_bets():
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    pending = []
    for i, row in enumerate(all_rows[1:], start=2):
        bet = row_to_bet(row)
        if not bet["result"]:
            pending.append((i, bet))

    return pending


def batch_update_results(updates):
    if not updates:
        return

    worksheet = get_sheet()
    cells = []
    for row_num, result, profit in updates:
        cells.append(gspread.Cell(row_num, COL_RESULT, result))
        cells.append(gspread.Cell(row_num, COL_PROFIT, f"{profit:+.2f}"))

    worksheet.update_cells(cells)
    logger.info(f"Updated results for {len(updates)} bets.")


def get_all_completed_bets():
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    completed = []
    for row in all_rows[1:]:
        bet = row_to_bet(row)
        if bet["result"] in ("W", "L", "P"):
            completed.append(bet)

    return completed


def get_bets_in_range(start_date, end_date):
    worksheet = get_sheet()
    all_rows = worksheet.get_all_values()

    in_range = []
    for row in all_rows[1:]:
        bet = row_to_bet(row)
        if start_date <= bet["date"] <= end_date:
            in_range.append(bet)

    return in_range


def _calc_stats(bets):
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


def _parse_odds(bet):
    try:
        return int(bet["odds"])
    except (ValueError, TypeError):
        return None


def update_reports_tab():
    client = _get_client()
    spreadsheet = client.open_by_key(config.ALL_BETS_SHEET_ID)

    try:
        reports = spreadsheet.worksheet("Reports")
        reports.clear()
    except gspread.exceptions.WorksheetNotFound:
        reports = spreadsheet.add_worksheet(title="Reports", rows=500, cols=10)

    tracked = spreadsheet.worksheet(config.ALL_BETS_TAB)
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
    rows.append([f"All Bets Reports - Updated {today.isoformat()}"])
    rows.append([])

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
                label, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])
        else:
            rows.append([label, 0, "'0-0-0", "0%", "$0", "$0", "$0", "$0", "0%"])

    # Edge bands
    rows.append([])
    rows.append(["EDGE BANDS"])
    rows.append(header)
    for low, high, label in config.EDGE_BANDS:
        band = [b for b in completed
                if _try_float(b, "delta_pct") is not None and low <= _try_float(b, "delta_pct") <= high]
        s = _calc_stats(band)
        if s:
            rows.append([
                label, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # By sportsbook
    rows.append([])
    rows.append(["BY SPORTSBOOK"])
    rows.append(header)
    books = {}
    for b in completed:
        bk = b.get("book", "Unknown")
        if bk not in books:
            books[bk] = []
        books[bk].append(b)
    for book, bbets in sorted(books.items()):
        s = _calc_stats(bbets)
        if s:
            rows.append([
                book, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # Sportsbook by band
    rows.append([])
    rows.append(["SPORTSBOOK BY EDGE BAND"])
    rows.append(["Book", "Band", "Bets", "Record", "Win%",
                 "Expected", "Actual", "vs Expected", "ROI"])
    for book, bbets in sorted(books.items()):
        for low, high, label in config.EDGE_BANDS:
            band = [b for b in bbets
                    if _try_float(b, "delta_pct") is not None and low <= _try_float(b, "delta_pct") <= high]
            s = _calc_stats(band)
            if s:
                rows.append([
                    book, label, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                    f"{s['win_pct']:.1f}%",
                    f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                    f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
                ])

    # Sportsbook by market
    rows.append([])
    rows.append(["SPORTSBOOK BY MARKET"])
    rows.append(["Book", "Market", "Bets", "Record", "Win%",
                 "Expected", "Actual", "vs Expected", "ROI"])
    for book, bbets in sorted(books.items()):
        markets = {}
        for b in bbets:
            m = b.get("market", "Unknown")
            if m not in markets:
                markets[m] = []
            markets[m].append(b)
        for market, mbets in sorted(markets.items()):
            s = _calc_stats(mbets)
            if s:
                rows.append([
                    book, market, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                    f"{s['win_pct']:.1f}%",
                    f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                    f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
                ])

    # By market
    rows.append([])
    rows.append(["BY MARKET"])
    rows.append(header)
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
                market, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # Odds bands
    rows.append([])
    rows.append(["BY ODDS RANGE"])
    rows.append(header)
    for low, high, label in config.ODDS_BANDS:
        if low >= 0:
            odds_bets = [b for b in completed
                         if _parse_odds(b) is not None and low <= _parse_odds(b) <= high]
        else:
            odds_bets = [b for b in completed
                         if _parse_odds(b) is not None and high <= _parse_odds(b) <= low]
        s = _calc_stats(odds_bets)
        if s:
            rows.append([
                label, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    # By date
    rows.append([])
    rows.append(["BY DATE"])
    rows.append(header)
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
                d, s["total"], f"'{s['wins']}-{s['losses']}-{s['pushes']}",
                f"{s['win_pct']:.1f}%", f"${s['wagered']:,.2f}",
                f"${s['exp']:+,.2f}", f"${s['actual']:+,.2f}",
                f"${s['diff']:+,.2f}", f"{s['roi']:+.1f}%",
            ])

    max_col = max(len(r) for r in rows)
    for r in rows:
        while len(r) < max_col:
            r.append("")

    reports.update(f"A1:{chr(64+max_col)}{len(rows)}", rows, value_input_option="USER_ENTERED")
    logger.info("All Bets Reports tab updated.")


def _try_float(bet, key):
    try:
        return float(bet[key])
    except (ValueError, TypeError):
        return None
