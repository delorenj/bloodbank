module.exports = {
  apps: [{
    name: 'bloodbank-api',
    script: '/home/delorenj/code/33GOD/bloodbank/.venv/bin/python',
    args: '-m uvicorn event_producers.http:app --host 0.0.0.0 --port 8682',
    cwd: '/home/delorenj/code/33GOD/bloodbank',
    kill_timeout: 5000,
    max_restarts: 5,
    min_uptime: 5000,
  }]
};
