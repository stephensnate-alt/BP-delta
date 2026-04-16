"""Parse BL Final Table transaction transcripts and emit an Excel workbook.

Usage:
    python build_trades_workbook.py trades_raw.txt BL_Trades.xlsx
"""

import re
import sys
from collections import defaultdict
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter


TRADE_RE = re.compile(
    r"^(?P<date>[A-Za-z]{3,} \d+)(?: - [^:]+)?:\s+"
    r"Team\s+\d+\s+\((?P<a>[^)]+)\)\s+reported trading"
    r"(?:\s+(?P<ags>.*?))?\s+to\s+team\s+\d+\s+\((?P<b>[^)]+)\)"
    r"(?:\s+for\s+(?P<bgs>.*?))?\s*\.\s*$"
)


def split_block_years(lines):
    """Return list of (year_idx, line) tagging each line with a 1-based block index."""
    out = []
    year = 0
    for line in lines:
        if line.startswith("BL Final Table Transactions"):
            year += 1
        out.append((max(year, 1), line))
    return out


def first_initial_last(full_name: str) -> str:
    """FirstName + first initial of last name. Handles middle initials/names."""
    parts = full_name.replace(".", "").split()
    if len(parts) == 1:
        return parts[0]
    first = parts[0]
    last = parts[-1]
    return f"{first}{last[0]}"


def sheet_safe(name: str) -> str:
    return re.sub(r"[\\/*?:\[\]]", "", name)[:31]


def normalize_items(raw: str | None) -> tuple[str, ...]:
    if not raw:
        return ()
    # Split on "; " and normalize whitespace
    parts = [p.strip() for p in raw.split(";")]
    return tuple(sorted(p for p in parts if p))


def parse(path: Path):
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    tagged = split_block_years(lines)
    trades = []  # list of dicts
    for year_idx, line in tagged:
        m = TRADE_RE.match(line.strip())
        if not m:
            continue
        a = m.group("a").strip()
        b = m.group("b").strip()
        ags = (m.group("ags") or "").strip()
        bgs = (m.group("bgs") or "").strip()
        date = m.group("date").strip()
        trades.append(
            {
                "year": year_idx,
                "date": date,
                "from": a,
                "to": b,
                "from_gave": ags,
                "from_got": bgs,
                "raw": line.strip(),
            }
        )
    return trades


def canonical_key(t):
    """Key that matches both-sides reports of the same trade."""
    a, b = t["from"], t["to"]
    ags = normalize_items(t["from_gave"])
    bgs = normalize_items(t["from_got"])
    pair = tuple(sorted([a, b]))
    # Order items to match owner-sort order
    if a <= b:
        items = (ags, bgs)
    else:
        items = (bgs, ags)
    return (t["year"], t["date"], pair, items)


def trade_summary(t: dict) -> str:
    """Readable one-line description of a trade for display in a cell."""
    ags = t["from_gave"] or "(nothing listed)"
    bgs = t["from_got"] or "(nothing listed)"
    return f"{t['date']}: {t['from']} sent {ags} -> {t['to']} for {bgs}"


def build_workbook(trades, out_path: Path):
    wb = Workbook()

    # --- All Trades sheet ---
    ws_all = wb.active
    ws_all.title = "All Trades"
    headers = ["Year Block", "Date", "From Owner", "To Owner", "From Gave", "From Got", "Full Line"]
    ws_all.append(headers)
    for cell in ws_all[1]:
        cell.font = Font(bold=True)
        cell.fill = PatternFill("solid", fgColor="D9E1F2")
    for t in trades:
        ws_all.append([
            t["year"],
            t["date"],
            t["from"],
            t["to"],
            t["from_gave"],
            t["from_got"],
            t["raw"],
        ])
    widths = [10, 10, 22, 22, 40, 40, 100]
    for i, w in enumerate(widths, 1):
        ws_all.column_dimensions[get_column_letter(i)].width = w
    ws_all.freeze_panes = "A2"

    # --- De-dup into unique trades ---
    unique = {}
    for t in trades:
        k = canonical_key(t)
        # Keep whichever direction came first; doesn't matter which
        if k not in unique:
            unique[k] = t

    # owner -> list of (partner, trade) for unique trades only
    by_owner_partner = defaultdict(lambda: defaultdict(list))
    all_owners = set()
    total_unique = len(unique)
    for t in unique.values():
        a, b = t["from"], t["to"]
        all_owners.add(a)
        all_owners.add(b)
        by_owner_partner[a][b].append(t)
        by_owner_partner[b][a].append(t)

    owners_sorted = sorted(all_owners)
    # Owner -> total unique trades involving them
    owner_total = {o: sum(len(v) for v in by_owner_partner[o].values()) for o in owners_sorted}
    # Total "counterparty slots" across the league: 2 * U (each trade has 2 owners)
    total_slots = sum(owner_total.values())  # == 2 * total_unique

    def pair_expected(a: str, b: str) -> float:
        """Symmetric expected trades between a and b (same value either direction).

        Under the null model where each owner picks partners proportional to
        partner activity, directional expecteds are T_a * T_b / (2U - T_a) and
        T_b * T_a / (2U - T_b). We use the average so the two owner sheets and
        the summary all agree on one number per pair."""
        ta, tb = owner_total[a], owner_total[b]
        if ta == 0 or tb == 0:
            return 0.0
        e_ab = (ta * tb) / (total_slots - ta) if total_slots - ta else 0.0
        e_ba = (tb * ta) / (total_slots - tb) if total_slots - tb else 0.0
        return (e_ab + e_ba) / 2

    # Sheet-name collision handling: ensure unique short names
    used_titles = {"All Trades"}

    def unique_title(base):
        t = sheet_safe(base)
        cand = t
        i = 2
        while cand in used_titles:
            cand = sheet_safe(f"{t}{i}")
            i += 1
        used_titles.add(cand)
        return cand

    for owner in owners_sorted:
        title = unique_title(first_initial_last(owner))
        ws = wb.create_sheet(title)
        ws["A1"] = f"{owner} - trades with each other owner"
        ws["A1"].font = Font(bold=True, size=14)
        ws.merge_cells("A1:F1")

        ws.append([])  # blank row
        header_row = [
            "Other Owner",
            "# Trades",
            "Expected",
            "Variance",
            "Partner League %",
            "Owner % with Partner",
            "Trade Index",
        ]
        # Figure max trades so we know how many trade columns
        partners = sorted(p for p in owners_sorted if p != owner)
        max_trades = max((len(by_owner_partner[owner].get(p, [])) for p in partners), default=0)
        for i in range(1, max_trades + 1):
            header_row.append(f"Trade {i}")
        ws.append(header_row)
        for cell in ws[ws.max_row]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="D9E1F2")

        total = owner_total.get(owner, 0)
        # Partner slots available to this owner (sum of T_Z for Z != X). Used so
        # Partner League % sums to 100% per sheet and the Index's weighted mean = 100.
        available_slots = total_slots - total
        data_start = ws.max_row + 1
        for p in partners:
            ts = sorted(by_owner_partner[owner].get(p, []), key=lambda x: (x["year"], x["date"]))
            count = len(ts)
            partner_league_share = (owner_total[p] / available_slots) if available_slots else 0.0
            owner_share = (count / total) if total else 0.0
            if partner_league_share > 0:
                index_val = (owner_share / partner_league_share) * 100
            else:
                index_val = 0.0
            # Directional expected from THIS owner's perspective so that the
            # column sums to the owner's Total Trades.
            expected = total * partner_league_share
            variance = count - expected
            row = [
                p,
                count,
                round(expected, 1),
                round(variance, 1),
                partner_league_share,  # percent-formatted below
                owner_share,           # percent-formatted below
                round(index_val),
            ]
            for t in ts:
                row.append(trade_summary(t))
            ws.append(row)
        data_end = ws.max_row

        # Apply Excel cell formats: percentages with 1 decimal, integer index
        for r in range(data_start, data_end + 1):
            ws.cell(row=r, column=5).number_format = "0.0%"
            ws.cell(row=r, column=6).number_format = "0.0%"
            ws.cell(row=r, column=7).number_format = "0"
            ws.cell(row=r, column=3).number_format = "0.0"
            ws.cell(row=r, column=4).number_format = "+0.0;-0.0;0.0"

        # Total row
        total_row = ["TOTAL", total, round(total, 1), 0.0, "", "", ""]
        ws.append(total_row)
        for cell in ws[ws.max_row][:7]:
            cell.font = Font(bold=True)
            cell.fill = PatternFill("solid", fgColor="FFF2CC")
        ws.cell(row=ws.max_row, column=3).number_format = "0.0"
        ws.cell(row=ws.max_row, column=4).number_format = "+0.0;-0.0;0.0"

        # Column widths
        ws.column_dimensions["A"].width = 24
        ws.column_dimensions["B"].width = 10
        ws.column_dimensions["C"].width = 11
        ws.column_dimensions["D"].width = 11
        ws.column_dimensions["E"].width = 18
        ws.column_dimensions["F"].width = 20
        ws.column_dimensions["G"].width = 13
        for i in range(8, 8 + max_trades):
            ws.column_dimensions[get_column_letter(i)].width = 80
        ws.freeze_panes = "H4"

    # --- Summary sheet: biggest pair-level variances ---
    pair_rows = []
    for i, a in enumerate(owners_sorted):
        for b in owners_sorted[i + 1:]:
            actual = len(by_owner_partner[a].get(b, []))
            expected = pair_expected(a, b)
            variance = actual - expected
            pair_rows.append({
                "a": a,
                "b": b,
                "actual": actual,
                "expected": expected,
                "variance": variance,
            })

    over = sorted(pair_rows, key=lambda r: r["variance"], reverse=True)[:10]
    under = sorted(pair_rows, key=lambda r: r["variance"])[:10]

    ws_sum = wb.create_sheet("Summary", 1)  # right after All Trades
    ws_sum["A1"] = "Biggest Pair-Level Trade Variances"
    ws_sum["A1"].font = Font(bold=True, size=14)
    ws_sum.merge_cells("A1:E1")
    ws_sum["A2"] = (
        "Expected trades per pair use a symmetric null model: if each owner "
        "picked partners in proportion to how often those partners trade, this "
        "is how many trades you'd expect between them. Variance = Actual - "
        "Expected. Positive = they trade more than expected; negative = less."
    )
    ws_sum["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws_sum.merge_cells("A2:E2")
    ws_sum.row_dimensions[2].height = 48

    def write_pair_block(start_row, title, rows):
        ws_sum.cell(row=start_row, column=1, value=title).font = Font(bold=True, size=12)
        ws_sum.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=5)
        header = ["Rank", "Owner A", "Owner B", "Actual", "Expected", "Variance"]
        for i, h in enumerate(header, 1):
            c = ws_sum.cell(row=start_row + 1, column=i, value=h)
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9E1F2")
        for i, r in enumerate(rows, 1):
            ws_sum.cell(row=start_row + 1 + i, column=1, value=i)
            ws_sum.cell(row=start_row + 1 + i, column=2, value=r["a"])
            ws_sum.cell(row=start_row + 1 + i, column=3, value=r["b"])
            ws_sum.cell(row=start_row + 1 + i, column=4, value=r["actual"])
            ce = ws_sum.cell(row=start_row + 1 + i, column=5, value=round(r["expected"], 1))
            ce.number_format = "0.0"
            cv = ws_sum.cell(row=start_row + 1 + i, column=6, value=round(r["variance"], 1))
            cv.number_format = "+0.0;-0.0;0.0"

    write_pair_block(4, "Top 10 Most Over-Traded Pairs (actual >> expected)", over)
    write_pair_block(4 + 2 + 10 + 2, "Top 10 Most Under-Traded Pairs (actual << expected)", under)

    ws_sum.column_dimensions["A"].width = 6
    ws_sum.column_dimensions["B"].width = 30
    ws_sum.column_dimensions["C"].width = 30
    ws_sum.column_dimensions["D"].width = 10
    ws_sum.column_dimensions["E"].width = 11
    ws_sum.column_dimensions["F"].width = 11

    # --- Totals sheet: trades per owner, sorted descending ---
    ws_tot = wb.create_sheet("Totals", 2)
    ws_tot["A1"] = "Total Unique Trades per Owner"
    ws_tot["A1"].font = Font(bold=True, size=14)
    ws_tot.merge_cells("A1:C1")
    header = ["Rank", "Owner", "Total Trades"]
    for i, h in enumerate(header, 1):
        c = ws_tot.cell(row=3, column=i, value=h)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
    by_total = sorted(owners_sorted, key=lambda o: owner_total[o], reverse=True)
    for i, o in enumerate(by_total, 1):
        ws_tot.cell(row=3 + i, column=1, value=i)
        ws_tot.cell(row=3 + i, column=2, value=o)
        ws_tot.cell(row=3 + i, column=3, value=owner_total[o])
    # Grand total row (sum of T_X = 2U because each trade has 2 owners)
    grand_row = 3 + len(by_total) + 1
    ws_tot.cell(row=grand_row, column=2, value="LEAGUE UNIQUE TRADES").font = Font(bold=True)
    ws_tot.cell(row=grand_row, column=3, value=total_unique).font = Font(bold=True)
    ws_tot.cell(row=grand_row, column=2).fill = PatternFill("solid", fgColor="FFF2CC")
    ws_tot.cell(row=grand_row, column=3).fill = PatternFill("solid", fgColor="FFF2CC")
    ws_tot.column_dimensions["A"].width = 6
    ws_tot.column_dimensions["B"].width = 36
    ws_tot.column_dimensions["C"].width = 14

    # --- Picky Traders sheet ---
    LOW_SAMPLE_OWNERS = {
        "Michael Zink",
        "Sean Forman / Mike Webber",
        "Ben Murphy / Ian Lefkowitz / Jared Weiss",
    }
    eligible = [o for o in owners_sorted if o not in LOW_SAMPLE_OWNERS]

    picky_rows = []
    for owner in eligible:
        total = owner_total[owner]
        available = total_slots - total
        # Partner pool for picky scoring: other eligible owners only
        partner_pool = [p for p in eligible if p != owner]
        high = 0
        low = 0
        indexes = []
        for p in partner_pool:
            count = len(by_owner_partner[owner].get(p, []))
            partner_share = (owner_total[p] / available) if available else 0.0
            owner_share = (count / total) if total else 0.0
            idx = (owner_share / partner_share * 100) if partner_share > 0 else 0.0
            indexes.append(idx)
            if idx >= 150:
                high += 1
            if idx <= 50:
                low += 1
        # Average absolute deviation from 100 (alt. dispersion measure)
        if indexes:
            mean_idx = sum(indexes) / len(indexes)
            stdev = (sum((x - mean_idx) ** 2 for x in indexes) / len(indexes)) ** 0.5
        else:
            stdev = 0.0
        picky_rows.append({
            "owner": owner,
            "total_trades": total,
            "high_count": high,
            "low_count": low,
            "extreme_count": high + low,
            "stdev": stdev,
        })

    ws_p = wb.create_sheet("Picky Traders", 3)
    ws_p["A1"] = "Pickiest / Least Picky Traders"
    ws_p["A1"].font = Font(bold=True, size=14)
    ws_p.merge_cells("A1:F1")
    ws_p["A2"] = (
        "For each owner, count how many partners produced a Trade Index of at "
        "least 150 (strong preference) or at most 50 (strong avoidance). More "
        "extremes = pickier. Michael Zink, Sean Forman / Mike Webber, and Ben "
        "Murphy / Ian Lefkowitz / Jared Weiss excluded from both the rankings "
        "and each owner's partner pool (low sample)."
    )
    ws_p["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws_p.merge_cells("A2:F2")
    ws_p.row_dimensions[2].height = 60

    def write_picky_block(start_row, title, rows):
        ws_p.cell(row=start_row, column=1, value=title).font = Font(bold=True, size=12)
        ws_p.merge_cells(start_row=start_row, start_column=1, end_row=start_row, end_column=6)
        hdr = [
            "Rank",
            "Owner",
            "Total Trades",
            "Partners Index >= 150",
            "Partners Index <= 50",
            "Extreme Count",
        ]
        for i, h in enumerate(hdr, 1):
            c = ws_p.cell(row=start_row + 1, column=i, value=h)
            c.font = Font(bold=True)
            c.fill = PatternFill("solid", fgColor="D9E1F2")
        for i, r in enumerate(rows, 1):
            ws_p.cell(row=start_row + 1 + i, column=1, value=i)
            ws_p.cell(row=start_row + 1 + i, column=2, value=r["owner"])
            ws_p.cell(row=start_row + 1 + i, column=3, value=r["total_trades"])
            ws_p.cell(row=start_row + 1 + i, column=4, value=r["high_count"])
            ws_p.cell(row=start_row + 1 + i, column=5, value=r["low_count"])
            ws_p.cell(row=start_row + 1 + i, column=6, value=r["extreme_count"])

    # Tie-break: more extremes first; then by stdev
    by_picky = sorted(picky_rows, key=lambda r: (-r["extreme_count"], -r["stdev"]))
    top5 = by_picky[:5]
    bottom5 = list(reversed(by_picky[-5:]))
    write_picky_block(4, "Top 5 Pickiest (most partners at extremes)", top5)
    write_picky_block(4 + 2 + 5 + 2, "Top 5 Least Picky (fewest extremes)", bottom5)

    ws_p.column_dimensions["A"].width = 6
    ws_p.column_dimensions["B"].width = 26
    ws_p.column_dimensions["C"].width = 14
    ws_p.column_dimensions["D"].width = 22
    ws_p.column_dimensions["E"].width = 22
    ws_p.column_dimensions["F"].width = 16

    # --- Never-Traded Pairs sheet (biggest expected, actual = 0) ---
    ws_nt = wb.create_sheet("Never Traded", 4)
    ws_nt["A1"] = "Biggest Unmet Expectations (Pairs That Never Traded)"
    ws_nt["A1"].font = Font(bold=True, size=14)
    ws_nt.merge_cells("A1:E1")
    ws_nt["A2"] = (
        "Pairs with zero unique trades between them, ranked by the symmetric "
        "expected-trade count. Variance = Actual - Expected = -Expected."
    )
    ws_nt["A2"].alignment = Alignment(wrap_text=True, vertical="top")
    ws_nt.merge_cells("A2:E2")
    ws_nt.row_dimensions[2].height = 32

    zero_pairs = [p for p in pair_rows if p["actual"] == 0]
    zero_pairs.sort(key=lambda r: r["expected"], reverse=True)
    hdr = ["Rank", "Owner A", "Owner B", "Expected", "Variance"]
    for i, h in enumerate(hdr, 1):
        c = ws_nt.cell(row=4, column=i, value=h)
        c.font = Font(bold=True)
        c.fill = PatternFill("solid", fgColor="D9E1F2")
    for i, r in enumerate(zero_pairs[:10], 1):
        ws_nt.cell(row=4 + i, column=1, value=i)
        ws_nt.cell(row=4 + i, column=2, value=r["a"])
        ws_nt.cell(row=4 + i, column=3, value=r["b"])
        ce = ws_nt.cell(row=4 + i, column=4, value=round(r["expected"], 1))
        ce.number_format = "0.0"
        cv = ws_nt.cell(row=4 + i, column=5, value=round(r["variance"], 1))
        cv.number_format = "+0.0;-0.0;0.0"
    ws_nt.column_dimensions["A"].width = 6
    ws_nt.column_dimensions["B"].width = 30
    ws_nt.column_dimensions["C"].width = 30
    ws_nt.column_dimensions["D"].width = 11
    ws_nt.column_dimensions["E"].width = 11

    wb.save(out_path)


def main():
    if len(sys.argv) < 3:
        print("Usage: build_trades_workbook.py INPUT.txt OUTPUT.xlsx")
        sys.exit(1)
    inp = Path(sys.argv[1])
    out = Path(sys.argv[2])
    trades = parse(inp)
    print(f"Parsed {len(trades)} trade lines.")
    build_workbook(trades, out)
    print(f"Wrote {out}")


if __name__ == "__main__":
    main()
