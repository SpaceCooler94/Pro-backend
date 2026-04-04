const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;

function fetchTank01(endpoint) {
  return new Promise((resolve, reject) => {
    const url = `https://tank01-fantasy-stats.p.rapidapi.com/${endpoint}`;
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
        catch(e) { reject(new Error('parse error: ' + data.slice(0, 200))); }
      });
    }).on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const data = await fetchTank01('getNBAPlayerList');
    res.end(JSON.stringify({
      statusCode: data.statusCode,
      bodyType: typeof data.body,
      isArray: Array.isArray(data.body),
      count: Array.isArray(data.body) ? data.body.length : 0,
      sample: Array.isArray(data.body) ? data.body.slice(0, 2) : data.body
    }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
