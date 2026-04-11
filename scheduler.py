#!/usr/bin/env python3
"""
Scheduler for automated BP-Delta runs.

Can be used standalone (built-in loop) or with cron.

Cron examples (add to crontab with `crontab -e`):

    # Scrape edges at 12:30 PM ET daily (before most first pitches)
    30 12 * * * cd /path/to/BP-delta && /path/to/venv/bin/python cli.py scrape

    # Check results at 1:00 AM ET (after all games finish)
    0 1 * * * cd /path/to/BP-delta && /path/to/venv/bin/python cli.py results

Standalone usage:
    python scheduler.py --scrape-at 12:30 --results-at 01:00
"""

import argparse
import logging
import time
import sys
import os
import fcntl
from datetime import datetime, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

LOCK_FILE = Path("/tmp/bp_delta.lock")


def acquire_lock():
    """Acquire a file lock to prevent concurrent runs."""
    try:
        fd = open(LOCK_FILE, "w")
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fd.write(str(os.getpid()))
        fd.flush()
        # Keep fd open to hold the lock
        acquire_lock._fd = fd
        return True
    except (IOError, OSError):
        logger.warning("Another instance is already running.")
        return False


def release_lock():
    """Release the file lock."""
    if hasattr(acquire_lock, "_fd"):
        try:
            fcntl.flock(acquire_lock._fd, fcntl.LOCK_UN)
            acquire_lock._fd.close()
            LOCK_FILE.unlink(missing_ok=True)
        except Exception:
            pass


def run_scrape():
    """Run the scrape command."""
    from scraper import scrape_bets
    from sheets import append_bets

    logger.info("Running scheduled scrape...")
    bets = scrape_bets()
    if bets:
        added = append_bets(bets)
        logger.info(f"Scrape complete: {len(bets)} found, {added} new.")
    else:
        logger.info("Scrape complete: no qualifying bets found.")


def run_results():
    """Run the results check command."""
    from sheets import get_pending_bets, batch_update_results
    from results import check_results

    logger.info("Running scheduled results check...")
    pending = get_pending_bets()
    if not pending:
        logger.info("No pending bets to check.")
        return

    updates = check_results(pending)
    if updates:
        batch_update_results(updates)
        wins = sum(1 for _, r, _ in updates if r == "W")
        losses = sum(1 for _, r, _ in updates if r == "L")
        logger.info(f"Results: {len(updates)} graded, {wins}W-{losses}L")
    else:
        logger.info("No games finalized yet.")


def run_loop(scrape_time, results_time):
    """
    Run a continuous loop, executing scrape and results at specified times.

    Args:
        scrape_time: "HH:MM" string for daily scrape time.
        results_time: "HH:MM" string for daily results check time.
    """
    scrape_h, scrape_m = map(int, scrape_time.split(":"))
    results_h, results_m = map(int, results_time.split(":"))

    last_scrape_date = None
    last_results_date = None

    logger.info(f"Scheduler started. Scrape at {scrape_time}, results at {results_time}")

    while True:
        now = datetime.now()
        today = now.date()

        # Check if it's time to scrape
        if (now.hour == scrape_h and now.minute == scrape_m
                and last_scrape_date != today):
            try:
                run_scrape()
                last_scrape_date = today
            except Exception:
                logger.exception("Scrape failed")

        # Check if it's time to check results
        if (now.hour == results_h and now.minute == results_m
                and last_results_date != today):
            try:
                run_results()
                last_results_date = today
            except Exception:
                logger.exception("Results check failed")

        time.sleep(30)  # Check every 30 seconds


def main():
    parser = argparse.ArgumentParser(description="BP-Delta Scheduler")
    parser.add_argument("--scrape-at", default="12:30",
                        help="Time to scrape (HH:MM, default 12:30)")
    parser.add_argument("--results-at", default="01:00",
                        help="Time to check results (HH:MM, default 01:00)")
    parser.add_argument("--run-once", choices=["scrape", "results"],
                        help="Run a single task and exit")
    args = parser.parse_args()

    if not acquire_lock():
        sys.exit(1)

    try:
        if args.run_once == "scrape":
            run_scrape()
        elif args.run_once == "results":
            run_results()
        else:
            run_loop(args.scrape_at, args.results_at)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
