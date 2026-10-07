import json
import sqlite3
import tempfile
import unittest
from datetime import datetime,timedelta,timezone
from server.core import connect, initialize, ledger
from server.live import initialize_live, normalize_scoreboard,normalize_news,normalize_injuries,normalize_rss,collect_feed,latest,context_for,live_summary

class LiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.db=connect(self.tmp.name+'/live.db'); initialize(self.db);initialize_live(self.db)
        self.feed=dict(self.db.execute("SELECT * FROM live_feeds WHERE id='espn:nba:events'").fetchone())
        self.now=datetime.now(timezone.utc)
        self.document={'events':[{'id':'test-1','name':'Away at Home','date':self.now.isoformat(),'links':[{'href':'https://www.espn.com/nba/game/_/gameId/1'}],'competitions':[{'competitors':[{'homeAway':'home','team':{'id':'1','displayName':'Home','abbreviation':'HOM'},'score':'100'},{'homeAway':'away','team':{'id':'2','displayName':'Away'},'score':'90'}],'status':{'type':{'state':'post','completed':True,'detail':'Final'}},'venue':{'fullName':'Test venue'}}]}]}
    def tearDown(self): self.db.close(); self.tmp.cleanup()
    def getfeed(self): return dict(self.db.execute('SELECT * FROM live_feeds WHERE id=?',(self.feed['id'],)).fetchone())
    def ingest(self,document=None): return collect_feed(self.db,self.getfeed(),lambda _:json.dumps(document or self.document).encode())
    def test_normalization_keeps_evidence_and_omits_odds(self):
        self.document['events'][0]['competitions'][0]['odds']=[{'details':'unwanted'}]
        event=normalize_scoreboard(self.document,'NBA')[0]
        self.assertEqual(event['home']['score'],'100');self.assertTrue(event['completed'])
        self.assertNotIn('odds',event); self.assertNotIn('probability',event)
        self.assertIsNone(event['match_type']);self.assertIsNone(event['weather'])
    def test_player_statistics_are_only_provider_supplied(self):
        c=self.document['events'][0]['competitions'][0]['competitors'][0]
        c['statistics']=[{'name':'rebounds','abbreviation':'REB','displayValue':'34'}]
        c['leaders']=[{'name':'points','leaders':[{'athlete':{'displayName':'Player A'},'displayValue':'20'}]}]
        event=normalize_scoreboard(self.document,'NBA')[0]
        self.assertEqual(event['home']['statistics'][0]['value'],'34')
        self.assertEqual(event['home']['player_leaders'][0]['player'],'Player A')
        self.assertEqual(event['away']['statistics'],[])
    def test_duplicates_and_revisions(self):
        self.assertTrue(self.ingest());self.assertTrue(self.ingest())
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM live_observations').fetchone()[0],1)
        self.document['events'][0]['competitions'][0]['competitors'][0]['score']='101'
        self.assertTrue(self.ingest()); self.assertEqual(len(latest(self.db,'events')),1)
        self.assertEqual(latest(self.db,'events')[0]['home']['score'],'101')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM live_observations').fetchone()[0],2)
        self.assertEqual(ledger(self.db),[])
    def test_failures_preserve_history_and_mark_unavailable(self):
        self.ingest()
        def fail(_): raise ConnectionError('Test-only offline response')
        self.assertFalse(collect_feed(self.db,self.getfeed(),fail))
        self.assertEqual(len(latest(self.db,'events')),1);self.assertFalse(latest(self.db,'events')[0]['fresh'])
        feed=self.getfeed();self.assertEqual(feed['status'],'unavailable');self.assertEqual(feed['consecutive_failures'],1)
        self.assertTrue(feed['last_success']);self.assertGreater(feed['next_attempt'],0)
    def test_missing_events_do_not_remain_fresh(self):
        self.ingest()
        old=(self.now-timedelta(hours=1)).isoformat()
        self.db.execute('UPDATE live_seen SET last_seen=?',(old,));self.db.commit()
        self.assertTrue(collect_feed(self.db,self.getfeed(),lambda _:b'{"events":[]}'))
        self.assertFalse(latest(self.db,'events')[0]['fresh'])
    def test_immutability(self):
        self.ingest()
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute('DELETE FROM live_observations')
        with self.assertRaises(sqlite3.IntegrityError):self.db.execute("UPDATE live_observations SET payload='{}'")
    def test_unknown_schemas_fail_closed(self):
        self.assertFalse(collect_feed(self.db,self.getfeed(),lambda _:b'{"changedSchema":[]}'))
        self.assertEqual(latest(self.db,'events'),[])
        with self.assertRaises(ValueError):normalize_injuries({'unexpected':[]},'NBA')
    def test_schedule_context_uses_only_prior_completed_events(self):
        self.ingest();previous=latest(self.db,'events')[0]
        future={**previous,'external_id':'later','completed':False,'starts_at':(self.now+timedelta(hours=24)).isoformat()}
        context=context_for(future,[previous,future],[])
        self.assertEqual(context['home']['hours_since_prior_start'],24)
        self.assertTrue(context['home']['back_to_back_schedule'])
        self.assertEqual(context['home']['injury_coverage'],'unknown; absence of reports is not confirmation of health')
        self.assertIsNone(context['home']['travel_distance'])
        self.assertIsNone(context['home']['fatigue_measurement'])
    def test_news_does_not_become_medical_status(self):
        news=normalize_news({'articles':[{'id':1,'headline':'Player suffers knee injury','links':{'web':{'href':'https://www.espn.com/story/1'}},'published':self.now.isoformat()}]},'NBA')
        self.assertTrue(news[0]['injury_related']);self.assertNotIn('status',news[0])
    def test_structured_injuries_retain_report_time_and_unknowns(self):
        injuries=normalize_injuries({'injuries':[{'team':{'id':1,'displayName':'Home'},'injuries':[{'athlete':{'id':1,'displayName':'Player'},'status':'Questionable'}]}]},'NBA')
        self.assertIsNone(injuries[0]['reported_at']); self.assertEqual(injuries[0]['status'],'Questionable')
    def test_actual_group_schema_and_active_status_filter(self):
        document={'injuries':[{'id':'1','displayName':'Atlanta Hawks','injuries':[{'id':'report-1','athlete':{'displayName':'Player A'},'status':'Day-To-Day'},{'id':'report-2','athlete':{'displayName':'Player B'},'status':'Active'}]}]}
        records=normalize_injuries(document,'NBA')
        self.assertEqual(len(records),1)
        self.assertEqual(records[0]['team_id'],'1')
        self.assertEqual(records[0]['team'],'Atlanta Hawks')
        self.assertEqual(records[0]['external_id'],'report-1')
    def test_rss_attribution_and_xml_safety(self):
        rss=b'<rss><channel><item><title>Sports report</title><link>https://www.bbc.com/sport/1</link><description>Reported facts</description></item></channel></rss>'
        self.assertEqual(normalize_rss(rss)[0]['headline'],'Sports report')
        self.assertIsNone(normalize_rss(rss)[0]['published_at'])
        with self.assertRaises(ValueError):normalize_rss(b'<!DOCTYPE rss><rss/>')
    def test_source_links_are_not_arbitrary(self):
        self.document['events'][0]['links']=[{'href':'javascript:alert(1)'}]
        self.assertIsNone(normalize_scoreboard(self.document,'NBA')[0]['source_url'])
    def test_empty_live_state_never_uses_demo(self):
        summary=live_summary(self.db)
        self.assertEqual(summary['events'],[]);self.assertEqual(summary['news'],[])
        self.assertIn('no validated',summary['forecast_status'])
        self.assertEqual(len(summary['feeds']),16)

if __name__=='__main__':unittest.main()
