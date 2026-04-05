const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;
const ODDS_API_KEY = process.env.ODDS_API_KEY;

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

function fetchHTML(url) {
  return new Promise((resolve, reject) => {
    const options = {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml',
      }
    };
    https.get(url, options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(data));
    }).on('error', reject);
  });
}

// ── VARIANCE FILTER ───────────────────────────────────────────────────────────
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

// ── TANK01 INJURIES ───────────────────────────────────────────────────────────
let injuryMap = {};
let injuryLastFetched = 0;

async function loadInjuries() {
  if (Date.now() - injuryLastFetched < 30 * 60 * 1000) return;
  try {
    const data = await fetchTank01('getNBAInjuryList', {});
    injuryMap = {};
    if (data.body) {
      for (const player of Object.values(data.body)) {
        const name = (player.longName || '').toLowerCase().trim();
        const status = (player.injuryStatus || '').toUpperCase();
        if (name && status) injuryMap[name] = status;
      }
    }
    injuryLastFetched = Date.now();
    console.log(`Loaded ${Object.keys(injuryMap).length} injury records`);
  } catch(e) {
    console.log('Injury load failed:', e.message);
  }
}

function isInjured(playerName) {
  const status = injuryMap[playerName.toLowerCase().trim()];
  if (!status) return false;
  return ['OUT', 'DOUBTFUL'].includes(status);
}

// ── DVP FROM HASHTAG BASKETBALL ───────────────────────────────────────────────
let dvpMap = {};
let dvpLastFetched = 0;

const DVP_STAT_MAP = {
  player_points: 'PTS',
  player_rebounds: 'REB',
  player_assists: 'AST',
  player_threes: '3PM'
};

async function loadDVP() {
  if (Date.now() - dvpLastFetched < 60 * 60 * 1000) return;
  try {
    const html = await fetchHTML('https://hashtagbasketball.com/nba-defense-vs-position');
    dvpMap = {};

    // parse table rows for team defensive rankings
    const rowRegex = /<tr[^>]*>[\s\S]*?<\/tr>/gi;
    const cellRegex = /<td[^>]*>([\s\S]*?)<\/td>/gi;
    const rows = html.match(rowRegex) || [];

    let headers = [];
    let headerFound = false;

    for (const row of rows) {
      const cells = [];
      let m;
      while ((m = cellRegex.exec(row)) !== null) {
        cells.push(m[1].replace(/<[^>]+>/g, '').trim());
      }
      cellRegex.lastIndex = 0;

      if (!headerFound && cells.some(c => c === 'TEAM')) {
        headers = cells;
        headerFound = true;
        continue;
      }

      if (headerFound && cells.length > 2) {
        const team = cells[0];
        if (!team || team.length < 2) continue;
        dvpMap[team] = {};
        headers.forEach((h, i) => {
          if (cells[i]) dvpMap[team][h] = cells[i];
        });
      }
    }

    dvpLastFetched = Date.now();
    console.log(`Loaded DVP for ${Object.keys(dvpMap).length} teams`);
  } catch(e) {
    console.log('DVP load failed:', e.message);
  }
}

function getDVPRank(teamName, marketKey) {
  const statKey = DVP_STAT_MAP[marketKey];
  if (!statKey || !dvpMap) return null;

  // try to match team name
  const teamKey = Object.keys(dvpMap).find(k =>
    teamName.toLowerCase().includes(k.toLowerCase()) ||
    k.toLowerCase().includes(teamName.toLowerCase().split(' ').pop())
  );

  if (!teamKey) return null;
  const row = dvpMap[teamKey];
  if (!row) return null;

  // look for rank column like PTS_RANK or #PTS
  const rankKey = Object.keys(row).find(k => k.includes(statKey) && k.includes('RANK'));
  if (rankKey) return parseInt(row[rankKey]);

  return null;
}

// ── PLAYER ANALYSIS ───────────────────────────────────────────────────────────
function analyzePlayer(games, line, stat) {
  const gameList = Object.values(games);
  const avgMins = calcAvg(gameList, 'mins', 10);
  const avgFGA = calcAvg(gameList, 'fga', 10);

  if (avgMins < 25) return { skip: true, reason: 'insufficient minutes' };
  if (avgFGA < 10) return { skip: true, reason: 'insufficient usage' };

  const L5 = calcAvg(gameList, stat, 5);
  const L10 = calcAvg(gameList, stat, 10);
  const L20 = calcAvg(gameList, stat, 20);

  // variance filter — skip if std dev over L10 is more than 2x the line
  const stdDev = calcStdDev(gameList, stat, 10);
  if (stdDev !== null && stdDev > line * 0.8) {
    return { skip: true, reason: 'high variance' };
  }

  const confirmed = L5 > line && L10 > line;

  return {
    skip: false, avgMins, avgFGA, L5, L10, L20,
    stdDev, line, stat, confirmed
  };
}

function getSharpPrice(bookmakers, playerName, marketKey) {
  const sharpBooks = ['novig', 'pinnacle'];
  for (const book of bookmakers) {
    if (!sharpBooks.includes(book.key)) continue;
    for (const market of book.markets) {
      if (market.key !== marketKey) continue;
      for (const outcome of market.outcomes) {
        if (outcome.name === 'Over' &&
            outcome.description &&
            outcome.description.toLowerCase() === playerName.toLowerCase()) {
          return { book: book.key, price: outcome.price, point: outcome.point };
        }
      }
    }
  }
  return null;
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

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    if (Object.keys(playerMap).length === 0) await loadPlayerMap();
    await loadInjuries();
    await loadDVP();

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
        `https://api.the-odds-api.com/v4/sports/basketball_nba/events/${event.id}/odds?apiKey=${ODDS_API_KEY}&regions=us,us_ex&markets=${MARKETS.join(',')}&oddsFormat=american&bookmakers=fanduel,draftkings,novig,pinnacle`
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

            // injury filter
            if (isInjured(playerName)) continue;

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

            // DVP check — skip if opponent defense is top 8 against this stat
            const dvpRank = getDVPRank(homeTeam, market.key) || getDVPRank(awayTeam, market.key);

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
              dvpRank: dvpRank || null
            });
          }
        }
      }
    }

    // sort by sharpest price first
    confirmedPlays.sort((a, b) => a.sharpPrice - b.sharpPrice);

    res.end(JSON.stringify({ confirmedPlays, total: confirmedPlays.length }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
  loadPlayerMap();
  loadInjuries();
  loadDVP();
});
