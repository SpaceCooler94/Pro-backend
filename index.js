const http = require('http');
const https = require('https');

const RAPIDAPI_KEY = process.env.RAPIDAPI_KEY;

function fetchURL(url, headers) {
  return new Promise((resolve, reject) => {
    https.get(url, { headers }, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try { resolve(JSON.parse(data)); }
        catch(e) { reject(new Error('parse error: ' + data.slice(0, 200))); }
      });
    }).on('error', reject);
  });
}

const TANK01_HEADERS = {
  'x-rapidapi-host': 'tank01-fantasy-stats.p.rapidapi.com',
  'x-rapidapi-key': RAPIDAPI_KEY,
  'Content-Type': 'application/json'
};

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const data = await fetchURL(
      'https://tank01-fantasy-stats.p.rapidapi.com/getNBAPlayerList',
      TANK01_HEADERS
    );
    res.end(JSON.stringify({
      statusCode: data.statusCode,
      bodyType: typeof data.body,
      isArray: Array.isArray(data.body),
      count: Array.isArray(data.body) ? data.body.length : 0,
      sample: Array.isArray(data.body) ? data.body.slice(0, 2) : data
    }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
