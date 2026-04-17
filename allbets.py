#!/usr/bin/env python3
"""
All Bets CLI: Scrape BP Odds Screen across all markets and all 6 sportsbooks.

Commands:
    scan            Scrape odds screen for all books with 5%+ edge
    grade           Grade pending bets
    report          Profitability reports
"""

import logging
from datetime import date, timedelta

import click

import config
from odds_scraper import scrape_odds_screen
from all_bets_sheets import (
    append_bets,
    get_pending_bets,
    batch_update_results,
    get_all_completed_bets,
    get_bets_in_range,
    update_reports_tab,
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
    """All Bets: BP Odds Screen tracker across all sportsbooks."""
    setup_logging(verbose)


@cli.command()
@click.option("--threshold", "-t", type=float, default=None,
              help=f"Minimum edge %% (default: {config.EDGE_THRESHOLD})")
@click.option("--dry-run", is_flag=True)
@click.option("--no-headless", is_flag=True)
@click.option("--tomorrow", is_flag=True, help="Scan tomorrow's lines instead of today")
@click.option("--date", "scan_date", default=None, help="Scan a specific date (YYYY-MM-DD)")
def scan(threshold, dry_run, no_headless, tomorrow, scan_date):
    """Scrape odds screen for bets with 5%+ edge across all books."""
    t = threshold or config.EDGE_THRESHOLD

    if scan_date:
        target = scan_date
    elif tomorrow:
        target = (date.today() + timedelta(days=1)).isoformat()
    else:
        target = date.today().isoformat()

    click.echo(f"Scanning odds screen for {target}, delta% >= {t}%...")

    bets = scrape_odds_screen(edge_threshold=t, headless=not no_headless, target_date=target)

    if not bets:
        click.echo("No qualifying bets found.")
        return

    click.echo(f"\n{'Player':<20} {'Market':<20} {'O/U':<4} {'Line':<6} "
               f"{'Book':<5} {'Odds':<6} {'BP':<6} {'Delta%':<8} {'Exp $':<8}")
    click.echo("-" * 90)

    total_exp = 0
    for b in bets:
        exp = b.get("exp_profit", 0)
        total_exp += exp
        click.echo(f"{b['player']:<20} {b['market']:<20} {b['over_under']:<4} "
                   f"{b['line']:<6} {b['book']:<5} {b['odds']:<6} {b['bp_odds']:<6} "
                   f"{b['delta_pct']:<8.1f} ${exp:<7.2f}")

    click.echo(f"\n{len(bets)} bets found. Total expected profit: ${total_exp:+,.2f}")

    # Show count by book
    book_counts = {}
    for b in bets:
        bk = b["book"]
        book_counts[bk] = book_counts.get(bk, 0) + 1
    click.echo("\nBy book: " + ", ".join(f"{k}:{v}" for k, v in sorted(book_counts.items())))

    if dry_run:
        click.echo("(Dry run - not writing to sheet)")
        return

    added = append_bets(bets)
    click.echo(f"{added} new bets written to All Bets sheet.")


@cli.command()
@click.option("--date", "-d", "game_date", default=None)
def grade(game_date):
    """Grade pending bets against actual MLB box scores."""
    click.echo("Fetching pending bets...")

    pending = get_pending_bets()

    if game_date:
        if game_date.lower() == "yesterday":
            target = (date.today() - timedelta(days=1)).isoformat()
        else:
            target = game_date
        pending = [(row, bet) for row, bet in pending if bet["date"] == target]

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

    click.echo("Updating Reports tab...")
    update_reports_tab()
    click.echo("Reports tab updated.")


@cli.command()
@click.option("--all", "show_all", is_flag=True)
def report(show_all):
    """Update the Reports tab in Google Sheet."""
    click.echo("Updating Reports tab...")
    update_reports_tab()
    click.echo("Done.")


if __name__ == "__main__":
    cli()
