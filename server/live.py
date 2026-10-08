"""Public sports observations, never synthetic fallbacks or wagering recommendations."""
import hashlib
import json
import math
import os
import re
import threading
import time
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse
from server.core import connect, canonical

LEAGUES = [('soccer','eng.1','Premier League'),('soccer','uefa.champions','Champions League'),('basketball','nba','NBA'),('hockey','nhl','NHL'),('football','nfl','NFL'),('baseball','mlb','MLB')]
ALLOWED_HOSTS={'site.api.espn.com','feeds.bbci.co.uk'}
MAX_BYTES=12_000_000
STALE_SECONDS=900

def utcnow(): return datetime.now(timezone.utc).isoformat()
def timestamp(value):
    if not isinstance(value,str): return None
    try:
        parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        return parsed.astimezone(timezone.utc).isoformat() if parsed.tzinfo else None
    except ValueError: return None

def source_url(value):
    if not isinstance(value,str): return None
    parsed=urlparse(value)
    if parsed.scheme!='https' or not parsed.hostname: return None
    host=parsed.hostname
    return value if host in ('espn.com','bbc.com','bbc.co.uk') or any(host.endswith('.'+h) for h in ('espn.com','bbc.com','bbc.co.uk')) else None

def initialize_live(db):
    from server.predictions import initialize_predictions
    from server.research import initialize_research
    initialize_predictions(db)
    initialize_research(db)
    db.executescript('''
    CREATE TABLE IF NOT EXISTS live_observations(
      id INTEGER PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
      category TEXT NOT NULL, league TEXT NOT NULL, payload TEXT NOT NULL,
      content_hash TEXT NOT NULL, fetched_at TEXT NOT NULL,
      UNIQUE(source,external_id,content_hash));
    CREATE INDEX IF NOT EXISTS live_lookup ON live_observations(source,external_id,id);
    CREATE TABLE IF NOT EXISTS live_feeds(
      id TEXT PRIMARY KEY,url TEXT NOT NULL,category TEXT NOT NULL,league TEXT NOT NULL,
      status TEXT NOT NULL,last_attempt TEXT,last_success TEXT,error TEXT,
      consecutive_failures INTEGER NOT NULL DEFAULT 0,next_attempt REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS live_seen(source TEXT NOT NULL,external_id TEXT NOT NULL,last_seen TEXT NOT NULL,PRIMARY KEY(source,external_id));
    CREATE TABLE IF NOT EXISTS live_days(source TEXT NOT NULL,date TEXT NOT NULL,last_success REAL NOT NULL,PRIMARY KEY(source,date));
    CREATE TRIGGER IF NOT EXISTS live_no_update BEFORE UPDATE ON live_observations BEGIN SELECT RAISE(ABORT,'Observations are append-only'); END;
    CREATE TRIGGER IF NOT EXISTS live_no_delete BEFORE DELETE ON live_observations BEGIN SELECT RAISE(ABORT,'Observations are append-only'); END;
    ''')
    for sport,key,league in LEAGUES:
        base=f'https://site.api.espn.com/apis/site/v2/sports/{sport}/{key}'
        for category,suffix in [('events','scoreboard'),('news','news')]:
            db.execute('INSERT OR IGNORE INTO live_feeds(id,url,category,league,status) VALUES(?,?,?,?,?)',(f'espn:{key}:{category}',f'{base}/{suffix}',category,league,'waiting'))
        if key in ('nba','nfl','nhl'):
            db.execute('INSERT OR IGNORE INTO live_feeds(id,url,category,league,status) VALUES(?,?,?,?,?)',(f'espn:{key}:injuries',f'{base}/injuries','injuries',league,'waiting'))
    db.execute('INSERT OR IGNORE INTO live_feeds(id,url,category,league,status) VALUES(?,?,?,?,?)',('bbc:sport:news','https://feeds.bbci.co.uk/sport/rss.xml','news','All sports','waiting'))
    db.commit()

def fetch(url):
    if urlparse(url).scheme!='https' or urlparse(url).hostname not in ALLOWED_HOSTS:
        raise ValueError('Unapproved feed destination')
    request=urllib.request.Request(url,headers={'User-Agent':'CourtsideResearch/0.2 (sports research; no betting)','Accept':'application/json, application/rss+xml, application/xml'})
    with urllib.request.urlopen(request,timeout=8) as response:
        if urlparse(response.geturl()).hostname not in ALLOWED_HOSTS:
            raise ValueError('Feed redirected outside allowed sources')
        data=response.read(MAX_BYTES+1)
        if len(data)>MAX_BYTES: raise ValueError('Feed response exceeds size limit')
        return data

def normalize_scoreboard(document,league):
    if not isinstance(document,dict) or not isinstance(document.get('events'),list):
        raise ValueError('Unsupported scoreboard schema')
    events=[]
    for event in document['events']:
        for competition in event.get('competitions',[])[:1]:
            competitors=competition.get('competitors',[])
            home=next((c for c in competitors if c.get('homeAway')=='home'),None)
            away=next((c for c in competitors if c.get('homeAway')=='away'),None)
            start=timestamp(competition.get('date') or event.get('date'))
            if not home or not away or not start or not event.get('id'): continue
            def participant(c):
                team=c.get('team',{})
                statistics=[{'name':str(s['name']),'label':str(s.get('abbreviation') or s['name']),'value':str(s['displayValue'])} for s in c.get('statistics',[]) if s.get('name') and s.get('displayValue') is not None]
                leaders=[{'metric':str(group.get('name','Unknown')),'player':str(l['athlete']['displayName']),'value':str(l['displayValue'])} for group in c.get('leaders',[]) for l in group.get('leaders',[]) if isinstance(l.get('athlete'),dict) and l['athlete'].get('displayName') and l.get('displayValue') is not None]
                return {'id':str(team.get('id') or c.get('id','')), 'name':team.get('displayName') or team.get('name'), 'code':team.get('abbreviation'), 'score':str(c['score']) if c.get('score') is not None else None, 'record':next((r.get('summary') for r in c.get('records',[]) if r.get('type')=='total'),None), 'statistics':statistics,'player_leaders':leaders}
            h,a=participant(home),participant(away)
            if not h['name'] or not a['name']: continue
            status=competition.get('status',event.get('status',{})).get('type',{})
            link=next((source_url(l.get('href')) for l in event.get('links',[]) if source_url(l.get('href'))),None)
            venue=competition.get('venue',{}); address=venue.get('address',{})
            events.append({'external_id':str(event['id']), 'league':league,'name':event.get('name') or f"{a['name']} at {h['name']}", 'starts_at':start, 'home':h,'away':a,'state':status.get('state','unknown'), 'completed':status.get('completed') is True, 'status':status.get('detail') or status.get('description') or 'Unknown', 'venue':venue.get('fullName'), 'location':', '.join(str(address[k]) for k in ('city','state','country') if address.get(k)), 'match_type':competition.get('type',{}).get('text'), 'source_url':link, 'source_updated_at':timestamp(event.get('lastUpdated')), 'weather':competition.get('weather') if isinstance(competition.get('weather'),dict) else None})
    if document['events'] and not events:
        raise ValueError('No supported team events in nonempty scoreboard')
    return events

def normalize_news(document,league):
    if not isinstance(document,dict) or not isinstance(document.get('articles'),list):
        raise ValueError('Unsupported news schema')
    articles=[]
    for article in document['articles']:
        headline=article.get('headline'); link=source_url(article.get('links',{}).get('web',{}).get('href'))
        if not headline or not link: continue
        tags=article.get('categories',[])
        teams=[str(c.get('teamId') or c.get('team',{}).get('id')) for c in tags if c.get('teamId') or isinstance(c.get('team'),dict) and c['team'].get('id')]
        articles.append({'external_id':str(article.get('id') or hashlib.sha256(link.encode()).hexdigest()),'league':league,'headline':str(headline)[:500], 'description':str(article.get('description',''))[:1000], 'source_url':link, 'published_at':timestamp(article.get('published')), 'team_ids':teams, 'injury_related':bool(re.search(r'\b(injur\w*|concussion|hamstring|ankle|knee|ruled out|sidelined)\b',str(headline),re.I)), 'evidence_type':'reported news; not verified injury status'})
    if document['articles'] and not articles: raise ValueError('No supported articles in nonempty feed')
    return articles

def normalize_injuries(document,league):
    if not isinstance(document,dict) or not isinstance(document.get('injuries'),list):
        raise ValueError('Structured injury feed unavailable or unsupported')
    records=[]
    for group in document['injuries']:
        team=group.get('team') or {'id':group.get('id'),'displayName':group.get('displayName')}
        for item in group.get('injuries',[]):
            athlete=item.get('athlete',{})
            name=athlete.get('displayName'); status=item.get('status'); updated=timestamp(item.get('date'))
            if not name or not status: continue
            if str(status).lower() in ('active','healthy'): continue
            records.append({'external_id':str(item.get('id') or f"{team.get('id','unknown')}:{athlete.get('id') or name}"),'league':league,'team_id':str(team.get('id','')),'team':team.get('displayName') or team.get('name'),'player':name,'status':str(status),'detail':str(item.get('longComment') or item.get('shortComment') or '')[:1500],'reported_at':updated,'source_url':next((source_url(l.get('href')) for l in athlete.get('links',[]) if source_url(l.get('href'))),None),'evidence_type':'provider-reported availability issue; completeness unknown'})
    if document['injuries'] and not records and not all(isinstance(g.get('injuries'),list) for g in document['injuries']): raise ValueError('Nonempty injury feed could not be normalized')
    return records

def normalize_rss(data):
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper(): raise ValueError('Unsupported XML declarations')
    root=ET.fromstring(data)
    if root.tag not in ('rss',): raise ValueError('Unsupported RSS schema')
    from email.utils import parsedate_to_datetime
    records=[]
    for item in root.findall('./channel/item'):
        link=source_url(item.findtext('link')); title=item.findtext('title')
        if not link or not title: continue
        try: published=parsedate_to_datetime(item.findtext('pubDate','')).astimezone(timezone.utc).isoformat()
        except (TypeError,ValueError,OverflowError): published=None
        description=re.sub('<[^>]*>','',item.findtext('description',''))[:1000]
        records.append({'external_id':hashlib.sha256(link.encode()).hexdigest(),'league':'All sports','headline':title[:500],'description':description,'published_at':published,'source_url':link,'team_ids':[],'injury_related':bool(re.search(r'\binjur\w*\b',title,re.I)),'evidence_type':'reported news; not verified injury status'})
    return records

def store_observations(db,feed,records,fetched_at):
    for record in records:
        content=canonical(record); h=hashlib.sha256(content.encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO live_observations(source,external_id,category,league,payload,content_hash,fetched_at) VALUES(?,?,?,?,?,?,?)',(feed['id'],record['external_id'],feed['category'],feed['league'],content,h,fetched_at))
        db.execute('INSERT INTO live_seen VALUES(?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET last_seen=excluded.last_seen',(feed['id'],record['external_id'],fetched_at))

def collect_feed(db,feed,fetcher=fetch):
    attempt=utcnow(); url=feed['url']
    try:
        if feed['category']=='events':
            today=datetime.now(timezone.utc)
            # ESPN requires single-day dates for these feeds. Limit requests;
            # gradually fill historical/future coverage rather than burst 22 days.
            offsets=[0,-1,1]
            extended=[offset for distance in range(2,15) for offset in (-distance,distance) if offset<=7]
            for offset in extended:
                day=(today+timedelta(days=offset)).strftime('%Y%m%d')
                row=db.execute('SELECT last_success FROM live_days WHERE source=? AND date=?',(feed['id'],day)).fetchone()
                if not row or time.time()-row[0]>21600:
                    offsets.append(offset); break
            records=[]; fetched_days=[]
            for offset in offsets:
                day=(today+timedelta(days=offset)).strftime('%Y%m%d')
                body=fetcher(url+f'?dates={day}&limit=200')
                records.extend(normalize_scoreboard(json.loads(body),feed['league']))
                fetched_days.append(day)
        elif feed['id'].startswith('bbc:'): records=normalize_rss(fetcher(url))
        else:
            parsed=json.loads(fetcher(url))
            records={'events':normalize_scoreboard,'news':normalize_news,'injuries':normalize_injuries}[feed['category']](parsed,feed['league'])
        with db:
            store_observations(db,feed,records,attempt)
            if feed['category']=='events':
                for day in fetched_days:
                    db.execute('INSERT INTO live_days VALUES(?,?,?) ON CONFLICT(source,date) DO UPDATE SET last_success=excluded.last_success',(feed['id'],day,time.time()))
            db.execute("UPDATE live_feeds SET status='ok',last_attempt=?,last_success=?,error=NULL,consecutive_failures=0,next_attempt=? WHERE id=?",(attempt,attempt,time.time()+300,feed['id']))
        return True
    except Exception as error:
        # Keep history and last success. No simulated replacement or made-up observations.
        message=f'{type(error).__name__}: {str(error)[:200]}'
        failures=int(feed['consecutive_failures'])+1
        with db:
            db.execute("UPDATE live_feeds SET status='unavailable',last_attempt=?,error=?,consecutive_failures=?,next_attempt=? WHERE id=?",(attempt,message,failures,time.time()+min(3600,60*2**min(failures,6)),feed['id']))
        return False

def latest(db,category):
    query='''SELECT o.*,f.status,f.last_success,s.last_seen FROM live_observations o JOIN live_feeds f ON f.id=o.source
      LEFT JOIN live_seen s ON s.source=o.source AND s.external_id=o.external_id
      WHERE o.category=? AND o.id=(SELECT MAX(n.id) FROM live_observations n WHERE n.source=o.source AND n.external_id=o.external_id)
      ORDER BY o.id DESC'''
    now=datetime.now(timezone.utc)
    records=[]
    for r in db.execute(query,(category,)):
        age=(now-datetime.fromisoformat(r['last_success'])).total_seconds() if r['last_success'] else math.inf
        seen_age=(now-datetime.fromisoformat(r['last_seen'])).total_seconds() if r['last_seen'] else math.inf
        records.append({**json.loads(r['payload']), 'source':r['source'],'fetched_at':r['fetched_at'],'last_checked_at':r['last_seen'],'fresh':r['status']=='ok' and max(age,seen_age)<=STALE_SECONDS,'content_hash':r['content_hash']})
    return records

def context_for(event,events,injuries):
    context={}
    kickoff=datetime.fromisoformat(event['starts_at'])
    for side in ('home','away'):
        team_id=event[side]['id']
        previous=[e for e in events if e['league']==event['league'] and e['completed'] and datetime.fromisoformat(e['starts_at'])<kickoff and any(e[s]['id']==team_id for s in ('home','away'))]
        previous.sort(key=lambda e:e['starts_at'],reverse=True)
        last=previous[0] if previous else None
        hours=round((kickoff-datetime.fromisoformat(last['starts_at'])).total_seconds()/3600,1) if last else None
        relevant=[] if event['completed'] else [i for i in injuries if i['league']==event['league'] and i['team_id']==team_id]
        coverage='not reconstructed for completed events' if event['completed'] else ('partial current reports' if relevant else 'unknown; absence of reports is not confirmation of health')
        context[side]={'hours_since_prior_start':hours,'back_to_back_schedule':hours is not None and hours<=30,'prior_event':last['name'] if last else None,'prior_source_url':last['source_url'] if last else None,'schedule_fresh':event['fresh'] and (last['fresh'] if last else False),'injuries':relevant,'injury_coverage':coverage,'travel_distance':None,'fatigue_measurement':None}
    return context

def live_summary(db):
    events=latest(db,'events'); injuries=[i for i in latest(db,'injuries') if i['status'].lower() not in ('active','healthy')]; news=latest(db,'news')
    upcoming=sorted([e for e in events if not e['completed']],key=lambda e:e['starts_at'])
    completed=sorted([e for e in events if e['completed']],key=lambda e:e['starts_at'],reverse=True)
    now=datetime.now(timezone.utc)
    feeds=[]
    for row in db.execute('SELECT * FROM live_feeds ORDER BY league,category'):
        f=dict(row); age=(now-datetime.fromisoformat(f['last_success'])).total_seconds() if f['last_success'] else None
        f['age_seconds']=round(age) if age is not None else None
        f['fresh']=f['status']=='ok' and age is not None and age<=STALE_SECONDS
        feeds.append(f)
    return {'mode':'live observations','enabled':os.environ.get('RESEARCH_LIVE','1')!='0', 'checked_at':utcnow(), 'feeds':feeds,'events':[{**e,'context':context_for(e,events,injuries)} for e in upcoming[:100]],'results':[{**e,'context':context_for(e,events,injuries)} for e in completed[:100]],'news':sorted(news,key=lambda n:n.get('published_at') or '',reverse=True)[:80],'injuries':injuries,'observation_count':db.execute('SELECT COUNT(*) FROM live_observations').fetchone()[0], 'forecasts_count':db.execute('SELECT COUNT(*) FROM outcome_forecasts').fetchone()[0], 'forecast_status':'Experimental result-based forecasts enabled; no validated live model or complete feature coverage.','limitations':['Public endpoints have no guaranteed availability or coverage.','News is attributed reporting, not verified medical information.','Schedule intervals are proxies, not measured fatigue.','Travel, player workloads and injury completeness are unknown.','Synthetic forecasts remain in the separate demo ledger.']}

def run_collector(path,stop):
    while not stop.is_set():
        db=connect(path)
        try:
            feeds=db.execute('SELECT * FROM live_feeds WHERE next_attempt<=? ORDER BY next_attempt,id',(time.time(),)).fetchall()
            for row in feeds:
                if stop.is_set(): break
                collect_feed(db,dict(row))
            from server.predictions import refresh_predictions
            try:
                events=latest(db,'events')
                refresh_predictions(db,events)
                from server.score_forecasts import refresh
                refresh(db,events,datetime.now(timezone.utc))
            except Exception as error:
                with db:
                    db.execute('INSERT INTO forecast_job VALUES(1,?,?,?) ON CONFLICT(id) DO UPDATE SET last_run=excluded.last_run,error=excluded.error',(utcnow(),f'{type(error).__name__}: forecast cycle failed','[]'))
                print(json.dumps({'level':'error','service':'forecast-worker','error_type':type(error).__name__}),flush=True)
        finally: db.close()
        stop.wait(15)

def start_collector(path):
    stop=threading.Event()
    if os.environ.get('RESEARCH_LIVE','1')!='0':
        threading.Thread(target=run_collector,args=(path,stop),daemon=True,name='sports-collector').start()
        threading.Thread(target=run_research,args=(path,stop),daemon=True,name='research-sources').start()
    return stop

def run_research(path,stop):
    from server.research import collect_source
    while not stop.is_set():
        db=connect(path)
        try:
            source=db.execute('SELECT * FROM research_sources WHERE next_attempt<=? ORDER BY next_attempt,id LIMIT 1',(time.time(),)).fetchone()
            if source:collect_source(db,dict(source))
        finally:db.close()
        stop.wait(1 if source else 60)
