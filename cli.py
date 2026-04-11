#!/usr/bin/env python3
"""
BP-Delta CLI: Ballpark Pal bet tracker.

Commands:
    scrape          Scrape BP for DK bets with positive edge
    results         Grade pending bets against actual MLB results
    report          Profitability report by date range and/or edge band
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


@cli.command()
@click.option("--threshold", "-t", type=float, default=None,
              help=f"Minimum edge %% (default: {config.EDGE_THRESHOLD})")
@click.option("--dry-run", is_flag=True,
              help="Scrape and display bets without writing to sheet")
@click.option("--no-headless", is_flag=True,
              help="Show browser window (for debugging)")
def scrape(threshold, dry_run, no_headless):
    """Scrape today's BP edges and log qualifying DK bets."""
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


@cli.command("results")
@click.option("--date", "-d", "game_date", default=None,
              help="Check results for a specific date (YYYY-MM-DD or 'yesterday')")
def check_results_cmd(game_date):
    """Grade pending bets against actual MLB box scores."""
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

    click.echo(f"\nUpdated {len(updates)} bets: {wins}W-{losses}L-{pushes}P")
    click.echo(f"Session P/L: {total_profit:+.2f}")


@cli.command()
@click.option("--start", "-s", default=None,
              help="Start date (YYYY-MM-DD)")
@click.option("--end", "-e", default=None,
              help="End date (YYYY-MM-DD)")
@click.option("--by-band", is_flag=True,
              help="Break down by edge percentage band")
def report(start, end, by_band):
    """Generate profitability report."""
    if start and end:
        bets = get_bets_in_range(start, end)
        date_label = f"{start} to {end}"
    else:
        bets = get_all_completed_bets()
        date_label = "All Time"

    # Filter to only completed bets
    completed = [b for b in bets if b.get("result") in ("W", "L", "P")]

    if not completed:
        click.echo("No completed bets found for the given range.")
        return

    _print_report(completed, date_label, by_band)


def _print_report(bets, date_label, by_band):
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
    click.echo(f"  Profitability Report: {date_label}")
    click.echo(f"{'=' * 55}")
    click.echo(f"  Total Bets:    {total}")
    click.echo(f"  Record:        {wins}-{losses}-{pushes} ({win_pct:.1f}%)")
    click.echo(f"  Total Wagered: ${wagered:,.2f}")
    click.echo(f"  Total Profit:  {'+' if total_profit >= 0 else ''}"
               f"${total_profit:,.2f}")
    click.echo(f"  ROI:           {roi:+.1f}%")

    if by_band:
        click.echo(f"\n  {'Edge Band':<14} {'Record':<12} {'Win%':<8} "
                   f"{'Profit':<12} {'ROI':<8}")
        click.echo(f"  {'-' * 52}")

        for low, high, label in config.EDGE_BANDS:
            band_bets = []
            for b in bets:
                try:
                    dpct = float(b["delta_pct"])
                except (ValueError, TypeError):
                    continue
                if low <= dpct <= high:
                    band_bets.append(b)

            if not band_bets:
                continue

            bw = sum(1 for b in band_bets if b["result"] == "W")
            bl = sum(1 for b in band_bets if b["result"] == "L")
            bp = sum(1 for b in band_bets if b["result"] == "P")
            bt = len(band_bets)

            bprofit = 0
            for b in band_bets:
                try:
                    bprofit += float(b["profit"])
                except (ValueError, TypeError):
                    pass

            bwagered = bt * bet_size
            bwin_pct = (bw / (bw + bl) * 100) if (bw + bl) > 0 else 0
            broi = (bprofit / bwagered * 100) if bwagered > 0 else 0

            record = f"{bw}-{bl}-{bp}"
            click.echo(f"  {label:<14} {record:<12} {bwin_pct:<8.1f} "
                       f"{'+'if bprofit >= 0 else ''}"
                       f"${bprofit:<11,.2f} {broi:+.1f}%")

    # Also show breakdown by market type
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
        mw = sum(1 for b in mbets if b["result"] == "W")
        ml = sum(1 for b in mbets if b["result"] == "L")
        mp = sum(1 for b in mbets if b["result"] == "P")
        mt = len(mbets)

        mprofit = 0
        for b in mbets:
            try:
                mprofit += float(b["profit"])
            except (ValueError, TypeError):
                pass

        mwagered = mt * bet_size
        mwin_pct = (mw / (mw + ml) * 100) if (mw + ml) > 0 else 0
        mroi = (mprofit / mwagered * 100) if mwagered > 0 else 0

        record = f"{mw}-{ml}-{mp}"
        click.echo(f"  {market:<22} {record:<12} {mwin_pct:<8.1f} "
                   f"{'+'if mprofit >= 0 else ''}"
                   f"${mprofit:<11,.2f} {mroi:+.1f}%")

    click.echo()


if __name__ == "__main__":
    cli()
