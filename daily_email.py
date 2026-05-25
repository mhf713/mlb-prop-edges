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
                bvp_mult=1.0, bvp_pa=0, bvp_woba=None):
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
    env_mult = park_mult * weather_mult * bullpen_mult * bvp_mult

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

def build_email_html(date, rows, stats, track_record_html=""):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        edge_color = "#166534" if r["best_edge"] > 0.02 else "#991b1b" if r["best_edge"] < -0.02 else "#6b7280"
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
        wx_str = r.get("weather_label") or "&mdash;"
        rows_html.append(
            f"<tr style='border-bottom:1px solid #f3f4f6'>"
            f"<td style='padding:8px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(r['best_edge'])}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{status}</td>"
            f"<td style='padding:8px 10px'><strong>{r['name']}</strong> <span style='color:#9ca3af;font-size:11px'>{r['team']}</span> <span style='color:#9ca3af;font-size:11px'>{order_str}</span></td>"
            f"<td style='padding:8px 10px'>vs {r['opp']}{hand_str} <span style='color:#9ca3af'>({r['game']})</span></td>"
            f"<td style='padding:8px 10px;font-weight:600'>O{r['best_line']}</td>"
            f"<td style='padding:8px 10px'>{r['e_hrr']:.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{recent_color}'>{recent_str}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_15'])}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_25'])}</td>"
            f"<td style='padding:8px 10px'>{r['hf']:.2f}</td>"
            f"<td style='padding:8px 10px'>{r.get('park_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{r.get('park_label','')}</span></td>"
            f"<td style='padding:8px 10px;font-size:11px'>{r.get('weather_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{wx_str}</span></td>"
            f"<td style='padding:8px 10px'>{r.get('bullpen_mult', 1.0):.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvp_color}'>{bvp_str}</td>"
            f"<td style='padding:8px 10px'>{round(r['impact']*100)}%</td>"
            f"</tr>"
        )
    return (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fafafa;color:#1f2937;margin:0;padding:20px'>"
        f"<div style='max-width:1080px;margin:0 auto'>"
        f"<h1 style='font-size:20px;margin:0 0 4px'>MLB Prop Edge — Top 10 H+R+RBI</h1>"
        f"<div style='color:#6b7280;font-size:13px;margin-bottom:16px'>{today_str} &middot; {stats['games']} games &middot; ranked by best edge vs -110 break-even (52.4%)</div>"
        f"{track_record_html}"
        f"<table style='width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;font-size:13px'>"
        f"<thead style='background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left'>"
        f"<tr>"
        f"<th style='padding:8px 10px'>Edge</th>"
        f"<th style='padding:8px 10px'>Status</th>"
        f"<th style='padding:8px 10px'>Batter (order)</th>"
        f"<th style='padding:8px 10px'>Matchup</th>"
        f"<th style='padding:8px 10px'>Line</th>"
        f"<th style='padding:8px 10px'>E[HRR]</th>"
        f"<th style='padding:8px 10px'>Recent 15d</th>"
        f"<th style='padding:8px 10px'>P(O1.5)</th>"
        f"<th style='padding:8px 10px'>P(O2.5)</th>"
        f"<th style='padding:8px 10px'>Hand&times;</th>"
        f"<th style='padding:8px 10px'>Park&times;</th>"
        f"<th style='padding:8px 10px'>Wx&times;</th>"
        f"<th style='padding:8px 10px'>BP&times;</th>"
        f"<th style='padding:8px 10px'>BvP&times;</th>"
        f"<th style='padding:8px 10px'>Cov</th>"
        f"</tr></thead>"
        f"<tbody>{''.join(rows_html)}</tbody>"
        f"</table>"
        f"<div style='margin-top:18px;font-size:12px;color:#4b5563;line-height:1.55'>"
        f"<div style='font-weight:700;color:#111827;margin-bottom:6px'>How the projection is built</div>"
        f"E[HRR] = expected_PA &times; (blended per-PA H/R/RBI rates) &times; Quality &times; Hand &times; Team &times; Park &times; Weather &times; Bullpen &times; BvP. "
        f"P(O1.5) = P(HRR &ge; 2) and P(O2.5) = P(HRR &ge; 3) via Poisson with that expected value. "
        f"Edge = best P(over) &minus; 52.4% (break-even at &minus;110 juice). "
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

def build_dashboard_html(date, rows, stats, track_record_html=""):
    """Standalone dashboard HTML — same data as the email, just more rows and a timestamp.

    Designed to be committed to docs/index.html and served via GitHub Pages.
    Everything is inlined (no external assets) so it renders identically anywhere.
    """
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    gen_at_utc = dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        edge_color = "#166534" if r["best_edge"] > 0.02 else "#991b1b" if r["best_edge"] < -0.02 else "#6b7280"
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
        wx_str = r.get("weather_label") or "&mdash;"
        bid_attr = f" data-bid='{r.get('_bid','')}'" if r.get("_bid") else ""
        rows_html.append(
            f"<tr style='border-bottom:1px solid #f3f4f6'{bid_attr}>"
            f"<td style='padding:8px 10px;color:#9ca3af;font-size:11px'>{i}</td>"
            f"<td style='padding:8px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(r['best_edge'])}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{status}</td>"
            f"<td style='padding:8px 10px'><strong>{r['name']}</strong> <span style='color:#9ca3af;font-size:11px'>{r['team']}</span> <span style='color:#9ca3af;font-size:11px'>{order_str}</span></td>"
            f"<td style='padding:8px 10px'>vs {r['opp']}{hand_str} <span style='color:#9ca3af'>({r['game']})</span></td>"
            f"<td style='padding:8px 10px;font-weight:600'>O{r['best_line']}</td>"
            f"<td style='padding:8px 10px'>{r['e_hrr']:.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{recent_color}'>{recent_str}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_15'])}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_25'])}</td>"
            f"<td style='padding:8px 10px'>{r['hf']:.2f}</td>"
            f"<td style='padding:8px 10px'>{r.get('park_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{r.get('park_label','')}</span></td>"
            f"<td style='padding:8px 10px;font-size:11px'>{r.get('weather_mult', 1.0):.2f}<br/><span style='color:#9ca3af;font-size:10px'>{wx_str}</span></td>"
            f"<td style='padding:8px 10px'>{r.get('bullpen_mult', 1.0):.2f}</td>"
            f"<td style='padding:8px 10px;font-size:11px;color:{bvp_color}'>{bvp_str}</td>"
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
        f"<th style='padding:8px 10px'>E[HRR]</th>"
        f"<th style='padding:8px 10px'>Recent 15d</th>"
        f"<th style='padding:8px 10px'>P(O1.5)</th>"
        f"<th style='padding:8px 10px'>P(O2.5)</th>"
        f"<th style='padding:8px 10px'>Hand&times;</th>"
        f"<th style='padding:8px 10px'>Park&times;</th>"
        f"<th style='padding:8px 10px'>Wx&times;</th>"
        f"<th style='padding:8px 10px'>BP&times;</th>"
        f"<th style='padding:8px 10px'>BvP&times;</th>"
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
        f"</table>"
        f"<div style='margin-top:14px;color:#6b7280'>Status: <span style='color:#166534'>&#9989; in</span> = confirmed in lineup, <em>bench</em> = active roster but not in posted lineup, <span style='color:#92400e'>&#9711; TBD</span> = lineup not yet released. Coverage = share of the pitcher&rsquo;s mix where the batter has real Savant data.</div>"
        f"</div>"
        f"</div></body></html>"
    )

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
                rec = {
                    "batter_id": pid, "name": name,
                    "hits":  int(bat.get("hits", 0) or 0),
                    "runs":  int(bat.get("runs", 0) or 0),
                    "rbi":   int(bat.get("rbi", 0) or 0),
                    "ab":    int(bat.get("atBats", 0) or 0),
                    "pa":    pa,
                    "played": pa > 0,
                }
                if pid:
                    tbucket[pid] = rec
                tbucket[_normalize_name(name)] = rec
    return out

def grade_picks_for_date(date_iso, picks_dir="picks", results_dir="results"):
    """If picks/{date}.json exists and results/{date}.json does not, fetch actual box
    scores for that day and write graded results. Returns the summary dict or None."""
    picks_file = os.path.join(picks_dir, f"{date_iso}.json")
    results_file = os.path.join(results_dir, f"{date_iso}.json")
    if not os.path.exists(picks_file):
        return None
    if os.path.exists(results_file):
        return None
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
    sums = {"graded": 0, "matched": 0, "played": 0, "over_15": 0, "over_25": 0,
            "wins_at_line": 0, "ev_o15_sum": 0.0, "ev_o25_sum": 0.0, "ev_line_sum": 0.0}
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
            results.append({**p, "matched": False, "played": False, "hrr": None})
            continue
        h, r_, rbi = rec["hits"], rec["runs"], rec["rbi"]
        hrr = h + r_ + rbi
        played = bool(rec["played"])
        line = str(p.get("line") or "1.5")
        over_15 = played and hrr >= 2
        over_25 = played and hrr >= 3
        win_at_line = over_15 if line == "1.5" else over_25
        results.append({**p, "matched": True, "played": played,
                        "hits": h, "runs": r_, "rbi": rbi, "hrr": hrr,
                        "over_15": over_15, "over_25": over_25,
                        "win_at_line": win_at_line})
        sums["matched"] += 1
        if played:
            sums["played"] += 1
            if over_15: sums["over_15"] += 1
            if over_25: sums["over_25"] += 1
            if win_at_line: sums["wins_at_line"] += 1
            sums["ev_o15_sum"]  += float(p.get("p_over_15") or 0.0)
            sums["ev_o25_sum"]  += float(p.get("p_over_25") or 0.0)
            sums["ev_line_sum"] += float((p.get("p_over_15") if line == "1.5" else p.get("p_over_25")) or 0.0)
    pl = max(1, sums["played"])
    summary = {
        **sums,
        "over_15_rate": sums["over_15"]/pl if sums["played"] else 0.0,
        "over_25_rate": sums["over_25"]/pl if sums["played"] else 0.0,
        "wins_at_line_rate": sums["wins_at_line"]/pl if sums["played"] else 0.0,
        "expected_over_15_rate": sums["ev_o15_sum"]/pl if sums["played"] else 0.0,
        "expected_over_25_rate": sums["ev_o25_sum"]/pl if sums["played"] else 0.0,
        "expected_wins_at_line_rate": sums["ev_line_sum"]/pl if sums["played"] else 0.0,
        "roi_at_line_pct": (sums["wins_at_line"]/pl * 1.909 - 1.0) if sums["played"] else 0.0,
    }
    os.makedirs(results_dir, exist_ok=True)
    with open(results_file, "w") as f:
        json.dump({"date": date_iso,
                   "graded_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
                   "results": results, "summary": summary}, f, indent=2)
    print(f"  graded {date_iso}: {sums['wins_at_line']}/{sums['played']} wins at picked line "
          f"(expected {summary['expected_wins_at_line_rate']*100:.1f}%, ROI {summary['roi_at_line_pct']*100:+.1f}%)", flush=True)
    return summary

def save_picks(date_iso, top_picks, picks_dir="picks"):
    """Write a JSON snapshot of today's top picks so we can grade them tomorrow."""
    os.makedirs(picks_dir, exist_ok=True)
    fname = os.path.join(picks_dir, f"{date_iso}.json")
    out = {
        "date": date_iso,
        "generated_at_utc": dt.datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "picks": [
            {"rank": i+1, "batter_id": str(r.get("_bid") or ""),
             "name": r.get("name", ""), "team": r.get("team", ""),
             "opp_pitcher": r.get("opp", ""), "game": r.get("game", ""),
             "in_lineup": r.get("in_lineup"), "lineup_pos": r.get("lineup_pos"),
             "line": r.get("best_line", "1.5"),
             "best_edge": r.get("best_edge", 0.0),
             "p_over_15": r.get("p_over_15", 0.0),
             "p_over_25": r.get("p_over_25", 0.0),
             "e_hrr": r.get("e_hrr", 0.0)}
            for i, r in enumerate(top_picks)
        ],
    }
    with open(fname, "w") as f:
        json.dump(out, f, indent=2)
    return fname

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
    if window_days is not None and today_iso:
        from_date = (dt.date.fromisoformat(today_iso) - dt.timedelta(days=window_days)).isoformat()
        all_results = [r for r in all_results if r.get("date", "") >= from_date]
    days = len(all_results)
    graded = matched = played = o15 = o25 = wins = 0
    ev_o15 = ev_o25 = ev_line = 0.0
    for d in all_results:
        s = d.get("summary", {}) or {}
        graded  += int(s.get("graded", 0) or 0)
        matched += int(s.get("matched", 0) or 0)
        played  += int(s.get("played", 0) or 0)
        o15     += int(s.get("over_15", 0) or 0)
        o25     += int(s.get("over_25", 0) or 0)
        wins    += int(s.get("wins_at_line", 0) or 0)
        ev_o15  += float(s.get("ev_o15_sum", 0) or 0)
        ev_o25  += float(s.get("ev_o25_sum", 0) or 0)
        ev_line += float(s.get("ev_line_sum", 0) or 0)
    pl = max(1, played)
    return {"days": days, "graded": graded, "matched": matched, "played": played,
            "over_15": o15, "over_25": o25, "wins": wins,
            "over_15_rate": o15/pl if played else 0.0,
            "over_25_rate": o25/pl if played else 0.0,
            "wins_at_line_rate": wins/pl if played else 0.0,
            "expected_over_15_rate": ev_o15/pl if played else 0.0,
            "expected_over_25_rate": ev_o25/pl if played else 0.0,
            "expected_wins_at_line_rate": ev_line/pl if played else 0.0,
            "roi_at_line_pct": (wins/pl * 1.909 - 1.0) if played else 0.0}

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
        if not a["played"]:
            return (f"<tr style='border-top:1px solid #f3f4f6'><td style='padding:5px 10px;color:#6b7280;font-weight:600'>{label}</td>"
                    f"<td colspan='6' style='padding:5px 10px;color:#9ca3af;font-size:11px'>no graded picks in window</td></tr>")
        edge = a["wins_at_line_rate"] - a["expected_wins_at_line_rate"]
        edge_color = "#166534" if edge > 0.02 else "#991b1b" if edge < -0.02 else "#6b7280"
        roi = a["roi_at_line_pct"]
        roi_color = "#166534" if roi > 0.02 else "#991b1b" if roi < -0.02 else "#6b7280"
        return (f"<tr style='border-top:1px solid #f3f4f6'>"
                f"<td style='padding:5px 10px;color:#374151;font-weight:600'>{label}</td>"
                f"<td style='padding:5px 10px;color:#6b7280'>{a['days']}d</td>"
                f"<td style='padding:5px 10px;color:#6b7280'><strong style='color:#374151'>{a['over_15']}</strong>/{a['graded']}</td>"
                f"<td style='padding:5px 10px'><strong>{a['wins_at_line_rate']*100:.1f}%</strong> "
                f"<span style='color:#9ca3af;font-size:11px'>({a['wins']}W)</span></td>"
                f"<td style='padding:5px 10px;color:#6b7280'>{a['expected_wins_at_line_rate']*100:.1f}%</td>"
                f"<td style='padding:5px 10px;font-weight:600;color:{edge_color}'>{edge*100:+.1f}%</td>"
                f"<td style='padding:5px 10px;font-weight:600;color:{roi_color}'>{roi*100:+.1f}%</td>"
                f"</tr>")
    return (
        f"<div style='margin:14px 0;background:#fff;border:1px solid #e5e7eb;border-radius:8px;overflow:hidden'>"
        f"<div style='padding:10px 14px;background:#f9fafb;border-bottom:1px solid #e5e7eb;font-weight:700;color:#111827;font-size:13px'>"
        f"Track Record &mdash; H+R+RBI hit rate at each pick&rsquo;s chosen line "
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
    if not a["played"]:
        return "Track Record: no plays graded yet."
    return (f"Track Record (all time): {a['days']} days, {a['played']}/{a['graded']} picks played, "
            f"hit rate {a['wins_at_line_rate']*100:.1f}% (expected {a['expected_wins_at_line_rate']*100:.1f}%), "
            f"ROI {a['roi_at_line_pct']*100:+.1f}% @ -110.")

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

    # Grade yesterday's picks, if we have them and haven't graded them yet. Safe
    # to call every run — it no-ops if the picks file is missing, if the results
    # file already exists, or if yesterday's games aren't all Final yet.
    try:
        YDATE = (et_today - dt.timedelta(days=1)).isoformat()
        grade_picks_for_date(YDATE)
    except Exception as e:
        print(f"  WARN: grading {YDATE} failed: {e}", file=sys.stderr)

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

        def score_side(roster, opp_pp, my_abbr, opp_abbr, lineup_ids, lineup_pos_map, game_label, my_team_id,
                       bp_mult, bp_ratio):
            if not opp_pp.get("id"): return
            pid = str(opp_pp["id"])
            if pid not in pitchers: return
            for pl in roster:
                bid = pl["id"]
                pos = lineup_pos_map.get(bid)  # None if not in lineup yet
                # First pass: no BvP yet (added in second pass for top candidates).
                r = project_hrr(bid, pid, pos, batters, pitchers, lg_mix, lg_xw, hand, p_hand_map,
                                season, team_rpg, league_rpg, my_team_id, recent,
                                park_mult=pk_mult, weather_mult=wx_mult, weather_label=wx_label,
                                bullpen_mult=bp_mult, bullpen_ratio=bp_ratio)
                if not r: continue
                in_lineup = None if not lineup_ids else (bid in lineup_ids)
                rows.append({
                    "name": pl["name"], "team": my_abbr, "opp": opp_pp.get("fullName", "?"),
                    "game": game_label, "in_lineup": in_lineup,
                    "_pid": pid, "_bid": bid, "_lineup_pos": pos, "_my_team_id": my_team_id,
                    "park_label": h_abbr,
                    **r,
                })

        score_side(rosters.get(a_tid, []), h_pp, a_abbr, h_abbr, a_lineup_ids, a_lineup_pos, f"{a_abbr} @ {h_abbr}", a_tid,
                   a_bp_mult, a_bp_ratio)
        score_side(rosters.get(h_tid, []), a_pp, h_abbr, a_abbr, h_lineup_ids, h_lineup_pos, f"{a_abbr} @ {h_abbr}", h_tid,
                   h_bp_mult, h_bp_ratio)

    # First-pass qualified pool
    qualified = [r for r in rows if r["impact"] >= 0.5 and r["season_pa"] >= 30]
    qualified.sort(key=lambda x: -x["best_edge"])

    # Second pass: fetch BvP for the top 60 candidates and re-score with that factor applied.
    BVP_LOOKUP_LIMIT = 60
    candidates = qualified[:BVP_LOOKUP_LIMIT]
    print(f"  fetching BvP history for top {len(candidates)} candidates...", flush=True)
    rescored = []
    for r in candidates:
        bid = r["_bid"]; pid = r["_pid"]
        bvp = get_bvp(bid, pid)
        season_woba = (season.get(bid) or {}).get("wOBA", 0.310)
        bvp_m, bvp_pa, bvp_w = bvp_factor(bvp, season_woba)
        # Recompute with BvP factored in (other factors stay the same)
        r2 = project_hrr(
            bid, pid, r["_lineup_pos"], batters, pitchers, lg_mix, lg_xw, hand, p_hand_map,
            season, team_rpg, league_rpg, r["_my_team_id"], recent,
            park_mult=r["park_mult"], weather_mult=r["weather_mult"], weather_label=r["weather_label"],
            bullpen_mult=r["bullpen_mult"], bullpen_ratio=r["bullpen_ratio"],
            bvp_mult=bvp_m, bvp_pa=bvp_pa, bvp_woba=bvp_w,
        )
        if not r2: continue
        rescored.append({
            "name": r["name"], "team": r["team"], "opp": r["opp"], "game": r["game"],
            "in_lineup": r["in_lineup"], "park_label": r["park_label"],
            "_pid": pid, "_bid": bid, "_lineup_pos": r["_lineup_pos"], "_my_team_id": r["_my_team_id"],
            **r2,
        })

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

    top = rescored[:10]

    # Snapshot today's top 15 to picks/{DATE}.json so tomorrow's run can grade them.
    # We save 15 (not just the top 10 sent in email) because the user reasons about
    # the top 15 on the dashboard and parlays.
    try:
        picks_file = save_picks(DATE, rescored[:15])
        print(f"  wrote {picks_file} (top 15)", flush=True)
    except Exception as e:
        print(f"  WARN: could not write picks snapshot: {e}", file=sys.stderr)

    # Build the running track record from any results/*.json files on disk.
    all_results = load_all_results()
    tr_html = build_track_record_html(DATE, all_results)
    tr_text = build_track_record_text(DATE, all_results)

    stats = {"games": len(games), "candidates": len(rows), "qualified": len(qualified), "rostered": rostered}
    html = build_email_html(DATE, top, stats, track_record_html=tr_html)
    text = build_email_text(DATE, top, stats, track_record_line=tr_text)
    subject = f"MLB Prop Edge — Top 10 H+R+RBI for {DATE}"

    print(f"  scored {len(rows)} batters, {len(qualified)} qualified for prop board; sending top 10...", flush=True)
    resp = send_email(api_key, recipient, subject, html, text)
    print(f"  sent: {resp}", flush=True)

    # Also write a standalone dashboard with more rows (top 30) for GitHub Pages.
    # Same model, same factors as the email — just more depth so you can scroll
    # past the top 10 and see the next tier of candidates.
    try:
        os.makedirs("docs", exist_ok=True)
        dash_rows = rescored[:30]
        dash_html = build_dashboard_html(DATE, dash_rows, stats, track_record_html=tr_html)
        with open("docs/index.html", "w", encoding="utf-8") as f:
            f.write(dash_html)
        print(f"  wrote docs/index.html ({len(dash_rows)} rows)", flush=True)
    except Exception as e:
        print(f"  WARN: could not write dashboard: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
