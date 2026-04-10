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

@app.route("/bets")

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
