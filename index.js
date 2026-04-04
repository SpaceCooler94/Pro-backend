const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;

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
        try {
          resolve(JSON.parse(data));
        } catch(e) {
          reject(new Error('Failed to parse response'));
        }
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

  if (avgMins < 25) {
    return { skip: true, reason: 'insufficient minutes', avgMins };
  }

  if (avgFGA < 10) {
    return { skip: true, reason: 'insufficient usage', avgFGA };
  }

  const L5 = calcAvg(gameList, stat, 5);
  const L10 = calcAvg(gameList, stat, 10);
  const L20 = calcAvg(gameList, stat, 20);

  const confirmed = L5 > line && L10 > line;

  return {
    skip: false,
    avgMins,
    avgFGA,
    L5,
    L10,
    L20,
    line,
    stat,
    L5_over_line: L5 > line,
    L10_over_line: L10 > line,
    confirmed
  };
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const data = await fetchTank01('getNBAGamesForPlayer', {
      playerID: '28268405032',
      numberOfGames: '20',
      season: '2024'
    });

    const games = data.body;
    const analysis = analyzePlayer(games, 9.5, 'reb');
    res.end(JSON.stringify(analysis));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
