"""
StatsServer.py
--------------
NBA stats API for Scriptable iOS prop hunter.
Uses ESPN's public CDN API — free, no key, no cloud IP blocking.

All endpoints:
  /health   — server status
  /stats    — player L5/L10/Szn averages, hit rates, signal
  /gamelog  — raw recent game log
  /debug    — diagnose player lookup issues
  /pace     — team pace data (coming soon)
  /bets     — bet tracker web app
"""

import json, os, re, time
import requests
from flask import Flask, jsonify, request

app = Flask(__name__)

# ── CONFIG ────────────────────────────────────────────────────────────────────
NBA_SEASON    = "2025-26"
ESPN_SEASON   = "2025"          # ESPN uses single year
THREE_PA_WARN = 5.0
PORT          = int(os.environ.get("PORT", 5001))

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba"
ESPN_CORE = "https://sports.core.api.espn.com/v2/sports/basketball/leagues/nba"

ESPN_HEADERS = {
    "User-Agent":      "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15",
    "Accept":          "application/json",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer":         "https://www.espn.com/nba/",
    "Origin":          "https://www.espn.com",
}

# ── MARKET → ESPN STAT CATEGORIES ────────────────────────────────────────────
# ESPN gamelog stats are parallel arrays; these are the abbreviation labels
MKT_ESPN = {
    "player_points":                  ["PTS"],
    "player_rebounds":                ["REB"],
    "player_assists":                 ["AST"],
    "player_threes":                  ["3PM"],
    "player_blocks":                  ["BLK"],
    "player_steals":                  ["STL"],
    "player_points_rebounds_assists": ["PTS","REB","AST"],
    "player_points_rebounds":         ["PTS","REB"],
    "player_points_assists":          ["PTS","AST"],
    "player_rebounds_assists":        ["REB","AST"],
    "player_points_alternate":        ["PTS"],
    "player_rebounds_alternate":      ["REB"],
    "player_assists_alternate":       ["AST"],
    "player_threes_alternate":        ["3PM"],
}
# Full market name for unknown market fallback
MKT_COLS = MKT_ESPN

# ── CACHES ────────────────────────────────────────────────────────────────────
_espn_id_cache  = {}   # norm_name → espn_athlete_id (str) or None
_espn_log_cache = {}   # espn_athlete_id → list[dict] newest-first
_espn_failed    = set()
_pace_cache     = {}

# ── HELPERS ───────────────────────────────────────────────────────────────────
def _norm(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[.'`\-]", " ", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()

def _norm_team(name: str) -> str:
    return re.sub(r"\s+", " ", name.lower().strip())

def _avg(rows, cols):
    vals = []
    for r in rows:
        try:
            vals.append(round(sum(float(r.get(c) or 0) for c in cols), 1))
        except Exception:
            pass
    return round(sum(vals) / len(vals), 1) if vals else None

def _hit_rate(rows, cols, line):
    hits = total = 0
    for r in rows:
        try:
            val = sum(float(r.get(c) or 0) for c in cols)
            total += 1
            if val >= line: hits += 1
        except Exception:
            pass
    return f"{hits}/{total}" if total > 0 else None

def _signal(season_avg, l10_avg, line, market_key, vol_3pa):
    if season_avg is None or l10_avg is None or line is None:
        return "NEUTRAL", None
    if market_key in ("player_threes", "player_threes_alternate"):
        if vol_3pa and vol_3pa >= THREE_PA_WARN:
            return "FADE_UNDER", f"HIGH VOLUME ({vol_3pa} 3PA/g L10) — fade unders"
    s_gap = season_avg - line
    l_gap = l10_avg - line
    if s_gap >= 1.5 and l_gap >= 1.5:   return "HAMMER_OVER",  None
    if s_gap <= -1.5 and l_gap <= -1.5: return "HAMMER_UNDER", None
    if s_gap >= 0.5  and l_gap >= 0.5:  return "LEAN_OVER",    None
    if s_gap <= -0.5 and l_gap <= -0.5: return "LEAN_UNDER",   None
    return "NEUTRAL", None

# ── ESPN PLAYER SEARCH ────────────────────────────────────────────────────────
def _find_espn_id(name: str):
    """Find ESPN athlete ID by name search. Cached per session."""
    n = _norm(name)
    if n in _espn_id_cache:
        return _espn_id_cache[n]
    try:
        r = requests.get(
            f"{ESPN_BASE}/athletes",
            params={"limit": 15, "search": name},
            headers=ESPN_HEADERS,
            timeout=10,
        )
        r.raise_for_status()
        data     = r.json()
        athletes = data.get("athletes", data.get("items", []))

        if not athletes:
            print(f"[WARN] ESPN: no results for '{name}'", flush=True)
            _espn_id_cache[n] = None
            return None

        # Try exact normalized match first
        best_id = None
        for a in athletes:
            full = _norm(a.get("fullName") or a.get("displayName") or "")
            if full == n:
                best_id = str(a.get("id") or a.get("uid","").split(":")[-1])
                break

        # Fall back to first result
        if not best_id:
            a       = athletes[0]
            best_id = str(a.get("id") or a.get("uid","").split(":")[-1])
            found   = a.get("fullName") or a.get("displayName")
            print(f"[INFO] ESPN: '{name}' → '{found}' (id={best_id})", flush=True)

        _espn_id_cache[n] = best_id
        return best_id

    except Exception as e:
        print(f"[WARN] ESPN player search failed for '{name}': {e}", flush=True)
        _espn_id_cache[n] = None
        return None

# ── ESPN GAME LOG FETCH ───────────────────────────────────────────────────────
def _fetch_espn_log(espn_id: str):
    """
    Fetch season game log from ESPN athlete gamelog endpoint.
    ESPN returns parallel arrays: categories (stat labels) + events (values).
    We parse into list of dicts keyed by stat abbreviation.
    """
    if espn_id in _espn_log_cache:
        return
    try:
        r = requests.get(
            f"{ESPN_BASE}/athletes/{espn_id}/gamelog",
            params={"season": ESPN_SEASON},
            headers=ESPN_HEADERS,
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()

        # ESPN gamelog format:
        # categories: [{name, abbreviation}, ...]
        # events: {event_id: {stats: [v1, v2, ...], eventId, ...}}
        # seasonTypes: [{categories, events}]  — same structure
        categories = []
        events_raw = {}

        # Try top-level structure first
        if "categories" in data and "events" in data:
            categories = data["categories"]
            events_raw = data["events"]
        # Else look inside seasonTypes[0] (regular season)
        elif "seasonTypes" in data:
            for st in data.get("seasonTypes", []):
                if st.get("type") in (2, "2", "regular"):
                    categories = st.get("categories", [])
                    events_raw = st.get("events", {})
                    break
            if not categories and data.get("seasonTypes"):
                st = data["seasonTypes"][0]
                categories = st.get("categories", [])
                events_raw = st.get("events", {})

        if not categories or not events_raw:
            print(f"[WARN] ESPN: empty gamelog for {espn_id}", flush=True)
            _espn_failed.add(espn_id)
            return

        # Build abbreviation list from categories
        abbrevs = [c.get("abbreviation", c.get("name","")).upper() for c in categories]

        # Parse each event into a flat dict
        games = []
        for event_id, ev in events_raw.items():
            stats_vals = ev.get("stats", [])
            if not stats_vals:
                continue
            row = {"event_id": event_id}
            # Match values to abbreviations
            for i, val in enumerate(stats_vals):
                if i < len(abbrevs):
                    row[abbrevs[i]] = val
            # Also store date if available
            row["date"] = ev.get("eventDate", ev.get("date", ""))
            games.append(row)

        if not games:
            _espn_failed.add(espn_id)
            print(f"[WARN] ESPN: parsed 0 games for {espn_id}", flush=True)
            return

        # Sort newest first by date
        games.sort(key=lambda g: g.get("date", ""), reverse=True)
        _espn_log_cache[espn_id] = games
        print(f"[OK] ESPN: loaded {len(games)} games for athlete {espn_id}", flush=True)

    except Exception as e:
        print(f"[WARN] ESPN game log failed for {espn_id}: {e}", flush=True)
        _espn_failed.add(espn_id)

# ── ROUTES ────────────────────────────────────────────────────────────────────
@app.route("/health")
def health():
    return jsonify({
        "status":         "ok",
        "season":         NBA_SEASON,
        "players_cached": len(_espn_id_cache),
        "logs_cached":    len(_espn_log_cache),
        "source":         "espn",
    })

@app.route("/stats")
def stats():
    player_name = request.args.get("player", "").strip()
    market_key  = request.args.get("market", "").strip()
    line_str    = request.args.get("line",   "0").strip()

    if not player_name or not market_key:
        return jsonify({"error": "player and market are required"}), 400
    try:
        line = float(line_str)
    except ValueError:
        return jsonify({"error": "line must be a number"}), 400

    cols = MKT_COLS.get(market_key)
    if not cols:
        return jsonify({"error": f"unknown market: {market_key}"}), 400

    espn_id = _find_espn_id(player_name)
    if espn_id is None:
        return jsonify({"found": False, "player": player_name, "reason": "id_not_found"}), 200

    _fetch_espn_log(espn_id)
    rows = _espn_log_cache.get(espn_id)
    if not rows:
        reason = "fetch_failed" if espn_id in _espn_failed else "no_games"
        return jsonify({"found": False, "player": player_name, "reason": reason}), 200

    l10 = rows[:10]
    l5  = rows[:5]

    season_avg = _avg(rows, cols)
    l10_avg    = _avg(l10,  cols)
    l5_avg     = _avg(l5,   cols)
    hr_season  = _hit_rate(rows, cols, line)
    hr_l10     = _hit_rate(l10,  cols, line)

    # 3PA volume for 3PM props
    vol_3pa = None
    if market_key in ("player_threes", "player_threes_alternate"):
        atts = [float(r.get("3PA") or 0) for r in l10 if r.get("3PA") is not None]
        vol_3pa = round(sum(atts) / len(atts), 1) if atts else None

    # FGA per game L10
    fga_vals = [float(r.get("FGA") or 0) for r in l10 if r.get("FGA") is not None]
    fga_l10  = round(sum(fga_vals) / len(fga_vals), 1) if fga_vals else None

    # Minutes per game L10
    min_vals = []
    for r in l10:
        m = r.get("MIN") or r.get("MINS") or ""
        try:
            if isinstance(m, str) and ":" in m:
                p = m.split(":")
                min_vals.append(float(p[0]) + float(p[1]) / 60)
            elif m:
                min_vals.append(float(m))
        except Exception:
            pass
    min_l10 = round(sum(min_vals) / len(min_vals), 1) if min_vals else None

    # Sparkline
    last5_raw = []
    for r in l5:
        try:
            last5_raw.append(round(sum(float(r.get(c) or 0) for c in cols), 1))
        except Exception:
            pass

    signal, warn = _signal(season_avg, l10_avg, line, market_key, vol_3pa)

    return jsonify({
        "found":      True,
        "player":     player_name,
        "market":     market_key,
        "line":       line,
        "season_avg": season_avg,
        "l10_avg":    l10_avg,
        "l5_avg":     l5_avg,
        "hr_season":  hr_season,
        "hr_l10":     hr_l10,
        "vol_3pa":    vol_3pa,
        "fga_l10":    fga_l10,
        "min_l10":    min_l10,
        "usg_pct":    None,
        "signal":     signal,
        "warn":       warn,
        "last5_raw":  last5_raw,
        "games":      len(rows),
        "source":     "espn",
    })

@app.route("/gamelog")
def gamelog():
    player_name = request.args.get("player", "").strip()
    n_str       = request.args.get("n", "10")
    if not player_name:
        return jsonify({"error": "player is required"}), 400
    try:
        n = int(n_str)
    except ValueError:
        n = 10

    espn_id = _find_espn_id(player_name)
    if espn_id is None:
        return jsonify({"found": False, "player": player_name}), 200

    _fetch_espn_log(espn_id)
    rows = _espn_log_cache.get(espn_id)
    if not rows:
        return jsonify({"found": False, "player": player_name}), 200

    return jsonify({"found": True, "player": player_name, "games": rows[:n]})

@app.route("/debug")
def debug():
    player_name = request.args.get("player", "").strip()
    if not player_name:
        return jsonify({"error": "provide player name"}), 400

    n       = _norm(player_name)
    espn_id = _find_espn_id(player_name)

    result = {
        "input":        player_name,
        "normalized":   n,
        "espn_id":      espn_id,
        "log_cached":   espn_id in _espn_log_cache if espn_id else False,
        "fetch_failed": espn_id in _espn_failed    if espn_id else False,
        "game_count":   len(_espn_log_cache.get(espn_id, [])) if espn_id else 0,
        "source":       "espn",
    }

    if espn_id and espn_id in _espn_log_cache:
        sample = _espn_log_cache[espn_id][:2]
        result["sample_game_keys"] = list(sample[0].keys()) if sample else []
        result["sample_games"]     = sample
    return jsonify(result)

@app.route("/pace")
def pace():
    home = request.args.get("home", "").strip()
    away = request.args.get("away", "").strip()
    if not home and not away:
        return jsonify({"error": "provide home and/or away team name"}), 400
    def find_pace(t):
        nk = _norm_team(t)
        if nk in _pace_cache: return _pace_cache[nk]
        last = nk.split()[-1] if nk else ""
        for key, p in _pace_cache.items():
            if last and last in key: return p
        return None
    home_pace = find_pace(home) if home else None
    away_pace = find_pace(away) if away else None
    avg_pace  = round((home_pace + away_pace) / 2, 1) if home_pace and away_pace else (home_pace or away_pace)
    signal    = ("FAST" if avg_pace and avg_pace >= 103
                 else "SLOW" if avg_pace and avg_pace < 100
                 else "AVERAGE") if avg_pace else None
    return jsonify({"home": home, "away": away,
                    "home_pace": home_pace, "away_pace": away_pace,
                    "avg_pace": avg_pace, "pace_signal": signal})



@app.route("/bets")
def bets_app():
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Sharp Picks</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#050505;--bg2:#0a0a0a;--bg3:#111;--bg4:#181818;
  --border:#1a1a1a;--border2:#252525;
  --text:#f5f0e0;--muted:#555;--dim:#222;
  --green:#00c853;--red:#e53935;--gold:#ffd700;--orange:#ff8f00;
  --fd:#1493ff;--dk:#00c853;--mgm:#e53935;
}
html,body{min-height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'SF Pro Display',sans-serif;-webkit-text-size-adjust:none;overflow-x:hidden}

/* HEADER */
.header{background:linear-gradient(180deg,#0a0500 0%,var(--bg) 100%);border-bottom:2px solid var(--gold);padding:16px;position:sticky;top:0;z-index:50}
.header-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:14px}
.logo{font-size:11px;font-weight:900;letter-spacing:4px;color:var(--gold);text-transform:uppercase}
.pnl{font-size:22px;font-weight:900;letter-spacing:-1px}
.tabs{display:flex;border:1px solid var(--border2);border-radius:8px;overflow:hidden;background:var(--bg2)}
.tab{flex:1;padding:9px 4px;text-align:center;font-size:12px;font-weight:700;letter-spacing:1px;color:var(--muted);cursor:pointer;text-transform:uppercase;transition:all .15s;position:relative}
.tab.active{background:var(--gold);color:#000}
.tab .badge{position:absolute;top:4px;right:8px;background:var(--red);color:#fff;border-radius:8px;font-size:9px;font-weight:900;padding:1px 5px;min-width:16px;text-align:center}
.panel{display:none;padding:12px}
.panel.active{display:block}

/* PICK CARD — the main visual element */
.pick-card{
  background:linear-gradient(145deg,#0f0800 0%,#080808 40%,#000d04 100%);
  border:1px solid #2a2200;
  border-radius:14px;
  margin-bottom:12px;
  overflow:hidden;
  position:relative;
}
.pick-card::before{
  content:'';position:absolute;top:0;left:0;right:0;height:3px;
  background:linear-gradient(90deg,var(--red),var(--gold),var(--green));
}
.pick-card.result-hit{border-color:rgba(0,200,83,0.3);background:linear-gradient(145deg,#001a07,#050505,#001a07)}
.pick-card.result-hit::before{background:var(--green)}
.pick-card.result-miss{border-color:rgba(229,57,53,0.25);background:linear-gradient(145deg,#1a0000,#050505,#1a0000)}
.pick-card.result-miss::before{background:var(--red)}

.card-top{padding:14px 14px 10px;display:flex;justify-content:space-between;align-items:flex-start}
.player-name{font-size:18px;font-weight:900;letter-spacing:-0.5px;line-height:1}
.player-sub{font-size:11px;color:var(--muted);letter-spacing:1px;margin-top:3px;text-transform:uppercase}
.card-badge{display:flex;flex-direction:column;align-items:flex-end;gap:4px}
.book-pill{font-size:10px;font-weight:900;letter-spacing:2px;padding:3px 10px;border-radius:20px}
.pill-fd{background:rgba(20,147,255,0.15);color:var(--fd);border:1px solid rgba(20,147,255,0.3)}
.pill-dk{background:rgba(0,200,83,0.12);color:var(--dk);border:1px solid rgba(0,200,83,0.25)}
.pill-mgm{background:rgba(229,57,53,0.12);color:var(--mgm);border:1px solid rgba(229,57,53,0.25)}

/* BIG ODDS DISPLAY */
.odds-strip{
  margin:0 14px 10px;
  background:rgba(0,0,0,0.4);
  border:1px solid #1a1a00;
  border-radius:10px;
  padding:12px;
  display:grid;
  grid-template-columns:1fr 1px 1fr 1px 1fr;
  gap:0;
  align-items:center;
}
.odds-divider{background:var(--border2);height:36px}
.odds-block{text-align:center;padding:0 8px}
.odds-val{font-size:26px;font-weight:900;letter-spacing:-1px;line-height:1}
.odds-lbl{font-size:8px;letter-spacing:2px;color:var(--muted);margin-top:3px;text-transform:uppercase}

/* PROP LINE — big bold display */
.prop-line{
  margin:0 14px 10px;
  text-align:center;
  padding:10px;
  background:linear-gradient(135deg,rgba(255,215,0,0.06),rgba(255,215,0,0.02));
  border:1px solid rgba(255,215,0,0.15);
  border-radius:10px;
}
.prop-market{font-size:13px;font-weight:700;color:var(--muted);letter-spacing:3px;text-transform:uppercase;margin-bottom:4px}
.prop-number{font-size:42px;font-weight:900;color:var(--gold);letter-spacing:-2px;line-height:1}
.prop-side{font-size:14px;font-weight:700;letter-spacing:3px;color:var(--text);margin-top:2px;text-transform:uppercase}

/* EDGE BAR */
.edge-bar{margin:0 14px 10px;display:flex;align-items:center;gap:10px}
.edge-label{font-size:10px;font-weight:700;letter-spacing:2px;color:var(--muted);text-transform:uppercase;white-space:nowrap}
.edge-track{flex:1;height:6px;background:var(--bg4);border-radius:3px;overflow:hidden}
.edge-fill{height:100%;border-radius:3px;transition:width .4s}
.edge-val{font-size:13px;font-weight:900;white-space:nowrap}

/* SIGNAL BADGE */
.signal-wrap{margin:0 14px 10px;display:flex;justify-content:center}
.signal-badge{font-size:11px;font-weight:900;letter-spacing:2px;padding:6px 16px;border-radius:20px;text-transform:uppercase}
.sig-hammer-over{background:rgba(0,200,83,0.15);color:var(--green);border:1px solid rgba(0,200,83,0.3)}
.sig-lean-over{background:rgba(255,143,0,0.12);color:var(--orange);border:1px solid rgba(255,143,0,0.25)}
.sig-neutral{background:rgba(255,255,255,0.04);color:var(--muted);border:1px solid var(--border)}
.sig-fade{background:rgba(229,57,53,0.12);color:var(--red);border:1px solid rgba(229,57,53,0.25)}

/* STATS ROW */
.stats-row{margin:0 14px 10px;display:grid;grid-template-columns:repeat(4,1fr);gap:6px}
.stat-chip{background:var(--bg2);border:1px solid var(--border);border-radius:8px;padding:7px 4px;text-align:center}
.stat-chip-val{font-size:15px;font-weight:800;line-height:1}
.stat-chip-lbl{font-size:8px;color:var(--muted);letter-spacing:1.5px;margin-top:2px;text-transform:uppercase}

/* ACTIONS */
.card-actions{padding:10px 14px 14px;display:flex;gap:8px}
.btn-hit{flex:1;padding:11px;border:none;border-radius:8px;font-size:13px;font-weight:900;letter-spacing:1px;cursor:pointer;background:rgba(0,200,83,0.15);color:var(--green);border:1px solid rgba(0,200,83,0.3);text-transform:uppercase}
.btn-miss{flex:1;padding:11px;border:none;border-radius:8px;font-size:13px;font-weight:900;letter-spacing:1px;cursor:pointer;background:rgba(229,57,53,0.1);color:var(--red);border:1px solid rgba(229,57,53,0.2);text-transform:uppercase}
.btn-del{padding:11px 14px;border:none;border-radius:8px;font-size:12px;font-weight:700;cursor:pointer;background:var(--bg3);color:var(--muted);border:1px solid var(--border)}
.result-stamp{padding:8px 14px 12px;text-align:center}
.stamp{font-size:22px;font-weight:900;letter-spacing:4px}
.stamp-pnl{font-size:13px;font-weight:700;margin-top:2px;letter-spacing:1px}

/* LOG FORM */
.form-section{margin-bottom:14px}
.form-label{font-size:9px;font-weight:900;letter-spacing:3px;color:var(--muted);margin-bottom:6px;display:block;text-transform:uppercase}
.form-input{width:100%;background:var(--bg3);border:1px solid var(--border2);color:var(--text);font-size:16px;padding:12px 14px;border-radius:8px;-webkit-appearance:none;outline:none;font-family:inherit}
.form-input:focus{border-color:var(--gold)}
.form-input::placeholder{color:var(--dim)}
select.form-input{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='6'%3E%3Cpath d='M0 0l5 6 5-6z' fill='%23555'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-hint{font-size:10px;color:var(--muted);margin-top:4px;letter-spacing:.5px}
.btn-log{width:100%;padding:15px;border:none;border-radius:10px;font-size:15px;font-weight:900;letter-spacing:2px;cursor:pointer;background:linear-gradient(135deg,var(--gold),var(--orange));color:#000;text-transform:uppercase;margin-top:4px}

/* RESULTS */
.results-header{margin-bottom:16px}
.pnl-hero{text-align:center;padding:20px;background:linear-gradient(145deg,#0a0800,#050505);border:1px solid rgba(255,215,0,0.15);border-radius:14px;margin-bottom:12px}
.pnl-hero-val{font-size:48px;font-weight:900;letter-spacing:-2px;line-height:1}
.pnl-hero-lbl{font-size:10px;letter-spacing:3px;color:var(--muted);margin-top:4px;text-transform:uppercase}
.pnl-hero-sub{font-size:13px;color:var(--muted);margin-top:8px}
.metrics-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:16px}
.metric-card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.metric-val{font-size:28px;font-weight:900;letter-spacing:-1px}
.metric-lbl{font-size:9px;letter-spacing:2px;color:var(--muted);margin-top:3px;text-transform:uppercase}
.breakdown-card{background:var(--bg2);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:10px}
.breakdown-title{font-size:9px;font-weight:900;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-bottom:12px}
.br-row{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.br-row:last-child{margin-bottom:0}
.br-label{font-size:12px;font-weight:700;min-width:90px}
.br-track{flex:1;height:8px;background:var(--bg4);border-radius:4px;overflow:hidden}
.br-fill{height:100%;border-radius:4px;background:linear-gradient(90deg,var(--gold),var(--green))}
.br-pct{font-size:13px;font-weight:800;min-width:36px;text-align:right;color:var(--gold)}
.br-count{font-size:10px;color:var(--muted);min-width:36px;text-align:right}

/* EMPTY */
.empty{text-align:center;padding:56px 20px;color:var(--muted)}
.empty-icon{font-size:48px;margin-bottom:16px;opacity:.3}
.empty-text{font-size:12px;letter-spacing:3px;text-transform:uppercase}

/* TOAST */
.toast{position:fixed;bottom:28px;left:50%;transform:translateX(-50%) translateY(80px);background:var(--gold);color:#000;padding:10px 24px;border-radius:20px;font-size:13px;font-weight:900;letter-spacing:1px;transition:transform .25s;pointer-events:none;z-index:100;text-transform:uppercase}
.toast.show{transform:translateX(-50%) translateY(0)}
</style>
</head>
<body>

<div class="header">
  <div class="header-top">
    <div class="logo">⚡ Sharp Picks</div>
    <div class="pnl" id="headerPnl">+$0.00</div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('log')">Log</div>
    <div class="tab" onclick="showTab('pending')">Pending<span class="badge" id="pendingBadge">0</span></div>
    <div class="tab" onclick="showTab('results')">Results</div>
  </div>
</div>

<div class="toast" id="toast"></div>

<!-- LOG -->
<div class="panel active" id="panel-log">
  <div class="form-section">
    <label class="form-label">Player</label>
    <input class="form-input" type="text" id="f-player" placeholder="e.g. Jalen Johnson" autocapitalize="words" autocomplete="off">
  </div>
  <div class="form-row">
    <div class="form-section">
      <label class="form-label">Market</label>
      <select class="form-input" id="f-market">
        <option>PTS</option><option>REB</option><option>AST</option>
        <option>3PM</option><option>PRA</option><option>PR</option>
        <option>PA</option><option>RA</option><option>BLK</option><option>STL</option>
      </select>
    </div>
    <div class="form-section">
      <label class="form-label">Line</label>
      <input class="form-input" type="number" id="f-line" placeholder="24.5" step="0.5" inputmode="decimal">
    </div>
  </div>
  <div class="form-row">
    <div class="form-section">
      <label class="form-label">Side</label>
      <select class="form-input" id="f-side"><option>Over</option><option>Under</option></select>
    </div>
    <div class="form-section">
      <label class="form-label">Book</label>
      <select class="form-input" id="f-book">
        <option value="FD">FanDuel</option>
        <option value="DK">DraftKings</option>
        <option value="MGM">BetMGM</option>
      </select>
    </div>
  </div>
  <div class="form-row">
    <div class="form-section">
      <label class="form-label">Your Odds</label>
      <input class="form-input" type="number" id="f-odds" placeholder="-135" inputmode="numeric">
      <div class="form-hint">American format</div>
    </div>
    <div class="form-section">
      <label class="form-label">Novig Fair</label>
      <input class="form-input" type="number" id="f-fair" placeholder="-150" inputmode="numeric">
      <div class="form-hint">From the script</div>
    </div>
  </div>
  <div class="form-row">
    <div class="form-section">
      <label class="form-label">Stake ($)</label>
      <input class="form-input" type="number" id="f-stake" placeholder="25" inputmode="decimal">
    </div>
    <div class="form-section">
      <label class="form-label">Signal</label>
      <select class="form-input" id="f-signal">
        <option value="HAMMER_OVER">▲ HAMMER OVER</option>
        <option value="LEAN_OVER">△ lean over</option>
        <option value="HAMMER_UNDER">▼ HAMMER UNDER</option>
        <option value="LEAN_UNDER">▽ lean under</option>
        <option value="NEUTRAL">~ neutral</option>
        <option value="FADE_UNDER">✗ FADE</option>
      </select>
    </div>
  </div>
  <div class="form-section">
    <label class="form-label">Stars (sharp consensus)</label>
    <select class="form-input" id="f-stars">
      <option value="1">★ Novig only</option>
      <option value="2">★★ 2 sharps agree</option>
      <option value="3">★★★ 3 sharps agree</option>
      <option value="4">★★★★ 4+ sharps agree</option>
    </select>
  </div>
  <div class="form-section">
    <label class="form-label">Notes (optional)</label>
    <input class="form-input" type="text" id="f-notes" placeholder="e.g. parlay leg, L5 hot" autocorrect="off">
  </div>
  <button class="btn-log" onclick="logBet()">LOG PICK</button>
</div>

<!-- PENDING -->
<div class="panel" id="panel-pending">
  <div id="pendingList"></div>
</div>

<!-- RESULTS -->
<div class="panel" id="panel-results">
  <div id="resultsSummary"></div>
</div>

<script>
const KEY = "sharppicks_v1";
function load(){try{return JSON.parse(localStorage.getItem(KEY)||"[]")}catch(e){return[]}}
function save(b){localStorage.setItem(KEY,JSON.stringify(b))}
function uid(){return Date.now().toString(36)+Math.random().toString(36).slice(2,5)}
function toast(m){const e=document.getElementById("toast");e.textContent=m;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),2000)}

function amImpl(o){const n=parseFloat(o);if(isNaN(n))return null;return n>0?100/(n+100):Math.abs(n)/(Math.abs(n)+100)}
function profit(o,s){const n=parseFloat(o);if(isNaN(n)||isNaN(s))return 0;return n>0?(n/100)*s:(100/Math.abs(n))*s}
function fmtOdds(n){return n>0?"+"+n:""+n}
function fmtMoney(n){return(n>=0?"+$":"-$")+Math.abs(n).toFixed(2)}
function fmtDate(ts){return new Date(ts).toLocaleDateString("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}

function edgeColor(e){if(e>=15)return"var(--green)";if(e>=8)return"var(--orange)";return"var(--muted)"}
function edgePct(e){return Math.min(100,Math.max(0,(e/30)*100))}

function signalClass(s){
  if(s==="HAMMER_OVER"||s==="HAMMER_UNDER")return"sig-hammer-over";
  if(s==="LEAN_OVER"||s==="LEAN_UNDER")return"sig-lean-over";
  if(s==="FADE_UNDER")return"sig-fade";
  return"sig-neutral";
}
function signalLabel(s){
  return{HAMMER_OVER:"▲ HAMMER OVER",LEAN_OVER:"△ lean over",HAMMER_UNDER:"▼ HAMMER UNDER",
         LEAN_UNDER:"▽ lean under",NEUTRAL:"~ neutral",FADE_UNDER:"✗ FADE"}[s]||s||"—";
}
function starStr(n){return"★".repeat(n)+"☆".repeat(Math.max(0,4-n))}
function bookClass(b){return{FD:"pill-fd",DK:"pill-dk",MGM:"pill-mgm"}[b]||""}

function showTab(n){
  document.querySelectorAll(".tab").forEach((t,i)=>t.classList.toggle("active",["log","pending","results"][i]===n));
  document.querySelectorAll(".panel").forEach(p=>p.classList.remove("active"));
  document.getElementById("panel-"+n).classList.add("active");
  if(n==="pending")renderPending();
  if(n==="results")renderResults();
}

function logBet(){
  const player=document.getElementById("f-player").value.trim();
  const market=document.getElementById("f-market").value;
  const line=parseFloat(document.getElementById("f-line").value);
  const side=document.getElementById("f-side").value;
  const book=document.getElementById("f-book").value;
  const odds=parseInt(document.getElementById("f-odds").value);
  const fair=parseInt(document.getElementById("f-fair").value);
  const stake=parseFloat(document.getElementById("f-stake").value);
  const signal=document.getElementById("f-signal").value;
  const stars=parseInt(document.getElementById("f-stars").value);
  const notes=document.getElementById("f-notes").value.trim();
  if(!player){toast("Enter player name");return}
  if(isNaN(line)){toast("Enter the line");return}
  if(isNaN(odds)){toast("Enter your odds");return}
  if(isNaN(fair)){toast("Enter Novig fair");return}
  if(isNaN(stake)||stake<=0){toast("Enter stake");return}
  const bets=load();
  bets.unshift({id:uid(),ts:Date.now(),player,market,line,side,book,odds,fair,edge:odds-fair,stake,signal,stars,notes,result:null,profit:null});
  save(bets);
  ["f-player","f-line","f-odds","f-fair","f-stake","f-notes"].forEach(id=>document.getElementById(id).value="");
  updateHeader();
  toast("Pick logged");
}

function settleBet(id,result){
  const bets=load();const b=bets.find(x=>x.id===id);if(!b)return;
  b.result=result;b.profit=result==="hit"?profit(b.odds,b.stake):-b.stake;
  save(bets);updateHeader();renderPending();
  toast(result==="hit"?"HIT! 🔥":"Miss");
}

function deleteBet(id){
  if(!confirm("Delete this pick?"))return;
  save(load().filter(b=>b.id!==id));
  updateHeader();renderPending();renderResults();
}

function pickCard(b, showActions){
  const ec=edgeColor(b.edge);
  const ep=edgePct(b.edge);
  const toWin=profit(b.odds,b.stake).toFixed(2);
  const impl=amImpl(b.fair);
  const implPct=impl?(impl*100).toFixed(1)+"%" : "--";

  const resultClass=b.result==="hit"?"result-hit":b.result==="miss"?"result-miss":"";

  let actionsHTML="";
  if(showActions){
    actionsHTML=`<div class="card-actions">
      <button class="btn-hit" onclick="settleBet('${b.id}','hit')">✓ HIT</button>
      <button class="btn-miss" onclick="settleBet('${b.id}','miss')">✗ MISS</button>
      <button class="btn-del" onclick="deleteBet('${b.id}')">Del</button>
    </div>`;
  } else if(b.result){
    const pnlColor=b.profit>=0?"var(--green)":"var(--red)";
    const stamp=b.result==="hit"?"HIT 🔥":"MISS";
    actionsHTML=`<div class="result-stamp">
      <div class="stamp" style="color:${pnlColor}">${stamp}</div>
      <div class="stamp-pnl" style="color:${pnlColor}">${fmtMoney(b.profit)}</div>
    </div>`;
  }

  return `<div class="pick-card ${resultClass}">
    <div class="card-top">
      <div>
        <div class="player-name">${b.player}</div>
        <div class="player-sub">${fmtDate(b.ts)} · ${starStr(b.stars||1)}</div>
      </div>
      <div class="card-badge">
        <div class="book-pill ${bookClass(b.book)}">${b.book}</div>
      </div>
    </div>

    <div class="prop-line">
      <div class="prop-market">${b.market}</div>
      <div class="prop-number">${b.line}</div>
      <div class="prop-side">${b.side}</div>
    </div>

    <div class="odds-strip">
      <div class="odds-block">
        <div class="odds-val" style="color:${b.odds>=-120?'var(--green)':'var(--text)'}">${fmtOdds(b.odds)}</div>
        <div class="odds-lbl">Your Odds</div>
      </div>
      <div class="odds-divider"></div>
      <div class="odds-block">
        <div class="odds-val" style="color:var(--muted)">${fmtOdds(b.fair)}</div>
        <div class="odds-lbl">NOV Fair</div>
      </div>
      <div class="odds-divider"></div>
      <div class="odds-block">
        <div class="odds-val" style="color:var(--gold)">$${toWin}</div>
        <div class="odds-lbl">To Win</div>
      </div>
    </div>

    <div class="edge-bar">
      <div class="edge-label">Edge</div>
      <div class="edge-track"><div class="edge-fill" style="width:${ep}%;background:${ec}"></div></div>
      <div class="edge-val" style="color:${ec}">+${b.edge}pts</div>
    </div>

    <div class="signal-wrap">
      <div class="signal-badge ${signalClass(b.signal)}">${signalLabel(b.signal)}</div>
    </div>

    ${b.notes?`<div style="padding:0 14px 10px;font-size:11px;color:var(--muted);font-style:italic">${b.notes}</div>`:""}
    ${actionsHTML}
  </div>`;
}

function renderPending(){
  const bets=load().filter(b=>b.result===null);
  document.getElementById("pendingBadge").textContent=bets.length;
  const el=document.getElementById("pendingList");
  if(!bets.length){
    el.innerHTML='<div class="empty"><div class="empty-icon">📋</div><div class="empty-text">No pending picks</div></div>';
    return;
  }
  el.innerHTML=bets.map(b=>pickCard(b,true)).join("");
}

function renderResults(){
  const all=load();const settled=all.filter(b=>b.result!==null);
  const el=document.getElementById("resultsSummary");
  if(!settled.length){
    el.innerHTML='<div class="empty"><div class="empty-icon">📊</div><div class="empty-text">No settled picks yet</div></div>';
    return;
  }
  const hits=settled.filter(b=>b.result==="hit");
  const pnl=settled.reduce((s,b)=>s+b.profit,0);
  const staked=settled.reduce((s,b)=>s+b.stake,0);
  const hitRate=(hits.length/settled.length*100).toFixed(0);
  const roi=(pnl/staked*100).toFixed(1);
  const pnlColor=pnl>=0?"var(--green)":"var(--red)";
  const roiColor=parseFloat(roi)>=0?"var(--green)":"var(--red)";

  const signals=["HAMMER_OVER","LEAN_OVER","HAMMER_UNDER","LEAN_UNDER","NEUTRAL"];
  const sigRows=signals.map(sig=>{
    const g=settled.filter(b=>b.signal===sig);if(!g.length)return null;
    const w=g.filter(b=>b.result==="hit").length;
    return{label:signalLabel(sig),won:w,total:g.length,pct:Math.round(w/g.length*100)};
  }).filter(Boolean);

  const markets=[...new Set(settled.map(b=>b.market))];
  const mktRows=markets.map(m=>{
    const g=settled.filter(b=>b.market===m);
    const w=g.filter(b=>b.result==="hit").length;
    return{label:m,won:w,total:g.length,pct:Math.round(w/g.length*100)};
  }).sort((a,b)=>b.total-a.total);

  const brRows=(rows)=>rows.map(r=>`
    <div class="br-row">
      <div class="br-label">${r.label}</div>
      <div class="br-track"><div class="br-fill" style="width:${r.pct}%"></div></div>
      <div class="br-pct">${r.pct}%</div>
      <div class="br-count">${r.won}/${r.total}</div>
    </div>`).join("");

  el.innerHTML=`
  <div class="pnl-hero">
    <div class="pnl-hero-val" style="color:${pnlColor}">${fmtMoney(pnl)}</div>
    <div class="pnl-hero-lbl">Total P&L</div>
    <div class="pnl-hero-sub">${settled.length} picks · $${staked.toFixed(0)} staked · ROI <span style="color:${roiColor}">${roi}%</span></div>
  </div>

  <div class="metrics-grid">
    <div class="metric-card">
      <div class="metric-val" style="color:var(--gold)">${hitRate}%</div>
      <div class="metric-lbl">Hit Rate</div>
    </div>
    <div class="metric-card">
      <div class="metric-val" style="color:${roiColor}">${roi}%</div>
      <div class="metric-lbl">ROI</div>
    </div>
  </div>

  <div class="breakdown-card">
    <div class="breakdown-title">By Signal</div>
    ${brRows(sigRows)}
  </div>

  <div class="breakdown-card">
    <div class="breakdown-title">By Market</div>
    ${brRows(mktRows)}
  </div>

  <div style="margin-top:16px;font-size:9px;font-weight:900;letter-spacing:3px;color:var(--muted);text-transform:uppercase;margin-bottom:10px">Recent Picks</div>
  ${settled.slice(0,10).map(b=>pickCard(b,false)).join("")}`;
}

function updateHeader(){
  const settled=load().filter(b=>b.result!==null);
  const pnl=settled.reduce((s,b)=>s+(b.profit||0),0);
  const el=document.getElementById("headerPnl");
  el.textContent=fmtMoney(pnl);
  el.style.color=pnl>=0?"var(--green)":"var(--red)";
  document.getElementById("pendingBadge").textContent=load().filter(b=>b.result===null).length;
}

updateHeader();
</script>
</body>
</html>"""
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}



if __name__ == "__main__":
    print(f"\n{'='*55}")
    print(f"  NBA Stats Server — Season {NBA_SEASON}")
    print(f"  Running on http://0.0.0.0:{PORT}")
    print(f"{'='*55}")
    print(f"\n  Find your local IP:")
    print(f"  System Settings > Wi-Fi > Details > IP Address")
    print(f"\n  Then in Scriptable, set STATS_SERVER_URL to:")
    print(f"  http://<YOUR-IP>:{PORT}")
    print(f"\n  Health check: http://localhost:{PORT}/health")
    print(f"  Example:      http://localhost:{PORT}/stats?player=Jalen+Johnson&market=player_threes&line=1.5\n")
    print(f"{'='*55}\n")

    _build_id_map()

    app.run(host="0.0.0.0", port=PORT, debug=False)
