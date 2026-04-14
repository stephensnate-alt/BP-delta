"""
MLB results checker for grading player prop bets.

Uses the free MLB Stats API (statsapi.mlb.com) to fetch box score data
and compare actual player stats to bet lines.
"""

import logging
import time
from datetime import date, timedelta

import requests

import config

logger = logging.getLogger(__name__)


class ResultsError(Exception):
    """Raised when result fetching or grading fails."""


# MLB team abbreviations -> Stats API team IDs
# Updated for 2026 season (30 teams)
TEAM_IDS = {
    "ARI": 109, "ATH": 133, "ATL": 144, "BAL": 110, "BOS": 111,
    "CHC": 112, "CHW": 145, "CIN": 113, "CLE": 114, "COL": 115,
    "DET": 116, "HOU": 117, "KC": 118, "LAA": 108, "LAD": 119,
    "MIA": 146, "MIL": 158, "MIN": 142, "NYM": 121, "NYY": 147,
    "OAK": 133, "PHI": 143, "PIT": 134, "SD": 135, "SF": 137,
    "SEA": 136, "STL": 138, "TB": 139, "TEX": 140, "TOR": 141,
    "WAS": 120,
}

# Map market names from BP to the stat fields in the MLB API box score
# Each entry: (player_type, stat_field)
#   player_type: "pitcher" or "batter"
#   stat_field: the key in the stats dict
MARKET_STAT_MAP = {
    "Strikeouts": ("pitcher", "strikeOuts"),
    "Pitcher Strikeouts": ("pitcher", "strikeOuts"),
    "Batter Strikeouts": ("batter", "strikeOuts"),
    "Batter Ks": ("batter", "strikeOuts"),
    "Outs": ("pitcher", "outs"),
    "Pitcher Outs": ("pitcher", "outs"),
    "Hits Allowed": ("pitcher", "hits"),
    "Pitcher Hits Allowed": ("pitcher", "hits"),
    "Walks": ("pitcher", "baseOnBalls"),
    "Pitcher Walks": ("pitcher", "baseOnBalls"),
    "Batting Walks": ("batter", "baseOnBalls"),
    "Batter Walks": ("batter", "baseOnBalls"),
    "Stolen Bases": ("batter", "stolenBases"),
    "Batter Stolen Bases": ("batter", "stolenBases"),
    "Bases": ("batter", "totalBases"),
    "Batter Total Bases": ("batter", "totalBases"),
    "Total Bases": ("batter", "totalBases"),
    "Hits + Runs + RBIs": ("batter", "hits+runs+rbi"),  # composite
    "Hits": ("batter", "hits"),
    "Batter Hits": ("batter", "hits"),
    "Runs": ("batter", "runs"),
    "Batter Runs": ("batter", "runs"),
    "RBIs": ("batter", "rbi"),
    "Batter RBIs": ("batter", "rbi"),
    "Home Runs": ("batter", "homeRuns"),
    "Batter Home Runs": ("batter", "homeRuns"),
    "Doubles": ("batter", "doubles"),
    "Batter Doubles": ("batter", "doubles"),
    "2B": ("batter", "doubles"),
    "Singles": ("batter", "singles"),
    "1B": ("batter", "singles"),
    "Earned Runs": ("pitcher", "earnedRuns"),
    "Pitcher Earned Runs": ("pitcher", "earnedRuns"),
    "Triples": ("batter", "triples"),
    "Batter Triples": ("batter", "triples"),
    "Runs First Inning": ("team", "runsFirstInning"),
    "Team Total Runs": ("team", "teamTotalRuns"),
}


def _api_get(endpoint, params=None, retries=3):
    """Make a GET request to the MLB Stats API with retry logic."""
    url = f"{config.MLB_API_BASE}{endpoint}"
    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(url, params=params, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except requests.RequestException as e:
            logger.warning(f"API request attempt {attempt}/{retries} failed: {e}")
            if attempt == retries:
                raise ResultsError(f"MLB API request failed: {e}") from e
            time.sleep(2 ** attempt)


def get_team_games(team_abbr, game_date):
    """
    Fetch the game(s) for a team on a given date.

    Returns list of game dicts from the schedule API.
    """
    team_id = TEAM_IDS.get(team_abbr.upper())
    if not team_id:
        logger.warning(f"Unknown team abbreviation: {team_abbr}")
        return []

    data = _api_get("/schedule", params={
        "sportId": 1,
        "date": game_date,
        "teamId": team_id,
        "hydrate": "linescore",
    })

    games = []
    for date_entry in data.get("dates", []):
        for game in date_entry.get("games", []):
            games.append(game)

    return games


def get_boxscore(game_pk):
    """Fetch the full box score for a game."""
    return _api_get(f"/game/{game_pk}/boxscore")


def _normalize_player_name(name):
    """
    Normalize a player name for matching.

    BP uses abbreviated first names like "E. Fedde" while the MLB API
    uses full names like "Emmanuel Clase" or sometimes "E. Fedde".
    Strips accents and matches on last name + first initial.
    """
    import unicodedata
    # Strip accents (é -> e, á -> a, ñ -> n, etc.)
    name = unicodedata.normalize("NFD", name)
    name = "".join(c for c in name if unicodedata.category(c) != "Mn")

    parts = name.strip().split()
    if len(parts) < 2:
        return name.lower().strip()

    first = parts[0].rstrip(".")
    last = parts[-1]
    return (first[0].lower(), last.lower())


def _find_player_in_boxscore(boxscore, player_name, player_type):
    """
    Find a player's stats in the box score.

    Args:
        boxscore: Full boxscore JSON from the API.
        player_name: Player name as shown on BP (e.g., "E. Fedde").
        player_type: "pitcher" or "batter".

    Returns:
        The player's stat dict, or None if not found.
    """
    target = _normalize_player_name(player_name)

    for side in ["away", "home"]:
        team_data = boxscore.get("teams", {}).get(side, {})
        players = team_data.get("players", {})

        for player_key, player_data in players.items():
            full_name = player_data.get("person", {}).get("fullName", "")
            normalized = _normalize_player_name(full_name)

            # Match on first initial + last name
            matched = False
            if isinstance(target, tuple) and isinstance(normalized, tuple):
                if target == normalized:
                    matched = True
                elif target[1] == normalized[1]:
                    # Same last name, check first initial
                    if target[0] == normalized[0]:
                        matched = True

            if matched:
                stats = player_data.get("stats", {})

                if player_type == "pitcher":
                    pitching = stats.get("pitching", {})
                    if pitching:
                        return pitching
                else:
                    batting = stats.get("batting", {})
                    if batting:
                        return batting

    logger.warning(f"Player not found in boxscore: {player_name} ({player_type})")
    return None


def _get_stat_value(stats, market):
    """
    Extract the relevant stat value for a given market.

    Handles composite stats like "Hits + Runs + RBIs".
    """
    mapping = MARKET_STAT_MAP.get(market)
    if not mapping:
        logger.warning(f"Unknown market type: {market}")
        return None

    _, stat_field = mapping

    # Handle composite stats
    if stat_field == "hits+runs+rbi":
        hits = stats.get("hits", 0)
        runs = stats.get("runs", 0)
        rbi = stats.get("rbi", 0)
        return hits + runs + rbi

    # Handle pitcher outs (convert from innings pitched if needed)
    if stat_field == "outs":
        # The API may give inningsPitched as "6.1" meaning 6 innings + 1 out
        if "outs" in stats:
            return stats["outs"]
        ip = stats.get("inningsPitched", "0")
        try:
            parts = str(ip).split(".")
            full_innings = int(parts[0])
            partial = int(parts[1]) if len(parts) > 1 else 0
            return full_innings * 3 + partial
        except (ValueError, IndexError):
            return 0

    return stats.get(stat_field, 0)


def calculate_profit(odds, won):
    """
    Calculate profit for a bet at the configured bet size.

    Args:
        odds: American odds as int (e.g., -150, +130).
        won: True if bet won, False if lost.

    Returns:
        Profit in dollars (positive for wins, negative for losses).
    """
    bet_size = config.BET_SIZE
    if won:
        if odds < 0:
            return round(bet_size * (100 / abs(odds)), 2)
        else:
            return round(bet_size * (odds / 100), 2)
    return -bet_size


def grade_bet(bet, stats):
    """
    Grade a single bet against actual stats.

    Args:
        bet: Bet dict from the sheet.
        stats: Player stat dict from the box score.

    Returns:
        (result, profit) tuple where result is "W", "L", or "P".
    """
    market = bet["market"]
    over_under = bet["over_under"].upper()

    actual = _get_stat_value(stats, market)
    if actual is None:
        logger.warning(f"Could not get stat for {market}")
        return None, None

    try:
        line = float(bet["line"])
        odds = int(bet["odds"])
    except (ValueError, TypeError):
        logger.warning(f"Could not parse line/odds: {bet['line']}/{bet['odds']}")
        return None, None

    # Grade: Over means actual > line, Under means actual < line
    if over_under == "O":
        if actual > line:
            return "W", calculate_profit(odds, True)
        elif actual < line:
            return "L", calculate_profit(odds, False)
        else:
            return "P", 0.0
    else:  # Under
        if actual < line:
            return "W", calculate_profit(odds, True)
        elif actual > line:
            return "L", calculate_profit(odds, False)
        else:
            return "P", 0.0


def check_results(pending_bets):
    """
    Check results for a list of pending bets.

    Args:
        pending_bets: List of (row_number, bet_dict) from sheets.get_pending_bets().

    Returns:
        List of (row_number, result, profit) for bets whose games are final.
        Bets whose games haven't finished are excluded.
    """
    updates = []

    # Group bets by (team, date) to minimize API calls
    game_cache = {}

    for row_num, bet in pending_bets:
        team = bet["team"]
        game_date = bet["date"]
        cache_key = (team, game_date)

        # Fetch game if not cached
        if cache_key not in game_cache:
            games = get_team_games(team, game_date)
            game_cache[cache_key] = games
            if not games:
                logger.warning(f"No games found for {team} on {game_date}")

        games = game_cache[cache_key]
        if not games:
            continue

        # Use the first game (doubleheaders are rare for props)
        game = games[0]
        status = game.get("status", {}).get("detailedState", "")

        if status not in ("Final", "Game Over", "Completed Early"):
            logger.info(f"Game {team} on {game_date} not final yet: {status}")
            continue

        game_pk = game["gamePk"]

        # Fetch boxscore
        boxscore_key = game_pk
        if boxscore_key not in game_cache:
            game_cache[boxscore_key] = get_boxscore(game_pk)

        boxscore = game_cache[boxscore_key]

        # Determine if this is a pitcher or batter prop
        mapping = MARKET_STAT_MAP.get(bet["market"])
        if not mapping:
            logger.warning(f"Unknown market: {bet['market']}")
            continue

        player_type = mapping[0]

        # Find player stats in boxscore
        stats = _find_player_in_boxscore(boxscore, bet["player"], player_type)
        if stats is None:
            # Game is final but player not found in stats - they didn't play
            # Mark as VOID (bet is voided, no profit/loss)
            logger.info(
                f"{bet['player']} did not play for {team} on {game_date} - VOID"
            )
            updates.append((row_num, "VOID", 0.0))
            continue

        result, profit = grade_bet(bet, stats)
        if result is not None:
            updates.append((row_num, result, profit))
            logger.info(
                f"{bet['player']} {bet['market']} {bet['over_under']} "
                f"{bet['line']}: {result} ({profit:+.2f})"
            )

    return updates


def check_live(pending_bets):
    """
    Check live/in-progress games for Over bets that have already hit.

    Returns list of dicts with bet info and current stat for display.
    """
    hits = []
    game_cache = {}

    for row_num, bet in pending_bets:
        # Only check Over bets
        if bet["over_under"].upper() != "O":
            continue

        team = bet["team"]
        game_date = bet["date"]
        cache_key = (team, game_date)

        if cache_key not in game_cache:
            games = get_team_games(team, game_date)
            game_cache[cache_key] = games

        games = game_cache.get(cache_key, [])
        if not games:
            continue

        game = games[0]
        status = game.get("status", {}).get("detailedState", "")

        if status == "Final":
            continue  # already graded or will be by grade command

        if "In Progress" not in status and "Live" not in status.lower():
            continue

        game_pk = game["gamePk"]
        if game_pk not in game_cache:
            game_cache[game_pk] = get_boxscore(game_pk)

        boxscore = game_cache[game_pk]

        mapping = MARKET_STAT_MAP.get(bet["market"])
        if not mapping:
            continue

        player_type = mapping[0]
        stats = _find_player_in_boxscore(boxscore, bet["player"], player_type)
        if stats is None:
            continue

        actual = _get_stat_value(stats, bet["market"])
        if actual is None:
            continue

        try:
            line = float(bet["line"])
        except (ValueError, TypeError):
            continue

        hit = actual > line
        hits.append({
            "player": bet["player"],
            "market": bet["market"],
            "line": line,
            "actual": actual,
            "hit": hit,
            "team": bet["team"],
            "status": status,
        })

    return hits
