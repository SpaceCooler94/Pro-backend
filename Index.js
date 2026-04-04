const http = require('http');

const server = http.createServer((req, res) => {
  res.writeHead(200, {'Content-Type': 'application/json'});
  res.end(JSON.stringify({ status: 'ok', message: 'prop-backend is alive' }));
});

server.listen(process.env.PORT || 3000, () => {
  console.log('Server running');
});
