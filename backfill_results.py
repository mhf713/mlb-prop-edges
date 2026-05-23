#!/usr/bin/env python3
"""
Backfill historic picks + results from the git history of docs/index.html.

For each unique slate date the daily workflow has ever published:
  1. Find the newest commit that touched docs/index.html for that date.
  2. Parse the top 15 picks (name, team, line, P(O1.5), P(O2.5), best_edge) out
     of the rendered HTML.
  3. Write picks/{date}.json (if not already present).
  4. Call daily_email.grade_picks_for_date(date) which queries MLB Stats API
     boxscores for that date and writes results/{date}.json.

Run from the repo root after pulling git history:

    python backfill_results.py

Idempotent — safe to re-run; skips any date that already has both a picks file
and a results file. Skips today / future dates.
"""
import datetime as dt
import json
import os
import re
import subprocess
import sys

# Reuse the same MLB-API + grader implementation the daily job uses so we get
# identical name-normalization + boxscore handling.
from daily_email import grade_picks_for_date

PICKS_DIR = "picks"
RESULTS_DIR = "results"
DASH_PATH = "docs/index.html"


def strip_tags(s):
    return (re.sub(r"<[^>]+>", "", s or "")
              .replace("&nbsp;", " ")
              .replace("&mdash;", "—")
              .replace("&minus;", "-")
              .strip())


def pct_to_frac(s):
    m = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", strip_tags(s))
    return (float(m.group(1)) / 100.0) if m else 0.0


def html_to_picks(html, top_n=15):
    """Parse a docs/index.html (rendered by build_dashboard_html) into pick dicts.

    Identifies pick rows by: (a) appearing inside a <tr>, (b) containing >=16
    <td>s (the picks table layout), and (c) whose first cell is an integer rank.
    That filter rejects the header row, the track-record table rows, and the
    weights legend.
    """
    out = []
    for m in re.finditer(r"<tr\b([^>]*)>(.*?)</tr>", html, flags=re.S):
        attrs, body = m.group(1), m.group(2)
        tds = re.findall(r"<td[^>]*>(.*?)</td>", body, flags=re.S)
        if len(tds) < 16:
            continue
        rank_txt = strip_tags(tds[0])
        try:
            rank = int(rank_txt)
        except Exception:
            continue
        bid_m = re.search(r"data-bid=['\"]([\d]+)['\"]", attrs)
        batter_id = bid_m.group(1) if bid_m else ""
        edge_txt = strip_tags(tds[1])
        em = re.search(r"([+\-]?\d+(?:\.\d+)?)\s*%", edge_txt)
        best_edge = (float(em.group(1)) / 100.0) if em else 0.0
        status_html = tds[2]
        in_lineup = None
        if "✅" in status_html or "&#9989;" in status_html:
            in_lineup = True
        elif "bench" in status_html.lower():
            in_lineup = False
        bcell = tds[3]
        name_m = re.search(r"<strong>(.*?)</strong>", bcell, flags=re.S)
        name = strip_tags(name_m.group(1)) if name_m else ""
        spans = re.findall(r"<span[^>]*>(.*?)</span>", bcell, flags=re.S)
        team = strip_tags(spans[0]) if spans else ""
        lineup_pos = None
        if len(spans) > 1:
            pos_m = re.match(r"#(\d+)", strip_tags(spans[1]))
            if pos_m:
                lineup_pos = int(pos_m.group(1))
        mcell = strip_tags(tds[4])
        opp_pitcher = ""
        opp_m = re.match(r"vs\s+([^(]+?)(?:\s*\([LR]HP\))?\s*\(", mcell)
        if opp_m:
            opp_pitcher = opp_m.group(1).strip()
        else:
            opp_m = re.match(r"vs\s+(.+)", mcell)
            if opp_m:
                opp_pitcher = opp_m.group(1).strip()
        line_txt = strip_tags(tds[5]).lstrip("O").strip()
        line = "2.5" if line_txt.startswith("2") else "1.5"
        try:
            e_hrr = float(strip_tags(tds[6]))
        except Exception:
            e_hrr = 0.0
        p15 = pct_to_frac(tds[8])
        p25 = pct_to_frac(tds[9])
        out.append({
            "rank": rank,
            "batter_id": batter_id,
            "name": name,
            "team": team,
            "opp_pitcher": opp_pitcher,
            "game": "",
            "in_lineup": in_lineup,
            "lineup_pos": lineup_pos,
            "line": line,
            "best_edge": best_edge,
            "p_over_15": p15,
            "p_over_25": p25,
            "e_hrr": e_hrr,
        })
        if len(out) >= top_n:
            break
    return out


def slate_date_from_html(html):
    """The dashboard <title> contains the slate's ET date: 'MLB Prop Edge — YYYY-MM-DD'."""
    m = re.search(r"<title>[^<]*?(\d{4}-\d{2}-\d{2})", html)
    return m.group(1) if m else None


def main():
    if not os.path.isdir(".git"):
        print("ERROR: run this from the repo root — no .git/ directory here", file=sys.stderr)
        sys.exit(2)

    try:
        out = subprocess.check_output(
            ["git", "log", "--format=%H %at", "--", DASH_PATH], text=True
        ).strip()
    except subprocess.CalledProcessError as e:
        print(f"ERROR: git log failed: {e}", file=sys.stderr)
        sys.exit(2)

    if not out:
        print("No commits touching docs/index.html — nothing to backfill.")
        return

    commits = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2:
            commits.append((parts[0], int(parts[1])))

    # commits are newest-first → keep newest commit per slate date
    latest_by_date = {}
    for sha, ts in commits:
        try:
            html = subprocess.check_output(["git", "show", f"{sha}:{DASH_PATH}"], text=True)
        except subprocess.CalledProcessError:
            continue
        date = slate_date_from_html(html) or dt.datetime.utcfromtimestamp(ts).date().isoformat()
        latest_by_date.setdefault(date, (sha, ts, html))

    print(f"Found {len(latest_by_date)} unique slate date(s) in dashboard history.", flush=True)

    today_iso = dt.date.today().isoformat()
    snapshotted = graded = skipped_today = parse_empty = 0

    for date in sorted(latest_by_date):
        if date >= today_iso:
            skipped_today += 1
            print(f"  {date}: skip (today/future)")
            continue
        sha, ts, html = latest_by_date[date]
        picks_file = os.path.join(PICKS_DIR, f"{date}.json")
        if not os.path.exists(picks_file):
            picks = html_to_picks(html, top_n=15)
            if not picks:
                parse_empty += 1
                print(f"  {date}: parsed 0 picks from commit {sha[:7]} — skipping")
                continue
            os.makedirs(PICKS_DIR, exist_ok=True)
            data = {
                "date": date,
                "generated_at_utc": (
                    dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d %H:%M UTC")
                    + " (backfilled from git)"
                ),
                "picks": picks,
            }
            with open(picks_file, "w") as f:
                json.dump(data, f, indent=2)
            snapshotted += 1
            print(f"  {date}: wrote {picks_file} ({len(picks)} picks from {sha[:7]})")
        else:
            print(f"  {date}: picks file already exists, skipping snapshot")

        # Grade (no-op if results file already exists, or if all of that day's
        # games weren't Final yet — but for historical dates they will be).
        summary = grade_picks_for_date(date)
        if summary is not None:
            graded += 1

    print("")
    print(f"Done. snapshotted={snapshotted}  graded={graded}  parse_empty={parse_empty}  skipped_today/future={skipped_today}")


if __name__ == "__main__":
    main()
