#!/usr/bin/env python3
"""
Scheduler for automated BP-Delta runs.

Schedule:
    6:00 AM  - Grade yesterday's bets + scrape today's edges + stock reports
    10:00 PM - Scrape next day's edges

Can be used standalone (built-in loop) or with cron.

Cron examples (add to crontab with `crontab -e`):

    # 6 AM: grade yesterday + analyze today + reports
    0 6 * * * cd /path/to/BP-delta && /path/to/venv/bin/python cli.py grade --date yesterday
    5 6 * * * cd /path/to/BP-delta && /path/to/venv/bin/python cli.py analyze

    # 10 PM: analyze for next day
    0 22 * * * cd /path/to/BP-delta && /path/to/venv/bin/python cli.py analyze

Standalone usage:
    python scheduler.py
    python scheduler.py --morning 06:00 --evening 22:00
    python scheduler.py --run-once morning
    python scheduler.py --run-once evening
"""

import argparse
import logging
import time
import sys
import os
import fcntl
from datetime import datetime, date, timedelta
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


def run_analyze():
    """Scrape BP for today's DK positive EV bets."""
    from scraper import scrape_bets
    from sheets import append_bets

    logger.info("Running analysis...")
    bets = scrape_bets()
    if bets:
        added = append_bets(bets)
        logger.info(f"Analysis complete: {len(bets)} found, {added} new.")
    else:
        logger.info("Analysis complete: no qualifying bets found.")


def run_grade():
    """Grade pending bets and print stock reports."""
    from sheets import (
        get_pending_bets, batch_update_results,
        get_all_completed_bets, get_bets_in_range,
    )
    from results import check_results

    logger.info("Running grading...")
    pending = get_pending_bets()
    if not pending:
        logger.info("No pending bets to grade.")
        return

    updates = check_results(pending)
    if updates:
        batch_update_results(updates)
        wins = sum(1 for _, r, _ in updates if r == "W")
        losses = sum(1 for _, r, _ in updates if r == "L")
        pushes = sum(1 for _, r, _ in updates if r == "P")
        total_profit = sum(p for _, _, p in updates)
        logger.info(
            f"Graded {len(updates)} bets: {wins}W-{losses}L-{pushes}P "
            f"(${total_profit:+,.2f})"
        )
    else:
        logger.info("No games finalized yet.")

    # Stock reports
    _log_stock_reports()


def _log_stock_reports():
    """Log the stock reports after grading."""
    from sheets import get_all_completed_bets, get_bets_in_range
    import config

    today = date.today()
    yesterday = today - timedelta(days=1)
    week_ago = today - timedelta(days=7)

    # Yesterday
    yest_bets = [
        b for b in get_bets_in_range(yesterday.isoformat(), yesterday.isoformat())
        if b.get("result") in ("W", "L", "P")
    ]
    if yest_bets:
        _log_summary(f"Yesterday ({yesterday})", yest_bets)

    # Last 7 days
    week_bets = [
        b for b in get_bets_in_range(week_ago.isoformat(), today.isoformat())
        if b.get("result") in ("W", "L", "P")
    ]
    if week_bets:
        _log_summary(f"Last 7 Days", week_bets)
        _log_bands(week_bets)

    # All time
    all_bets = get_all_completed_bets()
    if all_bets:
        _log_summary("All Time", all_bets)
        _log_bands(all_bets)


def _sum_profit(bets, key):
    """Sum a profit field safely."""
    total = 0
    for b in bets:
        try:
            total += float(b.get(key, 0))
        except (ValueError, TypeError):
            pass
    return total


def _log_summary(label, bets):
    """Log a quick summary line with expected vs actual."""
    import config
    wins = sum(1 for b in bets if b["result"] == "W")
    losses = sum(1 for b in bets if b["result"] == "L")
    pushes = sum(1 for b in bets if b["result"] == "P")
    exp = _sum_profit(bets, "exp_profit")
    actual = _sum_profit(bets, "profit")
    diff = actual - exp
    wagered = len(bets) * config.BET_SIZE
    roi = (actual / wagered * 100) if wagered > 0 else 0
    logger.info(
        f"[{label}] {wins}-{losses}-{pushes} | "
        f"Exp: ${exp:+,.2f} | Actual: ${actual:+,.2f} | "
        f"vs Exp: ${diff:+,.2f} | ROI: {roi:+.1f}%"
    )


def _log_bands(bets):
    """Log edge band breakdown with expected vs actual."""
    import config
    for low, high, label in config.EDGE_BANDS:
        band = [b for b in bets
                if _parse_delta(b) is not None and low <= _parse_delta(b) <= high]
        if not band:
            continue
        w = sum(1 for b in band if b["result"] == "W")
        l = sum(1 for b in band if b["result"] == "L")
        exp = _sum_profit(band, "exp_profit")
        actual = _sum_profit(band, "profit")
        logger.info(f"  {label}: {w}-{l} | Exp: ${exp:+,.2f} | Actual: ${actual:+,.2f}")


def _parse_delta(bet):
    try:
        return float(bet["delta_pct"])
    except (ValueError, TypeError):
        return None


def run_morning():
    """6 AM job: grade yesterday + analyze today + stock reports."""
    logger.info("=== MORNING RUN (6 AM) ===")
    run_grade()
    run_analyze()
    logger.info("=== MORNING RUN COMPLETE ===")


def run_evening():
    """10 PM job: analyze for next day's edges."""
    logger.info("=== EVENING RUN (10 PM) ===")
    run_analyze()
    logger.info("=== EVENING RUN COMPLETE ===")


def run_loop(morning_time, evening_time):
    """
    Run a continuous loop with the two daily jobs.

    Args:
        morning_time: "HH:MM" for the morning run (default "06:00").
        evening_time: "HH:MM" for the evening run (default "22:00").
    """
    morning_h, morning_m = map(int, morning_time.split(":"))
    evening_h, evening_m = map(int, evening_time.split(":"))

    last_morning_date = None
    last_evening_date = None

    logger.info(
        f"Scheduler started. Morning at {morning_time}, evening at {evening_time}"
    )

    while True:
        now = datetime.now()
        today = now.date()

        # Morning run
        if (now.hour == morning_h and now.minute == morning_m
                and last_morning_date != today):
            try:
                run_morning()
                last_morning_date = today
            except Exception:
                logger.exception("Morning run failed")

        # Evening run
        if (now.hour == evening_h and now.minute == evening_m
                and last_evening_date != today):
            try:
                run_evening()
                last_evening_date = today
            except Exception:
                logger.exception("Evening run failed")

        time.sleep(30)


def main():
    parser = argparse.ArgumentParser(description="BP-Delta Scheduler")
    parser.add_argument("--morning", default="06:00",
                        help="Morning run time (HH:MM, default 06:00)")
    parser.add_argument("--evening", default="22:00",
                        help="Evening run time (HH:MM, default 22:00)")
    parser.add_argument("--run-once",
                        choices=["morning", "evening", "analyze", "grade"],
                        help="Run a single job and exit")
    args = parser.parse_args()

    if not acquire_lock():
        sys.exit(1)

    try:
        if args.run_once == "morning":
            run_morning()
        elif args.run_once == "evening":
            run_evening()
        elif args.run_once == "analyze":
            run_analyze()
        elif args.run_once == "grade":
            run_grade()
        else:
            run_loop(args.morning, args.evening)
    finally:
        release_lock()


if __name__ == "__main__":
    main()
