#!/usr/bin/env python3
"""
MLB Prop Edge — Daily Email Sender
"""

import csv
import datetime as dt
import io
import json
import os
import sys
import urllib.request
import urllib.error

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

USER_AGENT = "MLB-PropEdge-DailyEmail/1.0"

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
    url = f"https://baseballsavant.mlb.com/leaderboard/pitch-arsenal-stats?type={kind}&min=10&season={season}&csv=true"
    return list(csv.DictReader(fetch(url).splitlines()))

def get_splits(sit, season):
    return json.loads(fetch(f"https://statsapi.mlb.com/api/v1/stats?stats=statSplits&group=hitting&sitCodes={sit}&season={season}&sportId=1&gameType=R&limit=2000"))

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

def get_weather(lat, lon):
    try:
        return json.loads(fetch(f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}&hourly=temperature_2m,wind_speed_10m,wind_direction_10m,precipitation_probability&temperature_unit=fahrenheit&wind_speed_unit=mph&forecast_days=2&timezone=auto"))
    except Exception:
        return None

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

def matchup(batters, pitchers, league_mix, league_xwoba, hand, pitcher_hand_map, bid, pid):
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
    ph = pitcher_hand_map.get(pid)
    hf = handedness_factor(hand, bid, ph)
    adj = mn * hf
    return {"matchup": mn, "adjusted": adj, "baseline": base, "edge": adj - base, "impact": impact, "ph": ph, "hf": hf}

def angle_diff(a, b):
    d = a - b
    while d > 180: d -= 360
    while d < -180: d += 360
    return d

def wind_relative(wind_from, cf_az):
    if wind_from is None: return None
    blow_to = (wind_from + 180) % 360
    d = abs(angle_diff(blow_to, cf_az))
    if d <= 35: return "out"
    if d >= 145: return "in"
    return "cross"

def game_weather(game, stadium_tuple):
    if not stadium_tuple: return {"unknown": True}
    lat, lon, roof, cfaz = stadium_tuple
    if roof == "dome": return {"dome": True}
    data = get_weather(lat, lon)
    if not data: return {"error": "weather fetch failed"}
    try:
        target = dt.datetime.fromisoformat(game["gameDate"].replace("Z", "+00:00")).replace(tzinfo=None)
        hours = data["hourly"]["time"]
        idx, best = 0, float("inf")
        for i, t in enumerate(hours):
            diff = abs((dt.datetime.fromisoformat(t) - target).total_seconds())
            if diff < best: best = diff; idx = i
        return {"temp": data["hourly"]["temperature_2m"][idx],
                "wind": data["hourly"]["wind_speed_10m"][idx],
                "wd":   data["hourly"]["wind_direction_10m"][idx],
                "precip": data["hourly"]["precipitation_probability"][idx],
                "windKind": wind_relative(data["hourly"]["wind_direction_10m"][idx], cfaz)}
    except Exception as e:
        return {"error": str(e)}

def weather_points(wx):
    if not wx or wx.get("dome") or wx.get("unknown") or wx.get("error"): return 0
    pts = 0
    t = wx.get("temp")
    if t is not None:
        if t >= 75: pts += min(10, (t - 70) * 0.6)
        elif t <= 60: pts += max(-10, (t - 65) * 0.8)
    wk = wx.get("windKind"); ws = wx.get("wind")
    if wk and ws is not None:
        if wk == "out": pts += min(8, ws * 0.45)
        elif wk == "in": pts -= min(8, ws * 0.45)
    if wx.get("precip", 0) >= 60: pts -= 4
    return pts

def coverage_penalty(c):
    return 0 if c >= 0.7 else (0.7 - c) * 25

def send_email(api_key, to_email, subject, html_body, text_body):
    print(f"DEBUG: sending to '{to_email}' (len={len(to_email)}) with key prefix '{api_key[:7]}...' (len={len(api_key)})", file=sys.stderr)
    payload = json.dumps({
        "from": "onboarding@resend.dev",
        "to": [to_email],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }).encode("utf-8")
    req = urllib.request.Request("https://api.resend.com/emails", data=payload,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "User-Agent": "Mozilla/5.0 (compatible; MLB-PropEdge/1.0)", "Accept": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"Resend API error HTTP {e.code}: {body}", file=sys.stderr)
        raise

def fmt_woba(w):
    if w is None: return "—"
    s = f"{w:.3f}"
    return s.lstrip("0") if s.startswith("0.") else s

def fmt_edge(e):
    if e is None: return "—"
    return f"{round(e * 1000):+d}"

def fmt_score(s):
    return f"{s:+.1f}"

def build_email_html(date, rows, stats):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    rows_html = []
    for i, r in enumerate(rows, 1):
        status = ("&#9989; in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "&#9711; TBD")
        score_color = "#166534" if r["score"] > 2 else "#991b1b" if r["score"] < -2 else "#6b7280"
        hand_str = f" ({r['ph']}HP)" if r["ph"] else ""
        rows_html.append(f"<tr style=\"border-bottom:1px solid #f3f4f6\"><td style=\"padding:8px 10px;font-weight:700;color:{score_color}\">{fmt_score(r['score'])}</td><td style=\"padding:8px 10px;font-size:11px\">{status}</td><td style=\"padding:8px 10px\"><strong>{r['name']}</strong> <span style=\"color:#9ca3af;font-size:11px\">{r['team']}</span></td><td style=\"padding:8px 10px\">vs {r['opp']}{hand_str} <span style=\"color:#9ca3af\">({r['game']})</span></td><td style=\"padding:8px 10px\">{fmt_woba(r['expWoba'])}</td><td style=\"padding:8px 10px\">{r['hf']:.2f}</td><td style=\"padding:8px 10px\">{fmt_edge(r['edge'])}</td><td style=\"padding:8px 10px\">{r['wx']:+.1f}</td><td style=\"padding:8px 10px\">{round(r['cov']*100)}%</td></tr>")
    return f"<!DOCTYPE html><html><body style=\"font-family:-apple-system,Helvetica,Arial,sans-serif;background:#fafafa;color:#1f2937;margin:0;padding:20px\"><div style=\"max-width:760px;margin:0 auto\"><h1 style=\"font-size:20px;margin:0 0 4px\">MLB Prop Edge — Top 10</h1><div style=\"color:#6b7280;font-size:13px;margin-bottom:16px\">{today_str} &middot; {stats['games']} games &middot; {stats['candidates']} candidates</div><table style=\"width:100%;border-collapse:collapse;background:#fff;border:1px solid #e5e7eb;border-radius:8px;font-size:13px\"><thead style=\"background:#f9fafb;color:#6b7280;font-size:11px;text-transform:uppercase;text-align:left\"><tr><th style=\"padding:8px 10px\">Score</th><th style=\"padding:8px 10px\">Status</th><th style=\"padding:8px 10px\">Batter</th><th style=\"padding:8px 10px\">Matchup</th><th style=\"padding:8px 10px\">Exp xwOBA</th><th style=\"padding:8px 10px\">Hand×</th><th style=\"padding:8px 10px\">&#916; base</th><th style=\"padding:8px 10px\">Wx</th><th style=\"padding:8px 10px\">Cov</th></tr></thead><tbody>{''.join(rows_html)}</tbody></table></div></body></html>"

def build_email_text(date, rows, stats):
    today_str = dt.date.fromisoformat(date).strftime("%A, %B %d, %Y")
    lines = [f"MLB Prop Edge — Top 10 for {today_str}", f"{stats['games']} games · {stats['candidates']} candidates", ""]
    for i, r in enumerate(rows, 1):
        status = "in" if r["in_lineup"] is True else "bench" if r["in_lineup"] is False else "TBD"
        ph = f" ({r['ph']}HP)" if r["ph"] else ""
        lines.append(f"{i:>2}. {fmt_score(r['score']):>6}  [{status:<5}]  {r['name']} ({r['team']})")
        lines.append(f"          vs {r['opp']}{ph} · {r['game']}")
        lines.append(f"          ExpW {fmt_woba(r['expWoba'])} · Hand× {r['hf']:.2f} · Δbase {fmt_edge(r['edge'])} · Wx {r['wx']:+.1f} · Cov {round(r['cov']*100)}%")
        lines.append("")
    return "\n".join(lines)

def main():
    api_key = os.environ.get("RESEND_API_KEY")
    recipient = os.environ.get("RECIPIENT_EMAIL")
    if not api_key:
        print("ERROR: RESEND_API_KEY env var not set", file=sys.stderr); sys.exit(2)
    if not recipient:
        print("ERROR: RECIPIENT_EMAIL env var not set", file=sys.stderr); sys.exit(2)
    et_today = (dt.datetime.utcnow() - dt.timedelta(hours=4)).date()
    DATE = et_today.isoformat()
    SEASON = et_today.year
    print(f"Running for date={DATE} (ET) season={SEASON}", flush=True)
    sched = get_schedule(DATE)
    games = (sched.get("dates", [{}])[0] or {}).get("games", [])
    if not games:
        send_email(api_key, recipient, f"MLB Prop Edge — No games {DATE}", f"<p>No MLB games scheduled for {DATE}.</p>", f"No MLB games scheduled for {DATE}.")
        return
    print(f"  schedule: {len(games)} games", flush=True)
    bat_rows = get_savant(SEASON, "batter")
    pit_rows = get_savant(SEASON, "pitcher")
    vl = get_splits("vl", SEASON)
    vr = get_splits("vr", SEASON)
    print(f"  batter rows: {len(bat_rows)}, pitcher rows: {len(pit_rows)}", flush=True)
    batters = index_batters(bat_rows)
    pitchers = index_pitchers(pit_rows)
    lg_mix = league_pitch_mix(pit_rows)
    lg_xw  = league_xwoba_by_pitch(bat_rows)
    hand   = index_handedness(vl, vr)
    pitcher_ids = set()
    for g in games:
        for s in ("away", "home"):
            pp = g["teams"][s].get("probablePitcher") or {}
            if pp.get("id"): pitcher_ids.add(str(pp["id"]))
    pitcher_hand_map = get_people_handedness(pitcher_ids)
    team_ids = set()
    for g in games:
        team_ids.add(g["teams"]["away"]["team"]["id"])
        team_ids.add(g["teams"]["home"]["team"]["id"])
    rosters = {tid: get_roster(tid) for tid in team_ids}
    rostered = sum(len(v) for v in rosters.values())
    print(f"  rosters: {rostered} position players across {len(rosters)} teams", flush=True)
    weather = {}
    for g in games:
        h_abbr = TEAM_ABBR.get(g["teams"]["home"]["team"].get("name", ""), "")
        weather[g["gamePk"]] = game_weather(g, STADIUMS.get(h_abbr))
    rows = []
    for g in games:
        a = g["teams"]["away"]; h = g["teams"]["home"]
        a_tid = a["team"]["id"]; h_tid = h["team"]["id"]
        a_abbr = TEAM_ABBR.get(a["team"]["name"], a["team"]["name"][:3].upper())
        h_abbr = TEAM_ABBR.get(h["team"]["name"], h["team"]["name"][:3].upper())
        a_pp = a.get("probablePitcher") or {}
        h_pp = h.get("probablePitcher") or {}
        ln = g.get("lineups") or {}
        a_lineup_ids = {str(p["id"]) for p in (ln.get("awayPlayers") or [])}
        h_lineup_ids = {str(p["id"]) for p in (ln.get("homePlayers") or [])}
        wx = weather.get(g["gamePk"])
        wxp = weather_points(wx)
        def score_side(roster, opp_pp, my_abbr, opp_abbr, lineup_ids, game_label, game_time):
            if not opp_pp.get("id"): return
            pid = str(opp_pp["id"])
            if pid not in pitchers: return
            for pl in roster:
                bid = pl["id"]
                r = matchup(batters, pitchers, lg_mix, lg_xw, hand, pitcher_hand_map, bid, pid)
                if not r: continue
                s = 1000*(r["adjusted"] - r["baseline"]) + wxp - coverage_penalty(r["impact"])
                in_lineup = None if not lineup_ids else (bid in lineup_ids)
                rows.append({"name": pl["name"], "team": my_abbr, "opp": opp_pp.get("fullName", "?"),
                    "ph": r["ph"], "expWoba": r["adjusted"], "hf": r["hf"], "edge": r["edge"],
                    "cov": r["impact"], "wx": wxp, "score": s,
                    "game": game_label, "game_time": game_time, "in_lineup": in_lineup})
        score_side(rosters.get(a_tid, []), h_pp, a_abbr, h_abbr, a_lineup_ids, f"{a_abbr} @ {h_abbr}", g["gameDate"])
        score_side(rosters.get(h_tid, []), a_pp, h_abbr, a_abbr, h_lineup_ids, f"{a_abbr} @ {h_abbr}", g["gameDate"])
    rows.sort(key=lambda x: -x["score"])
    top = rows[:10]
    stats = {"games": len(games), "candidates": len(rows), "rostered": rostered}
    html = build_email_html(DATE, top, stats)
    text = build_email_text(DATE, top, stats)
    subject = f"MLB Prop Edge — Top 10 for {DATE}"
    print(f"  candidates scored: {len(rows)}; sending top 10...", flush=True)
    resp = send_email(api_key, recipient, subject, html, text)
    print(f"  sent: {resp}", flush=True)

if __name__ == "__main__":
    main()
