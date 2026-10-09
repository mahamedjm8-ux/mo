"""Public research evidence library. Unmapped tables never become model features."""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from datetime import datetime,timezone
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse
from server.core import canonical
from server.predictions import PREFERRED_SOURCES

SOURCES=PREFERRED_SOURCES+[
 ('Soccer','FBref','https://fbref.com/'),('Basketball','Basketball Reference','https://www.basketball-reference.com/'),('American football','Pro Football Reference','https://www.pro-football-reference.com/'),('Ice hockey','Hockey Reference','https://www.hockey-reference.com/')]
HOSTS={urlparse(url).hostname for _,_,url in SOURCES}
USER_AGENT='CourtsideResearch'

def initialize_research(db):
    db.executescript('''
    CREATE TABLE IF NOT EXISTS research_sources(id TEXT PRIMARY KEY,name TEXT NOT NULL,sport TEXT NOT NULL,url TEXT UNIQUE NOT NULL,preferred INTEGER NOT NULL,status TEXT NOT NULL,last_attempt TEXT,last_success TEXT,error TEXT,next_attempt REAL NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS research_evidence(id INTEGER PRIMARY KEY,source_id TEXT NOT NULL REFERENCES research_sources(id),payload TEXT NOT NULL,hash TEXT NOT NULL,fetched_at TEXT NOT NULL,UNIQUE(source_id,hash));
    CREATE TRIGGER IF NOT EXISTS research_no_update BEFORE UPDATE ON research_evidence BEGIN SELECT RAISE(ABORT,'Research evidence is append-only'); END;
    CREATE TRIGGER IF NOT EXISTS research_no_delete BEFORE DELETE ON research_evidence BEGIN SELECT RAISE(ABORT,'Research evidence is append-only'); END;
    ''')
    for sport,name,url in SOURCES:
        id=hashlib.sha256(url.encode()).hexdigest()[:16]
        db.execute('INSERT OR IGNORE INTO research_sources(id,name,sport,url,preferred,status) VALUES(?,?,?,?,?,?)',(id,name,sport,url,int((sport,name,url) in PREFERRED_SOURCES),'waiting'))
    db.commit()

class EvidenceParser(HTMLParser):
    def __init__(self):
        super().__init__();self.skip=0;self.parts=[];self.title=[];self.in_title=False;self.heading=None;self.headings=[];self.current_table=None;self.tables=[];self.cell=None
    def handle_starttag(self,tag,attrs):
        if tag in ('script','style','noscript'):self.skip+=1
        if tag=='title':self.in_title=True
        if tag in ('h1','h2','h3'):self.heading=[]
        if tag=='table':self.current_table=[]
        if tag in ('td','th') and self.current_table is not None:self.cell=[]
    def handle_endtag(self,tag):
        if tag in ('script','style','noscript'):self.skip=max(0,self.skip-1)
        if tag=='title':self.in_title=False
        if tag in ('h1','h2','h3') and self.heading is not None:
            title=' '.join(self.heading).strip()
            if title and not re.search(r'\b(bet\w*|casino|odds|tips|picks|prediction\w*)\b',title,re.I):self.headings.append(title[:300])
            self.heading=None
        if tag in ('td','th') and self.cell is not None and self.current_table is not None:
            self.current_table.append(' '.join(self.cell).strip()[:300]);self.cell=None
        if tag=='table' and self.current_table is not None:
            cells=self.current_table
            if cells and not re.search(r'\b(odds|betting|payout|profit|best picks)\b',' '.join(cells),re.I):self.tables.append(cells[:500])
            self.current_table=None
    def handle_data(self,value):
        if self.skip or not value.strip():return
        value=re.sub(r'\s+',' ',unescape(value)).strip()
        self.parts.append(value)
        if self.in_title:self.title.append(value)
        if self.heading is not None:self.heading.append(value)
        if self.cell is not None:self.cell.append(value)

def request(url):
    if urlparse(url).scheme!='https' or urlparse(url).hostname not in HOSTS:raise ValueError('Unapproved research source')
    req=urllib.request.Request(url,headers={'User-Agent':USER_AGENT+'/0.3 (public sports research)'})
    with urllib.request.urlopen(req,timeout=12) as r:
        if urlparse(r.geturl()).hostname not in HOSTS or urlparse(r.geturl()).scheme!='https':raise ValueError('Unsupported redirect')
        body=r.read(2_000_001)
        if len(body)>2_000_000:raise ValueError('Research page exceeds size limit')
        return body.decode('utf-8',errors='replace')

def read_page(source,fetcher=request):
    url=source['url'];parts=urlparse(url)
    robots_url=f'{parts.scheme}://{parts.netloc}/robots.txt'
    try:
        robots=fetcher(robots_url)
        parser=urllib.robotparser.RobotFileParser();parser.parse(robots.splitlines())
        if not parser.can_fetch(USER_AGENT,url):raise ValueError('Source robots rules disallow this page')
    except urllib.error.HTTPError as e:
        if e.code not in (404,410):raise
    body=fetcher(url);parsed=EvidenceParser();parsed.feed(body)
    title=' '.join(parsed.title)
    if not title or re.search(r'just a moment|access denied|captcha|attention required|verify you are human',title,re.I):raise ValueError('Page is a challenge or unreadable')
    text=' '.join(parsed.parts)
    updated=re.search(r'(?:Updated:|Latest update:)\s*([^|]{0,80})',text,re.I)
    return {'title':title[:300],'source_url':url,'headings':parsed.headings[:45],'table_count':len(parsed.tables),'table_samples':parsed.tables[:12],'reported_update_text':updated.group(1).strip() if updated else None,'feature_use':'Research reference only; tables are not mapped or validated as forecasting features.','data_quality':'Unstructured public page; source coverage and report freshness may be unknown.'}

def collect_source(db,source,fetcher=request):
    now=datetime.now(timezone.utc).isoformat()
    try:
        evidence=read_page(source,fetcher);content=canonical(evidence);h=hashlib.sha256(content.encode()).hexdigest()
        with db:
            db.execute('INSERT OR IGNORE INTO research_evidence(source_id,payload,hash,fetched_at) VALUES(?,?,?,?)',(source['id'],content,h,now))
            db.execute("UPDATE research_sources SET status='readable',last_attempt=?,last_success=?,error=NULL,next_attempt=? WHERE id=?",(now,now,time.time()+21600,source['id']))
        return True
    except Exception as error:
        delay=3600
        if isinstance(error,urllib.error.HTTPError) and error.code==429:
            retry=error.headers.get('Retry-After') if error.headers else None
            if retry:
                try:delay=max(delay,int(retry))
                except ValueError:
                    from email.utils import parsedate_to_datetime
                    try:delay=max(delay,int((parsedate_to_datetime(retry)-datetime.now(timezone.utc)).total_seconds()))
                    except (ValueError,TypeError,OverflowError):pass
        with db:
            db.execute("UPDATE research_sources SET status='unavailable',last_attempt=?,error=?,next_attempt=? WHERE id=?",(now,f'{type(error).__name__}: {str(error)[:200]}',time.time()+delay,source['id']))
        return False

def research_summary(db):
    records=[]
    for source in db.execute('SELECT * FROM research_sources ORDER BY sport,preferred DESC,name'):
        row=dict(source)
        evidence=db.execute('SELECT payload,hash,fetched_at FROM research_evidence WHERE source_id=? ORDER BY id DESC LIMIT 1',(row['id'],)).fetchone()
        row['evidence']={**json.loads(evidence['payload']),'hash':evidence['hash'],'fetched_at':evidence['fetched_at']} if evidence else None
        row['used_in_forecasts']=False
        checked=datetime.fromisoformat(row['last_success']) if row['last_success'] else None
        age=(datetime.now(timezone.utc)-checked).total_seconds() if checked else None
        row['check_fresh']=row['status']=='readable' and age is not None and 0<=age<=43200
        row['evidence_type']='Static statistic tables' if row['evidence'] and row['evidence']['table_samples'] else 'Headings only' if row['evidence'] else 'No readable snapshot'
        row['data_freshness']='Source-reported text only; not independently verified' if row['evidence'] and row['evidence']['reported_update_text'] else 'Unknown; retrieval time is not data update time'
        records.append(row)
    return {'coverage':{'registered':len(records),'recently_readable':sum(r['check_fresh'] for r in records),'with_static_tables':sum(bool(r['evidence'] and r['evidence']['table_samples']) for r in records),'without_verified_update_time':len(records)},'sources':records,'policy':'Public access and site robots rules respected. No logins or paywall bypasses. Public readability is not API access.','forecast_features':'Experimental forecasts currently use observed ESPN match results only. Additional site evidence is retained separately pending verified feature adapters.'}
