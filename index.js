const http = require('http');
const https = require('https');

const ODDS_API_KEY = process.env.ODDS_API_KEY;

const cache = {};

function fetchNBA(url) {
  return new Promise((resolve, reject) => {
    const options = {
      headers: {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'en-US,en;q=0.9',
        'x-nba-stats-origin': 'stats',
        'x-nba-stats-token': 'true',
        'Connection': 'keep-alive'
      },
      timeout: 15000
    };
    const req = https.get(url, options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch(e) { reject(new Error('NBA parse error')); }
      });
    });
    req.on('timeout', () => { req.destroy(); reject(new Error('timeout')); });
    req.on('error', reject);
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

async function getPlayerGameLog(playerID) {
  if (cache[playerID]) return cache[playerID];
  const url = `https://stats.nba.com/stats/playergamelog?PlayerID=${playerID}&Season=2024-25&SeasonType=Regular+Season`;
  const data = await fetchNBA(url);
  const rows = data.resultSets[0].rowSet;
  const headers = data.resultSets[0].headers;
  const games = rows.map(row => {
    const obj = {};
    headers.forEach((h, i) => obj[h] = row[i]);
    return obj;
  });
  cache[playerID] = games;
  return games;
}

async function getNBAPlayerList() {
  if (cache['playerList']) return cache['playerList'];
  const url = 'https://stats.nba.com/stats/commonallplayers?LeagueID=00&Season=2024-25&IsOnlyCurrentSeason=1';
  const data = await fetchNBA(url);
  const rows = data.resultSets[0].rowSet;
  const map = {};
  rows.forEach(row => {
    const name = row[2].toLowerCase().trim();
    const id = row[0];
    map[name] = id;
  });
  cache['playerList'] = map;
  return map;
}

function calcAvg(games, stat, count) {
  const recent = games.slice(0, count);
  if (recent.length === 0) return null;
  const total = recent.reduce((sum, g) => sum + parseFloat(g[stat] || 0), 0);
  return parseFloat((total / recent.length).toFixed(2));
}

function analyzePlayer(games, line, stat) {
  const avgMins = calcAvg(games, 'MIN', 10);
  const avgFGA = calcAvg(games, 'FGA', 10);

  if (!avgMins || avgMins < 25) return { skip: true, reason: 'insufficient minutes' };
  if (!avgFGA || avgFGA < 10) return { skip: true, reason: 'insufficient usage' };

  const L5 = calcAvg(games, stat, 5);
  const L10 = calcAvg(games, stat, 10);
  const L20 = calcAvg(games, stat, 20);
  const confirmed = L5 > line && L10 > line;

  return { skip: false, avgMins, avgFGA, L5, L10, L20, line, stat, confirmed };
}

const STAT_MAP = {
  player_rebounds: 'REB',
  player_points: 'PTS',
  player_assists: 'AST',
  player_threes: 'FG3M'
};

const MARKETS = Object.keys(STAT_MAP);

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const playerMap = await getNBAPlayerList();

    const events = await fetchURL(
      `https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey=${ODDS_API_KEY}`
    );

    const confirmedPlays = [];
    const seen = new Set();

    for (const event of events) {
      const eventProps = await fetchURL(
        `https://api.the-odds-api.com/v4/sports/basketball_nba/events/${event.id}/odds?apiKey=${ODDS_API_KEY}&regions=us&markets=${MARKETS.join(',')}&oddsFormat=american&bookmakers=fanduel,draftkings`
      );

      if (!eventProps.bookmakers) continue;

      for (const book of eventProps.bookmakers) {
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

            const playerID = playerMap[playerName.toLowerCase().trim()];
            if (!playerID) continue;

            let games;
            try {
              games = await getPlayerGameLog(playerID);
            } catch(e) {
              continue;
            }

            if (!games || games.length === 0) continue;

            const analysis = analyzePlayer(games, line, stat);
            if (analysis.skip || !analysis.confirmed) continue;

            confirmedPlays.push({
              player: playerName,
              game: `${event.away_team} @ ${event.home_team}`,
              book: book.key,
              market: market.key,
              line,
              price,
              avgMins: analysis.avgMins,
              avgFGA: analysis.avgFGA,
              L5: analysis.L5,
              L10: analysis.L10,
              L20: analysis.L20
            });
          }
        }
      }
    }

    res.end(JSON.stringify({ confirmedPlays, total: confirmedPlays.length }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
