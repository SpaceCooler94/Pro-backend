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

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const playerName = 'Nikola Jokic';
    const lastName = playerName.split(' ').pop();

    const playerInfo = await fetchTank01('getNBAPlayerInfo', {
      playerName: lastName,
      statsToGet: 'averages'
    });

    const match = playerInfo.body ? playerInfo.body.find(p =>
      p.longName.toLowerCase() === playerName.toLowerCase()
    ) : null;

    const playerID = match ? match.playerID : null;

    let games = null;
    if (playerID) {
      const playerData = await fetchTank01('getNBAGamesForPlayer', {
        playerID,
        numberOfGames: '5'
      });
      games = playerData.body ? Object.values(playerData.body).slice(0, 2) : null;
    }

    res.end(JSON.stringify({
      playerName,
      lastName,
      matchFound: !!match,
      playerID,
      sampleGames: games
    }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
