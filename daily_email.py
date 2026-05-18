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

PA_BY_ORDER = {1: 4.55, 2: 4.45, 3: 4.35, 4: 4.25, 5: 4.15, 6: 4.05, 7: 3.95, 8: 3.85, 9: 3.75}
DEFAULT_PA = 4.15
BREAKEVEN_PROB = 0.524
USER_AGENT = "MLB-PropEdge/1.0 (daily-email)"


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
    out = {}
    splits = d.get("stats", [{}])[0].get("splits", [])
    for s in splits:
        pid = str(s["player"]["id"]); st = s["stat"]
        pa = st.get("plateAppearances", 0)
        if pa <= 0: continue
        out[pid] = {
            "PA": pa,
            "H_pa":   (st.get("hits", 0) or 0) / pa,
            "R_pa":   (st.get("runs", 0) or 0) / pa,
            "RBI_pa": (st.get("rbi",  0) or 0) / pa,
        }
    return out

def index_team_hitting(d):
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


def poisson_p_geq_k(lam, k):
    if lam <= 0: return 0.0 if k > 0 else 1.0
    if k <= 0: return 1.0
    term = math.exp(-lam)
    total = term
    for i in range(1, k):
        term *= lam / i
        total += term
    return max(0.0, min(1.0, 1.0 - total))


def project_hrr(bid, pid, lineup_pos, batters, pitchers, league_mix, league_xwoba, hand, p_hand_map,
                season, team_rpg_map, league_rpg, team_id_of_batter):
    base_xw = matchup_xwoba(batters, pitchers, league_mix, league_xwoba, bid, pid)
    if not base_xw: return None
    ph = p_hand_map.get(pid)
    hf = handedness_factor(hand, bid, ph)
    if base_xw["baseline"] > 0.15:
        quality_mult = base_xw["matchup"] / base_xw["baseline"]
    else:
        quality_mult = 1.0
    quality_mult = max(0.75, min(1.35, quality_mult))
    team_rpg = team_rpg_map.get(team_id_of_batter, league_rpg)
    team_factor = team_rpg / max(0.1, league_rpg)
    team_factor = max(0.85, min(1.15, team_factor))
    season_stats = season.get(bid)
    if not season_stats:
        season_stats = {"H_pa": 0.225, "R_pa": 0.115, "RBI_pa": 0.110, "PA": 0}
    adj_h   = season_stats["H_pa"]   * quality_mult * hf
    adj_r   = season_stats["R_pa"]   * quality_mult * hf * team_factor
    adj_rbi = season_stats["RBI_pa"] * quality_mult * hf * team_factor
    pa = PA_BY_ORDER.get(lineup_pos, DEFAULT_PA)
    e_h = adj_h * pa; e_r = adj_r * pa; e_rbi = adj_rbi * pa
    e_hrr = e_h + e_r + e_rbi
    p_over_15 = poisson_p_geq_k(e_hrr, 2)
    p_over_25 = poisson_p_geq_k(e_hrr, 3)
    edge_15 = p_over_15 - BREAKEVEN_PROB
    edge_25 = p_over_25 - BREAKEVEN_PROB
    best_line = "1.5" if edge_15 >= edge_25 else "2.5"
    best_edge = max(edge_15, edge_25)
    return {
        "matchup_xwoba": base_xw["matchup"], "baseline_xwoba": base_xw["baseline"],
        "impact": base_xw["impact"], "ph": ph, "hf": hf,
        "quality_mult": quality_mult, "team_factor": team_factor,
        "expected_pa": pa, "lineup_pos": lineup_pos, "season_pa": season_stats["PA"],
        "season_h_pa": season_stats["H_pa"], "season_r_pa": season_stats["R_pa"], "season_rbi_pa": season_stats["RBI_pa"],
        "e_h": e_h, "e_r": e_r, "e_rbi": e_rbi, "e_hrr": e_hrr,
        "p_over_15": p_over_15, "p_over_25": p_over_25,
        "edge_15": edge_15, "edge_25": edge_25,
        "best_line": best_line, "best_edge": best_edge,
    }


def send_email(api_key, to_email, subject, html_body, text_body):
    payload = json.dumps({
        "from": "onboarding@resend.dev", "to": [to_email],
        "subject": subject, "html": html_body, "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "Mozilla/5.0 (compatible; MLB-PropEdge/1.0)", "Accept": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Resend API error HTTP {e.code}: {body}", file=sys.stderr)
        raise

def fmt_pct(p): return f"{p*100:.1f}%"
def fmt_edge_pct(e): return f"{e*100:+.1f}%"

def build_email_html(date, rows, stats):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        edge_color = "#166534" if r["best_edge"] > 0.02 else "#991b1b" if r["best_edge"] < -0.02 else "#6b7280"
        hand_str = f" ({r['ph']}HP)" if r["ph"] else ""
        order_str = f"#{r['lineup_pos']}" if r["lineup_pos"] else "&mdash;"
        rows_html.append(
            f"<tr style='border-bottom:1px solid #f3f4f6'>"
            f"<td style='padding:8px 10px;font-weight:700;color:{edge_color}'>{fmt_edge_pct(r['best_edge'])}</td>"
            f"<td style='padding:8px 10px;font-size:11px'>{status}</td>"
            f"<td style='padding:8px 10px'><strong>{r['name']}</strong> <span style='color:#9ca3af;font-size:11px'>{r['team']}</span> <span style='color:#9ca3af;font-size:11px'>{order_str}</span></td>"
            f"<td style='padding:8px 10px'>vs {r['opp']}{hand_str} <span style='color:#9ca3af'>({r['game']})</span></td>"
            f"<td style='padding:8px 10px;font-weight:600'>O{r['best_line']}</td>"
            f"<td style='padding:8px 10px'>{r['e_hrr']:.2f}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_15'])}</td>"
            f"<td style='padding:8px 10px'>{fmt_pct(r['p_over_25'])}</td>"
            f"<td style='padding:8px 10px'>{r['hf']:.2f}</td>"
            f"<td style='padding:8px 10px'>{round(r['impact']*100)}%</td>"
            f"</tr>"
        )
    return (
        f"<!DOCTYPE html><html><body style='font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fafafa;color:#1f2937;margin:0;padding:20px'>"
        f"<div style='max-width:860px;margin:0 auto'>"
        f"<h1 style='font-size:20px;margin:0 0 4px'>MLB Prop Edge — Top 10 H+R+RBI</h1>"
        f"<div style='color:#6b7280;font-size:13px;margin-bottom:16px'>{today_str} &middot; {stats['games']} games &middot; ranked by best edge vs -110 break-even (52.4%)</div>"
        f"<table style='width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;font-size:13px'>"
        f"<thead style='background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left'>"
        f"<tr><th style='padding:8px 10px'>Edge</th><th style='padding:8px 10px'>Status</th>"
        f"<th style='padding:8px 10px'>Batter (order)</th><th style='padding:8px 10px'>Matchup</th>"
        f"<th style='padding:8px 10px'>Best Line</th><th style='padding:8px 10px'>E[HRR]</th>"
        f"<th style='padding:8px 10px'>P(O1.5)</th><th style='padding:8px 10px'>P(O2.5)</th>"
        f"<th style='padding:8px 10px'>Hand&times;</th><th style='padding:8px 10px'>Cov</th></tr>"
        f"</thead><tbody>{''.join(rows_html)}</tbody></table>"
        f"<div style='margin-top:18px;font-size:12px;color:#4b5563;line-height:1.5'>"
        f"<strong>How the edge is computed.</strong> For each batter, the model projects expected hits, runs, and RBIs separately and sums them. "
        f"E[HRR] = (season H/PA + R/PA + RBI/PA) &times; quality multiplier &times; handedness factor &times; team-offense factor &times; expected PA. "
        f"Quality multiplier = matchup xwOBA / batter baseline xwOBA (capped 0.75&ndash;1.35). Team factor scales R and RBI by team R/G vs league avg (capped 0.85&ndash;1.15). "
        f"P(O1.5) = P(HRR &ge; 2), P(O2.5) = P(HRR &ge; 3), via Poisson with that expected value. <strong>Edge</strong> = best P(over) &minus; 52.4% (break-even at &minus;110). "
        f"Status: <span style='color:#166534'>&#9989; in</span> = confirmed in lineup, <em>bench</em> = active roster but not posted, <span style='color:#92400e'>&#9711; TBD</span> = lineup not yet released. "
        f"Coverage = share of the pitcher&rsquo;s mix where the batter has real Savant data."
        f"</div></div></body></html>"
    )

def build_email_text(date, rows, stats):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    lines = [f"MLB Prop Edge — Top 10 H+R+RBI for {today_str}",
             f"{stats['games']} games, ranked by best edge vs -110 break-even.", ""]
    for i, r in enumerate(rows, 1):
        status = "in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "TBD"
        ph = f" ({r['ph']}HP)" if r["ph"] else ""
        ord_str = f"#{r['lineup_pos']}" if r["lineup_pos"] else "-"
        lines.append(f"{i:>2}. {fmt_edge_pct(r['best_edge']):>7}  [{status:<5}]  {r['name']} ({r['team']}) {ord_str}")
        lines.append(f"          vs {r['opp']}{ph} · {r['game']}")
        lines.append(f"          O{r['best_line']}: bet · E[HRR]={r['e_hrr']:.2f} · P(O1.5)={fmt_pct(r['p_over_15'])} · P(O2.5)={fmt_pct(r['p_over_25'])} · Hand×{r['hf']:.2f}")
        lines.append("")
    return "\n".join(lines)


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
    print(f"  fetched savant({len(bat_rows)} batter rows / {len(pit_rows)} pitcher rows), splits, season, teams", flush=True)

    batters = index_batters(bat_rows)
    pitchers = index_pitchers(pit_rows)
    lg_mix = league_pitch_mix(pit_rows)
    lg_xw  = league_xwoba_by_pitch(bat_rows)
    hand   = index_handedness(vl, vr)
    season = index_season_hitting(season_d)
    team_rpg, league_rpg = index_team_hitting(team_d)
    print(f"  season stats: {len(season)} batters · league R/G: {league_rpg:.2f}", flush=True)

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

        def score_side(roster, opp_pp, my_abbr, opp_abbr, lineup_ids, lineup_pos_map, game_label, my_team_id):
            if not opp_pp.get("id"): return
            pid = str(opp_pp["id"])
            if pid not in pitchers: return
            for pl in roster:
                bid = pl["id"]
                pos = lineup_pos_map.get(bid)
                r = project_hrr(bid, pid, pos, batters, pitchers, lg_mix, lg_xw, hand, p_hand_map,
                                season, team_rpg, league_rpg, my_team_id)
                if not r: continue
                in_lineup = None if not lineup_ids else (bid in lineup_ids)
                rows.append({
                    "name": pl["name"], "team": my_abbr, "opp": opp_pp.get("fullName", "?"),
                    "game": game_label, "in_lineup": in_lineup,
                    **r,
                })

        score_side(rosters.get(a_tid, []), h_pp, a_abbr, h_abbr, a_lineup_ids, a_lineup_pos, f"{a_abbr} @ {h_abbr}", a_tid)
        score_side(rosters.get(h_tid, []), a_pp, h_abbr, a_abbr, h_lineup_ids, h_lineup_pos, f"{a_abbr} @ {h_abbr}", h_tid)

    qualified = [r for r in rows if r["impact"] >= 0.5 and r["season_pa"] >= 30]
    qualified.sort(key=lambda x: -x["best_edge"])
    top = qualified[:10]

    stats = {"games": len(games), "candidates": len(rows), "qualified": len(qualified), "rostered": rostered}
    html = build_email_html(DATE, top, stats)
    text = build_email_text(DATE, top, stats)
    subject = f"MLB Prop Edge — Top 10 H+R+RBI for {DATE}"

    print(f"  scored {len(rows)} batters, {len(qualified)} qualified for prop board; sending top 10...", flush=True)
    resp = send_email(api_key, recipient, subject, html, text)
    print(f"  sent: {resp}", flush=True)

if __name__ == "__main__":
    main()
