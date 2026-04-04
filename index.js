const http = require('http');
const https = require('https');

const ODDS_API_KEY = process.env.ODDS_API_KEY;

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
    const events = await fetchURL(`https://api.the-odds-api.com/v4/sports/basketball_nba/events?apiKey=${ODDS_API_KEY}`);
    
    const firstEvent = events[0];
    const props = await fetchURL(`https://api.the-odds-api.com/v4/sports/basketball_nba/events/${firstEvent.id}/odds?apiKey=${ODDS_API_KEY}&regions=us&markets=player_rebounds&oddsFormat=american&bookmakers=fanduel,draftkings`);
    
    res.end(JSON.stringify({ event: firstEvent, props }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
