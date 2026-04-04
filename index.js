const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;
const ODDS_API_KEY = process.env.ODDS_API_KEY;

function fetchTank01(endpoint, params) {
  return new Promise((resolve, reject) => {
    const queryString = new URLSearchParams(params).toString();
    const url = `https://tank01-fantasy-stats.p.rapidapi.com/${endpoint}?${queryString}`;
    const options = {
      headers: {
        'x-rapidapi-host': 'tank01-fantasy-stats.p.rapidapi.com',
        'x-rapidapi-key': RAPIDAPI_KEY
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

function calcAvg(games, stat, count) {
  const recent = games.slice(0, count);
  if (recent.length === 0) return null;
  const total = recent.reduce((sum, g) => sum + parseFloat(g[stat] || 0), 0);
  return parseFloat((total / recent.length).toFixed(2));
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
  const confirmed = L5 > line && L10 > line;

  return { skip: false, avgMins, avgFGA, L5, L10, L20, line, stat, confirmed };
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
    if (data.body) {
      for (const player of data.body) {
        const name = player.longName.toLowerCase().trim();
        playerMap[name] = player.playerID;
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
    if (Object.keys(playerMap).length === 0) {
      await loadPlayerMap();
    }

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

            const playerID = findPlayerID(playerName);
            if (!playerID) continue;

            const playerData = await fetchTank01('getNBAGamesForPlayer', {
              playerID,
              numberOfGames: '20'
            });

            if (!playerData.body) continue;

            const analysis = analyzePlayer(playerData.body, line, stat);
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
  loadPlayerMap();
});
