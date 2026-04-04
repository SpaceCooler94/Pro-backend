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
      }
    };
    https.get(url, options, (res) => {
      let data = '';
      res.on('data', chunk => data += chunk);
      res.on('end', () => resolve(JSON.parse(data)));
    }).on('error', reject);
  });
}

const server = http.createServer(async (req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  
  const url = 'https://stats.nba.com/stats/leaguedashplayerstats?Season=2024-25&SeasonType=Regular+Season&PerMode=PerGame';
  const data = await fetchNBAStats(url);
  res.end(JSON.stringify(data));
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
