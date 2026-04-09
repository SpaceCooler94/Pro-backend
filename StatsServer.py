"""
StatsServer.py
--------------
Lightweight local stats API for Scriptable iOS.
Wraps nba_api so Scriptable doesn't have to hit stats.nba.com directly.

Usage:
    pip install nba_api flask
    python StatsServer.py

Then in Scriptable, fetch:
    http://<YOUR-MAC-LOCAL-IP>:5001/stats?player=Jalen+Johnson&market=player_threes&line=1.5
    http://<YOUR-MAC-LOCAL-IP>:5001/gamelog?player=Jalen+Johnson

Find your local IP: System Settings > Wi-Fi > Details > IP Address
Your Mac and iPhone must be on the same Wi-Fi network.
"""

import json
import os
import re
from flask import Flask, jsonify, request

try:
    from nba_api.stats.static import players as _nba_players
    from nba_api.stats.endpoints import playergamelog as _gamelog
    NBA_API_OK = True
except ImportError:
    NBA_API_OK = False
    print("[ERROR] nba_api not installed. Run: pip install nba_api flask")

# ── CONFIG ──────────────────────────────────────────────────────────────────
NBA_SEASON    = "2025-26"
THREE_PA_WARN = 5.0   # 3PM unders with 3PA/g >= this get flagged
PORT          = int(os.environ.get("PORT", 5001))

# ── MARKET → GAMELOG COLUMNS ────────────────────────────────────────────────
MKT_COLS = {
    "player_points":                  ["PTS"],
    "player_rebounds":                ["REB"],
    "player_assists":                 ["AST"],
    "player_threes":                  ["FG3M"],
    "player_blocks":                  ["BLK"],
    "player_steals":                  ["STL"],
    "player_points_rebounds_assists": ["PTS", "REB", "AST"],
    "player_points_rebounds":         ["PTS", "REB"],
    "player_points_assists":          ["PTS", "AST"],
    "player_rebounds_assists":        ["REB", "AST"],
    # alt markets resolve to same columns
    "player_points_alternate":        ["PTS"],
    "player_rebounds_alternate":      ["REB"],
    "player_assists_alternate":       ["AST"],
    "player_threes_alternate":        ["FG3M"],
}

# ── PLAYER CACHE ─────────────────────────────────────────────────────────────
_id_map   = {}   # norm_name → player_id (int)
_logs     = {}   # player_id → list[dict] newest-first
_failed   = set()

def _norm(name: str) -> str:
    """Normalize player name for fuzzy matching."""
    name = name.lower()
    name = re.sub(r"[.'`\-]", " ", name)
    name = re.sub(r"\b(jr|sr|ii|iii|iv)\b", "", name)
    return re.sub(r"\s+", " ", name).strip()

def _build_id_map():
    if not NBA_API_OK:
        return
    # Try static file -- check multiple possible locations
    possible_paths = [
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "nba_players.json"),
        os.path.join(os.getcwd(), "nba_players.json"),
        "nba_players.json",
    ]
    for static_path in possible_paths:
        if os.path.exists(static_path):
            try:
                with open(static_path) as f:
                    players = json.load(f)
                for p in players:
                    _id_map[_norm(p["full_name"])] = p["id"]
                print(f"[OK] Loaded {len(_id_map)} player IDs from {static_path}", flush=True)
                return
            except Exception as e:
                print(f"[WARN] Could not load {static_path}: {e}", flush=True)
    print(f"[WARN] nba_players.json not found. Tried: {possible_paths}", flush=True)
    # Fallback: live nba_api call
    try:
        for p in _nba_players.get_players():
            _id_map[_norm(p["full_name"])] = p["id"]
        print(f"[OK] Loaded {len(_id_map)} player IDs from nba_api", flush=True)
    except Exception as e:
        print(f"[WARN] Could not load player list: {e}", flush=True)

def _find_id(name: str):
    n = _norm(name)
    # Exact match
    if n in _id_map:
        return _id_map[n]
    # Partial match — all tokens present in key
    parts = n.split()
    for key, pid in _id_map.items():
        if all(p in key for p in parts):
            return pid
    return None

def _fetch_log(pid: int):
    """Fetch and cache game log for player_id. No-op if already cached."""
    if pid in _logs or pid in _failed:
        return
    try:
        gl = _gamelog.PlayerGameLog(
            player_id=pid,
            season=NBA_SEASON,
            season_type_all_star="Regular Season",
            timeout=12,
        )
        df = gl.get_data_frames()[0]
        if df.empty:
            _failed.add(pid)
        else:
            _logs[pid] = df.to_dict("records")  # newest game first
            print(f"[OK] Loaded {len(_logs[pid])} games for player_id {pid}")
    except Exception as e:
        print(f"[WARN] Failed to load game log for {pid}: {e}")
        _failed.add(pid)

def _avg(rows, cols):
    if not rows:
        return None
    vals = [sum(float(r.get(c) or 0) for c in cols) for r in rows]
    return round(sum(vals) / len(vals), 1) if vals else None

def _hit_rate(rows, cols, line):
    """Returns 'X/N' string showing how often player cleared this line."""
    if not rows:
        return None
    hits = sum(
        1 for r in rows
        if sum(float(r.get(c) or 0) for c in cols) > line
    )
    return f"{hits}/{len(rows)}"

def _signal(season_avg, l10_avg, line, market_key, vol_3pa):
    """
    Determines betting signal based on player profile vs line.
    Returns (signal_str, warn_str).
    """
    if season_avg is None or l10_avg is None:
        return None, None

    m    = season_avg - line   # positive = avg is above the line (lean over)
    m10  = l10_avg    - line

    # 3PM volume override — if attempting 5+ per game, under is high risk
    if market_key == "player_threes" and vol_3pa and vol_3pa >= THREE_PA_WARN:
        return "FADE_UNDER", f"HIGH VOLUME ({vol_3pa} 3PA/g L10) — fade unders"

    if   m >  1.5 and m10 >  1.0: return "HAMMER_OVER",  None
    elif m >  0.5 and m10 >  0.5: return "LEAN_OVER",    None
    elif m < -1.5 and m10 < -1.0: return "HAMMER_UNDER", None
    elif m < -0.5 and m10 < -0.5: return "LEAN_UNDER",   None
    else:                          return "NEUTRAL",       None

# ── FLASK APP ────────────────────────────────────────────────────────────────
app = Flask(__name__)

# Build player ID map at import time so gunicorn workers load it on startup
_build_id_map()

@app.route("/health")
def health():
    """Quick ping to confirm server is running."""
    return jsonify({
        "status":    "ok",
        "nba_api":   NBA_API_OK,
        "players":   len(_id_map),
        "cached":    len(_logs),
        "season":    NBA_SEASON,
    })

@app.route("/stats")
def stats():
    """
    GET /stats?player=Jalen+Johnson&market=player_threes&line=1.5

    Returns season avg, L10, L5, hit rates, 3PA/g, signal, and warn.
    Scriptable uses this to show the stat line under each edge.
    """
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

    if not NBA_API_OK:
        return jsonify({"error": "nba_api not installed on server"}), 503

    pid = _find_id(player_name)
    if pid is None:
        return jsonify({"found": False, "player": player_name}), 200

    _fetch_log(pid)
    rows = _logs.get(pid)
    if not rows:
        return jsonify({"found": False, "player": player_name}), 200

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
        atts = [float(r.get("FG3A") or 0) for r in l10]
        vol_3pa = round(sum(atts) / len(atts), 1) if atts else None

    signal, warn = _signal(season_avg, l10_avg, line, market_key, vol_3pa)

    # Last 5 raw values for sparkline display in Scriptable
    last5_raw = []
    for r in l5:
        try:
            last5_raw.append(round(sum(float(r.get(c) or 0) for c in cols), 1))
        except Exception:
            pass

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
        "signal":     signal,
        "warn":       warn,
        "last5_raw":  last5_raw,
        "games":      len(rows),
    })

@app.route("/gamelog")
def gamelog():
    """
    GET /gamelog?player=Jalen+Johnson&n=10

    Returns raw last N game values for all tracked stat categories.
    Useful for debugging or building custom displays in Scriptable.
    """
    player_name = request.args.get("player", "").strip()
    n           = int(request.args.get("n", 10))

    if not player_name:
        return jsonify({"error": "player is required"}), 400

    if not NBA_API_OK:
        return jsonify({"error": "nba_api not installed"}), 503

    pid = _find_id(player_name)
    if pid is None:
        return jsonify({"found": False, "player": player_name}), 200

    _fetch_log(pid)
    rows = _logs.get(pid, [])[:n]
    if not rows:
        return jsonify({"found": False, "player": player_name}), 200

    # Return a clean subset of columns rather than the full raw row
    clean = []
    for r in rows:
        clean.append({
            "date":  r.get("GAME_DATE"),
            "matchup": r.get("MATCHUP"),
            "pts":   r.get("PTS"),
            "reb":   r.get("REB"),
            "ast":   r.get("AST"),
            "fg3m":  r.get("FG3M"),
            "fg3a":  r.get("FG3A"),
            "blk":   r.get("BLK"),
            "stl":   r.get("STL"),
            "min":   r.get("MIN"),
        })

    return jsonify({"found": True, "player": player_name, "games": clean})

# ── STARTUP ──────────────────────────────────────────────────────────────────
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
