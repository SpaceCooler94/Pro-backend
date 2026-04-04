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

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const data = await fetchTank01('getNBAGamesForPlayer', {
      playerID: '28268405032',
      numberOfGames: '10',
      season: '2024'
    });
    res.end(JSON.stringify(data));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
