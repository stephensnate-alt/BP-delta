"""
NFL WRs drafted in Rounds 3-7, classes 2021-2025.
For each, compute rookie-year half-PPR season total, weeks 15-17 half-PPR total,
and flags for any single week 15-17 with >=15 / >=20 half-PPR points.

Output: wr_draft_analysis.xlsx, sorted by rookie-season half-PPR total (desc).
Half-PPR per game = (standard fantasy_points + fantasy_points_ppr) / 2
"""

import nfl_data_py as nfl
import pandas as pd

YEARS = [2021, 2022, 2023, 2024, 2025]

draft = nfl.import_draft_picks(YEARS)
wrs = draft[(draft["position"] == "WR") & (draft["round"].between(3, 7))].copy()
wrs = wrs[["season", "round", "pick", "team", "pfr_player_name", "gsis_id", "college"]]
wrs = wrs.rename(columns={"season": "draft_year", "pfr_player_name": "player"})

# Pull weekly player stats directly from nflverse (newer release tag works for all years)
WEEKLY_URL = "https://github.com/nflverse/nflverse-data/releases/download/stats_player/stats_player_week_{}.parquet"
weekly = pd.concat([pd.read_parquet(WEEKLY_URL.format(y)) for y in YEARS], ignore_index=True)
weekly = weekly[weekly["season_type"] == "REG"].copy()
weekly["half_ppr"] = (weekly["fantasy_points"].fillna(0) + weekly["fantasy_points_ppr"].fillna(0)) / 2.0

rows = []
for _, r in wrs.iterrows():
    gsis = r["gsis_id"]
    yr = r["draft_year"]
    name = r["player"]

    games = weekly[(weekly["player_id"] == gsis) & (weekly["season"] == yr)] if pd.notna(gsis) else pd.DataFrame()
    if games.empty and pd.notna(gsis):
        games = weekly[(weekly["player_id"] == gsis) & (weekly["season"] == yr)]
    # fallback by name if no gsis match (rare)
    if games.empty:
        games = weekly[(weekly["player_display_name"] == name) & (weekly["season"] == yr) & (weekly["position"] == "WR")]

    season_total = float(games["half_ppr"].sum()) if not games.empty else 0.0
    games_played = int(len(games))

    wk1517 = games[games["week"].between(15, 17)] if not games.empty else games
    wk1517_total = float(wk1517["half_ppr"].sum()) if not wk1517.empty else 0.0
    wk1517_max = float(wk1517["half_ppr"].max()) if not wk1517.empty else 0.0
    wk1517_games = int(len(wk1517))

    flag_15 = bool((wk1517["half_ppr"] >= 15).any()) if not wk1517.empty else False
    flag_20 = bool((wk1517["half_ppr"] >= 20).any()) if not wk1517.empty else False

    rows.append({
        "draft_year": int(yr),
        "round": int(r["round"]),
        "pick": int(r["pick"]),
        "player": name,
        "drafted_by": r["team"],
        "college": r["college"],
        "rookie_games": games_played,
        "rookie_half_ppr_total": round(season_total, 2),
        "wk15_17_half_ppr_total": round(wk1517_total, 2),
        "wk15_17_games_played": wk1517_games,
        "wk15_17_best_week": round(wk1517_max, 2),
        "any_wk15_17_ge_15": flag_15,
        "any_wk15_17_ge_20": flag_20,
    })

df = pd.DataFrame(rows).sort_values(
    ["rookie_half_ppr_total", "draft_year", "pick"],
    ascending=[False, True, True],
).reset_index(drop=True)

print(f"Total WRs (R3-7, 2021-2025): {len(df)}")
print(df.head(20).to_string(index=False))
print("...")
print(df.tail(10).to_string(index=False))

# write xlsx
out = "wr_draft_analysis_R3to7_2021-2025.xlsx"
with pd.ExcelWriter(out, engine="openpyxl") as xl:
    df.to_excel(xl, sheet_name="WR R3-7 2021-2025", index=False)
    # format header / widths
    ws = xl.sheets["WR R3-7 2021-2025"]
    widths = {
        "A": 11, "B": 7, "C": 7, "D": 24, "E": 12, "F": 22,
        "G": 13, "H": 22, "I": 22, "J": 19, "K": 17, "L": 19, "M": 19,
    }
    for col, w in widths.items():
        ws.column_dimensions[col].width = w
    ws.freeze_panes = "A2"

# also CSV for easy view in shell
df.to_csv("wr_draft_analysis_R3to7_2021-2025.csv", index=False)
print(f"\nWrote {out} and CSV companion.")
