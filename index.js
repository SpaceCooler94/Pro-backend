const http = require('http');
const https = require('https');

function fetchNBAStats(url) {
  return new Promise((resolve, reject) => {
    const options = {
      headers: {
        'User-Agent': 'Mozilla/5.0',
        'Referer': 'https://www.nba.com/',
        'Origin': 'https://www.nba.com',
        'Accept': 'application/json'
      },
      timeout: 10000
    };
    const req = https.get(url, options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => {
        try {
          resolve(JSON.parse(data));
        } catch(e) {
          reject(new Error('Failed to parse response'));
        }
      });
    });
    req.on('timeout', () => reject(new Error('Request timed out')));
    req.on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  try {
    const url = 'https://stats.nba.com/stats/leaguedashplayerstats?Season=2024-25&SeasonType=Regular+Season&PerMode=PerGame';
    const data = await fetchNBAStats(url);
    res.end(JSON.stringify({ status: 'ok', rowCount: data.resultSets[0].rowSet.length }));
  } catch(e) {
    res.end(JSON.stringify({ status: 'error', message: e.message }));
  }
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
