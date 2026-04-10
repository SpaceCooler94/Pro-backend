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
def bets_app():
    """
    Serves the full bet tracking web app.
    All data lives in the phone browser localStorage — server just delivers the page.
    Visit https://stats-server-oji0.onrender.com/bets on your phone.
    """
    html = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1, maximum-scale=1, user-scalable=no">
<meta name="apple-mobile-web-app-capable" content="yes">
<title>Bet Tracker</title>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg:#0d0d0d;--bg2:#141414;--bg3:#1a1a1a;--bg4:#222;
  --border:#2a2a2a;--border2:#333;
  --text:#f0f0f0;--muted:#666;--dim:#333;
  --green:#34c759;--red:#ff453a;--orange:#ff9f0a;--gold:#ffd60a;
  --cyan:#00c2cb;--fd:#1493ff;--dk:#3ddc97;--mgm:#c8a84b;
}
html,body{height:100%;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,sans-serif;font-size:15px;-webkit-text-size-adjust:none;overflow-x:hidden}
.header{position:sticky;top:0;z-index:50;background:var(--bg);border-bottom:1px solid var(--border);padding:14px 16px 0}
.header-top{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px}
.app-title{font-size:20px;font-weight:800;letter-spacing:-0.5px}
.app-title span{color:var(--cyan)}
.pnl-badge{font-size:13px;font-weight:700;padding:4px 10px;border-radius:20px;border:1px solid}
.tabs{display:flex;gap:4px}
.tab{flex:1;padding:8px 4px;text-align:center;font-size:13px;font-weight:600;color:var(--muted);border-bottom:2px solid transparent;cursor:pointer}
.tab.active{color:var(--cyan);border-bottom-color:var(--cyan)}
.tab-panel{display:none;padding:16px}
.tab-panel.active{display:block}
.form-section{margin-bottom:16px}
.form-label{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--muted);margin-bottom:6px;display:block;text-transform:uppercase}
.form-input{width:100%;background:var(--bg3);border:1px solid var(--border2);color:var(--text);font-size:16px;padding:12px 14px;border-radius:8px;-webkit-appearance:none;outline:none}
.form-input:focus{border-color:var(--cyan)}
.form-input::placeholder{color:var(--dim)}
select.form-input{background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='8'%3E%3Cpath d='M1 1l5 5 5-5' stroke='%23666' stroke-width='1.5' fill='none'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 14px center;padding-right:36px}
.form-row{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.form-hint{font-size:11px;color:var(--muted);margin-top:4px}
.btn{width:100%;padding:14px;border:none;border-radius:8px;font-size:16px;font-weight:700;cursor:pointer}
.btn-primary{background:var(--cyan);color:#000}
.btn-sm{padding:6px 12px;font-size:12px;font-weight:700;border:none;border-radius:6px;cursor:pointer}
.btn-hit{background:rgba(52,199,89,0.15);color:var(--green);border:1px solid rgba(52,199,89,0.3)}
.btn-miss{background:rgba(255,69,58,0.12);color:var(--red);border:1px solid rgba(255,69,58,0.25)}
.btn-delete{background:rgba(255,255,255,0.05);color:var(--muted);border:1px solid var(--border)}
.bet-card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px;margin-bottom:10px}
.bet-card-top{display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px}
.bet-player{font-size:15px;font-weight:700}
.bet-market{font-size:12px;color:var(--muted);margin-top:2px}
.bet-book-badge{font-size:11px;font-weight:700;padding:3px 8px;border-radius:12px}
.book-fd{background:rgba(20,147,255,0.15);color:var(--fd)}
.book-dk{background:rgba(61,220,151,0.12);color:var(--dk)}
.book-mgm{background:rgba(200,168,75,0.12);color:var(--mgm)}
.bet-details{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:10px}
.bet-detail{background:var(--bg2);border-radius:6px;padding:8px 10px;text-align:center}
.bet-detail-val{font-size:15px;font-weight:700}
.bet-detail-lbl{font-size:9px;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-top:2px}
.bet-edge{display:flex;align-items:center;gap:8px;font-size:12px;color:var(--muted);margin-bottom:10px;flex-wrap:wrap}
.edge-dot{width:6px;height:6px;border-radius:50%;flex-shrink:0}
.bet-actions{display:flex;gap:8px}
.bet-result-hit{border-color:rgba(52,199,89,0.3);background:rgba(52,199,89,0.04)}
.bet-result-miss{border-color:rgba(255,69,58,0.25);background:rgba(255,69,58,0.04)}
.result-badge{font-size:12px;font-weight:700;padding:4px 10px;border-radius:12px}
.result-hit{background:rgba(52,199,89,0.15);color:var(--green)}
.result-miss{background:rgba(255,69,58,0.12);color:var(--red)}
.signal-tag{font-size:11px;padding:2px 8px;border-radius:10px;background:var(--bg2);color:var(--muted)}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:10px;margin-bottom:16px}
.stat-card{background:var(--bg3);border:1px solid var(--border);border-radius:10px;padding:14px;text-align:center}
.stat-val{font-size:26px;font-weight:800;line-height:1}
.stat-lbl{font-size:10px;color:var(--muted);letter-spacing:1.5px;text-transform:uppercase;margin-top:4px}
.stat-sub{font-size:11px;color:var(--muted);margin-top:2px}
.breakdown-section{margin-bottom:20px}
.breakdown-title{font-size:11px;font-weight:700;letter-spacing:2px;color:var(--muted);text-transform:uppercase;margin-bottom:10px}
.breakdown-row{display:flex;align-items:center;gap:10px;padding:10px 0;border-bottom:1px solid var(--border)}
.breakdown-row:last-child{border-bottom:none}
.breakdown-label{font-size:13px;font-weight:600;flex:1;min-width:100px}
.breakdown-bar-wrap{flex:2;height:6px;background:var(--bg4);border-radius:3px;overflow:hidden}
.breakdown-bar{height:100%;border-radius:3px;background:var(--cyan)}
.breakdown-pct{font-size:13px;font-weight:700;min-width:36px;text-align:right}
.breakdown-count{font-size:11px;color:var(--muted);min-width:40px;text-align:right}
.empty-state{text-align:center;padding:48px 20px;color:var(--muted)}
.empty-icon{font-size:40px;margin-bottom:12px;opacity:0.4}
.section-title{font-size:13px;font-weight:700;color:var(--muted);letter-spacing:1px;text-transform:uppercase;margin-bottom:12px}
.divider{height:1px;background:var(--border);margin:16px 0}
.toast{position:fixed;bottom:32px;left:50%;transform:translateX(-50%);background:var(--bg4);color:var(--text);padding:10px 20px;border-radius:20px;font-size:13px;font-weight:600;opacity:0;transition:opacity .2s;pointer-events:none;z-index:100;border:1px solid var(--border2)}
.toast.show{opacity:1}
.pending-count{display:inline-flex;align-items:center;justify-content:center;background:var(--orange);color:#000;border-radius:10px;font-size:10px;font-weight:800;min-width:18px;height:18px;padding:0 5px;margin-left:4px}
</style>
</head>
<body>
<div class="header">
  <div class="header-top">
    <div class="app-title">BET <span>TRACKER</span></div>
    <div class="pnl-badge" id="headerPnl">+$0.00</div>
  </div>
  <div class="tabs">
    <div class="tab active" onclick="showTab('log')">Log Bet</div>
    <div class="tab" onclick="showTab('pending')">Pending <span class="pending-count" id="pendingCount">0</span></div>
    <div class="tab" onclick="showTab('results')">Results</div>
  </div>
</div>
<div class="toast" id="toast"></div>

<!-- LOG TAB -->
<div class="tab-panel active" id="tab-log">
  <div class="form-section">
    <label class="form-label">Player</label>
    <input class="form-input" type="text" id="f-player" placeholder="e.g. Jalen Johnson" autocomplete="off" autocapitalize="words">
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
      <select class="form-input" id="f-side">
        <option>Over</option><option>Under</option>
      </select>
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
        <option value="HAMMER_OVER">HAMMER OVER</option>
        <option value="LEAN_OVER">lean over</option>
        <option value="HAMMER_UNDER">HAMMER UNDER</option>
        <option value="LEAN_UNDER">lean under</option>
        <option value="NEUTRAL">neutral</option>
        <option value="FADE_UNDER">FADE</option>
      </select>
    </div>
  </div>
  <div class="form-section">
    <label class="form-label">Notes (optional)</label>
    <input class="form-input" type="text" id="f-notes" placeholder="e.g. parlay leg, hot streak" autocorrect="off">
  </div>
  <button class="btn btn-primary" onclick="logBet()">LOG BET</button>
</div>

<!-- PENDING TAB -->
<div class="tab-panel" id="tab-pending">
  <div id="pendingList"></div>
</div>

<!-- RESULTS TAB -->
<div class="tab-panel" id="tab-results">
  <div id="resultsSummary"></div>
</div>

<script>
const STORAGE_KEY = "bettracker_v1";
function loadBets(){try{return JSON.parse(localStorage.getItem(STORAGE_KEY)||"[]")}catch(e){return[]}}
function saveBets(b){localStorage.setItem(STORAGE_KEY,JSON.stringify(b))}
function amToImpl(o){const n=parseFloat(o);if(isNaN(n))return null;return n>0?100/(n+100):Math.abs(n)/(Math.abs(n)+100)}
function calcProfit(o,s){const n=parseFloat(o);if(isNaN(n)||isNaN(s))return 0;return n>0?(n/100)*s:(100/Math.abs(n))*s}
function fmtOdds(n){return n>0?"+"+n:""+n}
function fmtMoney(n){const a=Math.abs(n).toFixed(2);return(n>=0?"+$":"-$")+a}
function edgeColor(e){if(e>=15)return"var(--green)";if(e>=8)return"var(--orange)";return"var(--muted)"}
function signalLabel(s){return{HAMMER_OVER:"▲ HAMMER OVER",LEAN_OVER:"△ lean over",HAMMER_UNDER:"▼ HAMMER UNDER",LEAN_UNDER:"▽ lean under",NEUTRAL:"~ neutral",FADE_UNDER:"✗ FADE"}[s]||s||"—"}
function bookClass(b){return{FD:"book-fd",DK:"book-dk",MGM:"book-mgm"}[b]||""}
function uid(){return Date.now().toString(36)+Math.random().toString(36).slice(2,6)}
function toast(m){const e=document.getElementById("toast");e.textContent=m;e.classList.add("show");setTimeout(()=>e.classList.remove("show"),2000)}
function fmtDate(ts){return new Date(ts).toLocaleDateString("en-US",{month:"short",day:"numeric",hour:"numeric",minute:"2-digit"})}

function showTab(n){
  document.querySelectorAll(".tab").forEach((t,i)=>{
    t.classList.toggle("active",["log","pending","results"][i]===n)
  });
  document.querySelectorAll(".tab-panel").forEach(p=>p.classList.remove("active"));
  document.getElementById("tab-"+n).classList.add("active");
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
  const notes=document.getElementById("f-notes").value.trim();
  if(!player){toast("Enter player name");return}
  if(isNaN(line)){toast("Enter the line");return}
  if(isNaN(odds)){toast("Enter your odds");return}
  if(isNaN(fair)){toast("Enter Novig fair odds");return}
  if(isNaN(stake)||stake<=0){toast("Enter a stake");return}
  const bet={id:uid(),ts:Date.now(),player,market,line,side,book,odds,fair,edge:odds-fair,stake,signal,notes,result:null,profit:null};
  const bets=loadBets();bets.unshift(bet);saveBets(bets);
  ["f-player","f-line","f-odds","f-fair","f-stake","f-notes"].forEach(id=>document.getElementById(id).value="");
  updateHeader();toast("Bet logged");
}

function settleBet(id,result){
  const bets=loadBets();const bet=bets.find(b=>b.id===id);if(!bet)return;
  bet.result=result;bet.profit=result==="hit"?calcProfit(bet.odds,bet.stake):-bet.stake;
  saveBets(bets);updateHeader();renderPending();toast(result==="hit"?"Hit logged":"Miss logged");
}

function deleteBet(id){
  if(!confirm("Delete this bet?"))return;
  saveBets(loadBets().filter(b=>b.id!==id));
  updateHeader();renderPending();renderResults();
}

function renderPending(){
  const bets=loadBets().filter(b=>b.result===null);
  const el=document.getElementById("pendingList");
  document.getElementById("pendingCount").textContent=bets.length;
  if(!bets.length){el.innerHTML='<div class="empty-state"><div class="empty-icon">📋</div><div class="empty-text">No pending bets</div></div>';return}
  el.innerHTML=bets.map(b=>{
    const profit=calcProfit(b.odds,b.stake).toFixed(2);
    const ec=edgeColor(b.edge);
    return '<div class="bet-card">'+
      '<div class="bet-card-top"><div><div class="bet-player">'+b.player+'</div><div class="bet-market">'+b.market+' '+b.line+' '+b.side+'</div></div>'+
      '<div class="bet-book-badge '+bookClass(b.book)+'">'+b.book+'</div></div>'+
      '<div class="bet-details">'+
      '<div class="bet-detail"><div class="bet-detail-val">'+fmtOdds(b.odds)+'</div><div class="bet-detail-lbl">Odds</div></div>'+
      '<div class="bet-detail"><div class="bet-detail-val">$'+b.stake.toFixed(0)+'</div><div class="bet-detail-lbl">Stake</div></div>'+
      '<div class="bet-detail"><div class="bet-detail-val">$'+profit+'</div><div class="bet-detail-lbl">To Win</div></div></div>'+
      '<div class="bet-edge"><div class="edge-dot" style="background:'+ec+'"></div>'+
      '<span style="color:'+ec+'">'+b.edge+' pts</span> · '+
      '<span class="signal-tag">'+signalLabel(b.signal)+'</span> · <span>'+fmtDate(b.ts)+'</span></div>'+
      '<div class="bet-actions">'+
      '<button class="btn-sm btn-hit" onclick="settleBet(\'\'\'+b.id+'\'\', \'\'hit\'\')">✓ HIT</button>'+
      '<button class="btn-sm btn-miss" onclick="settleBet(\'\'\'+b.id+'\'\', \'\'miss\'\')">✗ MISS</button>'+
      '<button class="btn-sm btn-delete" onclick="deleteBet(\'\'\'+b.id+'\'\')">Delete</button></div></div>'
  }).join("")
}

function renderResults(){
  const all=loadBets();const settled=all.filter(b=>b.result!==null);
  const el=document.getElementById("resultsSummary");
  if(!settled.length){el.innerHTML='<div class="empty-state"><div class="empty-icon">📊</div><div class="empty-text">No settled bets yet</div></div>';return}
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

  const recent=settled.slice(0,15);

  const breakdownRows=(rows)=>rows.map(r=>
    '<div class="breakdown-row"><div class="breakdown-label">'+r.label+'</div>'+
    '<div class="breakdown-bar-wrap"><div class="breakdown-bar" style="width:'+r.pct+'%"></div></div>'+
    '<div class="breakdown-pct">'+r.pct+'%</div><div class="breakdown-count">'+r.won+'/'+r.total+'</div></div>'
  ).join("");

  el.innerHTML=
    '<div class="stats-grid">'+
    '<div class="stat-card"><div class="stat-val" style="color:'+pnlColor+'">'+fmtMoney(pnl)+'</div><div class="stat-lbl">Total P&L</div><div class="stat-sub">'+settled.length+' bets · $'+staked.toFixed(0)+' staked</div></div>'+
    '<div class="stat-card"><div class="stat-val">'+hitRate+'%</div><div class="stat-lbl">Hit Rate</div><div class="stat-sub">'+hits.length+'/'+settled.length+' · ROI <span style="color:'+roiColor+'">'+roi+'%</span></div></div>'+
    '</div>'+
    '<div class="breakdown-section"><div class="breakdown-title">By Signal</div>'+breakdownRows(sigRows)+'</div>'+
    '<div class="breakdown-section"><div class="breakdown-title">By Market</div>'+breakdownRows(mktRows)+'</div>'+
    '<div class="divider"></div><div class="section-title">Recent Bets</div>'+
    recent.map(b=>{
      const cls=b.result==="hit"?"bet-result-hit":"bet-result-miss";
      const badge=b.result==="hit"?'<span class="result-badge result-hit">HIT</span>':'<span class="result-badge result-miss">MISS</span>';
      const pc=b.profit>=0?"var(--green)":"var(--red)";
      return '<div class="bet-card '+cls+'">'+
        '<div class="bet-card-top"><div><div class="bet-player">'+b.player+'</div>'+
        '<div class="bet-market">'+b.market+' '+b.line+' '+b.side+' · '+b.book+' '+fmtOdds(b.odds)+'</div></div>'+
        '<div style="text-align:right">'+badge+'<div style="font-size:15px;font-weight:800;color:'+pc+';margin-top:4px">'+fmtMoney(b.profit)+'</div></div></div>'+
        '<div class="bet-edge"><div class="edge-dot" style="background:'+edgeColor(b.edge)+'"></div>'+
        '<span style="color:'+edgeColor(b.edge)+'">'+b.edge+' pts</span> · '+
        '<span class="signal-tag">'+signalLabel(b.signal)+'</span> · <span>'+fmtDate(b.ts)+'</span></div></div>'
    }).join("")
}

function updateHeader(){
  const settled=loadBets().filter(b=>b.result!==null);
  const pnl=settled.reduce((s,b)=>s+(b.profit||0),0);
  const el=document.getElementById("headerPnl");
  el.textContent=fmtMoney(pnl);
  el.style.color=pnl>=0?"var(--green)":"var(--red)";
  el.style.borderColor=pnl>=0?"rgba(52,199,89,0.3)":"rgba(255,69,58,0.25)";
  el.style.background=pnl>=0?"rgba(52,199,89,0.08)":"rgba(255,69,58,0.06)";
  document.getElementById("pendingCount").textContent=loadBets().filter(b=>b.result===null).length;
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
