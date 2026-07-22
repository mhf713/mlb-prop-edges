#!/usr/bin/env python3
"""
MLB Prop Edge — Daily Email Sender (v2)

Projects each batter's H+R+RBI for today's game, computes P(over 1.5) and
P(over 2.5) using a Poisson approximation, and emails the top 10 prop bet
edges via Resend.

Reads env vars:
  RESEND_API_KEY, RECIPIENT_EMAIL
"""

import csv
import datetime as dt
import json
import math
import os
import sys
import urllib.error
import urllib.request

W = {"uBB": 0.689, "HBP": 0.720, "1B": 0.882, "2B": 1.255, "3B": 1.583, "HR": 2.045}

TEAM_ABBR = {
    "Arizona Diamondbacks": "AZ", "Atlanta Braves": "ATL", "Baltimore Orioles": "BAL",
    "Boston Red Sox": "BOS", "Chicago Cubs": "CHC", "Chicago White Sox": "CWS",
    "Cincinnati Reds": "CIN", "Cleveland Guardians": "CLE", "Colorado Rockies": "COL",
    "Detroit Tigers": "DET", "Houston Astros": "HOU", "Kansas City Royals": "KC",
    "Los Angeles Angels": "LAA", "Los Angeles Dodgers": "LAD", "Miami Marlins": "MIA",
    "Milwaukee Brewers": "MIL", "Minnesota Twins": "MIN", "New York Mets": "NYM",
    "New York Yankees": "NYY", "Athletics": "ATH", "Oakland Athletics": "OAK",
    "Philadelphia Phillies": "PHI", "Pittsburgh Pirates": "PIT", "San Diego Padres": "SD",
    "San Francisco Giants": "SF", "Seattle Mariners": "SEA", "St. Louis Cardinals": "STL",
    "Tampa Bay Rays": "TB", "Texas Rangers": "TEX", "Toronto Blue Jays": "TOR",
    "Washington Nationals": "WSH",
}

STADIUMS = {
    "AZ":  (33.4453, -112.0667, "retractable",   0), "ATL": (33.8908,  -84.4678, "open",        137),
    "BAL": (39.2839,  -76.6217, "open",         61), "BOS": (42.3467,  -71.0972, "open",         45),
    "CHC": (41.9484,  -87.6553, "open",         39), "CWS": (41.8300,  -87.6339, "open",         41),
    "CIN": (39.0975,  -84.5067, "open",        354), "CLE": (41.4961,  -81.6852, "open",          9),
    "COL": (39.7561, -104.9942, "open",        358), "DET": (42.3390,  -83.0485, "open",        153),
    "HOU": (29.7570,  -95.3552, "retractable",  20), "KC":  (39.0517,  -94.4803, "open",          3),
    "LAA": (33.8003, -117.8827, "open",         45), "LAD": (34.0739, -118.2400, "open",         25),
    "MIA": (25.7783,  -80.2197, "retractable", 343), "MIL": (43.0280,  -87.9712, "retractable", 349),
    "MIN": (44.9817,  -93.2776, "open",        105), "NYM": (40.7571,  -73.8458, "open",         25),
    "NYY": (40.8296,  -73.9262, "open",         75), "ATH": (38.6553, -121.5083, "open",         60),
    "OAK": (37.7516, -122.2005, "open",         60), "PHI": (39.9061,  -75.1665, "open",         30),
    "PIT": (40.4469,  -80.0058, "open",         82), "SD":  (32.7073, -117.1566, "open",         21),
    "SF":  (37.7786, -122.3893, "open",        130), "SEA": (47.5914, -122.3325, "retractable",  40),
    "STL": (38.6226,  -90.1928, "open",         13), "TB":  (27.7682,  -82.6534, "dome",         45),
    "TEX": (32.7473,  -97.0817, "retractable",  10), "TOR": (43.6414,  -79.3894, "retractable",   5),
    "WSH": (38.8729,  -77.0074, "open",         29),
}

# 3-year rolling park factor for runs (1.00 = neutral). Sourced from public Statcast/FG
# park-factor leaderboards; small movements year to year are expected.
PARK_FACTORS = {
    "COL": 1.20, "CIN": 1.07, "BOS": 1.06, "PHI": 1.05, "BAL": 1.05,
    "TEX": 1.04, "ATL": 1.03, "WSH": 1.03, "AZ":  1.03, "TOR": 1.02,
    "CHC": 1.02, "MIL": 1.01, "MIN": 1.01, "HOU": 1.01, "KC":  1.00,
    "ATH": 1.00, "OAK": 1.00, "NYY": 1.00, "STL": 0.99, "CWS": 0.99,
    "LAA": 0.98, "DET": 0.98, "CLE": 0.97, "TB":  0.97, "NYM": 0.97,
    "LAD": 0.96, "MIA": 0.95, "SEA": 0.94, "SF":  0.94, "PIT": 0.93,
    "SD":  0.92,
}
DEFAULT_PARK = 1.00

# Share of plate appearances in a typical game that face the opposing bullpen (vs the starter).
# Modern MLB starters average ~5 IP, so roughly 35% of PA come vs relievers.
BULLPEN_PA_SHARE = 0.35

# Expected PA per game by batting order position (based on historical MLB averages).
PA_BY_ORDER = {1: 4.55, 2: 4.45, 3: 4.35, 4: 4.25, 5: 4.15, 6: 4.05, 7: 3.95, 8: 3.85, 9: 3.75}
DEFAULT_PA = 4.15  # if lineup not posted yet

BREAKEVEN_PROB = 0.524  # break-even at standard -110 sportsbook juice

USER_AGENT = "MLB-PropEdge/1.0 (daily-email)"

# =============== HTTP ===============

def fetch(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8-sig")

def num(x):
    try: return float(x)
    except: return None

def get_schedule(date):
    return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&hydrate=probablePitcher,lineups,team&date={date}"))

def get_savant(season, kind):
    return list(csv.DictReader(fetch(f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats?type={kind}&min=10&season={season}&csv=true").splitlines()))

def get_splits(sit, season):
    return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/stats?stats=statSplits&group=hitting&sitCodes={sit}&season={season}&sportId=1&gameType=R&limit=2000"))

def get_season_hitting(season):
    return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/stats?stats=season&group=hitting&season={season}&sportId=1&playerPool=All&limit=3000"))

def get_recent_hitting(end_date_iso, days=15):
    """Bulk hitting stats over the last N days ending at end_date_iso (yyyy-mm-dd)."""
    end_d = dt.date.fromisoformat(end_date_iso)
    start_d = end_d - dt.timedelta(days=days)
    return json.loads(fetch(
        f"https://statsapi.mlb.com/api/v1/stats?stats=byDateRange&group=hitting"
        f"&startDate={start_d.isoformat()}&endDate={end_d.isoformat()}&sportId=1&playerPool=All&limit=3000"
    ))

def get_team_hitting(season):
    return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/teams/stats?stats=season&group=hitting&season={season}&sportId=1"))

def get_roster(team_id):
    try:
        r = json.loads(fetch(f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster?rosterType=active"))
        return [{"id": str(p["person"]["id"]), "name": p["person"]["fullName"], "pos": (p.get("position") or {}).get("abbreviation", "")}
                for p in r.get("roster", []) if (p.get("position") or {}).get("type") != "Pitcher"]
    except Exception:
        return []

def get_people_handedness(ids):
    if not ids: return {}
    try:
        ppl = json.loads(fetch(f"https://statsapi.mlb.com/api/v1/people?personIds={','.join(sorted(ids))}"))
        return {str(p["id"]): (p.get("pitchHand") or {}).get("code") for p in ppl.get("people", [])}
    except Exception:
        return {}

def get_team_bullpen(season):
    """Team bullpen pitching stats (relievers only) for the season."""
    try:
        return json.loads(fetch(
            f"https://statsapi.mlb.com/api/v1/teams/stats?stats=statSplits&sitCodes=rp"
            f"&group=pitching&season={season}&sportId=1&gameType=R"
        ))
    except Exception:
        return {}

def get_bvp(batter_id, pitcher_id):
    """Career batter-vs-pitcher splits. Tiny samples — heavy regression downstream."""
    try:
        d = json.loads(fetch(
            f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=vsPlayer"
            f"&group=hitting&opposingPlayerId={pitcher_id}&sportId=1",
            timeout=15
        ))
        splits = (d.get("stats", [{}])[0] or {}).get("splits", [])
        # Combine across all returned splits (career line)
        agg = {"plateAppearances": 0, "hits": 0, "doubles": 0, "triples": 0, "homeRuns": 0,
               "baseOnBalls": 0, "intentionalWalks": 0, "hitByPitch": 0}
        for s in splits:
            st = s.get("stat", {})
            for k in agg:
                agg[k] += st.get(k, 0) or 0
        return agg
    except Exception:
        return None

def get_bvt(batter_id, opp_team_id):
    """Career batter-vs-team splits. Stronger sample than BvP — divisional rivals can
    accumulate hundreds of PA over a career. Uses MLB Stats API stats=vsTeam endpoint.
    """
    try:
        d = json.loads(fetch(
            f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=vsTeam"
            f"&group=hitting&opposingTeamId={opp_team_id}&sportId=1",
            timeout=15
        ))
        splits = (d.get("stats", [{}])[0] or {}).get("splits", [])
        agg = {"plateAppearances": 0, "hits": 0, "doubles": 0, "triples": 0, "homeRuns": 0,
               "baseOnBalls": 0, "intentionalWalks": 0, "hitByPitch": 0}
        for s in splits:
            st = s.get("stat", {})
            for k in agg:
                agg[k] += st.get(k, 0) or 0
        return agg
    except Exception:
        return None

_TEAM_VENUE_CACHE = {}

def _get_team_venue_map():
    """Cached team_id -> home venue_id lookup. Used to derive each gameLog row's
    venue (the gameLog response itself doesn't include venue.id, only isHome and
    opponent.id, so home games map via the batter's team and away games via the
    opponent's team)."""
    global _TEAM_VENUE_CACHE
    if _TEAM_VENUE_CACHE:
        return _TEAM_VENUE_CACHE
    try:
        d = json.loads(fetch("https://statsapi.mlb.com/api/v1/teams?sportId=1&activeStatus=Y"))
        for t in d.get("teams", []):
            v = (t.get("venue") or {}).get("id")
            if v:
                _TEAM_VENUE_CACHE[t["id"]] = v
    except Exception:
        pass
    return _TEAM_VENUE_CACHE

def get_bvs(batter_id, venue_id, seasons):
    """Career batter stats at a specific venue, aggregated from per-season gameLog.

    The MLB Stats API doesn't expose a direct 'career-by-venue' endpoint, and gameLog
    rows don't include venue.id directly. We derive it: home games map to the batter's
    own team's home venue at the time of the game, away games map to the opponent
    team's home venue. Then we filter to splits whose derived venue matches the
    target and sum hitting stats. Sample sizes are usually modest (15-30 PA per
    visiting park per 2 years), so the downstream shrinkage prior is large.
    """
    if not venue_id:
        return None
    team_venue = _get_team_venue_map()
    if not team_venue:
        return None
    agg = {"plateAppearances": 0, "hits": 0, "doubles": 0, "triples": 0, "homeRuns": 0,
           "baseOnBalls": 0, "intentionalWalks": 0, "hitByPitch": 0,
           "runs": 0, "rbi": 0, "games": 0}
    any_found = False
    for season in seasons:
        try:
            d = json.loads(fetch(
                f"https://statsapi.mlb.com/api/v1/people/{batter_id}/stats?stats=gameLog"
                f"&group=hitting&season={season}&sportId=1",
                timeout=15
            ))
            splits = (d.get("stats", [{}])[0] or {}).get("splits", [])
            for s in splits:
                is_home = s.get("isHome")
                if is_home is True:
                    own_tid = (s.get("team") or {}).get("id")
                    game_venue = team_venue.get(own_tid)
                elif is_home is False:
                    opp_tid = (s.get("opponent") or {}).get("id")
                    game_venue = team_venue.get(opp_tid)
                else:
                    continue
                if game_venue != venue_id:
                    continue
                any_found = True
                st = s.get("stat", {}) or {}
                agg["games"] += 1
                for k in ("plateAppearances", "hits", "doubles", "triples", "homeRuns",
                          "baseOnBalls", "intentionalWalks", "hitByPitch", "runs", "rbi"):
                    agg[k] += int(st.get(k, 0) or 0)
        except Exception:
            continue
    return agg if any_found else None

# =============== THE ODDS API ===============
# Real sportsbook lines + prices for H+R+RBI props. Replaces the previous
# assumption that every line is 1.5 at -110 juice.

ODDS_API_BASE = "https://api.the-odds-api.com/v4"
ODDS_SPORT = "baseball_mlb"
ODDS_MARKET = "batter_hits_runs_rbis"    # combined H+R+RBI prop market key
# Books we consider when shopping for the best line. The Odds API returns every
# book in this region in a single request, so adding books here costs ZERO extra
# API quota — we just compare more lines per player.
ODDS_BOOKS = ["draftkings", "fanduel", "betmgm", "caesars", "fanatics"]

# ---- Free-tier budget optimization ----
# The Odds API bills 1 credit per market per event per call. With 15 games and
# 4 markets, we'd burn ~2,250 credits/month — far over the 500-credit free tier.
# Instead we compute internal game projections first, rank games by projected
# margin (biggest mismatches = highest expected edge), and only query the top-N
# games. For each of those we make ONE combined-market call.
#
# Cost math: (TOP_N_GAMES × 4 markets + 1 events-list call) × 30 days = credits/month.
# At TOP_N_GAMES=3 that's 390/month — well under the 500-credit free tier with buffer.
# Bump this to 4 (480/month) or 5 (610/month, needs paid tier) once you upgrade.
ODDS_TOP_N_GAMES = 3                       # games queried per day
ODDS_COMBINED_MARKETS = "h2h,totals,team_totals,batter_hits_runs_rbis"
# NOTE: 'spreads' (runline) intentionally omitted to stay within the free tier.
# Runline edges tend to be smaller than moneyline/total edges because of the
# fixed ±1.5 handicap and high margin variance.

def get_odds_events(api_key, date_iso):
    """List today's MLB events from The Odds API. One request burns 1 from the
    monthly quota and returns all events for the sport. We filter to date_iso."""
    if not api_key:
        return []
    try:
        d = json.loads(fetch(
            f"{ODDS_API_BASE}/sports/{ODDS_SPORT}/events?apiKey={api_key}&dateFormat=iso",
            timeout=20
        ))
        out = []
        for e in d:
            ct = (e.get("commence_time") or "")[:10]
            if ct == date_iso:
                out.append(e)
        return out
    except urllib.error.HTTPError as he:
        body = ""
        try: body = he.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        print(f"  WARN: odds events HTTP {he.code}: {body}", file=sys.stderr)
        return []
    except Exception as e:
        print(f"  WARN: odds events fetch failed: {e}", file=sys.stderr)
        return []

def get_odds_for_event(api_key, event_id):
    """Fetch H+R+RBI prop lines for one event across all US books. One call
    returns all players' lines for that event. Burns 1 request from the quota
    (or more if the plan multiplies by markets/regions — check headers)."""
    if not api_key or not event_id:
        return None
    try:
        return json.loads(fetch(
            f"{ODDS_API_BASE}/sports/{ODDS_SPORT}/events/{event_id}/odds"
            f"?apiKey={api_key}&regions=us&markets={ODDS_MARKET}&oddsFormat=american",
            timeout=20
        ))
    except urllib.error.HTTPError as he:
        body = ""
        try: body = he.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        print(f"  WARN: odds event {event_id} HTTP {he.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  WARN: odds event {event_id} fetch failed: {e}", file=sys.stderr)
        return None

def american_to_breakeven(price):
    """Convert American odds (e.g. -120, +145) to implied break-even probability."""
    if price is None:
        return None
    if price > 0:
        return 100.0 / (100 + price)
    return -price / (-price + 100.0)

def american_to_decimal(price):
    """Convert American odds to decimal odds (payout per $1 risked, including stake)."""
    if price is None:
        return None
    if price > 0:
        return 1 + price / 100.0
    return 1 + 100.0 / -price

def extract_player_lines_from_event(event_odds, books=ODDS_BOOKS):
    """From one event's odds response, return {normalized_name: [{line, over_price,
    under_price, book}, ...]} — a list with every (line, book) combination across
    ALL the books we consider. pick_best_line_for_player then iterates this list
    and returns whichever combination has the highest edge against our projection.

    Line shopping is FREE in API-quota terms because the single /events/{id}/odds
    call already returns all books for the requested region; we're just choosing
    to compare them instead of taking the first one.
    """
    out = {}
    if not event_odds:
        return out
    bookmakers = event_odds.get("bookmakers", []) or []
    bm_by_key = {b.get("key"): b for b in bookmakers}
    for book_key in books:
        bm = bm_by_key.get(book_key)
        if not bm: continue
        for market in bm.get("markets", []):
            if market.get("key") != ODDS_MARKET:
                continue
            # Build per-player {line -> {over, under}} from this book's outcomes
            per_player = {}
            for outcome in market.get("outcomes", []):
                if outcome.get("name") not in ("Over", "Under"):
                    continue
                name = outcome.get("description") or ""
                key = _normalize_name(name)
                if not key: continue
                pt = outcome.get("point")
                if pt is None: continue
                pp = per_player.setdefault(key, {}).setdefault(pt, {
                    "line": pt, "over_price": None, "under_price": None,
                    "book": bm.get("title", book_key),
                })
                if outcome.get("name") == "Over":
                    pp["over_price"] = outcome.get("price")
                else:
                    pp["under_price"] = outcome.get("price")
            # Append every (line, book) tuple — pick_best_line_for_player will
            # decide which one is best across all of them.
            for key, lines in per_player.items():
                out.setdefault(key, []).extend(lines.values())
    return out

def pick_best_line_for_player(player_lines, e_hrr):
    """Given the list of available (line, over_price, under_price) entries for a
    player from a book, plus the model's E[HRR], return the line with the best
    OVER edge (P(over) - break-even). Returns None if no usable line.

    Bettors should bet whichever line has the largest positive edge. If a player
    has alternates (e.g. O1.5 at -180 and O2.5 at +160), we pick the one where our
    model's projection beats the book's implied probability by the most.
    """
    if not player_lines:
        return None
    best = None
    for ln in player_lines:
        op = ln.get("over_price")
        if op is None:
            continue
        line_val = ln["line"]
        # P(over line): for "over 1.5" we want P(HRR >= 2); generalize via ceil(line + epsilon)
        k = int(math.ceil(line_val + 1e-9))
        p_over = poisson_p_geq_k(e_hrr, k)
        be = american_to_breakeven(op)
        edge = p_over - be
        scored = {
            "line": line_val, "over_price": op,
            "under_price": ln.get("under_price"),
            "book": ln.get("book"),
            "p_over": p_over, "breakeven": be, "edge": edge,
        }
        if best is None or edge > best["edge"]:
            best = scored
    return best

def get_weather(lat, lon, date_iso):
    """Open-Meteo daily forecast (no auth). Returns hourly arrays for the date."""
    try:
        url = (
            f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
            f"&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,precipitation"
            f"&temperature_unit=fahrenheit&wind_speed_unit=mph"
            f"&timezone=America%2FNew_York&start_date={date_iso}&end_date={date_iso}"
        )
        return json.loads(fetch(url, timeout=15))
    except Exception:
        return None

# =============== INDEXES ===============

def index_batters(rows):
    out = {}
    for r in rows:
        pid = r.get("player_id")
        if not pid: continue
        out.setdefault(pid, {"n": r["last_name, first_name"], "t": r["team_name_alt"], "bp": {}})
        out[pid]["bp"][r["pitch_type"]] = {"pa": int(num(r.get("pa")) or 0), "xw": num(r.get("est_woba")) if r.get("est_woba") else None}
    return out

def index_pitchers(rows):
    out = {}
    for r in rows:
        pid = r.get("player_id")
        if not pid: continue
        out.setdefault(pid, {"n": r["last_name, first_name"], "t": r["team_name_alt"], "bp": {}, "_tot": 0})
        pitches = num(r.get("pitches")) or 0
        out[pid]["bp"][r["pitch_type"]] = {"_p": pitches}
        out[pid]["_tot"] += pitches
    for pid in out:
        tot = out[pid]["_tot"]
        for c in out[pid]["bp"]:
            out[pid]["bp"][c]["u"] = (out[pid]["bp"][c]["_p"] / tot) if tot else 0
    return out

def league_pitch_mix(rows):
    counts = {}; total = 0
    for r in rows:
        p = num(r.get("pitches")) or 0
        counts[r["pitch_type"]] = counts.get(r["pitch_type"], 0) + p
        total += p
    return {c: counts[c] / total for c in counts} if total else {}

def league_xwoba_by_pitch(rows):
    n, d = {}, {}
    for r in rows:
        v = num(r.get("est_woba")); pa = num(r.get("pa")) or 0
        if v is None: continue
        c = r["pitch_type"]
        n[c] = n.get(c, 0) + v * pa
        d[c] = d.get(c, 0) + pa
    return {c: n[c] / d[c] for c in n if d[c] > 0}

def woba_from_components(st):
    pa = st.get("plateAppearances", 0); iw = st.get("intentionalWalks", 0)
    den = pa - iw
    if den <= 0: return None
    ubb = (st.get("baseOnBalls", 0) - iw); hbp = st.get("hitByPitch", 0)
    h = st.get("hits", 0); ds = st.get("doubles", 0); ts = st.get("triples", 0); hr = st.get("homeRuns", 0)
    ones = max(0, h - ds - ts - hr)
    return (W["uBB"]*ubb + W["HBP"]*hbp + W["1B"]*ones + W["2B"]*ds + W["3B"]*ts + W["HR"]*hr) / den

def index_handedness(vl, vr):
    hand = {}
    def absorb(splits, side):
        for s in splits:
            pid = str(s["player"]["id"]); st = s["stat"]
            h = hand.setdefault(pid, {"paL": 0, "paR": 0, "wL": None, "wR": None,
                                      "_c": {"uBB": 0, "HBP": 0, "1B": 0, "2B": 0, "3B": 0, "HR": 0, "den": 0}})
            if side == "L":
                h["paL"] = st.get("plateAppearances", 0); h["wL"] = woba_from_components(st)
            else:
                h["paR"] = st.get("plateAppearances", 0); h["wR"] = woba_from_components(st)
            iw = st.get("intentionalWalks", 0)
            hh = st.get("hits", 0); ds = st.get("doubles", 0); ts = st.get("triples", 0); hr = st.get("homeRuns", 0)
            c = h["_c"]
            c["uBB"] += st.get("baseOnBalls", 0) - iw
            c["HBP"] += st.get("hitByPitch", 0)
            c["1B"]  += max(0, hh - ds - ts - hr)
            c["2B"]  += ds; c["3B"] += ts; c["HR"] += hr
            c["den"] += st.get("plateAppearances", 0) - iw
    absorb(vl.get("stats", [{}])[0].get("splits", []), "L")
    absorb(vr.get("stats", [{}])[0].get("splits", []), "R")
    for pid in hand:
        c = hand[pid]["_c"]
        if c["den"] > 0:
            hand[pid]["overall"] = (W["uBB"]*c["uBB"] + W["HBP"]*c["HBP"] + W["1B"]*c["1B"] + W["2B"]*c["2B"] + W["3B"]*c["3B"] + W["HR"]*c["HR"]) / c["den"]
        else:
            hand[pid]["overall"] = None
    return hand

def handedness_factor(hand, bid, pitcher_hand):
    h = hand.get(bid)
    if not h or not pitcher_hand or not h.get("overall"): return 1.0
    pa = h["paL"] if pitcher_hand == "L" else h["paR"]
    wv = h["wL"]  if pitcher_hand == "L" else h["wR"]
    if pa <= 0 or wv is None: return 1.0
    regress = pa / (pa + 50)
    return 1 + regress * ((wv / h["overall"]) - 1)

def index_season_hitting(d):
    """Returns {player_id_str: {H_pa, R_pa, RBI_pa, PA, wOBA}}"""
    out = {}
    splits = d.get("stats", [{}])[0].get("splits", [])
    for s in splits:
        pid = str(s["player"]["id"]); st = s["stat"]
        pa = st.get("plateAppearances", 0)
        if pa <= 0: continue
        w = woba_from_components(st)
        out[pid] = {
            "PA": pa,
            "H_pa":   (st.get("hits", 0) or 0) / pa,
            "R_pa":   (st.get("runs", 0) or 0) / pa,
            "RBI_pa": (st.get("rbi",  0) or 0) / pa,
            "wOBA":   w if w is not None else 0.310,
        }
    return out

# Shrinkage prior: at recent_PA = 100 the recent and season weight equally.
RECENT_PRIOR_PA = 100

def blend_season_recent(season_rates, recent_rates):
    """Blend recent (last-N-days) rates with season baselines using shrinkage toward season."""
    if not season_rates: return None
    if not recent_rates or recent_rates.get("PA", 0) <= 0:
        return {**season_rates, "recent_PA": 0, "recent_weight": 0.0,
                "season_H_pa": season_rates["H_pa"], "season_R_pa": season_rates["R_pa"], "season_RBI_pa": season_rates["RBI_pa"],
                "recent_H_pa": None, "recent_R_pa": None, "recent_RBI_pa": None}
    r_pa = recent_rates["PA"]
    w = r_pa / (r_pa + RECENT_PRIOR_PA)
    return {
        "PA": season_rates["PA"],
        "recent_PA": r_pa,
        "recent_weight": w,
        "H_pa":   w * recent_rates["H_pa"]   + (1 - w) * season_rates["H_pa"],
        "R_pa":   w * recent_rates["R_pa"]   + (1 - w) * season_rates["R_pa"],
        "RBI_pa": w * recent_rates["RBI_pa"] + (1 - w) * season_rates["RBI_pa"],
        "season_H_pa":   season_rates["H_pa"],
        "season_R_pa":   season_rates["R_pa"],
        "season_RBI_pa": season_rates["RBI_pa"],
        "recent_H_pa":   recent_rates["H_pa"],
        "recent_R_pa":   recent_rates["R_pa"],
        "recent_RBI_pa": recent_rates["RBI_pa"],
    }

def index_team_hitting(d):
    """Returns {team_id_int: r_per_game} and the league avg R/G."""
    by_team = {}
    splits = d.get("stats", [{}])[0].get("splits", [])
    tot_r = 0; tot_g = 0
    for s in splits:
        tid = s["team"]["id"]; st = s["stat"]
        g = st.get("gamesPlayed", 0) or 0; r = st.get("runs", 0) or 0
        if g > 0:
            by_team[tid] = r / g
            tot_r += r; tot_g += g
    league_rpg = (tot_r / tot_g) if tot_g else 4.5
    return by_team, league_rpg

def index_team_bullpen(d):
    """Returns {team_id_int: opp_woba_against} and the league avg opp wOBA for relievers."""
    by_team = {}
    splits = (d.get("stats", [{}])[0] or {}).get("splits", []) if d else []
    n_num, n_den = 0.0, 0
    for s in splits:
        tid = s.get("team", {}).get("id"); st = s.get("stat", {})
        if not tid: continue
        # Approximate opponent wOBA using FIP-style components from team pitching stats
        pa = (st.get("battersFaced", 0) or 0)
        iw = (st.get("intentionalWalks", 0) or 0)
        den = pa - iw
        if den <= 0: continue
        ubb = (st.get("baseOnBalls", 0) or 0) - iw
        hbp = (st.get("hitByPitch", 0) or 0)
        h   = (st.get("hits", 0) or 0)
        ds  = (st.get("doubles", 0) or 0)
        ts  = (st.get("triples", 0) or 0)
        hr  = (st.get("homeRuns", 0) or 0)
        ones = max(0, h - ds - ts - hr)
        w = (W["uBB"]*ubb + W["HBP"]*hbp + W["1B"]*ones + W["2B"]*ds + W["3B"]*ts + W["HR"]*hr) / den
        by_team[tid] = w
        n_num += w * den; n_den += den
    league_bp_woba = (n_num / n_den) if n_den else 0.310
    return by_team, league_bp_woba

def park_factor(home_abbr):
    return PARK_FACTORS.get(home_abbr, DEFAULT_PARK)

def weather_factor(home_abbr, date_iso, weather_cache):
    """Composite weather multiplier for offense at the venue on game date.

    Combines temperature (warmer ≈ more offense), wind (out boosts, in suppresses),
    and precipitation. Domes are always neutral; retractables get half-weighted.
    Capped to ±8%. Returns (factor, label).
    """
    info = STADIUMS.get(home_abbr)
    if not info:
        return 1.0, "n/a"
    lat, lon, roof, orient = info
    if roof == "dome":
        return 1.0, "dome"

    key = (home_abbr, date_iso)
    if key in weather_cache:
        return weather_cache[key]

    w = get_weather(lat, lon, date_iso)
    if not w or "hourly" not in w:
        weather_cache[key] = (1.0, "unavailable")
        return weather_cache[key]

    try:
        temps = w["hourly"]["temperature_2m"]
        winds = w["hourly"]["wind_speed_10m"]
        wdirs = w["hourly"]["wind_direction_10m"]
        precs = w["hourly"]["precipitation"]
        # Game hours window 18-22 local (covers most evening starts; day games will smear)
        idx = [i for i in range(18, 22) if i < len(temps)]
        if not idx:
            weather_cache[key] = (1.0, "unavailable")
            return weather_cache[key]
        temp = sum(temps[i] for i in idx) / len(idx)
        wind = sum(winds[i] for i in idx) / len(idx)
        wdir = sum(wdirs[i] for i in idx) / len(idx)
        prec = sum(precs[i] for i in idx) / len(idx)

        # Temperature: 70F neutral, ±0.4% per F. 90F → ~1.08, 50F → ~0.92.
        temp_f = 1.0 + (temp - 70) * 0.004
        temp_f = max(0.92, min(1.08, temp_f))

        # Wind: orient = compass bearing from home plate to center field.
        # Wind blowing OUT to center = wdir near (orient + 180) % 360 (wind comes from home plate side).
        out_dir = (orient + 180) % 360
        diff = abs(((wdir - out_dir) + 540) % 360 - 180)  # 0=blowing out, 180=blowing in
        if diff < 60:
            wind_f = 1.0 + wind * 0.0025   # blowing out: 10 mph ≈ +2.5%
        elif diff > 120:
            wind_f = 1.0 - wind * 0.0025   # blowing in: 10 mph ≈ -2.5%
        else:
            wind_f = 1.0
        wind_f = max(0.94, min(1.06, wind_f))

        # Precipitation: mild suppression once it's actually raining
        prec_f = 1.0 - min(0.04, prec * 0.05)

        factor = temp_f * wind_f * prec_f

        # Retractable roof: closed in bad weather, otherwise half-weighted exposure
        if roof == "retractable":
            if prec > 0.05 or temp < 50 or temp > 95:
                weather_cache[key] = (1.0, f"{roof} (likely closed)")
                return weather_cache[key]
            factor = 0.5 * factor + 0.5  # half exposure

        factor = max(0.92, min(1.08, factor))
        wind_str = "out" if diff < 60 else "in" if diff > 120 else "cross"
        label = f"{temp:.0f}F, {wind:.0f}mph {wind_str}"
        weather_cache[key] = (factor, label)
        return weather_cache[key]
    except Exception:
        weather_cache[key] = (1.0, "parse error")
        return weather_cache[key]

def bullpen_factor(opp_team_id, bullpen_map, league_bp_woba):
    """Bullpen multiplier on hits/runs/RBIs, applied to the ~35% of PA vs relievers.

    Returns (full_pa_multiplier, raw_bp_ratio). The full-PA multiplier blends
    (1 - share) * 1.0 + share * bp_ratio, so a bad bullpen lifts the whole projection a bit.
    """
    if not opp_team_id or not bullpen_map or not league_bp_woba:
        return 1.0, 1.0
    opp_w = bullpen_map.get(opp_team_id)
    if not opp_w:
        return 1.0, 1.0
    bp_ratio = opp_w / league_bp_woba
    bp_ratio = max(0.85, min(1.15, bp_ratio))
    blended = (1 - BULLPEN_PA_SHARE) + BULLPEN_PA_SHARE * bp_ratio
    return blended, bp_ratio

def bvp_factor(bvp_stats, batter_season_woba):
    """Career batter-vs-pitcher factor, heavily regressed because samples are tiny.

    weight = bvp_PA / (bvp_PA + 30). Factor = 1 + weight * (bvp_wOBA/season_wOBA - 1).
    Capped 0.85–1.20. Returns (factor, pa, bvp_woba_or_None).
    """
    if not bvp_stats or not batter_season_woba or batter_season_woba <= 0:
        return 1.0, 0, None
    pa = bvp_stats.get("plateAppearances", 0) or 0
    if pa <= 0:
        return 1.0, 0, None
    iw = bvp_stats.get("intentionalWalks", 0) or 0
    den = pa - iw
    if den <= 0:
        return 1.0, pa, None
    ubb = (bvp_stats.get("baseOnBalls", 0) or 0) - iw
    hbp = bvp_stats.get("hitByPitch", 0) or 0
    h   = bvp_stats.get("hits", 0) or 0
    ds  = bvp_stats.get("doubles", 0) or 0
    ts  = bvp_stats.get("triples", 0) or 0
    hr  = bvp_stats.get("homeRuns", 0) or 0
    ones = max(0, h - ds - ts - hr)
    bvp_woba = (W["uBB"]*ubb + W["HBP"]*hbp + W["1B"]*ones + W["2B"]*ds + W["3B"]*ts + W["HR"]*hr) / den
    weight = pa / (pa + 30.0)
    raw = bvp_woba / batter_season_woba
    factor = 1.0 + weight * (raw - 1.0)
    factor = max(0.85, min(1.20, factor))
    return factor, pa, bvp_woba

def bvt_factor(bvt_stats, batter_season_woba):
    """Career batter-vs-team factor, regressed by PA/(PA+100). Capped 0.90–1.15.

    BvT samples are typically larger than BvP (a hitter sees a team 15-19 times/year
    across all pitchers), so the prior is heavier but the cap is tighter — most of
    the BvP/handedness signal is already captured elsewhere, this is the residual.
    Returns (factor, pa, bvt_woba_or_None).
    """
    if not bvt_stats or not batter_season_woba or batter_season_woba <= 0:
        return 1.0, 0, None
    pa = bvt_stats.get("plateAppearances", 0) or 0
    if pa <= 0:
        return 1.0, 0, None
    iw = bvt_stats.get("intentionalWalks", 0) or 0
    den = pa - iw
    if den <= 0:
        return 1.0, pa, None
    ubb = (bvt_stats.get("baseOnBalls", 0) or 0) - iw
    hbp = bvt_stats.get("hitByPitch", 0) or 0
    h   = bvt_stats.get("hits", 0) or 0
    ds  = bvt_stats.get("doubles", 0) or 0
    ts  = bvt_stats.get("triples", 0) or 0
    hr  = bvt_stats.get("homeRuns", 0) or 0
    ones = max(0, h - ds - ts - hr)
    bvt_woba = (W["uBB"]*ubb + W["HBP"]*hbp + W["1B"]*ones + W["2B"]*ds + W["3B"]*ts + W["HR"]*hr) / den
    weight = pa / (pa + 100.0)
    raw = bvt_woba / batter_season_woba
    factor = 1.0 + weight * (raw - 1.0)
    factor = max(0.90, min(1.15, factor))
    return factor, pa, bvt_woba

def bvs_factor(bvs_stats, batter_season_woba):
    """Career batter-at-venue factor, regressed by PA/(PA+150). Capped 0.92–1.08.

    Visitor stadium samples are small (15-30 PA over 2 years for a non-rival), so the
    prior is heaviest of the three and the cap is the tightest. The park factor table
    already captures venue-wide offense; this is the residual player-specific tendency
    at a particular park (Judge at Fenway, Bregman in his home park, etc.).
    Returns (factor, pa, bvs_woba_or_None).
    """
    if not bvs_stats or not batter_season_woba or batter_season_woba <= 0:
        return 1.0, 0, None
    pa = bvs_stats.get("plateAppearances", 0) or 0
    if pa <= 0:
        return 1.0, 0, None
    iw = bvs_stats.get("intentionalWalks", 0) or 0
    den = pa - iw
    if den <= 0:
        return 1.0, pa, None
    ubb = (bvs_stats.get("baseOnBalls", 0) or 0) - iw
    hbp = bvs_stats.get("hitByPitch", 0) or 0
    h   = bvs_stats.get("hits", 0) or 0
    ds  = bvs_stats.get("doubles", 0) or 0
    ts  = bvs_stats.get("triples", 0) or 0
    hr  = bvs_stats.get("homeRuns", 0) or 0
    ones = max(0, h - ds - ts - hr)
    bvs_woba = (W["uBB"]*ubb + W["HBP"]*hbp + W["1B"]*ones + W["2B"]*ds + W["3B"]*ts + W["HR"]*hr) / den
    weight = pa / (pa + 150.0)
    raw = bvs_woba / batter_season_woba
    factor = 1.0 + weight * (raw - 1.0)
    factor = max(0.92, min(1.08, factor))
    return factor, pa, bvs_woba

# =============== xwOBA MATCHUP ===============

def matchup_xwoba(batters, pitchers, league_mix, league_xwoba, bid, pid):
    b = batters.get(bid); p = pitchers.get(pid)
    if not b or not p: return None
    m = base = used = impact = 0.0
    for c, pv in p["bp"].items():
        u = pv["u"]; lw = league_xwoba.get(c)
        if not u or lw is None: continue
        bv = b["bp"].get(c)
        usable = bv and bv["pa"] >= 10 and bv["xw"] is not None
        m += u * (bv["xw"] if usable else lw)
        used += u
        if usable: impact += u
    for c, lu in league_mix.items():
        lw = league_xwoba.get(c)
        if lw is None: continue
        bv = b["bp"].get(c)
        usable = bv and bv["pa"] >= 10 and bv["xw"] is not None
        base += lu * (bv["xw"] if usable else lw)
    mn = m / used if used > 0 else m
    return {"matchup": mn, "baseline": base, "impact": impact}

# =============== PLATT CALIBRATION ===============
# After ~422 graded picks the model was found to be ~27 percentage points
# overconfident on P(over 1.5) — predicted mean ~79% vs actual rate ~52%.
# Platt scaling fits a one-time logistic transform that pulls each raw
# prediction toward the observed hit rate at that level. Parameters are
# fit by calibrate_predictions.py and stored in calibration_params.json;
# this script just loads them and applies the transform.

CALIBRATION_FILE = "calibration_params.json"

def load_calibration():
    """Return calibration_params.json contents, or None if not fitted yet."""
    try:
        with open(CALIBRATION_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _cal_sigmoid(x):
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)

def _cal_logit(p):
    EPS = 1e-9
    p = max(EPS, min(1 - EPS, p))
    return math.log(p / (1 - p))

def apply_calibration(raw_p, params):
    """Transform a raw model probability via the fitted Platt scaling."""
    if params is None or raw_p is None:
        return raw_p
    return _cal_sigmoid(params["a"] + params["b"] * _cal_logit(raw_p))

# =============== POISSON ===============

def poisson_p_geq_k(lam, k):
    """P(X >= k) where X ~ Poisson(lam)."""
    if lam <= 0: return 0.0 if k > 0 else 1.0
    if k <= 0: return 1.0
    # 1 - sum_{i=0}^{k-1} P(X=i)
    term = math.exp(-lam)  # P(X=0)
    total = term
    for i in range(1, k):
        term *= lam / i  # turn P(X=i-1) into P(X=i)
        total += term
    return max(0.0, min(1.0, 1.0 - total))

# =============== HRR PROJECTION ===============

def project_hrr(bid, pid, lineup_pos, batters, pitchers, league_mix, league_xwoba, hand, p_hand_map,
                season, team_rpg_map, league_rpg, team_id_of_batter, recent_hit=None,
                park_mult=1.0, weather_mult=1.0, weather_label="",
                bullpen_mult=1.0, bullpen_ratio=1.0,
                bvp_mult=1.0, bvp_pa=0, bvp_woba=None,
                bvt_mult=1.0, bvt_pa=0, bvt_woba=None,
                bvs_mult=1.0, bvs_pa=0, bvs_woba=None):
    """Project expected H+R+RBI for a batter against today's pitcher, and prob of over lines."""
    base_xw = matchup_xwoba(batters, pitchers, league_mix, league_xwoba, bid, pid)
    if not base_xw: return None
    ph = p_hand_map.get(pid)
    hf = handedness_factor(hand, bid, ph)

    # Quality multiplier from matchup xwOBA vs the player's "vs league average mix" baseline.
    # Capped to avoid extreme outliers driven by tiny sample sizes.
    if base_xw["baseline"] > 0.15:
        quality_mult = base_xw["matchup"] / base_xw["baseline"]
    else:
        quality_mult = 1.0
    quality_mult = max(0.75, min(1.35, quality_mult))

    # Team offense factor — affects runs and RBIs (teammates ahead/behind in the order).
    team_rpg = team_rpg_map.get(team_id_of_batter, league_rpg)
    team_factor = team_rpg / max(0.1, league_rpg)
    team_factor = max(0.85, min(1.15, team_factor))

    season_stats = season.get(bid)
    if not season_stats:
        # Fallback to league averages if no season stats yet (e.g., rookie just called up).
        season_stats = {"H_pa": 0.225, "R_pa": 0.115, "RBI_pa": 0.110, "PA": 0, "wOBA": 0.310}
    # Blend with last-15-days form (shrunk toward season).
    blended = blend_season_recent(season_stats, (recent_hit or {}).get(bid))

    # Combined environment multiplier applied uniformly to H/R/RBI.
    env_mult = park_mult * weather_mult * bullpen_mult * bvp_mult * bvt_mult * bvs_mult

    # Adjusted per-PA rates use the blended (season + recent) rates.
    adj_h   = blended["H_pa"]   * quality_mult * hf * env_mult
    adj_r   = blended["R_pa"]   * quality_mult * hf * team_factor * env_mult
    adj_rbi = blended["RBI_pa"] * quality_mult * hf * team_factor * env_mult

    pa = PA_BY_ORDER.get(lineup_pos, DEFAULT_PA)

    e_h   = adj_h   * pa
    e_r   = adj_r   * pa
    e_rbi = adj_rbi * pa
    e_hrr = e_h + e_r + e_rbi

    p_over_15 = poisson_p_geq_k(e_hrr, 2)  # over 1.5 = at least 2
    p_over_25 = poisson_p_geq_k(e_hrr, 3)  # over 2.5 = at least 3

    edge_15 = p_over_15 - BREAKEVEN_PROB
    edge_25 = p_over_25 - BREAKEVEN_PROB
    best_line = "1.5" if edge_15 >= edge_25 else "2.5"
    best_edge = max(edge_15, edge_25)

    return {
        "matchup_xwoba": base_xw["matchup"],
        "baseline_xwoba": base_xw["baseline"],
        "impact": base_xw["impact"],
        "ph": ph, "hf": hf,
        "quality_mult": quality_mult, "team_factor": team_factor,
        "park_mult": park_mult,
        "weather_mult": weather_mult, "weather_label": weather_label,
        "bullpen_mult": bullpen_mult, "bullpen_ratio": bullpen_ratio,
        "bvp_mult": bvp_mult, "bvp_pa": bvp_pa, "bvp_woba": bvp_woba,
        "bvt_mult": bvt_mult, "bvt_pa": bvt_pa, "bvt_woba": bvt_woba,
        "bvs_mult": bvs_mult, "bvs_pa": bvs_pa, "bvs_woba": bvs_woba,
        "env_mult": env_mult,
        "expected_pa": pa, "lineup_pos": lineup_pos,
        "season_pa": season_stats["PA"],
        "season_h_pa": season_stats["H_pa"],
        "season_r_pa": season_stats["R_pa"],
        "season_rbi_pa": season_stats["RBI_pa"],
        "season_woba": season_stats.get("wOBA"),
        "recent_pa": blended.get("recent_PA", 0),
        "recent_weight": blended.get("recent_weight", 0.0),
        "recent_h_pa": blended.get("recent_H_pa"),
        "recent_r_pa": blended.get("recent_R_pa"),
        "recent_rbi_pa": blended.get("recent_RBI_pa"),
        "blended_h_pa": blended["H_pa"],
        "blended_r_pa": blended["R_pa"],
        "blended_rbi_pa": blended["RBI_pa"],
        "e_h": e_h, "e_r": e_r, "e_rbi": e_rbi, "e_hrr": e_hrr,
        "p_over_15": p_over_15, "p_over_25": p_over_25,
        "edge_15": edge_15, "edge_25": edge_25,
        "best_line": best_line, "best_edge": best_edge,
    }

# =============== EMAIL ===============

def send_email(api_key, to_email, subject, html_body, text_body):
    payload = json.dumps({
        "from": "onboarding@resend.dev", "to": [to_email],
        "subject": subject, "html": html_body, "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
        headers={
            "Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
            "User-Agent": "Mozilla/5.0 (compatible; MLB-PropEdge/1.0)", "Accept": "application/json",
        }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Resend API error HTTP {e.code}: {body}", file=sys.stderr)
        raise

def fmt_pct(p): return f"{p*100:.1f}%"
def fmt_edge_pct(e): return f"{e*100:+.1f}%"

def build_team_market_picks_html(team_market_picks, game_projections, top_n=15, min_edge=0.01):
    """Render the primary team-market picks panel: team totals, game totals,
    moneylines, and runlines, sorted by edge, filtered to positive-edge only."""
    positive = [p for p in team_market_picks if (p.get("edge") or 0) >= min_edge]
    if not positive and not game_projections:
        return ("<div style='margin:14px 0;padding:12px 14px;background:#fff;border:1px solid #e5e7eb;"
                "border-radius:8px;font-size:12px;color:#6b7280'>"
                "<strong style='color:#111827'>Team & Game Market Edges:</strong> no positive-edge "
                "picks found today, or Odds API not configured.</div>")

    def market_label(m):
        return {"team_total": "Team Total", "game_total": "Game Total",
                "moneyline": "Moneyline", "runline": "Runline"}.get(m, m)
    def side_cell(p):
        m = p.get("market", "")
        if m == "team_total":
            return f"<strong>{p.get('team','?')}</strong> {p.get('side','')} {p.get('line','?')}"
        if m == "game_total":
            return f"{p.get('side','')} {p.get('line','?')}"
        if m == "moneyline":
            return f"<strong>{p.get('team','?')}</strong> ML"
        if m == "runline":
            return f"<strong>{p.get('team','?')}</strong> {p.get('side','')}"
        return "?"
    def price_str(p):
        pr = p.get("price")
        if pr is None: return "&mdash;"
        return f"{int(pr):+d}" if pr > 0 else str(int(pr))
    def proj_str(p):
        pj = p.get("projected")
        if pj is None: return "&mdash;"
        if p.get("market") == "runline":
            return f"margin {pj:+.1f}"
        return f"{pj:.2f}"

    rows_html = []
    for i, p in enumerate(positive[:top_n], 1):
        edge = p.get("edge", 0)
        edge_color = "#166534" if edge > 0.05 else "#059669" if edge > 0.02 else "#6b7280"
        rows_html.append(
            f"<tr style='border-top:1px solid #f3f4f6'>"
            f"<td style='padding:6px 10px;color:#9ca3af;font-size:11px'>{i}</td>"
            f"<td style='padding:6px 10px;color:#6b7280;font-size:11px'>{market_label(p.get('market',''))}</td>"
            f"<td style='padding:6px 10px'>{side_cell(p)} <span style='color:#9ca3af;font-size:11px'>({p.get('game_label','')})</span></td>"
            f"<td style='padding:6px 10px'>{price_str(p)}</td>"
            f"<td style='padding:6px 10px;font-size:11px;color:#6b7280'>{p.get('book','&mdash;')}</td>"
            f"<td style='padding:6px 10px;font-size:11px'>{proj_str(p)}</td>"
            f"<td style='padding:6px 10px;font-size:11px'>{fmt_pct(p.get('p_win', 0))}</td>"
            f"<td style='padding:6px 10px;font-size:11px'>{fmt_pct(p.get('breakeven', 0.5))}</td>"
            f"<td style='padding:6px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(edge)}</td>"
            f"</tr>"
        )
    return (
        f"<div style='margin:14px 0;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<div style='padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:13px'>"
        f"Team &amp; Game Market Edges &mdash; primary picks "
        f"<span style='color:#9ca3af;font-weight:400;font-size:11px'>"
        f"({len(positive)} positive-edge picks across {len(game_projections)} games)</span>"
        f"</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
        f"<thead style='color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left;background:#fafafa'>"
        f"<tr>"
        f"<th style='padding:6px 10px'>#</th><th style='padding:6px 10px'>Market</th>"
        f"<th style='padding:6px 10px'>Pick</th><th style='padding:6px 10px'>Price</th>"
        f"<th style='padding:6px 10px'>Book</th><th style='padding:6px 10px'>Projection</th>"
        f"<th style='padding:6px 10px'>Model P</th><th style='padding:6px 10px'>Break-even</th>"
        f"<th style='padding:6px 10px'>Edge</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f"</div>"
    )

def build_game_projections_html(game_projections):
    """Render the game-projection reference table (one row per game with each
    team's projected runs). Purely informational — bettors can eyeball where
    the model sees mismatches."""
    if not game_projections:
        return ""
    rows_html = []
    for gp in sorted(game_projections, key=lambda x: -abs(x["margin"])):
        margin_color = "#166534" if abs(gp["margin"]) > 1.5 else "#6b7280"
        p_str = f"{gp['p_home_win']*100:.0f}% home"
        rows_html.append(
            f"<tr style='border-top:1px solid #f3f4f6'>"
            f"<td style='padding:6px 10px'>{gp['away_abbr']} @ {gp['home_abbr']}</td>"
            f"<td style='padding:6px 10px'>{gp['away_proj']['e_r']:.2f}</td>"
            f"<td style='padding:6px 10px'>{gp['home_proj']['e_r']:.2f}</td>"
            f"<td style='padding:6px 10px'>{gp['game_total']:.2f}</td>"
            f"<td style='padding:6px 10px;color:{margin_color};font-weight:600'>{gp['margin']:+.2f}</td>"
            f"<td style='padding:6px 10px;font-size:11px;color:#6b7280'>{p_str}</td>"
            f"</tr>"
        )
    return (
        f"<div style='margin:14px 0;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<div style='padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:13px'>"
        f"Game-Level Projections <span style='color:#9ca3af;font-weight:400;font-size:11px'>"
        f"(sorted by absolute run margin)</span>"
        f"</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
        f"<thead style='color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left;background:#fafafa'>"
        f"<tr><th style='padding:6px 10px'>Game</th><th style='padding:6px 10px'>Away R</th>"
        f"<th style='padding:6px 10px'>Home R</th><th style='padding:6px 10px'>Total</th>"
        f"<th style='padding:6px 10px'>Margin (Home-Away)</th>"
        f"<th style='padding:6px 10px'>Win Prob</th></tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f"</div>"
    )

def fmt_american_price(p):
    """Format American odds as '+135' or '-120'."""
    if p is None:
        return ""
    return f"{int(p):+d}"

def fmt_book_cell(r):
    """Render the 'Book' table cell: 'DK O1.5 -120' when real odds available,
    '&mdash;' when not. Bookmaker short codes for compactness."""
    if r.get("real_line") is None:
        return "&mdash;"
    book = (r.get("real_book") or "").upper()
    short = {"DRAFTKINGS": "DK", "FANDUEL": "FD", "BETMGM": "MGM",
             "CAESARS": "CSR", "POINTSBETUS": "PB"}.get(book, book[:3])
    return f"{short} O{r['real_line']} {fmt_american_price(r['real_over_price'])}"

def display_edge(r):
    """When we have a real-edge value (Odds API matched), show that. Otherwise
    fall back to the model's edge vs assumed -110."""
    return r.get("real_edge") if r.get("real_edge") is not None else r.get("best_edge", 0.0)

def _build_top5_commentary_block(rows):
    """Render the 'Why these picks?' section for the top 5 picks (or all rows if fewer)."""
    if not rows:
        return ""
    items = []
    for i, r in enumerate(rows[:5], 1):
        commentary = build_pick_commentary(r)
        items.append(
            f"<li style='margin:0 0 10px 0;line-height:1.5'>"
            f"<span style='color:#9ca3af;font-weight:600;margin-right:6px'>{i}.</span>"
            f"<span>{commentary}</span>"
            f"</li>"
        )
    return (
        f"<div style='margin:14px 0;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<div style='padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:13px'>"
        f"Why these picks? &mdash; commentary on the top {len(rows[:5])}"
        f"</div>"
        f"<ol style='margin:0;padding:12px 18px 12px 32px;font-size:12px;color:#374151;list-style:none'>"
        f"{''.join(items)}"
        f"</ol>"
        f"</div>"
    )

def build_pick_commentary(r):
    """Return a 2-sentence plain-English explanation of why this pick has its edge.

    Sentence 1: the dominant factor(s) driving the projection — boosters first, then
    drags, picking only factors whose multiplier is at least ±5% out of neutral so we
    don't bury the reader in 1.00x noise.
    Sentence 2: the resulting numerical projection (E[HRR], P(over), edge vs -110)
    plus any meaningful context (lineup slot, recent form, coverage).
    """
    name = r.get("name", "?")
    team = r.get("team", "?")
    opp  = r.get("opp", "?")
    line = r.get("best_line", "1.5")
    p_over = r.get("p_over_15", 0.0) if line == "1.5" else r.get("p_over_25", 0.0)
    edge = r.get("best_edge", 0.0) * 100
    e_hrr = r.get("e_hrr", 0.0)
    park_label = r.get("park_label", "")
    opp_team = r.get("opp_team", "")

    boosters = []   # factor descriptions where mult >= 1.05
    drags    = []   # factor descriptions where mult <= 0.95
    notes    = []   # context (PA, lineup slot, recent form, coverage)

    # Pitcher-matchup quality (xwOBA-derived)
    qm = r.get("quality_mult", 1.0)
    if qm >= 1.08:
        boosters.append(f"strong pitch-arsenal matchup vs {opp} (Quality &times;{qm:.2f})")
    elif qm <= 0.92:
        drags.append(f"poor pitch-arsenal matchup vs {opp} (Quality &times;{qm:.2f})")

    # Handedness
    ph = r.get("ph")
    hf = r.get("hf", 1.0)
    if hf >= 1.08 and ph:
        boosters.append(f"big platoon edge vs {ph}HP (&times;{hf:.2f})")
    elif hf <= 0.92 and ph:
        drags.append(f"tough handedness matchup vs {ph}HP (&times;{hf:.2f})")

    # Park
    pm = r.get("park_mult", 1.0)
    if pm >= 1.05:
        boosters.append(f"hitter-friendly park at {park_label} (&times;{pm:.2f})")
    elif pm <= 0.95:
        drags.append(f"pitcher-friendly park at {park_label} (&times;{pm:.2f})")

    # Weather
    wm = r.get("weather_mult", 1.0)
    wl = r.get("weather_label", "") or ""
    if wm >= 1.03:
        boosters.append(f"favorable weather ({wl}, &times;{wm:.2f})")
    elif wm <= 0.97:
        drags.append(f"unfavorable weather ({wl}, &times;{wm:.2f})")

    # Bullpen
    bp = r.get("bullpen_mult", 1.0)
    if bp >= 1.02:
        boosters.append(f"weak opposing bullpen (&times;{bp:.2f})")
    elif bp <= 0.98:
        drags.append(f"strong opposing bullpen (&times;{bp:.2f})")

    # BvP (only mention with PA >= 5; otherwise tiny sample noise)
    if r.get("bvp_pa", 0) >= 5:
        bvpm = r.get("bvp_mult", 1.0)
        if bvpm >= 1.05:
            boosters.append(f"strong career vs this pitcher ({r['bvp_pa']}PA, &times;{bvpm:.2f})")
        elif bvpm <= 0.95:
            drags.append(f"poor career vs this pitcher ({r['bvp_pa']}PA, &times;{bvpm:.2f})")

    # BvT (only mention with PA >= 30; samples are larger than BvP)
    if r.get("bvt_pa", 0) >= 30:
        bvtm = r.get("bvt_mult", 1.0)
        if bvtm >= 1.04:
            boosters.append(f"hits {opp_team or 'this team'} well over his career ({r['bvt_pa']}PA, &times;{bvtm:.2f})")
        elif bvtm <= 0.96:
            drags.append(f"poor career vs {opp_team or 'this team'} ({r['bvt_pa']}PA, &times;{bvtm:.2f})")

    # BvS (only mention with PA >= 20; samples are smallest of the three)
    if r.get("bvs_pa", 0) >= 20:
        bvsm = r.get("bvs_mult", 1.0)
        if bvsm >= 1.04:
            boosters.append(f"thrives at {park_label} ({r['bvs_pa']}PA in last 2 seasons, &times;{bvsm:.2f})")
        elif bvsm <= 0.96:
            drags.append(f"struggles at {park_label} ({r['bvs_pa']}PA in last 2 seasons, &times;{bvsm:.2f})")

    # Recent-form delta (last 15 days vs season per-PA HRR rate)
    s_hrr = (r.get("season_h_pa") or 0) + (r.get("season_r_pa") or 0) + (r.get("season_rbi_pa") or 0)
    rh = r.get("recent_h_pa")
    if rh is not None and (r.get("recent_pa") or 0) > 0 and s_hrr > 0:
        r_hrr = rh + (r.get("recent_r_pa") or 0) + (r.get("recent_rbi_pa") or 0)
        delta = (r_hrr - s_hrr) / s_hrr
        if delta > 0.12:
            notes.append(f"hot 15-day form (+{delta*100:.0f}% vs season on {r.get('recent_pa')} PA)")
        elif delta < -0.12:
            notes.append(f"cold 15-day form ({delta*100:.0f}% vs season on {r.get('recent_pa')} PA)")

    # Lineup spot — only flag the extremes
    pos = r.get("lineup_pos")
    epa = r.get("expected_pa", 0)
    if pos:
        if pos <= 3:
            notes.append(f"top-3 lineup slot (~{epa:.1f} PA)")
        elif pos >= 7:
            notes.append(f"bottom-of-order ({epa:.1f} PA limits ceiling)")
    else:
        notes.append("lineup not yet confirmed")

    # Build sentence 1: the matchup story
    if boosters and drags:
        s1 = (f"{name} ({team}) faces {opp}: "
              + " and ".join(boosters[:2])
              + ", offset by " + " and ".join(drags[:2]) + ".")
    elif boosters:
        s1 = (f"{name} ({team}) faces {opp}: "
              + " and ".join(boosters[:3]) + ".")
    elif drags:
        s1 = (f"{name} ({team}) faces {opp}: " + " and ".join(drags[:3])
              + " — the edge comes from baseline rates and lineup spot.")
    else:
        s1 = (f"{name} ({team}) faces {opp}: no individual factor stands out at &gt;5%, "
              "so the edge comes from solid baseline rates plus a favorable PA count.")

    # Build sentence 2: numbers + context
    note_text = (" (" + "; ".join(notes) + ")") if notes else ""
    # When a real sportsbook line is matched, frame the edge against the actual price.
    real_line = r.get("real_line")
    real_price = r.get("real_over_price")
    real_p = r.get("real_p_over")
    real_be = r.get("real_breakeven")
    real_edge = r.get("real_edge")
    real_book = (r.get("real_book") or "the book")
    if real_line is not None and real_price is not None and real_edge is not None:
        price_str = (f"+{int(real_price)}" if real_price > 0 else f"{int(real_price)}")
        s2 = (f"Projects to {e_hrr:.2f} expected H+R+RBI; {real_book} has O{real_line} at "
              f"{price_str} (implied {real_be*100:.1f}%) and our model gives "
              f"P(over {real_line}) = {real_p*100:.1f}%, leaving a {real_edge*100:+.1f}% "
              f"edge vs the book&rsquo;s actual price{note_text}.")
    else:
        s2 = (f"Projects to {e_hrr:.2f} expected H+R+RBI with P(over {line}) = {p_over*100:.1f}%, "
              f"leaving a {edge:+.1f}% edge vs the &minus;110 break-even (no live book line "
              f"matched){note_text}.")

    return s1 + " " + s2

def build_email_html(date, rows, stats, track_record_html="",
                     team_market_html="", game_projections_html=""):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        edge_val = display_edge(r)
        edge_color = "#166534" if edge_val > 0.02 else "#991b1b" if edge_val < -0.02 else "#6b7280"
        hand_str = f" ({r['ph']}HP)" if r["ph"] else ""
        order_str = f"#{r['lineup_pos']}" if r["lineup_pos"] else "&mdash;"
        # Recent-form indicator (last-15-day vs season HRR rate)
        s_hrr = (r["season_h_pa"] or 0) + (r["season_r_pa"] or 0) + (r["season_rbi_pa"] or 0)
        rh = r.get("recent_h_pa"); rr = r.get("recent_r_pa"); rrbi = r.get("recent_rbi_pa")
        recent_str = "&mdash;"; recent_color = "#9ca3af"
        if rh is not None and r.get("recent_pa", 0) > 0 and s_hrr > 0:
            r_hrr = rh + rr + rrbi
            delta = (r_hrr - s_hrr) / s_hrr
            arrow = "&#8593;" if delta > 0.10 else "&#8595;" if delta < -0.10 else "&#8594;"
            recent_color = "#166534" if delta > 0.10 else "#991b1b" if delta < -0.10 else "#6b7280"
            recent_str = f"{arrow} {delta*100:+.0f}% ({r.get('recent_pa')} PA)"
        bvp_str = "&mdash;"; bvp_color = "#9ca3af"
        if r.get("bvp_pa", 0) > 0:
            bvp_color = "#166534" if r.get("bvp_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvp_mult", 1.0) < 0.98 else "#6b7280"
            bvp_str = f"{r['bvp_mult']:.2f} ({r['bvp_pa']}PA)"
        bvt_str = "&mdash;"; bvt_color = "#9ca3af"
        if r.get("bvt_pa", 0) > 0:
            bvt_color = "#166534" if r.get("bvt_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvt_mult", 1.0) < 0.98 else "#6b7280"
            bvt_str = f"{r['bvt_mult']:.2f} ({r['bvt_pa']}PA)"
        bvs_str = "&mdash;"; bvs_color = "#9ca3af"
        if r.get("bvs_pa", 0) > 0:
            bvs_color = "#166534" if r.get("bvs_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvs_mult", 1.0) < 0.98 else "#6b7280"
            bvs_str = f"{r['bvs_mult']:.2f} ({r['bvs_pa']}PA)"
        wx_str = r.get("weather_label") or "&mdash;"
        rows_html.append(
            f"<tr style='border-bottom:1px solid #f3f4f6'>"
            f"<td style='padding:8px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(edge_val)}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{status}</td>"
            f"<td style='padding:8px 10px'><strong>{r['name']}</strong> <span style='color:#9ca3af;font-size:11px'>{r['team']}</span> <span style='color:#9ca3af;font-size:11px'>{order_str}</span></td>"
            f"<td style='padding:8px 10px'>vs {r['opp']}{hand_str} <span style='color:#9ca3af'>({r['game']})</span></td>"
            f"<td style='padding:8px 10px;font-weight:600'>O{r['best_line']}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{fmt_book_cell(r)}</td>"
            f"<td style='padding:8px 10px'>{r['e_hrr']:.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{recent_color}'>{recent_str}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_15'])}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_25'])}</td>"
            f"<td style='padding:8px 10px'>{r['hf']:.2f}</td>"
            f"<td style='padding:8px 10px'>{r.get('park_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{r.get('park_label','')}</span></td>"
            f"<td style='padding:8px 10px;font-size:11px'>{r.get('weather_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{wx_str}</span></td>"
            f"<td style='padding:8px 10px'>{r.get('bullpen_mult', 1.0):.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvp_color}'>{bvp_str}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvt_color}'>{bvt_str}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvs_color}'>{bvs_str}</td>"
            f"<td style='padding:8px 10px'>{round(r['impact']*100)}%</td>"
            f"</tr>"
        )
    return (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fafafa;color:#1f2937;margin:0;padding:20px'>"
        f"<div style='max-width:1080px;margin:0 auto'>"
        f"<h1 style='font-size:20px;margin:0 0 4px'>MLB Prop Edge — Top 10 H+R+RBI</h1>"
        f"<div style='color:#6b7280;font-size:13px;margin-bottom:16px'>{today_str} &middot; {stats['games']} games &middot; ranked by best edge vs -110 break-even (52.4%)</div>"
        f"{track_record_html}"
        f"{team_market_html}"
        f"{game_projections_html}"
        f"<h2 style='font-size:15px;margin:20px 0 6px'>Player H+R+RBI picks <span style='color:#9ca3af;font-weight:400;font-size:12px'>(secondary — higher variance)</span></h2>"
        f"{_build_top5_commentary_block(rows)}"
        f"<table style='width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;font-size:13px'>"
        f"<thead style='background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left'>"
        f"<tr>"
        f"<th style='padding:8px 10px'>Edge</th>"
        f"<th style='padding:8px 10px'>Status</th>"
        f"<th style='padding:8px 10px'>Batter (order)</th>"
        f"<th style='padding:8px 10px'>Matchup</th>"
        f"<th style='padding:8px 10px'>Line</th>"
        f"<th style='padding:8px 10px'>Book</th>"
        f"<th style='padding:8px 10px'>E[HRR]</th>"
        f"<th style='padding:8px 10px'>Recent 15d</th>"
        f"<th style='padding:8px 10px'>P(O1.5)</th>"
        f"<th style='padding:8px 10px'>P(O2.5)</th>"
        f"<th style='padding:8px 10px'>Hand&times;</th>"
        f"<th style='padding:8px 10px'>Park&times;</th>"
        f"<th style='padding:8px 10px'>Wx&times;</th>"
        f"<th style='padding:8px 10px'>BP&times;</th>"
        f"<th style='padding:8px 10px'>BvP&times;</th>"
        f"<th style='padding:8px 10px'>BvT&times;</th>"
        f"<th style='padding:8px 10px'>BvS&times;</th>"
        f"<th style='padding:8px 10px'>Cov</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f"<div style='margin-top:18px;font-size:12px;color:#4b5563;line-height:1.55'>"
        f"<div style='font-weight:700;color:#111827;margin-bottom:6px'>How the projection is built</div>"
        f"E[HRR] = expected_PA &times; (blended per-PA H/R/RBI rates) &times; Quality &times; Hand &times; Team &times; Park &times; Weather &times; Bullpen &times; BvP &times; BvT &times; BvS. "
        f"P(O1.5) = P(HRR &ge; 2) and P(O2.5) = P(HRR &ge; 3) via Poisson with that expected value. "
        f"Edge = P(over the BOOK&rsquo;s line) &minus; the book&rsquo;s implied break-even from its actual price (e.g. &minus;120 &rarr; 54.5%). "
        f"If no book line was found, edge falls back to model line minus 52.4% break-even (&minus;110 assumption). "
        f"Per-PA rates are blended from full-season and last-15-day rates using shrinkage toward season (weight = recent_PA / (recent_PA + 100)). "
        f"<div style='font-weight:700;color:#111827;margin:14px 0 6px'>Weight of each piece (multiplier range, typical impact)</div>"
        f"<table style='border-collapse:collapse;font-size:12px'>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Expected PA (lineup slot)</td><td style='padding:3px 0'>3.75&ndash;4.55 &nbsp; <span style='color:#9ca3af'>(≈ &plusmn;10% swing leadoff vs 9-hole; the single biggest lever)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Quality multiplier (pitch-arsenal xwOBA)</td><td style='padding:3px 0'>0.75&ndash;1.35 &nbsp; <span style='color:#9ca3af'>(most observations &plusmn;10%; biggest skill input)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Handedness factor</td><td style='padding:3px 0'>0.85&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(regressed by PA / (PA+50))</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Team offense (R/RBI only)</td><td style='padding:3px 0'>0.85&ndash;1.15 &nbsp; <span style='color:#9ca3af'>(team R/G vs league R/G)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Recent form blend</td><td style='padding:3px 0'>weight = recent_PA / (recent_PA + 100) &nbsp; <span style='color:#9ca3af'>(50 PA &asymp; 33% recent, 150 PA &asymp; 60% recent)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Park factor</td><td style='padding:3px 0'>0.92&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(COL ≈ 1.20, SD ≈ 0.92; static table)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Weather</td><td style='padding:3px 0'>0.92&ndash;1.08 &nbsp; <span style='color:#9ca3af'>(temp&plusmn;wind-direction&plusmn;precip; dome=1.00; retractable half-weighted)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Bullpen quality (opp relievers)</td><td style='padding:3px 0'>0.95&ndash;1.05 net &nbsp; <span style='color:#9ca3af'>(raw 0.85&ndash;1.15 applied to ~35% of PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvP (batter vs pitcher career)</td><td style='padding:3px 0'>0.85&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+30); typically tiny impact &lt;5 PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvT (batter vs team career)</td><td style='padding:3px 0'>0.90&ndash;1.15 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+100); divisional rivals can hit 300+ PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvS (batter at venue career)</td><td style='padding:3px 0'>0.92&ndash;1.08 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+150); from last 2 seasons of game logs)</span></td></tr>"
        f"</table>"
        f"<div style='margin-top:14px;color:#6b7280'>Status: <span style='color:#166534'>&#9989; in</span> = confirmed in lineup, <em>bench</em> = active roster but not in posted lineup, <span style='color:#92400e'>&#9711; TBD</span> = lineup not yet released. Coverage = share of the pitcher&rsquo;s mix where the batter has real Savant data.</div>"
        f"</div>"
        f"</div></body></html>"
    )

def build_email_text(date, rows, stats, track_record_line=""):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    lines = [f"MLB Prop Edge — Top 10 H+R+RBI for {today_str}",
             f"{stats['games']} games, ranked by best edge vs -110 break-even.", ""]
    if track_record_line:
        lines.append(track_record_line)
        lines.append("")
    for i, r in enumerate(rows, 1):
        status = "in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "TBD"
        ph = f" ({r['ph']}HP)" if r["ph"] else ""
        ord_str = f"#{r['lineup_pos']}" if r["lineup_pos"] else "-"
        lines.append(f"{i:>2}. {fmt_edge_pct(r['best_edge']):>7}  [{status:<5}]  {r['name']} ({r['team']}) {ord_str}")
        lines.append(f"          vs {r['opp']}{ph} · {r['game']}")
        # Recent form delta
        s_hrr = (r["season_h_pa"] or 0) + (r["season_r_pa"] or 0) + (r["season_rbi_pa"] or 0)
        rh = r.get("recent_h_pa"); recent_txt = ""
        if rh is not None and r.get("recent_pa", 0) > 0 and s_hrr > 0:
            r_hrr = rh + r.get("recent_r_pa", 0) + r.get("recent_rbi_pa", 0)
            delta = (r_hrr - s_hrr) / s_hrr * 100
            recent_txt = f" · Recent15d {delta:+.0f}% on {r.get('recent_pa')} PA"
        bvp_txt = ""
        if r.get("bvp_pa", 0) > 0:
            bvp_txt = f" · BvP×{r['bvp_mult']:.2f} ({r['bvp_pa']}PA)"
        wx_txt = f" · Wx×{r.get('weather_mult',1.0):.2f}" + (f" [{r['weather_label']}]" if r.get("weather_label") else "")
        lines.append(f"          O{r['best_line']}: bet · E[HRR]={r['e_hrr']:.2f} · P(O1.5)={fmt_pct(r['p_over_15'])} · P(O2.5)={fmt_pct(r['p_over_25'])}")
        lines.append(f"          Hand×{r['hf']:.2f} · Park×{r.get('park_mult',1.0):.2f} [{r.get('park_label','')}]{wx_txt} · BP×{r.get('bullpen_mult',1.0):.2f}{bvp_txt}{recent_txt}")
        lines.append("")
    lines.append("--- Weights ---")
    lines.append("Expected PA (3.75–4.55) · Quality 0.75–1.35 · Hand 0.85–1.20 · Team 0.85–1.15")
    lines.append("Park 0.92–1.20 · Weather 0.92–1.08 · Bullpen 0.95–1.05 net · BvP 0.85–1.20 (regressed)")
    return "\n".join(lines)

def build_dashboard_html(date, rows, stats, track_record_html="",
                         team_market_html="", game_projections_html=""):
    """Standalone dashboard HTML — same data as the email, just more rows and a timestamp.

    Designed to be committed to docs/index.html and served via GitHub Pages.
    Everything is inlined (no external assets) so it renders identically anywhere.
    """
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    gen_at_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        edge_val = display_edge(r)
        edge_color = "#166534" if edge_val > 0.02 else "#991b1b" if edge_val < -0.02 else "#6b7280"
        hand_str = f" ({r['ph']}HP)" if r["ph"] else ""
        order_str = f"#{r['lineup_pos']}" if r["lineup_pos"] else "&mdash;"
        s_hrr = (r["season_h_pa"] or 0) + (r["season_r_pa"] or 0) + (r["season_rbi_pa"] or 0)
        rh = r.get("recent_h_pa"); rr = r.get("recent_r_pa"); rrbi = r.get("recent_rbi_pa")
        recent_str = "&mdash;"; recent_color = "#9ca3af"
        if rh is not None and r.get("recent_pa", 0) > 0 and s_hrr > 0:
            r_hrr = rh + rr + rrbi
            delta = (r_hrr - s_hrr) / s_hrr
            arrow = "&#8593;" if delta > 0.10 else "&#8595;" if delta < -0.10 else "&#8594;"
            recent_color = "#166534" if delta > 0.10 else "#991b1b" if delta < -0.10 else "#6b7280"
            recent_str = f"{arrow} {delta*100:+.0f}% ({r.get('recent_pa')} PA)"
        bvp_str = "&mdash;"; bvp_color = "#9ca3af"
        if r.get("bvp_pa", 0) > 0:
            bvp_color = "#166534" if r.get("bvp_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvp_mult", 1.0) < 0.98 else "#6b7280"
            bvp_str = f"{r['bvp_mult']:.2f} ({r['bvp_pa']}PA)"
        bvt_str = "&mdash;"; bvt_color = "#9ca3af"
        if r.get("bvt_pa", 0) > 0:
            bvt_color = "#166534" if r.get("bvt_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvt_mult", 1.0) < 0.98 else "#6b7280"
            bvt_str = f"{r['bvt_mult']:.2f} ({r['bvt_pa']}PA)"
        bvs_str = "&mdash;"; bvs_color = "#9ca3af"
        if r.get("bvs_pa", 0) > 0:
            bvs_color = "#166534" if r.get("bvs_mult", 1.0) > 1.02 else "#991b1b" if r.get("bvs_mult", 1.0) < 0.98 else "#6b7280"
            bvs_str = f"{r['bvs_mult']:.2f} ({r['bvs_pa']}PA)"
        wx_str = r.get("weather_label") or "&mdash;"
        bid_attr = f" data-bid='{r.get('_bid','')}'" if r.get("_bid") else ""
        rows_html.append(
            f"<tr style='border-bottom:1px solid #f3f4f6'{bid_attr}>"
            f"<td style='padding:8px 10px;color:#9ca3af;font-size:11px'>{i}</td>"
            f"<td style='padding:8px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(edge_val)}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{status}</td>"
            f"<td style='padding:8px 10px'><strong>{r['name']}</strong> <span style='color:#9ca3af;font-size:11px'>{r['team']}</span> <span style='color:#9ca3af;font-size:11px'>{order_str}</span></td>"
            f"<td style='padding:8px 10px'>vs {r['opp']}{hand_str} <span style='color:#9ca3af'>({r['game']})</span></td>"
            f"<td style='padding:8px 10px;font-weight:600'>O{r['best_line']}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{fmt_book_cell(r)}</td>"
            f"<td style='padding:8px 10px'>{r['e_hrr']:.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{recent_color}'>{recent_str}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_15'])}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_25'])}</td>"
            f"<td style='padding:8px 10px'>{r['hf']:.2f}</td>"
            f"<td style='padding:8px 10px'>{r.get('park_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{r.get('park_label','')}</span></td>"
            f"<td style='padding:8px 10px;font-size:11px'>{r.get('weather_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{wx_str}</span></td>"
            f"<td style='padding:8px 10px'>{r.get('bullpen_mult', 1.0):.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvp_color}'>{bvp_str}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvt_color}'>{bvt_str}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvs_color}'>{bvs_str}</td>"
            f"<td style='padding:8px 10px'>{round(r['impact']*100)}%</td>"
            f"</tr>"
        )
    return (
        f"<!DOCTYPE html><html lang='en'><head><meta charset='utf-8'>"
        f"<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>MLB Prop Edge &mdash; {date}</title>"
        f"</head>"
        f"<body style='font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fafafa;color:#1f2937;margin:0;padding:20px'>"
        f"<div style='max-width:1200px;margin:0 auto'>"
        f"<h1 style='font-size:22px;margin:0 0 4px'>MLB Prop Edge &mdash; Top {len(rows)} H+R+RBI</h1>"
        f"<div style='color:#6b7280;font-size:13px;margin-bottom:6px'>{today_str} &middot; {stats['games']} games &middot; {stats['qualified']} qualified batters &middot; ranked by best edge vs &minus;110 break-even (52.4%)</div>"
        f"<div style='color:#9ca3af;font-size:11px;margin-bottom:6px'>Generated {gen_at_utc} &middot; same model as the daily email</div>"
        f"{track_record_html}"
        f"{team_market_html}"
        f"{game_projections_html}"
        f"<h2 style='font-size:16px;margin:20px 0 8px'>Player H+R+RBI picks <span style='color:#9ca3af;font-weight:400;font-size:12px'>(secondary — higher variance)</span></h2>"
        f"<div style='overflow-x:auto'>"
        f"<table style='width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;font-size:13px'>"
        f"<thead style='background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left'>"
        f"<tr>"
        f"<th style='padding:8px 10px'>#</th>"
        f"<th style='padding:8px 10px'>Edge</th>"
        f"<th style='padding:8px 10px'>Status</th>"
        f"<th style='padding:8px 10px'>Batter (order)</th>"
        f"<th style='padding:8px 10px'>Matchup</th>"
        f"<th style='padding:8px 10px'>Line</th>"
        f"<th style='padding:8px 10px'>Book</th>"
        f"<th style='padding:8px 10px'>E[HRR]</th>"
        f"<th style='padding:8px 10px'>Recent 15d</th>"
        f"<th style='padding:8px 10px'>P(O1.5)</th>"
        f"<th style='padding:8px 10px'>P(O2.5)</th>"
        f"<th style='padding:8px 10px'>Hand&times;</th>"
        f"<th style='padding:8px 10px'>Park&times;</th>"
        f"<th style='padding:8px 10px'>Wx&times;</th>"
        f"<th style='padding:8px 10px'>BP&times;</th>"
        f"<th style='padding:8px 10px'>BvP&times;</th>"
        f"<th style='padding:8px 10px'>BvT&times;</th>"
        f"<th style='padding:8px 10px'>BvS&times;</th>"
        f"<th style='padding:8px 10px'>Cov</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f"</div>"
        f"<div style='margin-top:18px;font-size:12px;color:#4b5563;line-height:1.55'>"
        f"<div style='font-weight:700;color:#111827;margin-bottom:6px'>How the projection is built</div>"
        f"E[HRR] = expected_PA &times; (blended per-PA H/R/RBI rates) &times; Quality &times; Hand &times; Team &times; Park &times; Weather &times; Bullpen &times; BvP. "
        f"P(O1.5) = P(HRR &ge; 2) and P(O2.5) = P(HRR &ge; 3) via Poisson with that expected value. "
        f"Edge = best P(over) &minus; 52.4% (break-even at &minus;110 juice). "
        f"Per-PA rates are blended from full-season and last-15-day rates using shrinkage toward season (weight = recent_PA / (recent_PA + 100))."
        f"<div style='font-weight:700;color:#111827;margin:14px 0 6px'>Weight of each piece (multiplier range, typical impact)</div>"
        f"<table style='border-collapse:collapse;font-size:12px'>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Expected PA (lineup slot)</td><td style='padding:3px 0'>3.75&ndash;4.55 &nbsp; <span style='color:#9ca3af'>(&asymp; &plusmn;10% swing leadoff vs 9-hole; the single biggest lever)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Quality multiplier (pitch-arsenal xwOBA)</td><td style='padding:3px 0'>0.75&ndash;1.35 &nbsp; <span style='color:#9ca3af'>(most observations &plusmn;10%; biggest skill input)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Handedness factor</td><td style='padding:3px 0'>0.85&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(regressed by PA / (PA+50))</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Team offense (R/RBI only)</td><td style='padding:3px 0'>0.85&ndash;1.15 &nbsp; <span style='color:#9ca3af'>(team R/G vs league R/G)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Recent form blend</td><td style='padding:3px 0'>weight = recent_PA / (recent_PA + 100) &nbsp; <span style='color:#9ca3af'>(50 PA &asymp; 33% recent, 150 PA &asymp; 60% recent)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Park factor</td><td style='padding:3px 0'>0.92&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(COL &asymp; 1.20, SD &asymp; 0.92; static table)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Weather</td><td style='padding:3px 0'>0.92&ndash;1.08 &nbsp; <span style='color:#9ca3af'>(temp&plusmn;wind-direction&plusmn;precip; dome=1.00; retractable half-weighted)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>Bullpen quality (opp relievers)</td><td style='padding:3px 0'>0.95&ndash;1.05 net &nbsp; <span style='color:#9ca3af'>(raw 0.85&ndash;1.15 applied to ~35% of PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvP (batter vs pitcher career)</td><td style='padding:3px 0'>0.85&ndash;1.20 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+30); typically tiny impact &lt;5 PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvT (batter vs team career)</td><td style='padding:3px 0'>0.90&ndash;1.15 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+100); divisional rivals can hit 300+ PA)</span></td></tr>"
        f"<tr><td style='padding:3px 14px 3px 0;color:#6b7280'>BvS (batter at venue career)</td><td style='padding:3px 0'>0.92&ndash;1.08 &nbsp; <span style='color:#9ca3af'>(regressed PA / (PA+150); from last 2 seasons of game logs)</span></td></tr>"
        f"</table>"
        f"<div style='margin-top:14px;color:#6b7280'>Status: <span style='color:#166534'>&#9989; in</span> = confirmed in lineup, <em>bench</em> = active roster but not in posted lineup, <span style='color:#92400e'>&#9711; TBD</span> = lineup not yet released. Coverage = share of the pitcher&rsquo;s mix where the batter has real Savant data.</div>"
        f"</div>"
        f"</div></body></html>"
    )

# =============== TEAM-LEVEL AGGREGATION ===============
# The user's Sept 2026 pivot: use the same batter-level projections as inputs to
# TEAM projections. Individual props have too much variance (single-batter ~4 PA
# per game); team-level projections aggregate across 9 batters × 4-5 PA = 40 PA
# per side, which averages toward true talent much faster.
#
# Aggregation rule: for each team, take the 9 batters most likely to start —
# preferring confirmed lineup slots when posted, falling back to top-9 by
# expected_pa (which incorporates recent role and playing time). Sum their
# projected E[Runs] (and E[Hits], E[RBI] for the summary section) to produce
# per-team projected runs. Sum both sides for the game total.

def team_projected_starters(batters_for_team, k=9):
    """Pick the batters most likely to start for a team, up to k of them.

    Preference order:
      1. Confirmed in posted lineup (in_lineup is True), sorted by lineup_pos
      2. Then top by expected_pa (which is DEFAULT_PA when lineup unposted, or
         the by-order value when it IS posted — proxies for likely role)
      3. Skip batters with recent_pa < 15 (already filtered upstream, but be defensive)
    """
    confirmed = [b for b in batters_for_team if b.get("in_lineup") is True]
    confirmed.sort(key=lambda b: b.get("lineup_pos") or 99)
    if len(confirmed) >= k:
        return confirmed[:k]
    remaining = [b for b in batters_for_team
                 if b.get("in_lineup") is not True
                 and (b.get("recent_pa") or 0) >= 15]
    remaining.sort(key=lambda b: -(b.get("expected_pa") or 0))
    return (confirmed + remaining)[:k]

def project_team_runs(batters_for_team, k=9):
    """Sum expected H/R/RBI over the k projected-starter batters for a team."""
    starters = team_projected_starters(batters_for_team, k=k)
    e_h   = sum((b.get("e_h") or 0) for b in starters)
    e_r   = sum((b.get("e_r") or 0) for b in starters)
    e_rbi = sum((b.get("e_rbi") or 0) for b in starters)
    return {
        "e_h": e_h, "e_r": e_r, "e_rbi": e_rbi,
        "e_hrr_team": e_h + e_r + e_rbi,
        "starters": starters,
        "n_confirmed": sum(1 for b in starters if b.get("in_lineup") is True),
    }

def project_games(rescored, games_meta):
    """Aggregate the scored batter pool into per-game projections.

    games_meta is a list of dicts with per-game context: away/home team ids,
    abbreviations, venue, game label. Returns list of game-level projections.
    """
    by_team = {}
    for r in rescored:
        tid = r.get("_my_team_id")
        if tid is None: continue
        by_team.setdefault(tid, []).append(r)
    out = []
    for g in games_meta:
        a_tid = g["away_team_id"]; h_tid = g["home_team_id"]
        a_proj = project_team_runs(by_team.get(a_tid, []))
        h_proj = project_team_runs(by_team.get(h_tid, []))
        game_total = a_proj["e_r"] + h_proj["e_r"]
        margin = h_proj["e_r"] - a_proj["e_r"]     # positive = home favored on runs
        # Pythagorean-esque win probability. Empirical MLB exponent ~1.83.
        # Use log-space to avoid overflow with very small run totals.
        def p_win(r_for, r_against, exponent=1.83):
            r_for = max(0.1, r_for)
            r_against = max(0.1, r_against)
            num = r_for ** exponent
            den = num + r_against ** exponent
            return num / den if den > 0 else 0.5
        p_home_win = p_win(h_proj["e_r"], a_proj["e_r"])
        out.append({
            "game_label": g["game_label"],
            "away_abbr": g["away_abbr"], "home_abbr": g["home_abbr"],
            "away_team_id": a_tid, "home_team_id": h_tid,
            "venue": g.get("venue", ""),
            "away_proj": a_proj, "home_proj": h_proj,
            "game_total": game_total, "margin": margin,
            "p_home_win": p_home_win, "p_away_win": 1 - p_home_win,
        })
    return out

# =============== GAME-LEVEL ODDS + EDGES ===============
# The Odds API extension: query totals, h2h (moneyline), spreads (runline), and
# team_totals in one pass per event. Then compute edges against our projected
# team runs, game total, and win probabilities.

def get_combined_odds_for_event(api_key, event_id, markets=ODDS_COMBINED_MARKETS):
    """Fetch all markets we need for one event in a SINGLE combined call.

    This is the free-tier-optimized replacement for calling get_odds_for_event()
    and get_game_odds_for_event() separately. The Odds API bills per-market so
    combining doesn't save credits, but it does halve the number of round-trips
    (and rate-limit headers).

    Cost: len(markets.split(',')) credits per call, or 4 for our default set.
    """
    if not api_key or not event_id:
        return None
    try:
        return json.loads(fetch(
            f"{ODDS_API_BASE}/sports/{ODDS_SPORT}/events/{event_id}/odds"
            f"?apiKey={api_key}&regions=us&markets={markets}&oddsFormat=american",
            timeout=25
        ))
    except urllib.error.HTTPError as he:
        body = ""
        try: body = he.read().decode("utf-8", errors="replace")[:200]
        except Exception: pass
        print(f"  WARN: combined odds {event_id} HTTP {he.code}: {body}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"  WARN: combined odds {event_id} failed: {e}", file=sys.stderr)
        return None

# Legacy alias — old code paths may still call this name
def get_game_odds_for_event(api_key, event_id):
    return get_combined_odds_for_event(api_key, event_id)

def _best_by_direction(offerings, direction, expected_line=None):
    """From a list of {book, name, point, price} entries, return the best-price
    offering that matches `direction` (Over/Under/Home/Away/etc.). If expected_line
    is given, prefer offerings at that line."""
    matches = [o for o in offerings if o.get("name") == direction]
    if not matches: return None
    if expected_line is not None:
        exact = [o for o in matches if abs((o.get("point") or 0) - expected_line) < 0.01]
        if exact: matches = exact
    def price_val(o):
        p = o.get("price")
        if p is None: return -999
        return american_to_breakeven(p) * -1
    return max(matches, key=price_val)

def extract_game_market_offers(event_odds, books=ODDS_BOOKS):
    """From event_odds, return dict of market_key -> list of {book, name, point, price}
    across all books we care about."""
    out = {"totals": [], "h2h": [], "spreads": [], "team_totals": []}
    if not event_odds:
        return out
    for bm in event_odds.get("bookmakers", []) or []:
        if bm.get("key") not in books:
            continue
        book_title = bm.get("title", bm.get("key", ""))
        for market in bm.get("markets", []) or []:
            mkey = market.get("key")
            if mkey not in out:
                continue
            for outcome in market.get("outcomes", []) or []:
                out[mkey].append({
                    "book": book_title,
                    "book_key": bm.get("key"),
                    "name": outcome.get("name"),
                    "description": outcome.get("description"),
                    "point": outcome.get("point"),
                    "price": outcome.get("price"),
                })
    return out

def compute_game_edges(game_proj, offers):
    """Given a projected game (from project_games) and the shopped market offers,
    return a list of individual bet 'picks' with computed edges.

    Each pick: {market, side, book, line/handicap, price, projection, breakeven,
    edge, note}. Edges > 0 = model likes it.
    """
    picks = []

    # ---- Game total (Over/Under) ----
    gt_projection = game_proj["game_total"]
    for direction in ("Over", "Under"):
        best = _best_by_direction(offers["totals"], direction)
        if not best: continue
        line = best.get("point")
        price = best.get("price")
        if line is None or price is None: continue
        # Simple linear edge: (projection - line) magnitude represents strength.
        # For probability edge we treat the projected total as if it were the mean
        # and use a normal approximation with sigma ~ 3.0 (empirical MLB total sd).
        margin = gt_projection - line
        # Approximate P(over) via normal cdf with sigma=3.0
        SIGMA_TOTAL = 3.0
        z = margin / SIGMA_TOTAL
        # normal cdf via math.erf
        p_over = 0.5 * (1 + math.erf(z / math.sqrt(2)))
        p_bet = p_over if direction == "Over" else (1 - p_over)
        be = american_to_breakeven(price)
        edge = p_bet - be
        picks.append({
            "market": "game_total", "side": direction, "line": line,
            "price": price, "book": best.get("book"),
            "projected": gt_projection, "p_win": p_bet, "breakeven": be, "edge": edge,
        })

    # ---- Team totals (per team, Over/Under) ----
    for side_label, side_proj, side_abbr in (
        ("away", game_proj["away_proj"], game_proj["away_abbr"]),
        ("home", game_proj["home_proj"], game_proj["home_abbr"])
    ):
        team_proj_runs = side_proj["e_r"]
        # team_totals outcomes typically use "Over"/"Under" as name and the team
        # name (or "Home"/"Away") in description. Books vary — grab whichever
        # side matches this team.
        team_offers = []
        for o in offers["team_totals"]:
            desc = (o.get("description") or "").lower()
            # Normalize — sometimes it's the full team name, sometimes an abbr
            if side_abbr.lower() in desc or side_label.lower() in desc:
                team_offers.append(o)
        for direction in ("Over", "Under"):
            best = _best_by_direction(team_offers, direction)
            if not best: continue
            line = best.get("point")
            price = best.get("price")
            if line is None or price is None: continue
            margin = team_proj_runs - line
            # Team totals have lower variance than game totals; sigma ~ 2.0
            SIGMA_TEAM = 2.0
            z = margin / SIGMA_TEAM
            p_over_team = 0.5 * (1 + math.erf(z / math.sqrt(2)))
            p_bet = p_over_team if direction == "Over" else (1 - p_over_team)
            be = american_to_breakeven(price)
            edge = p_bet - be
            picks.append({
                "market": "team_total", "team": side_abbr, "side": direction,
                "line": line, "price": price, "book": best.get("book"),
                "projected": team_proj_runs, "p_win": p_bet, "breakeven": be, "edge": edge,
            })

    # ---- Moneyline (h2h) ----
    # h2h outcomes: name = full team name, no point
    for side_label, p_side_win, side_abbr, team_name_key in (
        ("away", game_proj["p_away_win"], game_proj["away_abbr"], "away_full"),
        ("home", game_proj["p_home_win"], game_proj["home_abbr"], "home_full")
    ):
        # Best price for this team
        best_price = None
        best_book = None
        for o in offers["h2h"]:
            oname = (o.get("name") or "").lower()
            if side_abbr.lower() in oname:
                pr = o.get("price")
                if pr is not None and (best_price is None or american_to_breakeven(pr) < american_to_breakeven(best_price)):
                    best_price = pr
                    best_book = o.get("book")
        if best_price is None:
            continue
        be = american_to_breakeven(best_price)
        edge = p_side_win - be
        picks.append({
            "market": "moneyline", "team": side_abbr, "side": side_label,
            "price": best_price, "book": best_book,
            "projected": None, "p_win": p_side_win, "breakeven": be, "edge": edge,
        })

    # ---- Runline (spreads) ----
    # SKIPPED by default to stay under the free-tier Odds API budget. If offers["spreads"]
    # is empty (as it will be when we don't query the market), this block is a no-op.
    # Left in place so switching back to a paid tier just requires adding 'spreads' to
    # ODDS_COMBINED_MARKETS.
    # Standard MLB runline: -1.5 / +1.5. Model the game margin (home_score - away_score)
    # as Normal(mu=home_projected_margin, sigma=3.0). Winning conditions:
    #   Home -1.5:  home wins by 2+       → P(margin >  1.5)
    #   Home +1.5:  home doesn't lose 2+  → P(margin > -1.5)
    #   Away -1.5:  away wins by 2+       → P(margin < -1.5)
    #   Away +1.5:  away doesn't lose 2+  → P(margin <  1.5)
    home_margin = game_proj["margin"]
    SIGMA_MARGIN = 3.0
    def _norm_cdf(x):
        return 0.5 * (1 + math.erf(x / math.sqrt(2)))
    runline_specs = [
        ("Home -1.5", game_proj["home_abbr"], -1.5,
         1 - _norm_cdf((1.5 - home_margin) / SIGMA_MARGIN)),
        ("Home +1.5", game_proj["home_abbr"], +1.5,
         1 - _norm_cdf((-1.5 - home_margin) / SIGMA_MARGIN)),
        ("Away -1.5", game_proj["away_abbr"], -1.5,
         _norm_cdf((-1.5 - home_margin) / SIGMA_MARGIN)),
        ("Away +1.5", game_proj["away_abbr"], +1.5,
         _norm_cdf((1.5 - home_margin) / SIGMA_MARGIN)),
    ]
    for direction, side_abbr, target_point, p_bet in runline_specs:
        best = None
        for o in offers["spreads"]:
            oname = (o.get("name") or "").lower()
            pt = o.get("point")
            if side_abbr.lower() not in oname: continue
            if pt is None or abs(pt - target_point) > 0.01: continue
            pr = o.get("price")
            if pr is None: continue
            if best is None or american_to_breakeven(pr) < american_to_breakeven(best.get("price")):
                best = o
        if best is None: continue
        be = american_to_breakeven(best.get("price"))
        edge = p_bet - be
        picks.append({
            "market": "runline", "team": side_abbr, "side": direction,
            "line": target_point, "price": best.get("price"), "book": best.get("book"),
            "projected": home_margin, "p_win": p_bet, "breakeven": be, "edge": edge,
        })

    return picks

# =============== GRADING / TRACK RECORD ===============

def _normalize_name(s):
    import unicodedata
    s = unicodedata.normalize("NFD", s or "")
    s = "".join(c for c in s if unicodedata.category(c) != "Mn")
    return " ".join(s.lower().split())

def get_boxscore(game_pk):
    try:
        return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/game/{game_pk}/boxscore", timeout=20))
    except Exception:
        return None

def build_results_index(date_iso):
    """Pull schedule + boxscores for date_iso; index actual H/R/RBI by team & player.

    Returns {team_abbr: {key: rec}} where key is BOTH the player's MLB id (string)
    AND the normalized fullName, so lookups by either work. Only games that have
    reached abstractGameState=='Final' contribute (so grading skips silently while
    a slate is still in progress).
    """
    out = {}
    try:
        sched = json.loads(fetch(f"https://statsapi.mlb.com/api/v1/schedule?sportId=1&date={date_iso}"))
    except Exception:
        return out
    games = (sched.get("dates", [{}])[0] or {}).get("games", [])
    for g in games:
        if ((g.get("status") or {}).get("abstractGameState")) != "Final":
            continue
        box = get_boxscore(g.get("gamePk"))
        if not box:
            continue
        for side in ("away", "home"):
            tm = box.get("teams", {}).get(side, {})
            tname = (tm.get("team") or {}).get("name", "")
            abbr = TEAM_ABBR.get(tname, tname[:3].upper())
            tbucket = out.setdefault(abbr, {})
            for _, pdata in (tm.get("players") or {}).items():
                person = pdata.get("person", {}) or {}
                pid = str(person.get("id", "") or "")
                name = person.get("fullName", "") or ""
                bat = (pdata.get("stats") or {}).get("batting") or {}
                pa = int(bat.get("plateAppearances", 0) or 0)
                # MLB Stats API: batting order is a 3-digit string like "100", "200" for
                # starters (#1 through #9 slots); subs are "101", "202", etc. So a starter
                # is anyone whose battingOrder ends in "00".
                batting_order = pdata.get("battingOrder")
                started = bool(batting_order) and str(batting_order).endswith("00")
                new_rec = {
                    "batter_id": pid, "name": name,
                    "hits":  int(bat.get("hits", 0) or 0),
                    "runs":  int(bat.get("runs", 0) or 0),
                    "rbi":   int(bat.get("rbi", 0) or 0),
                    "ab":    int(bat.get("atBats", 0) or 0),
                    "pa":    pa,
                    "played": pa > 0,
                    "started": started,
                }
                # Doubleheader aggregation: if this player already has a record (because
                # they appeared in an earlier game today), merge stats and OR the flags.
                existing = tbucket.get(pid) if pid else None
                if existing is None:
                    existing = tbucket.get(_normalize_name(name))
                if existing is None:
                    if pid:
                        tbucket[pid] = new_rec
                    tbucket[_normalize_name(name)] = new_rec
                else:
                    existing["hits"]    += new_rec["hits"]
                    existing["runs"]    += new_rec["runs"]
                    existing["rbi"]     += new_rec["rbi"]
                    existing["ab"]      += new_rec["ab"]
                    existing["pa"]      += new_rec["pa"]
                    existing["played"]  = existing["played"]  or new_rec["played"]
                    existing["started"] = existing["started"] or new_rec["started"]
    return out

def grade_picks_for_date(date_iso, picks_dir="picks", results_dir="results"):
    """If picks/{date}.json exists and results/{date}.json does not, fetch actual box
    scores for that day and write graded results. Returns the summary dict or None.

    Counts only picks whose player STARTED that day's game (battingOrder ending in '00').
    Players who were scratched, benched, or only came in as substitutes are excluded.
    Everything is graded against the O1.5 line (the user's chosen prop line).

    Auto-regrades old results files that lack the 'started' field so the on-disk
    ledger matches the current schema.
    """
    picks_file = os.path.join(picks_dir, f"{date_iso}.json")
    results_file = os.path.join(results_dir, f"{date_iso}.json")
    if not os.path.exists(picks_file):
        return None
    # If a results file already exists in the current schema (has both 'started'
    # and 'real_line_picks'), skip — otherwise re-grade so the schema migrates.
    if os.path.exists(results_file):
        try:
            with open(results_file) as f:
                existing = json.load(f)
            ex_sum = existing.get("summary", {}) or {}
            if "started" in ex_sum and "real_line_picks" in ex_sum:
                return None
            print(f"  {date_iso}: re-grading (old results schema)", flush=True)
        except Exception:
            pass
    try:
        with open(picks_file) as f:
            picks_data = json.load(f)
    except Exception as e:
        print(f"  WARN: could not read {picks_file}: {e}", file=sys.stderr)
        return None
    idx = build_results_index(date_iso)
    if not idx:
        print(f"  no Final games yet for {date_iso} — skipping grade", flush=True)
        return None

    results = []
    sums = {"graded": 0, "matched": 0, "played": 0, "started": 0,
            "over_15": 0, "over_25": 0, "ev_o15_sum": 0.0, "ev_o25_sum": 0.0,
            # Real-line accounting (only counts picks where a real_line was saved):
            "real_line_picks": 0, "real_line_wins": 0,
            "real_line_profit_units": 0.0,   # $-units returned on $1-per-pick stakes
            "real_line_staked_units": 0.0}
    for p in picks_data.get("picks", []):
        team = p.get("team", "")
        nm = p.get("name", "")
        bid = str(p.get("batter_id") or "")
        rec = None
        if team in idx:
            tidx = idx[team]
            if bid and bid in tidx:
                rec = tidx[bid]
            if rec is None:
                rec = tidx.get(_normalize_name(nm))
        sums["graded"] += 1
        if rec is None:
            results.append({**p, "matched": False, "played": False, "started": False, "hrr": None})
            continue
        h, r_, rbi = rec["hits"], rec["runs"], rec["rbi"]
        hrr = h + r_ + rbi
        played = bool(rec["played"])
        started = bool(rec.get("started"))
        over_15 = started and hrr >= 2
        over_25 = started and hrr >= 3
        # Real-line outcome (if the pick was saved with a real line). Bet is OVER
        # the book's line; win if HRR strictly exceeds that line.
        real_line = p.get("real_line")
        real_price = p.get("real_over_price")
        real_decimal = american_to_decimal(real_price) if real_price is not None else None
        real_won = None
        if started and real_line is not None:
            real_won = hrr > real_line
        results.append({**p, "matched": True, "played": played, "started": started,
                        "hits": h, "runs": r_, "rbi": rbi, "hrr": hrr,
                        "over_15": over_15, "over_25": over_25,
                        "real_won": real_won})
        sums["matched"] += 1
        if played:
            sums["played"] += 1
        if started:
            sums["started"] += 1
            if over_15: sums["over_15"] += 1
            if over_25: sums["over_25"] += 1
            sums["ev_o15_sum"] += float(p.get("p_over_15") or 0.0)
            sums["ev_o25_sum"] += float(p.get("p_over_25") or 0.0)
            if real_line is not None and real_decimal is not None:
                sums["real_line_picks"] += 1
                sums["real_line_staked_units"] += 1.0
                if real_won:
                    sums["real_line_wins"] += 1
                    sums["real_line_profit_units"] += (real_decimal - 1.0)
                else:
                    sums["real_line_profit_units"] -= 1.0
    st = max(1, sums["started"])
    rl = max(1, sums["real_line_picks"])
    summary = {
        **sums,
        # Model-line metrics (kept for backward compat with the assumed -110 view)
        "over_15_rate":          sums["over_15"]/st if sums["started"] else 0.0,
        "over_25_rate":          sums["over_25"]/st if sums["started"] else 0.0,
        "expected_over_15_rate": sums["ev_o15_sum"]/st if sums["started"] else 0.0,
        "expected_over_25_rate": sums["ev_o25_sum"]/st if sums["started"] else 0.0,
        "roi_o15_pct":           (sums["over_15"]/st * 1.909 - 1.0) if sums["started"] else 0.0,
        # Real-line metrics — computed only over picks that had a confirmed sportsbook line
        "real_line_win_rate":    sums["real_line_wins"]/rl if sums["real_line_picks"] else 0.0,
        "real_line_roi_pct":     (sums["real_line_profit_units"]/sums["real_line_staked_units"])
                                  if sums["real_line_staked_units"] else 0.0,
    }
    os.makedirs(results_dir, exist_ok=True)
    with open(results_file, "w") as f:
        json.dump({"date": date_iso,
                   "graded_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                   "results": results, "summary": summary}, f, indent=2)
    msg = (f"  graded {date_iso}: {sums['over_15']}/{sums['started']} over 1.5 (starters) "
           f"@ assumed -110 ROI {summary['roi_o15_pct']*100:+.1f}%")
    if sums["real_line_picks"]:
        msg += (f"  |  REAL-LINE: {sums['real_line_wins']}/{sums['real_line_picks']} wins, "
                f"ROI {summary['real_line_roi_pct']*100:+.1f}% on actual prices")
    print(msg, flush=True)
    return summary

def save_picks(date_iso, top_picks, picks_dir="picks"):
    """Write a JSON snapshot of today's top picks so we can grade them tomorrow.

    Includes:
      - Model summary stats (p_over_15, p_over_25, e_hrr, best_edge)
      - Every factor multiplier (hf, park, weather, bullpen, BvP, BvT, BvS, quality)
        so factor_calibration.py can break performance down per factor
      - Real-odds data (line, price, book, edge) when the Odds API matched
    """
    os.makedirs(picks_dir, exist_ok=True)
    fname = os.path.join(picks_dir, f"{date_iso}.json")
    out = {
        "date": date_iso,
        "generated_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "picks": [
            {"rank": i+1, "batter_id": str(r.get("_bid") or ""),
             "name": r.get("name", ""), "team": r.get("team", ""),
             "opp_pitcher": r.get("opp", ""), "opp_team": r.get("opp_team", ""),
             "game": r.get("game", ""),
             "in_lineup": r.get("in_lineup"), "lineup_pos": r.get("lineup_pos"),
             # Model side (assumed line / -110). p_over_*_raw are the Poisson
             # outputs BEFORE Platt calibration; p_over_* are post-calibration.
             # calibrate_predictions.py uses the _raw fields to refit so the
             # transform doesn't compound on itself.
             "line": r.get("best_line", "1.5"),
             "best_edge": r.get("best_edge", 0.0),
             "p_over_15": r.get("p_over_15", 0.0),
             "p_over_25": r.get("p_over_25", 0.0),
             "p_over_15_raw": r.get("p_over_15_raw"),
             "p_over_25_raw": r.get("p_over_25_raw"),
             "e_hrr": r.get("e_hrr", 0.0),
             # Factor multipliers (so per-factor calibration can be done later)
             "expected_pa":   r.get("expected_pa"),
             "quality_mult":  r.get("quality_mult"),
             "hf":            r.get("hf"),
             "park_mult":     r.get("park_mult"),
             "weather_mult":  r.get("weather_mult"),
             "bullpen_mult":  r.get("bullpen_mult"),
             "bvp_mult":      r.get("bvp_mult"),
             "bvp_pa":        r.get("bvp_pa"),
             "bvt_mult":      r.get("bvt_mult"),
             "bvt_pa":        r.get("bvt_pa"),
             "bvs_mult":      r.get("bvs_mult"),
             "bvs_pa":        r.get("bvs_pa"),
             "impact":        r.get("impact"),
             "recent_pa":     r.get("recent_pa"),
             # Real-odds side (Odds API). Null when no real line was matched.
             "real_line":       r.get("real_line"),
             "real_over_price": r.get("real_over_price"),
             "real_book":       r.get("real_book"),
             "real_p_over":     r.get("real_p_over"),
             "real_breakeven":  r.get("real_breakeven"),
             "real_edge":       r.get("real_edge")}
            for i, r in enumerate(top_picks)
        ],
    }
    with open(fname, "w") as f:
        json.dump(out, f, indent=2)
    return fname

def migrate_old_results(picks_dir="picks", results_dir="results"):
    """Walk every results/*.json file once; re-grade any whose summary lacks the
    current schema's 'started' field. Cheap when everything's already current
    (just a file read each), expensive only when there's stale data to fix.

    Called once per main() run so old days clean up automatically as the schema
    evolves, without requiring a separate manual backfill.
    """
    if not os.path.isdir(results_dir):
        return 0
    migrated = 0
    for fn in sorted(os.listdir(results_dir)):
        if not fn.endswith(".json"):
            continue
        date_iso = fn[:-5]
        try:
            with open(os.path.join(results_dir, fn)) as f:
                existing = json.load(f)
        except Exception:
            continue
        ex_sum = existing.get("summary", {}) or {}
        if "started" in ex_sum and "real_line_picks" in ex_sum:
            continue  # already current schema
        print(f"  migrating stale result file {date_iso}", flush=True)
        try:
            summary = grade_picks_for_date(date_iso, picks_dir=picks_dir, results_dir=results_dir)
            if summary is not None:
                migrated += 1
        except Exception as e:
            print(f"  WARN: could not migrate {date_iso}: {e}", file=sys.stderr)
    return migrated

def load_all_results(results_dir="results"):
    if not os.path.isdir(results_dir): return []
    out = []
    for fn in sorted(os.listdir(results_dir)):
        if not fn.endswith(".json"): continue
        try:
            with open(os.path.join(results_dir, fn)) as f:
                out.append(json.load(f))
        except Exception:
            continue
    out.sort(key=lambda d: d.get("date", ""), reverse=True)
    return out

def aggregate_results(all_results, window_days=None, today_iso=None):
    """Aggregate per-day result summaries into a rolling view.

    Everything is measured against the O1.5 H+R+RBI line, and ONLY picks where the
    player actually started (battingOrder ending in '00') contribute. Scratches,
    benchings, and substitute appearances are excluded entirely.

    Result files written before the 'started'-aware schema landed are skipped
    entirely here so the displayed numerator and denominator are always consistent.
    Those stale files get re-graded automatically by migrate_old_results() on the
    next workflow run.
    """
    if window_days is not None and today_iso:
        from_date = (dt.date.fromisoformat(today_iso) - dt.timedelta(days=window_days)).isoformat()
        all_results = [r for r in all_results if r.get("date", "") >= from_date]
    # Skip old-schema files entirely; their over_15 count was scored under the
    # 'played' branch (not 'started') and would inflate the numerator vs denominator.
    all_results = [r for r in all_results if "started" in (r.get("summary", {}) or {})]
    days = len(all_results)
    graded = matched = played = started = o15 = o25 = 0
    ev_o15 = ev_o25 = 0.0
    rl_picks = rl_wins = 0
    rl_profit = rl_staked = 0.0
    for d in all_results:
        s = d.get("summary", {}) or {}
        graded  += int(s.get("graded", 0) or 0)
        matched += int(s.get("matched", 0) or 0)
        played  += int(s.get("played", 0) or 0)
        started += int(s.get("started", 0) or 0)
        o15     += int(s.get("over_15", 0) or 0)
        o25     += int(s.get("over_25", 0) or 0)
        ev_o15  += float(s.get("ev_o15_sum", 0) or 0)
        ev_o25  += float(s.get("ev_o25_sum", 0) or 0)
        rl_picks  += int(s.get("real_line_picks", 0) or 0)
        rl_wins   += int(s.get("real_line_wins", 0) or 0)
        rl_profit += float(s.get("real_line_profit_units", 0) or 0)
        rl_staked += float(s.get("real_line_staked_units", 0) or 0)
    st = max(1, started)
    rlp = max(1, rl_picks)
    rls = max(1.0, rl_staked)
    return {"days": days, "graded": graded, "matched": matched,
            "played": played, "started": started,
            "over_15": o15, "over_25": o25,
            "over_15_rate":          o15/st if started else 0.0,
            "over_25_rate":          o25/st if started else 0.0,
            "expected_over_15_rate": ev_o15/st if started else 0.0,
            "expected_over_25_rate": ev_o25/st if started else 0.0,
            "roi_o15_pct":           (o15/st * 1.909 - 1.0) if started else 0.0,
            # Real-line aggregates
            "real_line_picks":  rl_picks,
            "real_line_wins":   rl_wins,
            "real_line_win_rate": rl_wins/rlp if rl_picks else 0.0,
            "real_line_roi_pct":  rl_profit/rls if rl_staked else 0.0}

def build_track_record_html(today_iso, all_results):
    if not all_results:
        return ("<div style='margin:14px 0;padding:12px 14px;background:#fff;border:1px solid #e5e7eb;"
                "border-radius:8px;font-size:12px;color:#6b7280'>"
                "<strong style='color:#111827'>Track Record:</strong> no graded picks yet "
                "(first batch appears once a full slate of games has completed).</div>")
    agg_all = aggregate_results(all_results)
    agg_30  = aggregate_results(all_results, 30, today_iso)
    agg_7   = aggregate_results(all_results, 7,  today_iso)
    def row(label, a):
        if not a["started"]:
            return (f"<tr style='border-top:1px solid #f3f4f6'><td style='padding:5px 10px;color:#6b7280;font-weight:600'>{label}</td>"
                    f"<td colspan='6' style='padding:5px 10px;color:#9ca3af;font-size:11px'>no starters in window</td></tr>")
        edge = a["over_15_rate"] - a["expected_over_15_rate"]
        edge_color = "#166534" if edge > 0.02 else "#991b1b" if edge < -0.02 else "#6b7280"
        roi = a["roi_o15_pct"]
        roi_color = "#166534" if roi > 0.02 else "#991b1b" if roi < -0.02 else "#6b7280"
        return (f"<tr style='border-top:1px solid #f3f4f6'>"
                f"<td style='padding:5px 10px;color:#374151;font-weight:600'>{label}</td>"
                f"<td style='padding:5px 10px;color:#6b7280'>{a['days']}d</td>"
                f"<td style='padding:5px 10px;color:#6b7280'><strong style='color:#374151'>{a['over_15']}</strong>/{a['started']}</td>"
                f"<td style='padding:5px 10px'><strong>{a['over_15_rate']*100:.1f}%</strong></td>"
                f"<td style='padding:5px 10px;color:#6b7280'>{a['expected_over_15_rate']*100:.1f}%</td>"
                f"<td style='padding:5px 10px;font-weight:600;color:{edge_color}'>{edge*100:+.1f}%</td>"
                f"<td style='padding:5px 10px;font-weight:600;color:{roi_color}'>{roi*100:+.1f}%</td>"
                f"</tr>")
    return (
        f"<div style='margin:14px 0;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<div style='padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:13px'>"
        f"Track Record &mdash; Over 1.5 H+R+RBI (starters only) "
        f"<span style='color:#9ca3af;font-weight:400;font-size:11px'>({len(all_results)} day{'s' if len(all_results)!=1 else ''} graded)</span>"
        f"</div>"
        f"<table style='width:100%;border-collapse:collapse;font-size:12px'>"
        f"<thead style='color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left;background:#fafafa'>"
        f"<tr><th style='padding:6px 10px'>Window</th><th style='padding:6px 10px'>Days</th>"
        f"<th style='padding:6px 10px'>Over 1.5</th><th style='padding:6px 10px'>Actual Hit Rate</th>"
        f"<th style='padding:6px 10px'>Expected</th><th style='padding:6px 10px'>Edge vs Model</th>"
        f"<th style='padding:6px 10px'>ROI @ &minus;110</th></tr></thead>"
        f"<tbody>{row('All time', agg_all)}{row('Last 30d', agg_30)}{row('Last 7d', agg_7)}</tbody>"
        f"</table>"
        f"</div>"
    )

def build_track_record_text(today_iso, all_results):
    if not all_results:
        return "Track Record: no graded picks yet."
    a = aggregate_results(all_results)
    if not a["started"]:
        return "Track Record: no starters graded yet."
    return (f"Track Record (all time, starters only, vs O1.5): {a['days']} days, "
            f"{a['over_15']}/{a['started']} over 1.5 "
            f"({a['over_15_rate']*100:.1f}%, expected {a['expected_over_15_rate']*100:.1f}%), "
            f"ROI {a['roi_o15_pct']*100:+.1f}% @ -110.")

# =============== MAIN ===============

def main():
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("RECIPIENT_EMAIL")
    if not api_key:
        print("ERROR: RESEND_API_KEY not set", file=sys.stderr); sys.exit(2)
    if not recipient:
        print("ERROR: RECIPIENT_EMAIL not set", file=sys.stderr); sys.exit(2)

    et_today = (dt.datetime.utcnow() - dt.timedelta(hours=4)).date()
    DATE = et_today.isoformat()
    SEASON = et_today.year
    print(f"Running for date={DATE} (ET) season={SEASON}", flush=True)

    # Load calibration parameters (None if not yet fitted)
    cal_params = load_calibration()
    if cal_params:
        print(f"  calibration: a={cal_params['a']:+.3f} b={cal_params['b']:+.3f} "
              f"fitted from N={cal_params['n_train']} on {cal_params.get('fitted_at_utc','?')}", flush=True)
    else:
        print(f"  no calibration_params.json found — using RAW model predictions", flush=True)

    # Grade yesterday's picks, if we have them and haven't graded them yet. Safe
    # to call every run — it no-ops if the picks file is missing, if the results
    # file already exists, or if yesterday's games aren't all Final yet.
    try:
        YDATE = (et_today - dt.timedelta(days=1)).isoformat()
        grade_picks_for_date(YDATE)
    except Exception as e:
        print(f"  WARN: grading {YDATE} failed: {e}", file=sys.stderr)

    # Migrate any older result files written under a previous schema. Cheap when
    # everything's current; without this, old days' over_15 counts inflate the
    # rolling numerator while their (missing) 'started' counts contribute 0 to
    # the denominator, producing impossible ratios like 11/10.
    try:
        n = migrate_old_results()
        if n:
            print(f"  migrated {n} stale result file(s) to current schema", flush=True)
    except Exception as e:
        print(f"  WARN: migrate_old_results failed: {e}", file=sys.stderr)

    sched = get_schedule(DATE)
    games = (sched.get("dates", [{}])[0] or {}).get("games", [])
    if not games:
        send_email(api_key, recipient, f"MLB Prop Edge — No games {DATE}",
                   f"<p>No MLB games scheduled for {DATE}.</p>", f"No games scheduled for {DATE}.")
        return
    print(f"  schedule: {len(games)} games", flush=True)

    bat_rows = get_savant(SEASON, "batter")
    pit_rows = get_savant(SEASON, "pitcher")
    vl = get_splits("vl", SEASON)
    vr = get_splits("vr", SEASON)
    season_d = get_season_hitting(SEASON)
    team_d   = get_team_hitting(SEASON)
    recent_d = get_recent_hitting(DATE, days=15)
    bp_d     = get_team_bullpen(SEASON)
    print(f"  fetched savant({len(bat_rows)} batter rows / {len(pit_rows)} pitcher rows), splits, season, teams, bullpens", flush=True)

    batters = index_batters(bat_rows)
    pitchers = index_pitchers(pit_rows)
    lg_mix = league_pitch_mix(pit_rows)
    lg_xw  = league_xwoba_by_pitch(bat_rows)
    hand   = index_handedness(vl, vr)
    season = index_season_hitting(season_d)
    recent = index_season_hitting(recent_d)  # same shape (H/R/RBI per PA, last 15 days)
    team_rpg, league_rpg = index_team_hitting(team_d)
    bullpen_map, league_bp_woba = index_team_bullpen(bp_d)
    print(f"  recent stats: {len(recent)} batters with last-15-day PA", flush=True)
    print(f"  season stats: {len(season)} batters · league R/G: {league_rpg:.2f}", flush=True)
    print(f"  bullpen: {len(bullpen_map)} teams · league relief wOBA: {league_bp_woba:.3f}", flush=True)

    pitcher_ids = set()
    for g in games:
        for s in ("away", "home"):
            pp = g["teams"][s].get("probablePitcher") or {}
            if pp.get("id"): pitcher_ids.add(str(pp["id"]))
    p_hand_map = get_people_handedness(pitcher_ids)

    team_ids = set()
    for g in games:
        team_ids.add(g["teams"]["away"]["team"]["id"]); team_ids.add(g["teams"]["home"]["team"]["id"])
    rosters = {tid: get_roster(tid) for tid in team_ids}
    rostered = sum(len(v) for v in rosters.values())
    print(f"  rosters: {rostered} position players across {len(rosters)} teams", flush=True)

    # Pre-compute park and weather per game (one weather call per venue per date).
    weather_cache = {}

    rows = []
    for g in games:
        a = g["teams"]["away"]; h = g["teams"]["home"]
        a_tid = a["team"]["id"]; h_tid = h["team"]["id"]
        a_abbr = TEAM_ABBR.get(a["team"]["name"], a["team"]["name"][:3].upper())
        h_abbr = TEAM_ABBR.get(h["team"]["name"], h["team"]["name"][:3].upper())
        venue_id = (g.get("venue") or {}).get("id")  # for BvS lookups in second pass
        a_pp = a.get("probablePitcher") or {}
        h_pp = h.get("probablePitcher") or {}
        ln = g.get("lineups") or {}
        a_lineup = ln.get("awayPlayers") or []
        h_lineup = ln.get("homePlayers") or []
        a_lineup_ids = {str(p["id"]) for p in a_lineup}
        h_lineup_ids = {str(p["id"]) for p in h_lineup}
        a_lineup_pos = {str(p["id"]): idx+1 for idx, p in enumerate(a_lineup)}
        h_lineup_pos = {str(p["id"]): idx+1 for idx, p in enumerate(h_lineup)}

        # Park + weather are home-venue-based and apply to both sides
        pk_mult = park_factor(h_abbr)
        wx_mult, wx_label = weather_factor(h_abbr, DATE, weather_cache)

        # Bullpen factor: each side faces the OPPOSING team's bullpen
        a_bp_mult, a_bp_ratio = bullpen_factor(h_tid, bullpen_map, league_bp_woba)
        h_bp_mult, h_bp_ratio = bullpen_factor(a_tid, bullpen_map, league_bp_woba)

        def score_side(roster, opp_pp, my_abbr, opp_abbr, lineup_ids, lineup_pos_map, game_label, my_team_id, opp_team_id,
                       bp_mult, bp_ratio):
            if not opp_pp.get("id"): return
            pid = str(opp_pp["id"])
            if pid not in pitchers: return
            for pl in roster:
                bid = pl["id"]
                pos = lineup_pos_map.get(bid)  # None if not in lineup yet
                # First pass: no BvP/BvT/BvS yet (added in second pass for top candidates).
                r = project_hrr(bid, pid, pos, batters, pitchers, lg_mix, lg_xw, hand, p_hand_map,
                                season, team_rpg, league_rpg, my_team_id, recent,
                                park_mult=pk_mult, weather_mult=wx_mult, weather_label=wx_label,
                                bullpen_mult=bp_mult, bullpen_ratio=bp_ratio)
                if not r: continue
                in_lineup = None if not lineup_ids else (bid in lineup_ids)
                rows.append({
                    "name": pl["name"], "team": my_abbr, "opp": opp_pp.get("fullName", "?"),
                    "opp_team": opp_abbr,
                    "game": game_label, "in_lineup": in_lineup,
                    "_pid": pid, "_bid": bid, "_lineup_pos": pos,
                    "_my_team_id": my_team_id, "_opp_team_id": opp_team_id, "_venue_id": venue_id,
                    "park_label": h_abbr,
                    **r,
                })

        score_side(rosters.get(a_tid, []), h_pp, a_abbr, h_abbr, a_lineup_ids, a_lineup_pos, f"{a_abbr} @ {h_abbr}", a_tid, h_tid,
                   a_bp_mult, a_bp_ratio)
        score_side(rosters.get(h_tid, []), a_pp, h_abbr, a_abbr, h_lineup_ids, h_lineup_pos, f"{a_abbr} @ {h_abbr}", h_tid, a_tid,
                   h_bp_mult, h_bp_ratio)

    # First-pass qualified pool. Filters:
    #   * Savant pitch-arsenal coverage >= 50% of the opposing pitcher's mix
    #   * Season PA >= 30 (filters out players with too little data to project)
    #   * Recent 15-day PA >= 15 (filters out injured / called-up / barely-playing
    #     players — without this, the model can recommend players who haven't
    #     touched a bat in two weeks)
    MIN_RECENT_PA = 15
    pre_recent = sum(1 for r in rows if r["impact"] >= 0.5 and r["season_pa"] >= 30)
    qualified = [r for r in rows
                 if r["impact"] >= 0.5
                 and r["season_pa"] >= 30
                 and (r.get("recent_pa") or 0) >= MIN_RECENT_PA]
    qualified.sort(key=lambda x: -x["best_edge"])
    dropped = pre_recent - len(qualified)
    if dropped:
        print(f"  recent-PA filter (>= {MIN_RECENT_PA} in last 15d): dropped {dropped} "
              f"players, {len(qualified)} remain", flush=True)

    # Second pass: fetch BvP, BvT, and BvS for the top 60 candidates and re-score.
    # Each candidate now triggers up to 4 batter-detail API calls (BvP + BvT + BvS×2 seasons).
    BVP_LOOKUP_LIMIT = 60
    BVS_SEASONS = [SEASON, SEASON - 1]   # last 2 seasons of gameLog for venue stats
    candidates = qualified[:BVP_LOOKUP_LIMIT]
    print(f"  fetching BvP/BvT/BvS history for top {len(candidates)} candidates...", flush=True)
    rescored = []
    for r in candidates:
        bid = r["_bid"]; pid = r["_pid"]
        opp_tid = r.get("_opp_team_id")
        vid = r.get("_venue_id")
        season_woba = (season.get(bid) or {}).get("wOBA", 0.310)

        bvp = get_bvp(bid, pid)
        bvp_m, bvp_pa, bvp_w = bvp_factor(bvp, season_woba)

        bvt = get_bvt(bid, opp_tid) if opp_tid else None
        bvt_m, bvt_pa, bvt_w = bvt_factor(bvt, season_woba)

        bvs = get_bvs(bid, vid, BVS_SEASONS) if vid else None
        bvs_m, bvs_pa, bvs_w = bvs_factor(bvs, season_woba)

        # Recompute with all three career factors applied
        r2 = project_hrr(
            bid, pid, r["_lineup_pos"], batters, pitchers, lg_mix, lg_xw, hand, p_hand_map,
            season, team_rpg, league_rpg, r["_my_team_id"], recent,
            park_mult=r["park_mult"], weather_mult=r["weather_mult"], weather_label=r["weather_label"],
            bullpen_mult=r["bullpen_mult"], bullpen_ratio=r["bullpen_ratio"],
            bvp_mult=bvp_m, bvp_pa=bvp_pa, bvp_woba=bvp_w,
            bvt_mult=bvt_m, bvt_pa=bvt_pa, bvt_woba=bvt_w,
            bvs_mult=bvs_m, bvs_pa=bvs_pa, bvs_woba=bvs_w,
        )
        if not r2: continue
        rescored.append({
            "name": r["name"], "team": r["team"], "opp": r["opp"], "opp_team": r.get("opp_team", ""),
            "game": r["game"], "in_lineup": r["in_lineup"], "park_label": r["park_label"],
            "_pid": pid, "_bid": bid, "_lineup_pos": r["_lineup_pos"],
            "_my_team_id": r["_my_team_id"], "_opp_team_id": opp_tid, "_venue_id": vid,
            **r2,
        })

    # Apply Platt-scaling calibration to every projected probability BEFORE
    # ranking. The raw P(over) values from the Poisson model are systematically
    # ~27% too high; this transform shrinks them to match observed reality.
    # We save the raw values as p_over_15_raw / p_over_25_raw so the picks JSON
    # records both, which lets calibrate_predictions.py re-fit periodically.
    if cal_params is not None:
        for r in rescored:
            r["p_over_15_raw"] = r["p_over_15"]
            r["p_over_25_raw"] = r["p_over_25"]
            r["p_over_15"] = apply_calibration(r["p_over_15"], cal_params)
            r["p_over_25"] = apply_calibration(r["p_over_25"], cal_params)
            # Recompute edge + best-line decision against the CALIBRATED probs
            edge_15 = r["p_over_15"] - BREAKEVEN_PROB
            edge_25 = r["p_over_25"] - BREAKEVEN_PROB
            r["edge_15"] = edge_15
            r["edge_25"] = edge_25
            r["best_line"] = "1.5" if edge_15 >= edge_25 else "2.5"
            r["best_edge"] = max(edge_15, edge_25)

    rescored.sort(key=lambda x: -x["best_edge"])

    # Dedupe: if a player is in a doubleheader (e.g. STL @ CIN twice today), they'd
    # appear twice in the ranking — once per game. Keep only the higher-edge entry
    # per (player_id, team) so each batter shows up at most once.
    seen = set()
    deduped = []
    for r in rescored:
        key = (r["_bid"], r["team"])
        if key in seen: continue
        seen.add(key)
        deduped.append(r)
    rescored = deduped

    # =============== REAL ODDS + TEAM AGGREGATION (unified path) ===============
    # Free-tier-optimized flow: (1) aggregate batter projections to games first,
    # (2) rank games by projected margin, (3) hit the Odds API for only the top-N
    # games with all markets combined in a single per-event call. This keeps us
    # under ~500 credits/month on the free tier.
    odds_api_key = os.environ.get("ODDS_API_KEY", "").strip()
    team_market_picks = []
    game_projections = []

    # Build games_meta and initial game projections regardless of API key — the
    # internal game projections are useful on their own even without odds.
    games_meta = []
    for g in games:
        a = g["teams"]["away"]; h = g["teams"]["home"]
        a_tid = a["team"]["id"]; h_tid = h["team"]["id"]
        a_abbr = TEAM_ABBR.get(a["team"]["name"], a["team"]["name"][:3].upper())
        h_abbr = TEAM_ABBR.get(h["team"]["name"], h["team"]["name"][:3].upper())
        games_meta.append({
            "away_team_id": a_tid, "home_team_id": h_tid,
            "away_abbr": a_abbr, "home_abbr": h_abbr,
            "away_full": a["team"]["name"], "home_full": h["team"]["name"],
            "venue": (g.get("venue") or {}).get("name", ""),
            "game_label": f"{a_abbr} @ {h_abbr}",
        })
    game_projections = project_games(rescored, games_meta)
    print(f"  built {len(game_projections)} game projections (internal)", flush=True)

    # Rank games by |margin| — biggest projected mismatches first. That's where
    # we expect the biggest sportsbook line disagreements.
    ranked_games = sorted(
        list(zip(game_projections, games_meta)),
        key=lambda pair: -abs(pair[0].get("margin", 0))
    )

    if odds_api_key:
        try:
            top_games = ranked_games[:ODDS_TOP_N_GAMES]
            print(f"  querying Odds API for top {len(top_games)} games (of {len(games_meta)}) — "
                  f"~{ODDS_TOP_N_GAMES * 4} credits/day at 4 markets each", flush=True)

            # Get the event list (1 credit) to resolve names → event IDs
            events_list = get_odds_events(odds_api_key, DATE)
            event_id_by_pair = {}
            for e in events_list:
                a_norm = _normalize_name(e.get("away_team", ""))
                h_norm = _normalize_name(e.get("home_team", ""))
                if a_norm and h_norm:
                    event_id_by_pair[(a_norm, h_norm)] = e.get("id")

            # For each of the top games, ONE combined-market API call, then parse
            # BOTH player-props AND game-market outcomes from the same response
            player_lines_by_event = {}
            matched_events = 0
            for gp, gmeta in top_games:
                event_id = event_id_by_pair.get(
                    (_normalize_name(gmeta["away_full"]), _normalize_name(gmeta["home_full"]))
                )
                if not event_id:
                    continue
                event_odds = get_combined_odds_for_event(odds_api_key, event_id)
                if not event_odds:
                    continue
                matched_events += 1
                # Parse player-prop lines (batter_hits_runs_rbis is one of the requested markets)
                player_lines_by_event[event_id] = extract_player_lines_from_event(event_odds)
                # Also tag each batter row that plays in this game with the event id
                for r in rescored:
                    if r.get("game") == gmeta["game_label"]:
                        r["_odds_event_id"] = event_id
                # Parse game-market offers and compute edges
                offers = extract_game_market_offers(event_odds)
                for pick in compute_game_edges(gp, offers):
                    pick["game_label"] = gp["game_label"]
                    team_market_picks.append(pick)

            print(f"    fetched {matched_events} events; {len(team_market_picks)} candidate market picks", flush=True)

            # Overlay real player lines for batters in queried games
            matched = 0
            for r in rescored:
                eid = r.get("_odds_event_id")
                if not eid or eid not in player_lines_by_event:
                    continue
                key = _normalize_name(r.get("name", ""))
                player_lines = player_lines_by_event[eid].get(key)
                if not player_lines:
                    continue
                best = pick_best_line_for_player(player_lines, r["e_hrr"])
                if not best:
                    continue
                matched += 1
                r["real_line"]       = best["line"]
                r["real_over_price"] = best["over_price"]
                r["real_book"]       = best["book"]
                r["real_p_over"]     = best["p_over"]
                r["real_breakeven"]  = best["breakeven"]
                r["real_edge"]       = best["edge"]
            print(f"    matched real prop lines for {matched} batters in the top-N games", flush=True)

            # Re-rank rescored: batters with real_edge sort by that; others keep model edge
            def rank_key(r):
                if "real_edge" in r:
                    return -r["real_edge"]
                return -(r["best_edge"] - 0.01)
            rescored.sort(key=rank_key)

            # Sort team_market_picks by edge for display
            team_market_picks.sort(key=lambda p: -(p.get("edge") or 0))
        except Exception as e:
            print(f"  WARN: Odds API integration failed: {e}", file=sys.stderr)
    else:
        print(f"  ODDS_API_KEY not set — using assumed lines / no game markets", flush=True)

    top = rescored[:10]

    # Snapshot today's top 15 to picks/{DATE}.json so tomorrow's run can grade them.
    # We save 15 (not just the top 10 sent in email) because the user reasons about
    # the top 15 on the dashboard and parlays.
    try:
        picks_file = save_picks(DATE, rescored[:15])
        # Enrich the picks file with team-level projections + game-market picks
        # so tomorrow's grader (once updated) can grade both markets.
        try:
            with open(picks_file, "r") as f:
                pdata = json.load(f)
            pdata["game_projections"] = [
                {"game_label": gp["game_label"], "away_abbr": gp["away_abbr"], "home_abbr": gp["home_abbr"],
                 "away_projected_runs": round(gp["away_proj"]["e_r"], 3),
                 "home_projected_runs": round(gp["home_proj"]["e_r"], 3),
                 "game_total": round(gp["game_total"], 3),
                 "margin": round(gp["margin"], 3),
                 "p_home_win": round(gp["p_home_win"], 4),
                 "away_e_h": round(gp["away_proj"]["e_h"], 3),
                 "home_e_h": round(gp["home_proj"]["e_h"], 3),
                 "away_e_rbi": round(gp["away_proj"]["e_rbi"], 3),
                 "home_e_rbi": round(gp["home_proj"]["e_rbi"], 3)}
                for gp in game_projections
            ]
            # Keep the top-20 positive-edge picks (all markets combined) for tomorrow's grader
            pdata["team_market_picks"] = [
                {k: v for k, v in tp.items() if k != "_odds_event_id"}
                for tp in team_market_picks[:20]
            ]
            with open(picks_file, "w") as f:
                json.dump(pdata, f, indent=2, default=str)
            print(f"  enriched {picks_file} with {len(game_projections)} game projections "
                  f"+ {min(20, len(team_market_picks))} market picks", flush=True)
        except Exception as ee:
            print(f"  WARN: could not enrich picks file with team data: {ee}", file=sys.stderr)
    except Exception as e:
        print(f"  WARN: could not write picks snapshot: {e}", file=sys.stderr)

    # Build the running track record from any results/*.json files on disk.
    all_results = load_all_results()
    tr_html = build_track_record_html(DATE, all_results)
    tr_text = build_track_record_text(DATE, all_results)

    stats = {"games": len(games), "candidates": len(rows), "qualified": len(qualified), "rostered": rostered}

    # Build team/game market panels (empty strings if no data — degrades cleanly)
    team_market_html = build_team_market_picks_html(team_market_picks, game_projections)
    game_proj_html = build_game_projections_html(game_projections)

    html = build_email_html(DATE, top, stats,
                            track_record_html=tr_html,
                            team_market_html=team_market_html,
                            game_projections_html=game_proj_html)
    text = build_email_text(DATE, top, stats, track_record_line=tr_text)
    subject = f"MLB Prop Edge — Team Totals + Player Picks for {DATE}"

    print(f"  scored {len(rows)} batters, {len(qualified)} qualified for prop board; sending top 10...", flush=True)
    resp = send_email(api_key, recipient, subject, html, text)
    print(f"  sent: {resp}", flush=True)

    # Also write a standalone dashboard with more rows (top 30) for GitHub Pages.
    # Same model, same factors as the email — just more depth so you can scroll
    # past the top 10 and see the next tier of candidates.
    try:
        os.makedirs("docs", exist_ok=True)
        dash_rows = rescored[:30]
        dash_html = build_dashboard_html(DATE, dash_rows, stats,
                                         track_record_html=tr_html,
                                         team_market_html=team_market_html,
                                         game_projections_html=game_proj_html)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(dash_html)
        print(f"  wrote docs/index.html ({len(dash_rows)} rows)", flush=True)
    except Exception as e:
        print(f"  WARN: could not write dashboard: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
