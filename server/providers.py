"""Fixture import boundary. FotMob web JSON is not a documented supported API.

No odds or wagering data are imported. No live forecasts are generated from fixtures.
"""
import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from server.core import connect, initialize, canonical, append_audit

def normalize_fotmob(document):
    if not isinstance(document,dict) or not isinstance(document.get('leagues'),list):
        raise ValueError('Expected FotMob matches JSON with leagues array')
    events = []
    for league in document['leagues']:
        for match in league.get('matches',[]):
            status = match.get('status',{})
            start = status.get('utcTime')
            if not start or not match.get('id') or not match.get('home',{}).get('name') or not match.get('away',{}).get('name'):
                raise ValueError('Fixture missing ID, team names, or kickoff time')
            dt = datetime.fromisoformat(start.replace('Z','+00:00'))
            if dt.tzinfo is None:
                raise ValueError('Kickoff time requires timezone')
            events.append({'id':f"fotmob:{match['id']}", 'provider':'fotmob-import', 'sport':'football', 'league':str(league.get('name','Unknown')), 'home':match['home']['name'], 'away':match['away']['name'], 'starts_at':dt.isoformat(), 'status':'finished' if status.get('finished') else 'scheduled', 'source_kind':'user_supplied_import'})
    return events

def import_events(db, document):
    events = normalize_fotmob(document)
    now = datetime.now(timezone.utc).isoformat()
    added = 0
    with db:
        for event in events:
            cursor = db.execute('INSERT OR IGNORE INTO provider_events VALUES(?,?,?,?)',(event['id'],event['provider'],canonical(event),now))
            if cursor.rowcount:
                added += 1
                append_audit(db,'fixture_imported',{'event':event,'imported_at':now})
    return added

def main():
    parser = argparse.ArgumentParser(description='Import lawfully obtained FotMob matches JSON; fixtures only')
    parser.add_argument('file', type=Path)
    parser.add_argument('--db',type=Path, default=Path('.data/research.sqlite'))
    args = parser.parse_args()
    if args.file.stat().st_size > 10_000_000:
        raise ValueError('Import exceeds 10 MB limit')
    args.db.parent.mkdir(parents=True,exist_ok=True)
    db = connect(args.db)
    initialize(db)
    try:
        print(json.dumps({'imported':import_events(db,json.loads(args.file.read_text())),'provider':'fotmob-import'}))
    finally:
        db.close()

if __name__ == '__main__': main()
