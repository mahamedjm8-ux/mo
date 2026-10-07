"""Deterministic research forecasts. All bundled observations are synthetic."""
import hashlib
import json
import math
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

MODEL_VERSION = 'baseline-logit-v1'
TEAMS = [('BOS', 'Boston Celtics', 7.1), ('TOR', 'Toronto Raptors', -2.0), ('NYK', 'New York Knicks', 4.2), ('CLE', 'Cleveland Cavaliers', 5.8), ('DEN', 'Denver Nuggets', 5.1), ('LAL', 'Los Angeles Lakers', 2.4), ('GSW', 'Golden State Warriors', 1.8), ('OKC', 'Oklahoma City Thunder', 8.2)]
HOCKEY = [('TOR','Toronto Maple Leafs',3.1),('BOS','Boston Bruins',1.2),('NYR','New York Rangers',2.8),('FLA','Florida Panthers',4.2),('EDM','Edmonton Oilers',4.0),('VAN','Vancouver Canucks',1.4),('COL','Colorado Avalanche',3.8),('DAL','Dallas Stars',3.0)]
FOOTBALL = [('ARS','Arsenal',5.1),('CHE','Chelsea',2.8),('LIV','Liverpool',5.8),('MCI','Manchester City',6.2),('TOT','Tottenham',1.2),('NEW','Newcastle',2.4),('AVL','Aston Villa',2.2),('BHA','Brighton',1.8)]

def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)

def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()

def probability(home_rating, away_rating):
    # Illustrative untrained baseline. Never describe as a validated model.
    return 1 / (1 + math.exp(-(home_rating - away_rating + 2.0) / 8))

def connect(path):
    db = sqlite3.connect(path, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    return db

def initialize(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS forecasts(id TEXT PRIMARY KEY, event_id TEXT UNIQUE NOT NULL, payload TEXT NOT NULL, hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS results(event_id TEXT PRIMARY KEY REFERENCES forecasts(event_id), outcome INTEGER NOT NULL CHECK(outcome IN (0,1)), source TEXT NOT NULL, recorded_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, payload TEXT NOT NULL, previous_hash TEXT NOT NULL, hash TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS provider_events(id TEXT PRIMARY KEY, provider TEXT NOT NULL, payload TEXT NOT NULL, imported_at TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS forecast_no_update BEFORE UPDATE ON forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS forecast_no_delete BEFORE DELETE ON forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS result_no_update BEFORE UPDATE ON results BEGIN SELECT RAISE(ABORT,'Results are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS result_no_delete BEFORE DELETE ON results BEGIN SELECT RAISE(ABORT,'Results are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit BEGIN SELECT RAISE(ABORT,'Audit is append-only'); END;
    ''')

def append_audit(db, kind, payload):
    row = db.execute('SELECT hash FROM audit ORDER BY id DESC LIMIT 1').fetchone()
    previous = row['hash'] if row else '0' * 64
    db.execute('INSERT INTO audit(kind,payload,previous_hash,hash) VALUES(?,?,?,?)', (kind, canonical(payload), previous, digest({'kind': kind, 'payload': payload, 'previous_hash': previous})))

def record_forecast(db, forecast):
    required = {'event_id','published_at','starts_at','feature_timestamp','probability','mode','model_version','feature_snapshot','provider','home','away','league'}
    if not required.issubset(forecast):
        raise ValueError('Missing forecast provenance')
    p = forecast['probability']
    if not isinstance(p, (int, float)) or not math.isfinite(p) or not 0 < p < 1:
        raise ValueError('Invalid probability')
    times = [datetime.fromisoformat(forecast[k]) for k in ('feature_timestamp','published_at','starts_at')]
    if any(t.tzinfo is None for t in times) or not times[0] <= times[1] < times[2]:
        raise ValueError('Features must precede publication and event start')
    if forecast['mode'] != 'demo' or forecast['provider'] != 'synthetic-v1':
        raise ValueError('Only the configured demo provider is available')
    h = digest(forecast)
    with db:
        cursor = db.execute('INSERT OR IGNORE INTO forecasts VALUES(?,?,?,?)', (h[:24], forecast['event_id'], canonical(forecast), h))
        if cursor.rowcount:
            append_audit(db, 'forecast_published', {'event_id':forecast['event_id'], 'forecast_hash':h})
    return h

def record_result(db, event_id, outcome, source, recorded_at):
    row = db.execute('SELECT payload FROM forecasts WHERE event_id=?', (event_id,)).fetchone()
    if not row:
        raise ValueError('Unknown event')
    forecast = json.loads(row['payload'])
    if source != 'synthetic-v1' or outcome not in (0, 1) or datetime.fromisoformat(recorded_at) < datetime.fromisoformat(forecast['starts_at']):
        raise ValueError('Invalid result provenance or time')
    with db:
        cursor = db.execute('INSERT OR IGNORE INTO results VALUES(?,?,?,?)', (event_id, outcome, source, recorded_at))
        if cursor.rowcount:
            append_audit(db, 'synthetic_result_recorded', {'event_id':event_id, 'outcome':outcome,'source':source})

def seed_demo(db):
    if db.execute('SELECT COUNT(*) FROM forecasts').fetchone()[0]:
        return
    now = datetime.now(timezone.utc).replace(hour=18, minute=0, second=0, microsecond=0)
    for i in range(124):
        league, teams = [('NBA',TEAMS),('NHL',HOCKEY),('Premier League',FOOTBALL)][i%3]
        home = teams[i % 8]
        away = teams[(i + 1 + i // 8 % 6) % 8]
        start = now + timedelta(days=i - 120)
        published = start - timedelta(hours=6)
        p = probability(home[2], away[2])
        f = {'event_id':f'demo-event-{i:04d}', 'home':home[1], 'home_code':home[0], 'away':away[1], 'away_code':away[0], 'league':league, 'target':'Home win (draw counts as non-win)' if league=='Premier League' else 'Home win, including overtime', 'starts_at':start.isoformat(), 'published_at':published.isoformat(), 'feature_timestamp':(published-timedelta(minutes=10)).isoformat(), 'probability':round(p,6), 'mode':'demo', 'provider':'synthetic-v1', 'model_version':MODEL_VERSION, 'feature_snapshot':{'home_rating':home[2], 'away_rating':away[2], 'home_adjustment':2.0}, 'uncertainty':0.09}
        record_forecast(db, f)
        if start + timedelta(hours=3) < datetime.now(timezone.utc):
            # Independent deterministic synthetic outcome generator, not the predictor.
            u = int(hashlib.sha256(f'result-{i}'.encode()).hexdigest()[:8],16) / 2**32
            record_result(db, f['event_id'], int(u < 0.52), 'synthetic-v1', (start+timedelta(hours=3)).isoformat())

def ledger(db):
    rows = db.execute('SELECT f.id,f.payload,f.hash,r.outcome,r.recorded_at FROM forecasts f LEFT JOIN results r ON f.event_id=r.event_id ORDER BY json_extract(f.payload,\'$.published_at\') DESC').fetchall()
    return [{**json.loads(r['payload']), 'id':r['id'], 'hash':r['hash'], 'outcome':r['outcome'], 'result_available_at':r['recorded_at']} for r in rows]

def walk_forward(rows, min_train=40, test_size=20):
    """Chronological synthetic benchmark; train only on already available results."""
    if min_train < 1 or test_size < 1:
        raise ValueError('Window sizes must be positive')
    ordered = sorted([r for r in rows if r['outcome'] is not None], key=lambda r:r['published_at'])
    folds = []
    for offset in range(min_train, len(ordered), test_size):
        test = ordered[offset:offset+test_size]
        cutoff = datetime.fromisoformat(test[0]['published_at'])
        train = [r for r in ordered[:offset] if r.get('result_available_at') and datetime.fromisoformat(r['result_available_at']) < cutoff]
        if len(train) < min_train:
            continue
        # Laplace-smoothed historical home-win frequency, fitted exclusively to train.
        prior = (sum(r['outcome'] for r in train)+1)/(len(train)+2)
        folds.append({'train_count':len(train), 'test_count':len(test), 'test_from':test[0]['published_at'][:10], 'test_to':test[-1]['published_at'][:10], 'prior_probability':prior, 'model_brier':sum((r['probability']-r['outcome'])**2 for r in test)/len(test), 'prior_brier':sum((prior-r['outcome'])**2 for r in test)/len(test), 'latest_training_result':max(r['result_available_at'] for r in train)})
    return folds

def metrics(rows):
    settled = [r for r in rows if r['outcome'] is not None]
    count = len(settled)
    bins = []
    for low, high in [(0,.4),(.4,.5),(.5,.6),(.6,.7),(.7,1.01)]:
        group = [r for r in settled if low <= r['probability'] < high]
        bins.append({'bucket':f'{int(low*100)}–{min(100,int(high*100))}%', 'predicted':sum(r['probability'] for r in group)/len(group) if group else None, 'observed':sum(r['outcome'] for r in group)/len(group) if group else None, 'count':len(group)})
    running = []
    for i,r in enumerate(sorted(settled,key=lambda r:r['published_at'])):
        group = sorted(settled,key=lambda r:r['published_at'])[:i+1]
        running.append({'date':r['published_at'][:10], 'brier':sum((x['probability']-x['outcome'])**2 for x in group)/len(group), 'baseline':.25})
    return {'total':len(rows), 'settled':count, 'pending':len(rows)-count, 'brier':sum((r['probability']-r['outcome'])**2 for r in settled)/count if count else None, 'log_loss':-sum(r['outcome']*math.log(r['probability'])+(1-r['outcome'])*math.log(1-r['probability']) for r in settled)/count if count else None, 'accuracy':sum((r['probability']>=.5)==bool(r['outcome']) for r in settled)/count if count else None, 'calibration':bins, 'timeline':running}

def verify(db):
    for row in db.execute('SELECT payload,hash FROM forecasts'):
        if digest(json.loads(row['payload'])) != row['hash']:
            return False
    previous = '0'*64
    for row in db.execute('SELECT * FROM audit ORDER BY id'):
        if row['previous_hash'] != previous or row['hash'] != digest({'kind':row['kind'], 'payload':json.loads(row['payload']), 'previous_hash':previous}):
            return False
        previous = row['hash']
    return True
