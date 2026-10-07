"""Experimental outcome forecasts. No odds, stakes, or wagering recommendations."""
import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from server.core import canonical, digest, append_audit

MODEL='experimental-elo-v1'
DRAW_LEAGUES={'Premier League','Champions League','NFL'}
SUPPORTED={'NBA','NHL','MLB',*DRAW_LEAGUES}
MIN_TEAM_GAMES=2
MIN_LEAGUE_GAMES=8
K=20.0
SCALE=400.0

def aware(value):
    dt=datetime.fromisoformat(value)
    if dt.tzinfo is None: raise ValueError('Timezone required')
    return dt.astimezone(timezone.utc)

def initialize_predictions(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS outcome_forecasts(
      id TEXT PRIMARY KEY, event_key TEXT UNIQUE NOT NULL,
      payload TEXT NOT NULL, hash TEXT NOT NULL, published_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS outcome_results(
      forecast_id TEXT PRIMARY KEY REFERENCES outcome_forecasts(id),
      payload TEXT NOT NULL, hash TEXT NOT NULL, observed_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS forecast_job(id INTEGER PRIMARY KEY CHECK(id=1),last_run TEXT,error TEXT,rejections TEXT NOT NULL);
    CREATE TRIGGER IF NOT EXISTS outcome_forecast_no_update BEFORE UPDATE ON outcome_forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS outcome_forecast_no_delete BEFORE DELETE ON outcome_forecasts BEGIN SELECT RAISE(ABORT,'Forecasts are permanent'); END;
    CREATE TRIGGER IF NOT EXISTS outcome_result_no_update BEFORE UPDATE ON outcome_results BEGIN SELECT RAISE(ABORT,'Results are immutable'); END;
    CREATE TRIGGER IF NOT EXISTS outcome_result_no_delete BEFORE DELETE ON outcome_results BEGIN SELECT RAISE(ABORT,'Results are permanent'); END;
    ''')

def event_key(event): return f"{event['source']}:{event['external_id']}"

def outcome(event):
    if not event.get('completed') or event.get('state')!='post': return None
    status=str(event.get('status','')).lower()
    if any(t in status for t in ('cancel','postpon','abandon','suspend')) or re.search(r'penalt|\bpens?\b',status):return None
    if not (status.startswith('final') or status in ('ft','full time','aet','after extra time')):return None
    scores=[]
    for side in ('home','away'):
        value=event.get(side,{}).get('score')
        if value is None or isinstance(value,bool): return None
        try: number=float(value)
        except (ValueError,TypeError): return None
        if not math.isfinite(number) or number<0 or number!=int(number): return None
        scores.append(int(number))
    if scores[0]==scores[1]:
        return 'draw' if event['league'] in DRAW_LEAGUES else None
    return 'home' if scores[0]>scores[1] else 'away'

def training_events(events,league,cutoff):
    # Inputs must already have been observed before publication. No historical
    # predictions are retroactively written from later-collected information.
    by_key={}
    for event in events:
        if event['league']!=league or not event['source'].startswith('espn:') or outcome(event) is None: continue
        if not event['home'].get('id') or not event['away'].get('id') or event['home']['id']==event['away']['id']:continue
        try:
            if aware(event['starts_at'])>=cutoff or aware(event['fetched_at'])>cutoff: continue
        except (KeyError,ValueError,TypeError): continue
        by_key[event_key(event)]=event
    return sorted(by_key.values(),key=lambda e:(e['starts_at'],event_key(e)))

def fit_elo(history):
    ratings=defaultdict(lambda:1500.0); counts=Counter(); draws=0
    for e in history:
        h,a=e['home']['id'],e['away']['id']
        if not h or not a or h==a: continue
        expected=1/(1+10**((ratings[a]-ratings[h])/SCALE))
        result=outcome(e); observed=1 if result=='home' else 0 if result=='away' else .5
        adjustment=K*(observed-expected)
        ratings[h]+=adjustment; ratings[a]-=adjustment
        counts[h]+=1;counts[a]+=1;draws+=result=='draw'
    return ratings,counts,draws

def estimate(event,history):
    ratings,counts,draws=fit_elo(history)
    h,a=event['home']['id'],event['away']['id']
    conditional_home=1/(1+10**((ratings[a]-ratings[h])/SCALE))
    draw=(draws+1)/(len(history)+3) if event['league'] in DRAW_LEAGUES else 0.0
    probs={'home':(1-draw)*conditional_home,'draw':draw,'away':(1-draw)*(1-conditional_home)}
    winner=max(probs,key=probs.get)
    if abs(probs['home']-probs['away'])<1e-12 and probs['home']>=probs['draw']: winner=None
    return {'probabilities':probs,'predicted_outcome':winner,'ratings':{'home':ratings[h],'away':ratings[a]},'team_games':{'home':counts[h],'away':counts[a]},'league_games':len(history)}

def build_forecast(event,events,now):
    if event['league'] not in SUPPORTED: return None,'Unsupported league'
    if not event['source'].startswith('espn:'): return None,'Source not configured for forecasting'
    if not event.get('fresh'): return None,'Stale or unavailable fixture feed'
    try:
        if event.get('completed') or event.get('state')!='pre' or aware(event['starts_at'])<=now+timedelta(minutes=15): return None,'Not a scheduled pre-match fixture at least 15 minutes away'
        if aware(event['last_checked_at'])>now or now-aware(event['last_checked_at'])>timedelta(minutes=15): return None,'Fixture timestamp is stale or in the future'
    except (KeyError,ValueError,TypeError): return None,'Invalid fixture timestamps'
    if not event['home']['id'] or not event['away']['id'] or event['home']['id']==event['away']['id']: return None,'Invalid team identities'
    history=training_events(events,event['league'],now)
    if len(history)<MIN_LEAGUE_GAMES: return None,f'Need {MIN_LEAGUE_GAMES} observed completed league games; have {len(history)}'
    prediction=estimate(event,history)
    if min(prediction['team_games'].values())<MIN_TEAM_GAMES: return None,f'Need {MIN_TEAM_GAMES} observed games per team; have home {prediction["team_games"]["home"]}, away {prediction["team_games"]["away"]}'
    if prediction['predicted_outcome'] is None: return None,'Balanced estimates; no directional forecast'
    refs=[{'event_key':event_key(e),'snapshot_hash':e['content_hash'],'observed_at':e['fetched_at'],'starts_at':e['starts_at'],'outcome':outcome(e)} for e in history]
    record={'event_key':event_key(event),'event_id':event['external_id'],'event':event['name'],'league':event['league'],'home':event['home']['name'],'away':event['away']['name'],'home_id':event['home']['id'],'away_id':event['away']['id'],'starts_at':event['starts_at'],'published_at':now.isoformat(),'mode':'experimental-live','model_version':MODEL,'source':event['source'],'source_url':event['source_url'],'fixture_snapshot_hash':event['content_hash'],'fixture_checked_at':event['last_checked_at'],'fixture_snapshot':{k:v for k,v in event.items() if k not in ('context',)},'training_snapshot':refs,**prediction,'target':'Provider-reported final-score outcome, including overtime and decisive NHL shootout scores; football penalty outcomes remain ungraded','uncertainty':'Uncalibrated probabilities from a small, incomplete sample; no validated confidence interval.','used_features':['Observed completed results','Team identities and chronological Elo ratings','Observed league draw rate for draw-capable leagues'],'not_used':['Injury/news adjustments','Rest/fatigue','Travel','Player workloads','External website tables without validated adapters'],'parameters':{'initial_rating':1500,'k':K,'scale':SCALE,'home_advantage':0,'min_team_games':MIN_TEAM_GAMES,'min_league_games':MIN_LEAGUE_GAMES},'training_data_hash':digest(refs)}
    return record,None

def publish(db,forecast,event,now):
    # Validate again inside the write transaction, including current DB quote.
    if forecast['event_key']!=event_key(event) or forecast['mode']!='experimental-live' or forecast['model_version']!=MODEL: raise ValueError('Invalid provenance')
    if aware(forecast['published_at'])>now or aware(forecast['starts_at'])<=now+timedelta(minutes=15): raise ValueError('Invalid publication time')
    if not event['fresh'] or event['state']!='pre' or event['completed']: raise ValueError('Fixture is not eligible')
    p=forecast['probabilities']
    if set(p)!= {'home','away','draw'} or any(not isinstance(v,(int,float)) or not math.isfinite(v) or not 0<=v<=1 for v in p.values()) or abs(sum(p.values())-1)>1e-9: raise ValueError('Invalid probability distribution')
    if forecast['predicted_outcome'] not in p or p[forecast['predicted_outcome']]!=max(p.values()): raise ValueError('Invalid predicted class')
    if any(aware(ref['observed_at'])>aware(forecast['published_at']) or aware(ref['starts_at'])>=aware(forecast['published_at']) for ref in forecast['training_snapshot']): raise ValueError('Future training data')
    h=digest(forecast)
    with db:
        db.execute('BEGIN IMMEDIATE')
        current=db.execute('SELECT payload,content_hash FROM live_observations WHERE source=? AND external_id=? ORDER BY id DESC LIMIT 1',(event['source'],event['external_id'])).fetchone()
        seen=db.execute('SELECT last_seen FROM live_seen WHERE source=? AND external_id=?',(event['source'],event['external_id'])).fetchone()
        feed=db.execute('SELECT status,last_success FROM live_feeds WHERE id=?',(event['source'],)).fetchone()
        if not current or current['content_hash']!=forecast['fixture_snapshot_hash'] or not seen or not feed or feed['status']!='ok' or not feed['last_success']: raise ValueError('Fixture changed or feed degraded before publication')
        fresh_time=aware(seen['last_seen']);feed_time=aware(feed['last_success'])
        if fresh_time>now or feed_time>now or min(fresh_time,feed_time)<now-timedelta(minutes=15):raise ValueError('Fixture freshness audit failed')
        actual=json.loads(current['payload'])
        if actual['state']!='pre' or actual['completed'] or actual['starts_at']!=forecast['starts_at'] or actual['league']!=forecast['league'] or actual['home']['id']!=forecast['home_id'] or actual['away']['id']!=forecast['away_id']:raise ValueError('Fixture changed before publication')
        cursor=db.execute('INSERT OR IGNORE INTO outcome_forecasts VALUES(?,?,?,?,?)',(h[:24],forecast['event_key'],canonical(forecast),h,forecast['published_at']))
        if cursor.rowcount:append_audit(db,'live_outcome_forecast_published',{'forecast_hash':h,'event_key':forecast['event_key']})
    return bool(cursor.rowcount)

def settle(db,events,now):
    by_key={event_key(e):e for e in events}
    count=0
    for row in db.execute('SELECT f.* FROM outcome_forecasts f LEFT JOIN outcome_results r ON r.forecast_id=f.id WHERE r.forecast_id IS NULL').fetchall():
        forecast=json.loads(row['payload']);event=by_key.get(forecast['event_key'])
        if not event or not event.get('fresh') or outcome(event) is None: continue
        if event['home']['id']!=forecast['home_id'] or event['away']['id']!=forecast['away_id']: continue
        if aware(event['starts_at'])<=aware(forecast['published_at']) or aware(event['starts_at'])>now or aware(forecast['starts_at'])>now: continue
        if aware(event['fetched_at'])<aware(forecast['published_at']) or aware(event['fetched_at'])>now or aware(event['last_checked_at'])>now: continue
        actual=outcome(event)
        result={'outcome':actual,'correct':actual==forecast['predicted_outcome'],'home_score':event['home']['score'],'away_score':event['away']['score'],'source':event['source'],'source_url':event['source_url'],'result_snapshot_hash':event['content_hash'],'result_snapshot':{k:v for k,v in event.items() if k!='context'},'observed_at':now.isoformat(),'source_observed_at':event['fetched_at']}
        with db:
            db.execute('BEGIN IMMEDIATE')
            current=db.execute('SELECT payload,content_hash FROM live_observations WHERE source=? AND external_id=? ORDER BY id DESC LIMIT 1',(event['source'],event['external_id'])).fetchone()
            feed=db.execute('SELECT status,last_success FROM live_feeds WHERE id=?',(event['source'],)).fetchone()
            seen=db.execute('SELECT last_seen FROM live_seen WHERE source=? AND external_id=?',(event['source'],event['external_id'])).fetchone()
            if not current or current['content_hash']!=event['content_hash'] or outcome(json.loads(current['payload']))!=actual or not feed or feed['status']!='ok' or not feed['last_success'] or not seen:continue
            if not now-timedelta(minutes=15)<=min(aware(seen['last_seen']),aware(feed['last_success']))<=now:continue
            cursor=db.execute('INSERT OR IGNORE INTO outcome_results VALUES(?,?,?,?)',(row['id'],canonical(result),digest(result),now.isoformat()))
            if cursor.rowcount:
                count+=1;append_audit(db,'live_outcome_forecast_scored',{'forecast_id':row['id'],'result_hash':digest(result),'correct':result['correct']})
    return count

def forecast_ledger(db):
    rows=db.execute('SELECT f.*,r.payload result_payload,r.hash result_hash FROM outcome_forecasts f LEFT JOIN outcome_results r ON r.forecast_id=f.id ORDER BY f.published_at DESC,f.id').fetchall()
    return [{**json.loads(r['payload']),'id':r['id'],'hash':r['hash'],'result':json.loads(r['result_payload']) if r['result_payload'] else None,'result_hash':r['result_hash']} for r in rows]

def evaluation(rows):
    scored=[r for r in rows if r['result'] is not None];n=len(scored)
    wins=sum(r['result']['correct'] for r in scored)
    buckets=[]
    for lo,hi in [(0,.5),(.5,.6),(.6,.7),(.7,.8),(.8,1.01)]:
        group=[r for r in scored if lo<=max(r['probabilities'].values())<hi]
        buckets.append({'bucket':f'{int(lo*100)}–{min(100,int(hi*100))}%', 'count':len(group),'predicted':sum(max(r['probabilities'].values()) for r in group)/len(group) if group else None,'observed':sum(r['result']['correct'] for r in group)/len(group) if group else None})
    accuracy=wins/n if n else None
    # Wilson interval for observed classification accuracy; not a forecast interval.
    interval=None
    if n:
        z=1.96;den=1+z*z/n;centre=(accuracy+z*z/(2*n))/den;half=z*math.sqrt(accuracy*(1-accuracy)/n+z*z/(4*n*n))/den;interval=[max(0,centre-half),min(1,centre+half)]
    return {'total':len(rows),'wins':wins,'losses':n-wins,'pending':len(rows)-n,'scored':n,'accuracy':accuracy,'accuracy_interval':interval,'brier':sum(sum((r['probabilities'][c]-int(c==r['result']['outcome']))**2 for c in ('home','draw','away')) for r in scored)/n if n else None,'log_loss':-sum(math.log(max(r['probabilities'][r['result']['outcome']],1e-15)) for r in scored)/n if n else None,'calibration':buckets,'definition':'Win = correct match-outcome class; loss = incorrect class. No wagers or financial returns.'}

def verify_predictions(db):
    return all(digest(json.loads(r['payload']))==r['hash'] for table in ('outcome_forecasts','outcome_results') for r in db.execute(f'SELECT payload,hash FROM {table}'))

def monitoring(rows):
    scored=[r for r in rows if r['result'] is not None and r['model_version']==MODEL]
    m=evaluation(scored)
    if not scored:return {'status':'collecting evidence','scored':0,'reference_brier':None,'reference_log_loss':None,'pause_new_forecasts':False}
    reference_brier=sum(1-1/(3 if r['probabilities']['draw']>0 else 2) for r in scored)/len(scored)
    reference_log_loss=sum(math.log(3 if r['probabilities']['draw']>0 else 2) for r in scored)/len(scored)
    review=len(scored)>=30 and (m['brier']>reference_brier or m['log_loss']>reference_log_loss)
    return {'status':'model review required' if review else 'experimental monitoring','scored':len(scored),'reference_brier':reference_brier,'reference_log_loss':reference_log_loss,'pause_new_forecasts':review,'rule':'Heuristic review gate: after 30 scored forecasts, pause new forecasts if Brier or log loss is worse than a uniform-class reference. This is not a statistical validation or promotion test.'}

def refresh_predictions(db,events,now=None):
    supplied_time=now is not None
    now=now or datetime.now(timezone.utc)
    settled=settle(db,events,now);published=0;rejected=[]
    review=monitoring(forecast_ledger(db))
    from server.live import context_for, latest
    injuries=latest(db,'injuries')
    existing={r[0] for r in db.execute('SELECT event_key FROM outcome_forecasts')}
    for e in events:
        if e.get('completed') or event_key(e) in existing:continue
        if review['pause_new_forecasts']:
            rejected.append({'event':e['name'],'league':e['league'],'reason':'Model review required: out-of-sample error exceeds the uniform-class reference after at least 30 scored forecasts'})
            continue
        forecast,reason=build_forecast(e,events,now)
        if forecast:
            forecast['context_snapshot']=context_for(e,events,injuries)
            forecast['external_research_refs']=[]
            for row in db.execute('SELECT s.name,s.url,e.payload,e.hash,e.fetched_at FROM research_sources s JOIN research_evidence e ON e.source_id=s.id WHERE e.id=(SELECT MAX(n.id) FROM research_evidence n WHERE n.source_id=s.id)'):
                if aware(row['fetched_at'])>now:continue
                evidence=json.loads(row['payload'])
                text=' '.join(evidence.get('headings',[])+[cell for table in evidence.get('table_samples',[]) for cell in table]).casefold()
                if any(name.casefold() in text for name in (e['home']['name'],e['away']['name'])):
                    forecast['external_research_refs'].append({'name':row['name'],'url':row['url'],'evidence_hash':row['hash'],'fetched_at':row['fetched_at'],'use':'Matched team-name research context only; no numerical model weight'})
            try:published+=publish(db,forecast,e,now if supplied_time else datetime.now(timezone.utc))
            except ValueError as error:rejected.append({'event':e['name'],'league':e['league'],'reason':str(error)})
        else:rejected.append({'event':e['name'],'league':e['league'],'reason':reason})
    with db:
        db.execute('INSERT INTO forecast_job VALUES(1,?,NULL,?) ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run,error=NULL,rejections=excluded.rejections',(now.isoformat(),canonical(rejected)))
    return {'published':published,'scored':settled,'rejected':rejected}

def predictions_summary(db):
    rows=forecast_ledger(db)
    job=db.execute('SELECT * FROM forecast_job WHERE id=1').fetchone()
    per_league={league:evaluation([r for r in rows if r['league']==league]) for league in sorted({r['league'] for r in rows})}
    return {'mode':'experimental-live','model_version':MODEL,'forecasts':rows,'metrics':evaluation(rows),'by_league':per_league,'monitoring':monitoring(rows),'integrity':verify_predictions(db),'last_run':job['last_run'] if job else None,'error':job['error'] if job else None,'rejections':json.loads(job['rejections']) if job else [],'model_status':'Experimental Elo baseline. Not calibrated or independently validated.','source_policy':'Current model inputs: previously observed ESPN completed results. Public website tables, injuries, and news are research context, not fitted numerical features.'}

PREFERRED_SOURCES=[
 ('Soccer','FotMob','https://www.fotmob.com/'),('Basketball','NBA Stats','https://www.nba.com/stats'),('American football','NFL Stats','https://www.nfl.com/stats/player-stats/'),('Ice hockey','NHL Stats','https://www.nhl.com/stats/'),('Horse racing','Racing Post','https://www.racingpost.com/'),('Tennis','Ultimate Tennis Statistics','https://www.ultimatetennisstatistics.com/'),('Golf','Data Golf','https://datagolf.com/'),('Cricket','Cricbuzz','https://www.cricbuzz.com/'),('Darts','Darts Orakel','https://app.dartsorakel.com/'),('Rugby','RugbyPass','https://www.rugbypass.com/'),('Snooker','CueTracker','https://cuetracker.net/'),('Greyhound racing','Greyhound Stats UK','https://greyhoundstats.co.uk/'),('Formula 1','Stats F1','https://statsf1.com/'),('Boxing','BoxRec','https://boxrec.com/'),('MMA','Fight Matrix','https://www.fightmatrix.com/'),('Esports','Esports Charts','https://escharts.com/'),('Baseball','FanGraphs','https://www.fangraphs.com/')]
