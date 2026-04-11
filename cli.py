#!/usr/bin/env python3
"""
BP-Delta CLI: Ballpark Pal bet tracker.

Commands:
    analyze         Scrape BP for DK bets with positive edge
    grade           Grade pending bets + auto-print stock reports
    report          Custom profitability report (date range, bands, markets)
"""

import logging
from datetime import date, datetime, timedelta

import click

import config
from scraper import scrape_bets, ScraperError
from sheets import (
    append_bets,
    get_pending_bets,
    batch_update_results,
    get_all_completed_bets,
    get_bets_in_range,
)
from results import check_results, ResultsError


def setup_logging(verbose):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[logging.StreamHandler()],
    )


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable debug logging")
def cli(verbose):
    """BP-Delta: Ballpark Pal bet tracker."""
    setup_logging(verbose)


# ─── ANALYZE (scrape BP for edges) ───────────────────────────────


@cli.command()
@click.option("--threshold", "-t", type=float, default=None,
              help=f"Minimum edge %% (default: {config.EDGE_THRESHOLD})")
@click.option("--dry-run", is_flag=True,
              help="Scrape and display bets without writing to sheet")
@click.option("--no-headless", is_flag=True,
              help="Show browser window (for debugging)")
def analyze(threshold, dry_run, no_headless):
    """Scrape BP for today's DK bets with positive edge."""
    t = threshold or config.EDGE_THRESHOLD
    click.echo(f"Scraping BP for DK bets with delta% >= {t}%...")

    try:
        bets = scrape_bets(edge_threshold=t, headless=not no_headless)
    except ScraperError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    if not bets:
        click.echo("No qualifying bets found.")
        return

    # Display found bets
    click.echo(f"\n{'Player':<20} {'Market':<20} {'O/U':<4} {'Line':<6} "
               f"{'Odds':<6} {'Delta%':<8} {'Team':<5}")
    click.echo("-" * 75)
    for b in bets:
        click.echo(f"{b['player']:<20} {b['market']:<20} {b['over_under']:<4} "
                   f"{b['line']:<6} {b['odds']:<6} {b['delta_pct']:<8.1f} "
                   f"{b['team']:<5}")
    click.echo(f"\n{len(bets)} bets found.")

    if dry_run:
        click.echo("(Dry run - not writing to sheet)")
        return

    added = append_bets(bets)
    click.echo(f"{added} new bets written to Google Sheet.")


# ─── GRADE (check results + auto stock reports) ─────────────────


@cli.command()
@click.option("--date", "-d", "game_date", default=None,
              help="Grade a specific date (YYYY-MM-DD or 'yesterday')")
def grade(game_date):
    """Grade pending bets and print stock reports."""
    click.echo("Fetching pending bets from sheet...")

    pending = get_pending_bets()

    # Filter by date if specified
    if game_date:
        if game_date.lower() == "yesterday":
            target = (date.today() - timedelta(days=1)).isoformat()
        else:
            target = game_date
        pending = [(row, bet) for row, bet in pending if bet["date"] == target]
        click.echo(f"Filtering to {target}: {len(pending)} pending bets.")

    if not pending:
        click.echo("No pending bets to check.")
        return

    click.echo(f"Checking results for {len(pending)} pending bets...")

    try:
        updates = check_results(pending)
    except ResultsError as e:
        click.echo(f"Error: {e}", err=True)
        raise SystemExit(1)

    if not updates:
        click.echo("No games have completed yet.")
        return

    batch_update_results(updates)

    wins = sum(1 for _, r, _ in updates if r == "W")
    losses = sum(1 for _, r, _ in updates if r == "L")
    pushes = sum(1 for _, r, _ in updates if r == "P")
    total_profit = sum(p for _, _, p in updates)

    click.echo(f"\nGraded {len(updates)} bets: {wins}W-{losses}L-{pushes}P")
    click.echo(f"Session P/L: ${total_profit:+,.2f}")

    # ── Auto stock reports after grading ──
    _run_stock_reports()


def _run_stock_reports():
    """Print the stock reports: today, last 7 days, all time, by band."""
    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # 1. Today's results
    today_bets = get_bets_in_range(today.isoformat(), today.isoformat())
    today_completed = [b for b in today_bets if b.get("result") in ("W", "L", "P")]
    if today_completed:
        _print_report(today_completed, f"Today ({today.isoformat()})", by_band=False)

    # 2. Yesterday's results
    yest_bets = get_bets_in_range(yesterday.isoformat(), yesterday.isoformat())
    yest_completed = [b for b in yest_bets if b.get("result") in ("W", "L", "P")]
    if yest_completed:
        _print_report(yest_completed, f"Yesterday ({yesterday.isoformat()})", by_band=False)

    # 3. Last 7 days
    week_bets = get_bets_in_range(week_ago.isoformat(), today.isoformat())
    week_completed = [b for b in week_bets if b.get("result") in ("W", "L", "P")]
    if week_completed:
        _print_report(week_completed, f"Last 7 Days ({week_ago} to {today})", by_band=True)

    # 4. All time with bands
    all_bets = get_all_completed_bets()
    if all_bets:
        _print_report(all_bets, "All Time", by_band=True)


# ─── REPORT (custom) ────────────────────────────────────────────


@cli.command()
@click.option("--start", "-s", default=None,
              help="Start date (YYYY-MM-DD)")
@click.option("--end", "-e", default=None,
              help="End date (YYYY-MM-DD)")
@click.option("--by-band", is_flag=True,
              help="Break down by edge percentage band")
@click.option("--by-market", is_flag=True,
              help="Break down by market/prop type")
@click.option("--last-week", is_flag=True,
              help="Report for the last 7 days")
@click.option("--today", "show_today", is_flag=True,
              help="Report for today only")
@click.option("--yesterday", "show_yesterday", is_flag=True,
              help="Report for yesterday only")
@click.option("--all", "show_all", is_flag=True,
              help="Full stock report (today, yesterday, last week, all time + bands)")
def report(start, end, by_band, by_market, last_week, show_today, show_yesterday, show_all):
    """Generate profitability report."""
    today = date.today()

    # --all: run the full stock report suite
    if show_all:
        _run_stock_reports()
        return

    # Shortcut flags
    if show_today:
        start = today.isoformat()
        end = today.isoformat()
    elif show_yesterday:
        yest = today - timedelta(days=1)
        start = yest.isoformat()
        end = yest.isoformat()
    elif last_week:
        start = (today - timedelta(days=7)).isoformat()
        end = today.isoformat()
        by_band = True  # always show bands for week view

    if start and end:
        bets = get_bets_in_range(start, end)
        date_label = f"{start} to {end}"
    else:
        bets = get_all_completed_bets()
        date_label = "All Time"
        by_band = True  # always show bands for all time

    completed = [b for b in bets if b.get("result") in ("W", "L", "P")]

    if not completed:
        click.echo("No completed bets found for the given range.")
        return

    _print_report(completed, date_label, by_band=by_band, by_market=by_market)


# ─── Report printer ─────────────────────────────────────────────


def _print_report(bets, date_label, by_band=False, by_market=True):
    """Print a formatted profitability report."""
    bet_size = config.BET_SIZE

    wins = sum(1 for b in bets if b["result"] == "W")
    losses = sum(1 for b in bets if b["result"] == "L")
    pushes = sum(1 for b in bets if b["result"] == "P")
    total = len(bets)

    total_profit = 0
    for b in bets:
        try:
            total_profit += float(b["profit"])
        except (ValueError, TypeError):
            pass

    wagered = total * bet_size
    win_pct = (wins / (wins + losses) * 100) if (wins + losses) > 0 else 0
    roi = (total_profit / wagered * 100) if wagered > 0 else 0

    click.echo(f"\n{'=' * 55}")
    click.echo(f"  {date_label}")
    click.echo(f"{'=' * 55}")
    click.echo(f"  Total Bets:    {total}")
    click.echo(f"  Record:        {wins}-{losses}-{pushes} ({win_pct:.1f}%)")
    click.echo(f"  Total Wagered: ${wagered:,.2f}")
    click.echo(f"  Total Profit:  {'+'if total_profit >= 0 else ''}"
               f"${total_profit:,.2f}")
    click.echo(f"  ROI:           {roi:+.1f}%")

    if by_band:
        click.echo(f"\n  {'Edge Band':<14} {'Record':<12} {'Win%':<8} "
                   f"{'Profit':<12} {'ROI':<8}")
        click.echo(f"  {'-' * 52}")

        for low, high, label in config.EDGE_BANDS:
            band_bets = _filter_by_delta(bets, low, high)
            if not band_bets:
                continue
            _print_breakdown_line(band_bets, label, bet_size)

    if by_market:
        click.echo(f"\n  {'Market':<22} {'Record':<12} {'Win%':<8} "
                   f"{'Profit':<12} {'ROI':<8}")
        click.echo(f"  {'-' * 60}")

        markets = {}
        for b in bets:
            m = b.get("market", "Unknown")
            if m not in markets:
                markets[m] = []
            markets[m].append(b)

        for market, mbets in sorted(markets.items()):
            _print_breakdown_line(mbets, market, bet_size, label_width=22)

    click.echo()


def _filter_by_delta(bets, low, high):
    """Filter bets by delta% range."""
    result = []
    for b in bets:
        try:
            dpct = float(b["delta_pct"])
        except (ValueError, TypeError):
            continue
        if low <= dpct <= high:
            result.append(b)
    return result


def _print_breakdown_line(bets, label, bet_size, label_width=14):
    """Print a single breakdown line (used for bands and markets)."""
    bw = sum(1 for b in bets if b["result"] == "W")
    bl = sum(1 for b in bets if b["result"] == "L")
    bp = sum(1 for b in bets if b["result"] == "P")
    bt = len(bets)

    bprofit = 0
    for b in bets:
        try:
            bprofit += float(b["profit"])
        except (ValueError, TypeError):
            pass

    bwagered = bt * bet_size
    bwin_pct = (bw / (bw + bl) * 100) if (bw + bl) > 0 else 0
    broi = (bprofit / bwagered * 100) if bwagered > 0 else 0

    record = f"{bw}-{bl}-{bp}"
    click.echo(f"  {label:<{label_width}} {record:<12} {bwin_pct:<8.1f} "
               f"{'+'if bprofit >= 0 else ''}"
               f"${bprofit:<11,.2f} {broi:+.1f}%")


if __name__ == "__main__":
    cli()
