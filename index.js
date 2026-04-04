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

let playerMap = {};

async function loadPlayerMap() {
  const data = await fetchTank01('getNBAPlayerList', {});
  if (data.body) {
    for (const player of data.body) {
      playerMap[player.longName.toLowerCase().trim()] = player.playerID;
    }
  }
  return Object.keys(playerMap).length;
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const mapSize = await loadPlayerMap();

    const events = await fetchURL(
      `https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey=${ODDS_API_KEY}`
    );

    const firstEvent = events[0];
    const eventProps = await fetchURL(
      `https://api.the-odds-api.com/v4/sports/basketball_nba/events/${firstEvent.id}/odds?apiKey=${ODDS_API_KEY}&regions=us&markets=player_points&oddsFormat=american&bookmakers=fanduel,draftkings`
    );

    const firstBook = eventProps.bookmakers?.[0];
    const firstMarket = firstBook?.markets?.[0];
    const firstPlayer = firstMarket?.outcomes?.[0];

    const playerName = firstPlayer?.description;
    const playerID = playerName ? playerMap[playerName.toLowerCase().trim()] : null;

    let games = null;
    if (playerID) {
      const playerData = await fetchTank01('getNBAGamesForPlayer', {
        playerID,
        numberOfGames: '5'
      });
      games = playerData.body ? Object.values(playerData.body).slice(0, 1) : null;
    }

    res.end(JSON.stringify({
      mapSize,
      firstEvent: firstEvent.home_team + ' vs ' + firstEvent.away_team,
      playerName,
      playerID,
      sampleGame: games?.[0] || null
    }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
