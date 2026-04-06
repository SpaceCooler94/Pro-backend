const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;
const ODDS_API_KEY = process.env.ODDS_API_KEY;

// ── CACHE ─────────────────────────────────────────────────────────────────────
let cachedResult = null;
let cacheTime = 0;
const CACHE_TTL = 30 * 60 * 1000;

// ── TANKING TEAMS ─────────────────────────────────────────────────────────────
const TANKING_TEAMS = new Set([
  'Toronto Raptors',
  'Washington Wizards',
  'Charlotte Hornets',
  'Utah Jazz',
  'Portland Trail Blazers',
  'San Antonio Spurs',
  'Detroit Pistons',
  'New Orleans Pelicans',
]);

function fetchTank01(endpoint, params) {
  return new Promise((resolve, reject) => {
    const queryString = new URLSearchParams(params).toString();
    const url = `https://tank01-fantasy-stats.p.rapidapi.com/${endpoint}${queryString ? '?' + queryString : ''}`;
    const options = {
      headers: {
        'x-rapidapi-host': 'tank01-fantasy-stats.p.rapidapi.com',
        'x-rapidapi-key': RAPIDAPI_KEY,
        'Content-Type': 'application/json'
      }
    };
    https.get(url, options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch(e) { reject(new Error('Tank01 parse error')); }
      });
    }).on('error', reject);
  });
}

function fetchURL(url) {
  return new Promise((resolve, reject) => {
    https.get(url, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch(e) { reject(new Error('parse error')); }
      });
    }).on('error', reject);
  });
}

function calcStdDev(games, stat, count) {
  const recent = games.slice(0, count);
  if (recent.length < 3) return null;
  const vals = recent.map(g => parseFloat(g[stat] || 0));
  const avg = vals.reduce((a, b) => a + b, 0) / vals.length;
  const variance = vals.reduce((sum, v) => sum + Math.pow(v - avg, 2), 0) / vals.length;
  return parseFloat(Math.sqrt(variance).toFixed(2));
}

function calcAvg(games, stat, count) {
  const recent = games.slice(0, count);
  if (recent.length === 0) return null;
  const total = recent.reduce((sum, g) => sum + parseFloat(g[stat] || 0), 0);
  return parseFloat((total / recent.length).toFixed(2));
}

function calcHitRate(games, stat, line, count) {
  const recent = games.slice(0, count);
  if (recent.length === 0) return null;
  const hits = recent.filter(g => parseFloat(g[stat] || 0) > line).length;
  return parseFloat((hits / recent.length).toFixed(2));
}

function analyzePlayer(games, line, stat) {
  const gameList = Object.values(games);
  const avgMins = calcAvg(gameList, 'mins', 10);
  const avgFGA = calcAvg(gameList, 'fga', 10);

  if (avgMins < 25) return { skip: true, reason: 'insufficient minutes' };
  if (avgFGA < 10) return { skip: true, reason: 'insufficient usage' };

  const L5 = calcAvg(gameList, stat, 5);
  const L10 = calcAvg(gameList, stat, 10);
  const L20 = calcAvg(gameList, stat, 20);

  const stdDev = calcStdDev(gameList, stat, 10);
  if (stdDev !== null && stdDev > line * 0.6) {
    return { skip: true, reason: 'high variance' };
  }

  const hitRate10 = calcHitRate(gameList, stat, line, 10);
  if (hitRate10 !== null && hitRate10 < 0.6) {
    return { skip: true, reason: 'low hit rate L10' };
  }

  const hitRate5 = calcHitRate(gameList, stat, line, 5);
  if (hitRate5 !== null && hitRate5 < 0.6) {
    return { skip: true, reason: 'low hit rate L5' };
  }

  const confirmed = L5 > line && L10 > line;

  return {
    skip: false, avgMins, avgFGA, L5, L10, L20,
    stdDev, hitRate5, hitRate10, line, stat, confirmed
  };
}

// ── SHARP PRICE — ANY EXCHANGE AT -150 OR BETTER ──────────────────────────────
const SHARP_BOOKS = ['novig', 'pinnacle', 'prophetx', 'betopenly'];

function getSharpPrice(bookmakers, playerName, marketKey) {
  let bestSharp = null;

  for (const book of bookmakers) {
    if (!SHARP_BOOKS.includes(book.key)) continue;
    for (const market of book.markets) {
      if (market.key !== marketKey) continue;
      for (const outcome of market.outcomes) {
        if (outcome.name === 'Over' &&
            outcome.description &&
            outcome.description.toLowerCase() === playerName.toLowerCase()) {
          // keep the sharpest (most negative) price across all sharp books
          if (!bestSharp || outcome.price < bestSharp.price) {
            bestSharp = { book: book.key, price: outcome.price, point: outcome.point };
          }
        }
      }
    }
  }
  return bestSharp;
}

const STAT_MAP = {
  player_rebounds: 'reb',
  player_points: 'pts',
  player_assists: 'ast',
  player_threes: 'tptfgm'
};

const MARKETS = Object.keys(STAT_MAP);

let playerMap = {};

async function loadPlayerMap() {
  try {
    const data = await fetchTank01('getNBAPlayerList', {});
    if (data.body && Array.isArray(data.body)) {
      for (const player of data.body) {
        playerMap[player.longName.toLowerCase().trim()] = player.playerID;
      }
      console.log(`Loaded ${Object.keys(playerMap).length} players`);
    }
  } catch(e) {
    console.log('Failed to load player map:', e.message);
  }
}

function findPlayerID(playerName) {
  return playerMap[playerName.toLowerCase().trim()] || null;
}

async function runAnalysis() {
  if (Object.keys(playerMap).length === 0) await loadPlayerMap();

  const today = new Date();
  const todayStr = today.toISOString().split('T')[0];
  const tomorrow = new Date(today);
  tomorrow.setDate(tomorrow.getDate() + 1);
  const tomorrowStr = tomorrow.toISOString().split('T')[0];

  const events = await fetchURL(
    `https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey=${ODDS_API_KEY}&commenceTimeFrom=${todayStr}T00:00:00Z&commenceTimeTo=${tomorrowStr}T00:00:00Z`
  );

  const confirmedPlays = [];
  const seen = new Set();

  for (const event of events) {
    const homeTeam = event.home_team;
    const awayTeam = event.away_team;

    const eventProps = await fetchURL(
      `https://api.the-odds-api.com/v4/sports/basketball_nba/events/${event.id}/odds?apiKey=${ODDS_API_KEY}&regions=us,us_ex&markets=${MARKETS.join(',')}&oddsFormat=american&bookmakers=fanduel,draftkings,novig,pinnacle,prophetx,betopenly`
    );

    if (!eventProps.bookmakers) continue;

    for (const book of eventProps.bookmakers) {
      if (!['fanduel', 'draftkings'].includes(book.key)) continue;

      for (const market of book.markets) {
        const stat = STAT_MAP[market.key];
        if (!stat) continue;

        for (const outcome of market.outcomes) {
          if (outcome.name !== 'Over') continue;

          const playerName = outcome.description;
          const line = outcome.point;
          const price = outcome.price;
          const key = `${playerName}|${market.key}|${line}|${book.key}`;

          if (seen.has(key)) continue;
          seen.add(key);

          const sharpLine = getSharpPrice(eventProps.bookmakers, playerName, market.key);
          if (!sharpLine) continue;
          if (sharpLine.price > -150) continue;

          const playerID = findPlayerID(playerName);
          if (!playerID) continue;

          const playerData = await fetchTank01('getNBAGamesForPlayer', {
            playerID,
            numberOfGames: '20',
            season: '2025'
          });

          if (!playerData.body) continue;

          const analysis = analyzePlayer(playerData.body, line, stat);
          if (analysis.skip || !analysis.confirmed) continue;

          // tanking team flag
          const playerTeam = Object.values(playerData.body)[0]?.team;
          const isTankingPlayer = playerTeam && [...TANKING_TEAMS].some(t =>
            t.toLowerCase().includes(playerTeam.toLowerCase()) ||
            playerTeam.toLowerCase().includes(t.toLowerCase())
          );

          confirmedPlays.push({
            player: playerName,
            game: `${awayTeam} @ ${homeTeam}`,
            book: book.key,
            market: market.key,
            line,
            retailPrice: price,
            sharpBook: sharpLine.book,
            sharpPrice: sharpLine.price,
            avgMins: analysis.avgMins,
            avgFGA: analysis.avgFGA,
            L5: analysis.L5,
            L10: analysis.L10,
            L20: analysis.L20,
            stdDev: analysis.stdDev,
            hitRate5: Math.round(analysis.hitRate5 * 100),
            hitRate10: Math.round(analysis.hitRate10 * 100),
            tankingTeam: isTankingPlayer || false
          });
        }
      }
    }
  }

  confirmedPlays.sort((a, b) => a.sharpPrice - b.sharpPrice);
  return {
    confirmedPlays,
    total: confirmedPlays.length,
    cachedAt: new Date().toISOString()
  };
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const now = Date.now();
    if (cachedResult && (now - cacheTime) < CACHE_TTL) {
      console.log('Serving from cache');
      return res.end(JSON.stringify({ ...cachedResult, fromCache: true }));
    }

    console.log('Running fresh analysis');
    const result = await runAnalysis();
    cachedResult = result;
    cacheTime = now;
    res.end(JSON.stringify({ ...result, fromCache: false }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
  loadPlayerMap();
});
