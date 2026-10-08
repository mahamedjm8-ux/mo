"""Prospective score-event research, with fixed thresholds and no odds inputs."""
import json
import math
from datetime import timedelta
from server.core import canonical, digest, append_audit
from server.predictions import aware, outcome, training_events, event_key, SUPPORTED

MODEL='experimental-score-frequency-v1'
TOTALS={'NBA':220.5,'NFL':44.5,'NHL':5.5,'MLB':8.5,'Premier League':2.5,'Champions League':2.5}
MARGINS={'NBA':5.5,'NFL':6.5,'NHL':1.5,'MLB':1.5,'Premier League':1.5,'Champions League':1.5}
MIN_LEAGUE=20
MIN_TEAM=5
PRIOR_WEIGHT=10

def initialize(db):
    db.executescript('''CREATE TABLE IF NOT EXISTS score_forecasts(id TEXT PRIMARY KEY,event_key TEXT NOT NULL,target_key TEXT NOT NULL,payload TEXT NOT NULL,hash TEXT NOT NULL,published_at TEXT NOT NULL,UNIQUE(event_key,target_key));
    CREATE TABLE IF NOT EXISTS score_results(forecast_id TEXT PRIMARY KEY REFERENCES score_forecasts(id),payload TEXT NOT NULL,hash TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS score_forecast_no_update BEFORE UPDATE ON score_forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS score_forecast_no_delete BEFORE DELETE ON score_forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are permanent'); END;
    CREATE TRIGGER IF NOT EXISTS score_result_no_update BEFORE UPDATE ON score_results BEGIN SELECT RAISE(ABORT,'Results are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS score_result_no_delete BEFORE DELETE ON score_results BEGIN SELECT RAISE(ABORT,'Results are permanent'); END;''')

def targets(league):
    result=[{'kind':'total','threshold':TOTALS[league],'target_key':'total','description':f'Combined final score above {TOTALS[league]}'}]
    for threshold in (-MARGINS[league],MARGINS[league]):
        result.append({'kind':'margin','threshold':threshold,'target_key':f'margin:{threshold}','description':f'Home final-score margin above {threshold:+g}'})
    if league in ('Premier League','Champions League'):
        result.append({'kind':'both_score','threshold':None,'target_key':'both_score','description':'Both teams score at least once'})
    return result

def value(event,target,team=None):
    h,a=float(event['home']['score']),float(event['away']['score'])
    if target['kind']=='total':return h+a>target['threshold']
    if target['kind']=='both_score':return h>0 and a>0
    # Translate historical margins into the fixture's home-team orientation.
    if team is not None and event['away']['id']==team:h,a=a,h
    return h-a>target['threshold']

def build(event,events,now):
    if event['league'] not in SUPPORTED:return [],'Unsupported league'
    if not event['source'].startswith('espn:') or not event.get('fresh'):return [],'Fresh ESPN fixture required'
    try:
        if event['state']!='pre' or event['completed'] or aware(event['starts_at'])<=now+timedelta(minutes=15):return [],'Pre-match publication at least 15 minutes before start required'
        if not now-timedelta(minutes=15)<=aware(event['last_checked_at'])<=now:return [],'Stale fixture timestamp'
    except (KeyError,ValueError,TypeError):return [],'Invalid fixture timestamp'
    h,a=event['home']['id'],event['away']['id']
    if not h or not a or h==a:return [],'Invalid team identities'
    history=training_events(events,event['league'],now)
    home=[e for e in history if h in (e['home']['id'],e['away']['id'])]
    away=[e for e in history if a in (e['home']['id'],e['away']['id'])]
    if len(history)<MIN_LEAGUE or min(len(home),len(away))<MIN_TEAM:
        return [],f'Score model needs {MIN_LEAGUE} league games and {MIN_TEAM} per team; have {len(history)}, home {len(home)}, away {len(away)}'
    # Each shared match contributes once, avoiding artificial sample inflation.
    relevant={event_key(e):e for e in home+away}
    refs=[{'event_key':event_key(e),'snapshot_hash':e['content_hash'],'observed_at':e['fetched_at'],'starts_at':e['starts_at']} for e in history]
    records=[]
    for target in targets(event['league']):
        league_rate=(sum(value(e,target) for e in history)+1)/(len(history)+2)
        samples=[]
        for e in relevant.values():
            orientation=h if h in (e['home']['id'],e['away']['id']) else a
            v=value(e,target,orientation)
            if target['kind']=='margin' and orientation==a:
                # An away-team margin of m implies home-oriented margin -m.
                team_margin=float(e['home']['score'])-float(e['away']['score'])
                if e['away']['id']==a:team_margin=-team_margin
                v=-team_margin>target['threshold']
            samples.append(v)
        probability=(sum(samples)+PRIOR_WEIGHT*league_rate)/(len(samples)+PRIOR_WEIGHT)
        predicted='yes' if probability>.5 else 'no'
        if probability==.5:continue
        records.append({**target,'event_key':event_key(event),'event':event['name'],'league':event['league'],'home':event['home']['name'],'away':event['away']['name'],'home_id':h,'away_id':a,'source':event['source'],'source_url':event['source_url'],'starts_at':event['starts_at'],'published_at':now.isoformat(),'model_version':MODEL,'probabilities':{'yes':probability,'no':1-probability},'predicted_outcome':predicted,'estimated_probability':max(probability,1-probability),'league_games':len(history),'team_games':{'home':len(home),'away':len(away)},'unique_team_games':len(samples),'training_snapshot':refs,'training_data_hash':digest(refs),'fixture_snapshot_hash':event['content_hash'],'fixture_snapshot':{k:v for k,v in event.items() if k!='context'},'parameters':{'prior_weight':PRIOR_WEIGHT,'minimum_league_games':MIN_LEAGUE,'minimum_team_games':MIN_TEAM},'uncertainty':'Uncalibrated empirical team-score frequencies shrunk toward the observed league rate. Correlated, incomplete samples; 60% estimated probability is not 60% demonstrated accuracy.','score_scope':'Provider final score, including overtime and decisive NHL shootout score. Penalty outcomes remain ungraded. Fixed research thresholds, not sportsbook lines.'})
    return records,None

def audit_current(db,event,now,expected_hash):
    current=db.execute('SELECT payload,content_hash FROM live_observations WHERE source=? AND external_id=? ORDER BY id DESC LIMIT 1',(event['source'],event['external_id'])).fetchone()
    feed=db.execute('SELECT status,last_success FROM live_feeds WHERE id=?',(event['source'],)).fetchone()
    seen=db.execute('SELECT last_seen FROM live_seen WHERE source=? AND external_id=?',(event['source'],event['external_id'])).fetchone()
    if not current or current['content_hash']!=expected_hash or not feed or feed['status']!='ok' or not feed['last_success'] or not seen:return None
    if not all(now-timedelta(minutes=15)<=aware(t)<=now for t in (feed['last_success'],seen['last_seen'])):return None
    return json.loads(current['payload'])

def publish(db,record,event,now):
    if record['model_version']!=MODEL or record['event_key']!=event_key(event):raise ValueError('Invalid provenance')
    if aware(record['published_at'])>now or aware(record['starts_at'])<=now+timedelta(minutes=15):raise ValueError('Invalid publication time')
    if any(aware(r['observed_at'])>aware(record['published_at']) or aware(r['starts_at'])>=aware(record['published_at']) for r in record['training_snapshot']):raise ValueError('Future training information')
    p=record['probabilities']
    if set(p)!={'yes','no'} or any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<v<1 for v in p.values()) or abs(sum(p.values())-1)>1e-9 or record['predicted_outcome'] not in p or p[record['predicted_outcome']]!=max(p.values()) or record['estimated_probability']!=max(p.values()):raise ValueError('Invalid probabilities')
    if not any(all(record[k]==t[k] for k in ('kind','threshold','target_key','description')) for t in targets(record['league'])):raise ValueError('Invalid target')
    with db:
        db.execute('BEGIN IMMEDIATE')
        current=audit_current(db,event,now,record['fixture_snapshot_hash'])
        if not current or current['state']!='pre' or current['completed'] or current['starts_at']!=record['starts_at'] or current['home']['id']!=record['home_id'] or current['away']['id']!=record['away_id']:raise ValueError('Fixture freshness or identity changed')
        hash_=digest(record)
        cursor=db.execute('INSERT OR IGNORE INTO score_forecasts VALUES(?,?,?,?,?,?)',(hash_[:24],record['event_key'],record['target_key'],canonical(record),hash_,record['published_at']))
        if cursor.rowcount:append_audit(db,'score_forecast_published',{'forecast_hash':hash_,'event_key':record['event_key'],'target':record['target_key']})
    return bool(cursor.rowcount)

def ledger(db):
    return [{**json.loads(r['payload']),'id':r['id'],'hash':r['hash'],'result':json.loads(r['result']) if r['result'] else None} for r in db.execute('SELECT f.*,r.payload result FROM score_forecasts f LEFT JOIN score_results r ON r.forecast_id=f.id ORDER BY f.published_at DESC,f.id')]

def metrics(rows):
    from server.predictions import evaluation
    # Shared scoring implementation supports arbitrary categorical distributions.
    return {**evaluation(rows),'definition':'Win = correct fixed score-event forecast; loss = incorrect. No wagers or financial returns.'}

def monitoring(rows):
    report={}
    for kind in ('total','margin','both_score'):
        scored=[r for r in rows if r['kind']==kind and r['result'] is not None]
        m=metrics(scored)
        distinct=len({r['event_key'] for r in scored})
        pause=distinct>=30 and (m['brier']>.5 or m['log_loss']>math.log(2))
        report[kind]={'scored':len(scored),'distinct_matches':distinct,'pause_new_forecasts':pause,'rule':'After 30 distinct scored matches, pause a target family if Brier or log loss exceeds the uniform binary reference. Heuristic only; related forecasts are correlated.'}
    return report

def refresh(db,events,now):
    initialize(db)
    existing={(r['event_key'],r['target_key']) for r in db.execute('SELECT event_key,target_key FROM score_forecasts')}
    rejected=[]
    review=monitoring(ledger(db))
    for event in events:
        if event.get('completed'):continue
        records,reason=build(event,events,now)
        if reason:rejected.append({'event':event['name'],'league':event['league'],'reason':reason})
        for record in records:
            if review[record['kind']]['pause_new_forecasts']:
                rejected.append({'event':event['name'],'league':event['league'],'reason':record['kind']+': model review required'})
                continue
            if (record['event_key'],record['target_key']) in existing:continue
            try:publish(db,record,event,now)
            except ValueError as error:rejected.append({'event':event['name'],'league':event['league'],'reason':str(error)})
    by_key={event_key(e):e for e in events}
    for record in ledger(db):
        if record['result']:continue
        e=by_key.get(record['event_key'])
        if not e or not e.get('fresh') or outcome(e) is None:continue
        if e['home']['id']!=record['home_id'] or e['away']['id']!=record['away_id'] or e['league']!=record['league']:continue
        if not aware(record['published_at'])<aware(e['starts_at'])<=now or aware(record['starts_at'])>now or not aware(record['published_at'])<=aware(e['fetched_at'])<=now:continue
        actual='yes' if value(e,record) else 'no'
        result={'outcome':actual,'correct':actual==record['predicted_outcome'],'home_score':e['home']['score'],'away_score':e['away']['score'],'result_snapshot':{k:v for k,v in e.items() if k!='context'},'result_snapshot_hash':e['content_hash'],'observed_at':now.isoformat()}
        with db:
            db.execute('BEGIN IMMEDIATE')
            current=audit_current(db,e,now,e['content_hash'])
            if not current or outcome(current) is None:continue
            cursor=db.execute('INSERT OR IGNORE INTO score_results VALUES(?,?,?)',(record['id'],canonical(result),digest(result)))
            if cursor.rowcount:append_audit(db,'score_forecast_scored',{'forecast_id':record['id'],'result_hash':digest(result)})
    return rejected

def summary(db):
    initialize(db);rows=ledger(db)
    from server.live import latest
    from datetime import datetime, timezone
    now=datetime.now(timezone.utc)
    exclusions=[]
    events=latest(db,'events')
    for e in events:
        if not e.get('completed'):
            _,reason=build(e,events,now)
            if reason:exclusions.append({'event':e['name'],'league':e['league'],'reason':reason})
    return {'model_version':MODEL,'monitoring':monitoring(rows),'exclusions':exclusions,'forecasts':rows,'metrics':metrics(rows),'above_60_metrics':metrics([r for r in rows if r['estimated_probability']>=.6]),'by_kind':{kind:metrics([r for r in rows if r['kind']==kind]) for kind in ('total','margin','both_score')},'integrity':all(digest(json.loads(r['payload']))==r['hash'] for table in ('score_forecasts','score_results') for r in db.execute(f'SELECT payload,hash FROM {table}')),'policy':'All eligible fixed research targets are recorded, including below 60%. Display filtering never removes losses. Probabilities are uncalibrated; no guaranteed accuracy.'}
