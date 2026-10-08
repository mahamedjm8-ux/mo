"""Read-only local research API; run with python -m server.app."""
import json
import os
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse
from server.core import connect, initialize, seed_demo, ledger, metrics, verify, walk_forward
from server.live import initialize_live, live_summary, start_collector
from server.predictions import predictions_summary
from server.research import research_summary

ROOT = Path(__file__).resolve().parents[1]
DB_PATH = Path(os.environ.get('RESEARCH_DB', ROOT / '.data' / 'research.sqlite'))

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = urlparse(self.path).path
        if path.startswith('/api/'):
            db = connect(DB_PATH)
            try:
                rows = ledger(db)
                if path=='/api/live':
                    self.respond(200,live_summary(db))
                    return
                if path=='/api/outcomes':
                    self.respond(200,predictions_summary(db))
                    return
                if path=='/api/score-forecasts':
                    from server.score_forecasts import summary
                    self.respond(200,summary(db))
                    return
                if path=='/api/research':
                    self.respond(200,research_summary(db))
                    return
                endpoints = {
                    '/api/overview': {'mode':'demo', 'provider':'synthetic-v1', 'model_version':'baseline-logit-v1', 'metrics':metrics(rows), 'forecasts':rows[:8], 'integrity':verify(db), 'walk_forward':walk_forward(rows)},
                    '/api/forecasts':rows,
                    '/api/events':[json.loads(r['payload']) for r in db.execute('SELECT payload FROM provider_events ORDER BY imported_at DESC')],
                    '/api/health':{'database':'connected', 'integrity':verify(db), 'live_collection_enabled':os.environ.get('RESEARCH_LIVE','1')!='0', 'healthy_live_feeds':sum(f['fresh'] for f in live_summary(db)['feeds']), 'live_forecasts_enabled':os.environ.get('RESEARCH_LIVE','1')!='0', 'forecast_model_status':'experimental, not validated', 'checked_at':datetime.now(timezone.utc).isoformat()},
                    '/api/audit':[dict(r) for r in db.execute('SELECT * FROM audit ORDER BY id DESC LIMIT 200')],
                }
                if path not in endpoints:
                    self.respond(404, {'error':'Not found'})
                else:
                    self.respond(200, endpoints[path])
            except Exception:
                self.respond(500, {'error':'Research data unavailable'})
            finally:
                db.close()
            return
        relative = path.lstrip('/') or 'index.html'
        target = (ROOT / 'dist' / relative).resolve()
        if not target.is_relative_to(ROOT / 'dist'):
            self.respond(404, {'error':'Not found'})
            return
        if not target.is_file():
            target = ROOT / 'dist' / 'index.html'
        if not target.is_file():
            self.respond(503, {'error':'Build frontend with npm run build, or start npm run dev'})
            return
        import mimetypes
        data = target.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', mimetypes.guess_type(target)[0] or 'application/octet-stream')
        self.send_header('Content-Length', str(len(data)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(data)

    def security_headers(self):
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('X-Frame-Options','DENY')
        self.send_header('Referrer-Policy','same-origin')
        self.send_header('Cache-Control','no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; script-src 'self'; connect-src 'self'; frame-ancestors 'none'")

    def respond(self, status, data):
        body = json.dumps(data, allow_nan=False).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',str(len(body)))
        self.security_headers()
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        print(json.dumps({'level':'info','service':'research-api','message':fmt % args}), flush=True)

def main():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = connect(DB_PATH)
    initialize(db)
    initialize_live(db)
    seed_demo(db)
    db.close()
    server=ThreadingHTTPServer((os.environ.get('RESEARCH_HOST','127.0.0.1'), int(os.environ.get('PORT','8000'))), Handler)
    stop=start_collector(DB_PATH)
    print('Research API: live observations + separate demo forecasts', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()

if __name__ == '__main__':
    main()
